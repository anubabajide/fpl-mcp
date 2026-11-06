"""Open FPL's sign-in page in a real browser window and catch where it redirects.

The browser is only the place where the person signs in. It starts with a fresh,
throwaway profile, so nothing is kept from it afterwards. When FPL redirects
back to the web app with an authorization code, that navigation is intercepted
before the web app loads, and the window shows a short confirmation instead.
The code is useless without the PKCE verifier, which never leaves this process.

Browser choice: an installed Chrome or Edge is preferred over Playwright's
bundled Chromium. On some machines the bundled build cannot start headed at all
(Windows fails to resolve its side-by-side assembly and the launch dies with
`spawn UNKNOWN`), while the installed browsers work fine. A browser that
reliably opens a window matters more here than a pinned build.
"""
from __future__ import annotations

import asyncio
import contextlib

from .auth import BrowserUnavailable, SignInError

# Tried in order. None means Playwright's own bundled Chromium.
CHANNELS: tuple[str | None, ...] = ("chrome", "msedge", None)

# How long the confirmation page stays up before the window closes itself.
CONFIRMATION_SECONDS = 2.5

CONFIRMATION_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Signed in to FPL</title>
<style>
  body { font: 16px/1.5 system-ui, sans-serif; display: grid; place-items: center;
         min-height: 100vh; margin: 0; background: #37003c; color: #fff; }
  main { text-align: center; padding: 2rem; }
  h1 { font-size: 1.5rem; margin: 0 0 .5rem; }
</style></head>
<body><main>
  <h1>Signed in to Fantasy Premier League</h1>
  <p>You can return to your assistant. This window closes by itself.</p>
</main></body></html>
"""


class BrowserAuthError(SignInError):
    """The sign-in window failed or was abandoned."""


def _require_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise BrowserUnavailable(
            "Playwright is not installed, so no sign-in window can be opened here."
        ) from exc
    return async_playwright


def _candidates(preferred: str | None) -> list[str | None]:
    """Channel order, putting a previously working choice first.

    `None` means "no preference recorded", not "prefer bundled Chromium". The
    two must not be conflated, or a fresh install would try the bundled build
    first, which is exactly the one most likely to be unusable.
    """
    order = list(CHANNELS)
    if preferred and preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    return order


async def _launch(pw, preferred_channel: str | None, headless: bool):
    errors: list[str] = []
    for channel in _candidates(preferred_channel):
        options: dict = {
            "headless": headless,
            # Keeps the sign-in page's bot check from refusing a person signing
            # in by hand just because the browser was started by a program.
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if channel:
            options["channel"] = channel
        try:
            return await pw.chromium.launch(**options), channel
        except Exception as exc:  # this browser is unusable here; try the next
            errors.append(
                (channel or "bundled chromium") + ": " + str(exc).split("Call log")[0].strip()[:160]
            )
    raise BrowserUnavailable(
        "No browser window could be opened on this machine. Tried "
        + str(len(errors))
        + ": "
        + " | ".join(errors)
    )


async def capture_redirect(
    authorize_url: str,
    *,
    redirect_prefix: str,
    timeout_seconds: float = 600.0,
    preferred_channel: str | None = None,
    headless: bool = False,
) -> tuple[str, str | None]:
    """Show `authorize_url`, wait for a redirect to `redirect_prefix`, return its URL.

    Also returns the browser channel that worked, so the next sign-in can try
    it first. Raises `BrowserUnavailable` if no browser starts, and
    `BrowserAuthError` if the window is closed or the sign-in times out.
    """
    async_playwright = _require_playwright()
    loop = asyncio.get_running_loop()
    landed: asyncio.Future[str] = loop.create_future()

    def is_callback(url: str) -> bool:
        if not url.startswith(redirect_prefix):
            return False
        query = url.partition("?")[2]
        return "code=" in query or "error=" in query

    def settle(url: str) -> None:
        if not landed.done():
            landed.set_result(url)

    def abandon(message: str) -> None:
        if not landed.done():
            landed.set_exception(BrowserAuthError(message))

    async with async_playwright() as pw:
        browser, channel = await _launch(pw, preferred_channel, headless)
        try:
            context = await browser.new_context(no_viewport=True)
            route_served = False

            async def answer_callback(route) -> None:
                nonlocal route_served
                route_served = True
                settle(route.request.url)
                await route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=CONFIRMATION_PAGE
                )

            # The route shows the confirmation page instead of loading the FPL app.
            # The request listener is a backstop in case a redirect bypasses routing:
            # Playwright does not run route handlers for a navigation reached via a
            # 302, so the browser follows it and the FPL app starts loading. We still
            # catch the address from the request event and replace the page below.
            await context.route(is_callback, answer_callback)
            context.on("request", lambda request: is_callback(request.url) and settle(request.url))

            def on_page_close(_page) -> None:
                if not any(not p.is_closed() for p in context.pages):
                    abandon("The sign-in window was closed before sign-in finished.")

            context.on("page", lambda page: page.on("close", on_page_close))
            browser.on(
                "disconnected",
                lambda _b: abandon("The browser closed before sign-in finished."),
            )

            page = await context.new_page()
            with contextlib.suppress(Exception):
                # A slow or interrupted first load is not fatal; the user can reload.
                await page.goto(authorize_url, wait_until="domcontentloaded", timeout=45_000)

            try:
                redirect_url = await asyncio.wait_for(landed, timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise BrowserAuthError(
                    "Sign-in was not completed within "
                    + str(int(timeout_seconds // 60))
                    + " minutes, so the window was closed. Run `fpl_login` to try again."
                ) from exc

            if not route_served and not page.is_closed():
                # A redirect bypassed the route. Let whatever it led to settle, then
                # replace it, so the window never sits on the FPL app.
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=5_000)
                with contextlib.suppress(Exception):
                    await page.set_content(CONFIRMATION_PAGE)

            if not headless and browser.is_connected():
                await asyncio.sleep(CONFIRMATION_SECONDS)
            return redirect_url, channel
        finally:
            if not landed.done():
                landed.cancel()  # so closing the browser below cannot set an unread error
            with contextlib.suppress(Exception):
                await browser.close()

"""Token acquisition by driving a real browser session.

FPL's identity provider disables the OAuth password grant for the site's client
id, so a stored email/password cannot be exchanged for a token directly. The
supported flow is authorization-code + PKCE in a browser.

Rather than store credentials, this module keeps a persistent browser profile.
You sign in once yourself in a visible window; the resulting session lives in
that profile and is replayed headlessly afterwards to mint fresh bearer tokens
for as long as the session stays valid.

Tokens are never typed by hand: they are read off the `X-Api-Authorization`
header that the FPL web app itself puts on its API calls.

Browser choice: an installed Chrome or Edge is preferred over Playwright's
bundled Chromium. On some machines the bundled build cannot start headed at all
— Windows fails to resolve its side-by-side assembly and the launch dies with
`spawn UNKNOWN` — while the installed browsers work fine. Since the whole point
of this module is a window the user can see, a browser that reliably opens one
matters more than a pinned build.
"""
from __future__ import annotations

import asyncio

from .config import CONFIG_DIR

PROFILE_DIR = CONFIG_DIR / "browser-profile"
LOGIN_URL = "https://fantasy.premierleague.com/my-team"
API_MARKER = "/api/"
AUTH_HEADER = "x-api-authorization"

# Tried in order. None means Playwright's own bundled Chromium.
CHANNELS: tuple[str | None, ...] = ("chrome", "msedge", None)

# One profile directory cannot back two browser contexts at once.
_profile_lock = asyncio.Lock()


class BrowserAuthError(RuntimeError):
    """Raised when a token could not be obtained from the browser session."""


def _require_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise BrowserAuthError(
            "Playwright is not installed. Reinstall the server, then run:\n"
            "  playwright install chromium"
        ) from exc
    return async_playwright


def profile_exists() -> bool:
    return PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())


def _candidates(preferred: str | None) -> list[str | None]:
    """Channel order, putting a previously-working choice first.

    `None` means "no preference recorded", not "prefer bundled Chromium" — the
    two must not be conflated, or a fresh install would try the bundled build
    first, which is exactly the one most likely to be unusable.
    """
    order = list(CHANNELS)
    if preferred and preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    return order


async def _capture_token(
    headless: bool, timeout_seconds: float, preferred_channel: str | None = None
) -> tuple[str, str | None]:
    """Load the team page in the saved profile and read the app's auth header.

    Returns the token and the channel that worked, so the caller can reuse the
    same browser next time — a persistent profile belongs to one browser.
    """
    async_playwright = _require_playwright()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with _profile_lock:
        async with async_playwright() as pw:
            launch_errors: list[str] = []
            for channel in _candidates(preferred_channel):
                options: dict = {
                    "user_data_dir": str(PROFILE_DIR),
                    "headless": headless,
                    "args": ["--disable-blink-features=AutomationControlled"],
                    "viewport": {"width": 1280, "height": 900},
                }
                if channel:
                    options["channel"] = channel
                try:
                    context = await pw.chromium.launch_persistent_context(**options)
                except Exception as exc:  # this browser is unusable here; try the next
                    launch_errors.append(
                        (channel or "bundled chromium") + ": "
                        + str(exc).split("Call log")[0].strip()[:120]
                    )
                    continue

                try:
                    token = await _watch_for_token(context, timeout_seconds, headless)
                    return token, channel
                finally:
                    await context.close()

            raise BrowserAuthError(
                "No browser could be launched. Tried "
                + str(len(launch_errors))
                + ": "
                + " | ".join(launch_errors)
            )


async def _watch_for_token(context, timeout_seconds: float, headless: bool) -> str:
    captured: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def on_request(request) -> None:
        if API_MARKER not in request.url or captured.done():
            return
        value = request.headers.get(AUTH_HEADER)
        if value and value.lower().startswith("bearer "):
            token = value.split(" ", 1)[1].strip()
            if token.count(".") == 2:
                captured.set_result(token)

    context.on("request", on_request)
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
    except Exception:
        pass  # a redirect to the login host can abort navigation; keep waiting

    try:
        return await asyncio.wait_for(captured, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise BrowserAuthError(
            "No FPL token appeared within "
            + str(int(timeout_seconds))
            + " seconds."
            + (
                " The saved session has probably expired — run `fpl_login` to sign in again."
                if headless
                else " The sign-in was not completed in the browser window."
            )
        ) from exc


async def interactive_login(
    timeout_seconds: float = 600.0, preferred_channel: str | None = None
) -> tuple[str, str | None]:
    """Open a visible browser so the user can sign in, then capture a token.

    Anything the login throws up — password, two-factor, a bot check — is handled
    by the person at the keyboard, in a real browser window. Nothing is bypassed
    and no credentials pass through this process.
    """
    return await _capture_token(
        headless=False, timeout_seconds=timeout_seconds, preferred_channel=preferred_channel
    )


async def silent_refresh(
    timeout_seconds: float = 45.0, preferred_channel: str | None = None
) -> tuple[str, str | None]:
    """Reuse the saved session headlessly to mint a fresh token."""
    if not profile_exists():
        raise BrowserAuthError(
            "No saved browser session yet. Run the `fpl_login` tool once to sign in."
        )
    return await _capture_token(
        headless=True, timeout_seconds=timeout_seconds, preferred_channel=preferred_channel
    )


def clear_profile() -> None:
    """Forget the saved session."""
    import shutil

    if PROFILE_DIR.exists():
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)

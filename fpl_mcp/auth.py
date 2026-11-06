"""Signing in to FPL: OAuth 2.0 authorization code + PKCE, kept alive by refresh tokens.

FPL accounts live in a PingOne identity provider at account.premierleague.com.
The FPL web app is a public OAuth client: it signs people in with the
authorization-code flow plus PKCE, asks for `offline_access`, and renews its
access token with a refresh token. This module does the same, so:

* the user signs in once, on FPL's own sign-in page, in a real browser. This
  server never sees a password, and two-factor or a bot check is handled by the
  person at the keyboard;
* afterwards, access tokens are renewed with one HTTPS request. No browser is
  involved after sign-in.

The only thing written to disk is `credentials.json`, holding the refresh token
and the current access token, readable only by the current user. Renewals are
serialised across processes by a lock file: the identity provider may rotate
the refresh token on every use, and two server processes (say two agents) racing
on a rotated token would sign the user out.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from filelock import AsyncFileLock, Timeout

from . import __version__
from .config import CONFIG_DIR, write_private_json

log = logging.getLogger(__name__)

ISSUER = "https://account.premierleague.com/as"
AUTHORIZE_URL = ISSUER + "/authorize"
TOKEN_URL = ISSUER + "/token"
REVOKE_URL = ISSUER + "/revoke"
# The FPL web app's own public client registration, read from its JS bundle.
CLIENT_ID = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"
REDIRECT_URI = "https://fantasy.premierleague.com/"
# Mirrors the web app exactly. The access token's audience follows the scopes
# requested, and the FPL API only accepts the token the web app would get.
SCOPE = "openid profile email offline_access p1:update:user consent interest"

CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"
LOCK_PATH = CONFIG_DIR / "credentials.lock"
# Version 0.2.0 kept a whole browser profile here and replayed it headlessly.
LEGACY_PROFILE_DIR = CONFIG_DIR / "browser-profile"

USER_AGENT = (
    "fantasy-premier-league-mcp/" + __version__ + " (+https://github.com/anubabajide/fpl-mcp)"
)
RENEW_MARGIN_SECONDS = 120
PENDING_TTL_SECONDS = 20 * 60
LOCK_TIMEOUT_SECONDS = 60

NOT_SIGNED_IN = (
    "Not signed in to FPL. Run the `fpl_login` tool: it opens FPL's own sign-in "
    "page in a browser window, and the session is remembered afterwards."
)


# ------------------------------------------------------------------ errors
class AuthFailure(RuntimeError):
    """Base class for sign-in problems. Messages are written for the end user."""


class SignInRequired(AuthFailure):
    """There is no usable session; the user has to run `fpl_login`."""


class SignInError(AuthFailure):
    """A sign-in attempt failed."""


class BrowserUnavailable(SignInError):
    """No browser window could be opened on this machine."""


class TokenServiceError(AuthFailure):
    """FPL's sign-in service could not be reached or gave an unexpected answer."""


class _InvalidGrant(Exception):
    """The token endpoint rejected a code or refresh token as unusable."""


# ------------------------------------------------------------------ tokens
def jwt_claims(token: str | None) -> dict:
    """Decode a JWT payload without verifying it. We are not the issuer."""
    token = (token or "").strip().removeprefix("Bearer ").strip()
    if token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float  # unix seconds
    scope: str | None = None

    @classmethod
    def from_response(
        cls, data: dict, *, previous: "Tokens | None" = None, now: float | None = None
    ) -> "Tokens":
        access = data.get("access_token")
        if not isinstance(access, str) or access.count(".") != 2:
            raise TokenServiceError("FPL's sign-in service returned no usable access token.")
        now = time.time() if now is None else now
        expires_in = data.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            expires_at = now + float(expires_in)
        else:
            exp = jwt_claims(access).get("exp")
            expires_at = float(exp) if isinstance(exp, (int, float)) else now + 3600
        # Without rotation the provider omits refresh_token; the old one stays valid.
        return cls(
            access_token=access,
            refresh_token=data.get("refresh_token") or (previous and previous.refresh_token),
            expires_at=expires_at,
            scope=data.get("scope") or (previous and previous.scope),
        )

    def seconds_left(self, now: float | None = None) -> int:
        return int(self.expires_at - (time.time() if now is None else now))


class TokenStore:
    """The owner-only file holding the signed-in session."""

    def __init__(self, path: Path = CREDENTIALS_PATH, lock_path: Path = LOCK_PATH) -> None:
        self.path = path
        self.lock_path = lock_path
        self._lock: AsyncFileLock | None = None

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Tokens | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("Ignoring unreadable FPL credentials file at %s", self.path)
            return None
        if not isinstance(data, dict) or data.get("client_id") != CLIENT_ID:
            return None
        access, expires_at = data.get("access_token"), data.get("expires_at")
        if not isinstance(access, str) or not isinstance(expires_at, (int, float)):
            return None
        return Tokens(
            access_token=access,
            refresh_token=data.get("refresh_token") or None,
            expires_at=float(expires_at),
            scope=data.get("scope") or None,
        )

    def save(self, tokens: Tokens) -> None:
        write_private_json(
            self.path,
            {
                "version": 1,
                "client_id": CLIENT_ID,
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expires_at": int(tokens.expires_at),
                "scope": tokens.scope,
                "saved_at": int(time.time()),
            },
        )

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def lock(self) -> AsyncFileLock:
        """Cross-process lock around read-modify-write of the credentials."""
        if self._lock is None:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._lock = AsyncFileLock(str(self.lock_path), timeout=LOCK_TIMEOUT_SECONDS)
        return self._lock


# ----------------------------------------------------------- sign-in attempt
def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class PendingSignIn:
    """One authorization request: its state, and the PKCE secret that redeems it."""

    state: str
    verifier: str
    created_at: float = field(default_factory=time.monotonic)

    @classmethod
    def start(cls) -> "PendingSignIn":
        return cls(state=secrets.token_urlsafe(24), verifier=secrets.token_urlsafe(64))

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > PENDING_TTL_SECONDS

    @property
    def authorize_url(self) -> str:
        return AUTHORIZE_URL + "?" + urlencode(
            {
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "response_mode": "query",
                "scope": SCOPE,
                "state": self.state,
                "code_challenge": _pkce_challenge(self.verifier),
                "code_challenge_method": "S256",
            }
        )

    def code_from(self, redirect_url: str) -> str:
        """Pull the authorization code out of the address FPL redirected to."""
        raw = redirect_url.strip().strip("'\"<>")
        query = urlsplit(raw).query
        if not query and "?" not in raw and "=" in raw:
            query = raw  # just the query string was pasted
        params = parse_qs(query)

        def one(name: str) -> str | None:
            values = params.get(name)
            return values[0] if values else None

        if one("error"):
            raise SignInError(
                "FPL's sign-in page reported a problem: "
                + (one("error_description") or one("error") or "unknown error")
            )
        code = one("code")
        if not code:
            raise SignInError(
                "That address has no sign-in code in it. After signing in, copy the "
                "full address of the page you land on; it starts with "
                + REDIRECT_URI
                + "?code="
            )
        if one("state") != self.state:
            raise SignInError(
                "That address belongs to a different sign-in attempt. Run `fpl_login` "
                "again and use the newest link."
            )
        return code


# --------------------------------------------------------------- the session
BrowserLogin = Callable[..., Awaitable[tuple[str, "str | None"]]]


async def _default_browser(
    authorize_url: str, *, timeout_seconds: float, preferred_channel: str | None
) -> tuple[str, str | None]:
    from . import browser_auth

    return await browser_auth.capture_redirect(
        authorize_url,
        redirect_prefix=REDIRECT_URI,
        timeout_seconds=timeout_seconds,
        preferred_channel=preferred_channel,
    )


def legacy_profile_exists() -> bool:
    return LEGACY_PROFILE_DIR.exists()


async def remove_legacy_profile() -> None:
    if LEGACY_PROFILE_DIR.exists():
        await asyncio.to_thread(shutil.rmtree, LEGACY_PROFILE_DIR, ignore_errors=True)


class Session:
    """The signed-in FPL session: signs in, keeps the access token fresh, signs out."""

    def __init__(
        self,
        store: TokenStore | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        browser: BrowserLogin | None = None,
    ) -> None:
        self.store = store or TokenStore()
        self._transport = transport
        self._browser = browser or _default_browser
        self._renew_lock = asyncio.Lock()
        self._pending: PendingSignIn | None = None
        self._login_task: asyncio.Task | None = None
        self.last_login_error: AuthFailure | None = None
        # Told which browser channel opened the window, so it can be tried first next time.
        self.on_browser_used: Callable[[str | None], None] | None = None

    # ------------------------------------------------------------- tokens
    def has_credentials(self) -> bool:
        return self.store.exists()

    def tokens(self) -> Tokens | None:
        return self.store.load()

    async def access_token(
        self, *, min_seconds: int = RENEW_MARGIN_SECONDS, rejected: str | None = None
    ) -> str:
        """A usable access token, renewing it first if it is close to expiry.

        `rejected` names a token the API has just refused. It is renewed even if
        it looks unexpired, unless another process has already replaced it.
        """

        def usable(tokens: Tokens) -> bool:
            return tokens.access_token != rejected and tokens.seconds_left() > min_seconds

        async with self._renew_lock:
            tokens = self.store.load()
            if tokens is None:
                raise SignInRequired(NOT_SIGNED_IN)
            if usable(tokens):
                return tokens.access_token
            try:
                async with self.store.lock():
                    # Re-read: another process may have renewed while we waited.
                    tokens = self.store.load()
                    if tokens is None:
                        raise SignInRequired(NOT_SIGNED_IN)
                    if usable(tokens):
                        return tokens.access_token
                    return (await self._renew(tokens)).access_token
            except Timeout as exc:
                raise TokenServiceError(
                    "Timed out waiting for another FPL MCP process to finish renewing "
                    "the session. Try again."
                ) from exc

    async def _renew(self, tokens: Tokens) -> Tokens:
        if not tokens.refresh_token:
            raise SignInRequired(
                "The saved FPL session cannot renew itself. Run `fpl_login` to sign in again."
            )
        form = {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": tokens.refresh_token,
        }
        if tokens.scope:
            form["scope"] = tokens.scope
        try:
            data = await self._token_request(form)
        except _InvalidGrant as exc:
            self.store.clear()
            raise SignInRequired(
                "Your FPL session has expired or was signed out. Run `fpl_login` to "
                "sign in again."
            ) from exc
        fresh = Tokens.from_response(data, previous=tokens)
        self.store.save(fresh)
        return fresh

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    async def _token_request(self, form: dict) -> dict:
        try:
            async with self._http() as http:
                resp = await http.post(TOKEN_URL, data=form)
        except httpx.HTTPError as exc:
            raise TokenServiceError("Could not reach FPL's sign-in service: " + str(exc)) from exc
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code == 200 and isinstance(body, dict):
            return body
        error = body.get("error") if isinstance(body, dict) else None
        detail = (body.get("error_description") if isinstance(body, dict) else None) or ""
        if error == "invalid_grant":
            raise _InvalidGrant(detail or error)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TokenServiceError(
                "FPL's sign-in service is unavailable right now ("
                + str(resp.status_code)
                + "). Try again shortly."
            )
        raise TokenServiceError(
            "FPL's sign-in service refused the request ("
            + str(resp.status_code)
            + (" " + error if error else "")
            + (": " + detail if detail else "")
            + "). FPL may have changed how sign-in works; please report this at "
            "https://github.com/anubabajide/fpl-mcp/issues"
        )

    # ------------------------------------------------------------ sign-in
    @property
    def login_in_progress(self) -> bool:
        return self._login_task is not None and not self._login_task.done()

    @property
    def pending(self) -> PendingSignIn | None:
        if self._pending is not None and self._pending.expired:
            self._pending = None
        return self._pending

    def start_login(
        self, *, open_browser: bool, preferred_channel: str | None, timeout_seconds: float
    ) -> PendingSignIn:
        """Begin signing in, or return the attempt already under way.

        An unexpired attempt is reused, so a sign-in link already handed to the
        user keeps working however many times this is called.
        """
        if self.login_in_progress and self._pending is not None:
            return self._pending
        pending = self.pending or PendingSignIn.start()
        self._pending = pending
        self.last_login_error = None
        if open_browser:
            task = asyncio.create_task(
                self._browser_login(pending, preferred_channel, timeout_seconds)
            )
            task.add_done_callback(self._observe_login)
            self._login_task = task
        return pending

    async def wait_for_login(self, seconds: float) -> bool:
        """Wait up to `seconds` for a browser sign-in. True once it has finished.

        The sign-in task is never cancelled by this, so a caller giving up early
        leaves the window open for the user to finish.
        """
        task = self._login_task
        if task is None or task.done():
            return True
        done, _ = await asyncio.wait({task}, timeout=max(0.0, seconds))
        return bool(done)

    async def _browser_login(
        self, pending: PendingSignIn, preferred_channel: str | None, timeout_seconds: float
    ) -> None:
        redirect_url, channel = await self._browser(
            pending.authorize_url,
            timeout_seconds=timeout_seconds,
            preferred_channel=preferred_channel,
        )
        if self.on_browser_used is not None:
            try:
                self.on_browser_used(channel)
            except Exception:  # remembering the browser is a nicety, never fatal
                log.warning("Could not record the browser used for sign-in", exc_info=True)
        await self.complete_login(redirect_url, pending=pending)

    def _observe_login(self, task: asyncio.Task) -> None:
        # Cancellation only ever comes from sign-out or a sign-in finished another way.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        if isinstance(exc, AuthFailure):
            self.last_login_error = exc
        else:
            log.error("FPL sign-in failed unexpectedly", exc_info=exc)
            self.last_login_error = SignInError("Sign-in failed unexpectedly: " + str(exc))

    async def complete_login(
        self, redirect_url: str, *, pending: PendingSignIn | None = None
    ) -> Tokens:
        """Redeem the address FPL redirected to after sign-in, and save the session."""
        pending = pending or self.pending
        if pending is None or pending.expired:
            raise SignInError(
                "No sign-in is waiting for that address; it may have expired. Run "
                "`fpl_login` to start again."
            )
        code = pending.code_from(redirect_url)
        try:
            data = await self._token_request(
                {
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": pending.verifier,
                }
            )
        except _InvalidGrant as exc:
            raise SignInError(
                "FPL would not accept that sign-in code (" + str(exc) + "). Codes work "
                "once and expire within minutes; run `fpl_login` to start again."
            ) from exc
        tokens = Tokens.from_response(data)
        async with self.store.lock():
            self.store.save(tokens)
        if self._pending is pending:
            self._pending = None
        self.last_login_error = None
        # Finished via a pasted link while a browser window was still open: close it.
        task = self._login_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        await remove_legacy_profile()
        return tokens

    def login_status(self) -> dict | None:
        """Describe a sign-in that is under way or has just failed, if any."""
        pending = self.pending
        if self.login_in_progress and pending is not None:
            return {
                "status": "awaiting_browser",
                "message": "FPL's sign-in page is open in a browser window on this machine.",
                "sign_in_url": pending.authorize_url,
            }
        if pending is not None:
            return {"status": "awaiting_redirect_url", "sign_in_url": pending.authorize_url}
        if self.last_login_error is not None:
            return {"status": "failed", "error": str(self.last_login_error)}
        return None

    async def sign_out(self) -> bool:
        """Forget the session here and revoke it at FPL. True if FPL confirmed."""
        task, self._login_task = self._login_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        self._pending = None
        self.last_login_error = None
        async with self._renew_lock:
            async with self.store.lock():
                tokens = self.store.load()
                self.store.clear()
        await remove_legacy_profile()
        if tokens is None or not tokens.refresh_token:
            return False
        try:
            async with self._http() as http:
                resp = await http.post(
                    REVOKE_URL,
                    data={
                        "client_id": CLIENT_ID,
                        "token": tokens.refresh_token,
                        "token_type_hint": "refresh_token",
                    },
                )
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

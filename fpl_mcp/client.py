"""Thin async client over the Fantasy Premier League API."""
from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

from .config import Config

BASE = "https://fantasy.premierleague.com/api"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Static-ish payloads are reused for this long (seconds) inside one process.
TTL_BOOTSTRAP = 600
TTL_FIXTURES = 600
TTL_ENTRY = 60
TTL_LIVE = 60


class FPLError(RuntimeError):
    """Raised for API failures that should be reported to the user verbatim."""


class AuthError(FPLError):
    """Raised when the bearer token is missing, malformed or rejected."""


class FPLClient:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.load()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._client: httpx.AsyncClient | None = None

    # ---------------------------------------------------------------- plumbing
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(25.0),
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json",
                    "Referer": "https://fantasy.premierleague.com/",
                    "Origin": "https://fantasy.premierleague.com",
                },
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ensure_token(self, margin_seconds: int = 120) -> None:
        """Make sure a usable token is in hand, refreshing from the saved session.

        Called before every authenticated request. A token that is missing or
        about to expire triggers a headless replay of the stored browser
        session; if that session has itself lapsed the caller is told to run
        `fpl_login` again.
        """
        from . import browser_auth

        left = self.token_seconds_left()
        if self.config.bearer_token and (left is None or left > margin_seconds):
            return
        if not (self.config.browser_session and browser_auth.profile_exists()):
            if self.config.bearer_token:
                return  # a manually pasted token; let the API judge it
            raise AuthError(
                "Not signed in to FPL. Run the `fpl_login` tool once — it opens a "
                "browser window for you to sign in, and remembers the session."
            )
        try:
            token, channel = await browser_auth.silent_refresh(
                preferred_channel=self.config.browser_channel
            )
        except browser_auth.BrowserAuthError as exc:
            raise AuthError(str(exc)) from exc
        self.config.bearer_token = token
        self.config.browser_channel = channel
        self.config.save()

    def _auth_headers(self) -> dict[str, str]:
        token = (self.config.bearer_token or "").strip()
        if not token:
            raise AuthError(
                "Not signed in to FPL. Run the `fpl_login` tool once — it opens a "
                "browser window for you to sign in, and remembers the session."
            )
        token = token.removeprefix("Bearer ").strip()
        if token.count(".") != 2:
            raise AuthError(
                "The stored token is not a JWT (expected three dot-separated parts). "
                "Copy the full value of the `X-Api-Authorization` header, minus the "
                "leading `Bearer `."
            )
        return {"X-Api-Authorization": f"Bearer {token}"}

    async def _get(self, path: str, ttl: int = 0, auth: bool = False) -> Any:
        key = f"GET {path} auth={auth}"
        if ttl:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]

        http = await self._http()
        if auth:
            await self.ensure_token()
        headers = self._auth_headers() if auth else {}
        try:
            resp = await http.get(f"{BASE}/{path.lstrip('/')}", headers=headers)
        except httpx.HTTPError as exc:
            raise FPLError(f"Network error calling FPL API: {exc}") from exc

        data = self._unwrap(resp, path)
        if ttl:
            self._cache[key] = (time.monotonic(), data)
        return data

    async def _post(self, path: str, payload: dict) -> Any:
        http = await self._http()
        await self.ensure_token()
        headers = self._auth_headers() | {"Content-Type": "application/json"}
        try:
            resp = await http.post(
                f"{BASE}/{path.lstrip('/')}", headers=headers, json=payload
            )
        except httpx.HTTPError as exc:
            raise FPLError(f"Network error calling FPL API: {exc}") from exc
        return self._unwrap(resp, path)

    @staticmethod
    def _unwrap(resp: httpx.Response, path: str) -> Any:
        if resp.status_code in (401, 403):
            raise AuthError(
                f"FPL rejected the request to /{path} ({resp.status_code}). The token "
                f"is stale and the saved browser session could not renew it, so the "
                f"session itself has most likely expired — run `fpl_login` to sign in "
                f"again. Server said: {resp.text[:300]}"
            )
        if resp.status_code >= 400:
            raise FPLError(f"FPL API error {resp.status_code} on /{path}: {resp.text[:500]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise FPLError(f"FPL returned non-JSON for /{path}: {resp.text[:200]}") from exc

    # ------------------------------------------------------------ public reads
    async def bootstrap(self) -> dict:
        return await self._get("bootstrap-static/", ttl=TTL_BOOTSTRAP)

    async def fixtures(self, event: int | None = None) -> list[dict]:
        path = "fixtures/" if event is None else f"fixtures/?event={event}"
        return await self._get(path, ttl=TTL_FIXTURES)

    async def element_summary(self, element_id: int) -> dict:
        return await self._get(f"element-summary/{element_id}/", ttl=TTL_BOOTSTRAP)

    async def entry(self, entry_id: int) -> dict:
        return await self._get(f"entry/{entry_id}/", ttl=TTL_ENTRY)

    async def entry_history(self, entry_id: int) -> dict:
        return await self._get(f"entry/{entry_id}/history/", ttl=TTL_ENTRY)

    async def entry_picks(self, entry_id: int, event: int) -> dict:
        return await self._get(f"entry/{entry_id}/event/{event}/picks/", ttl=TTL_ENTRY)

    async def entry_transfers(self, entry_id: int) -> list[dict]:
        return await self._get(f"entry/{entry_id}/transfers/", ttl=TTL_ENTRY)

    async def live(self, event: int) -> dict:
        return await self._get(f"event/{event}/live/", ttl=TTL_LIVE)

    async def league_standings(self, league_id: int, page: int = 1) -> dict:
        return await self._get(
            f"leagues-classic/{league_id}/standings/?page_standings={page}", ttl=TTL_ENTRY
        )

    # -------------------------------------------------------- authed endpoints
    async def me(self) -> dict:
        return await self._get("me/", auth=True)

    async def my_team(self, entry_id: int) -> dict:
        """Live squad including selling prices and free-transfer count."""
        return await self._get(f"my-team/{entry_id}/", auth=True)

    async def post_lineup(self, entry_id: int, picks: list[dict], chip: str | None = None) -> Any:
        return await self._post(f"my-team/{entry_id}/", {"chip": chip, "picks": picks})

    async def post_transfers(
        self, entry_id: int, event: int, transfers: list[dict], chip: str | None = None
    ) -> Any:
        return await self._post(
            "transfers/",
            {
                "confirmed": True,
                "entry": entry_id,
                "event": event,
                "transfers": transfers,
                "chip": chip,
            },
        )

    # ------------------------------------------------------------------ tokens
    def token_claims(self) -> dict:
        """Decode the JWT payload without verifying it (we are not the issuer)."""
        token = (self.config.bearer_token or "").strip().removeprefix("Bearer ").strip()
        if token.count(".") != 2:
            return {}
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        try:
            return json.loads(base64.urlsafe_b64decode(payload))
        except Exception:
            return {}

    def token_seconds_left(self) -> int | None:
        exp = self.token_claims().get("exp")
        if not isinstance(exp, (int, float)):
            return None
        return int(exp - time.time())

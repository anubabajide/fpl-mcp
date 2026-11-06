"""Thin async client over the Fantasy Premier League API."""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from . import auth
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
    """Raised when there is no usable FPL session, or FPL rejects it."""


class FPLClient:
    def __init__(self, config: Config | None = None, session: auth.Session | None = None) -> None:
        self.config = config or Config.load()
        self.session = session or auth.Session()
        self.session.on_browser_used = self._remember_browser
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

    def _remember_browser(self, channel: str | None) -> None:
        if channel != self.config.browser_channel:
            self.config.browser_channel = channel
            self.config.save()

    def has_credentials(self) -> bool:
        """True if authenticated endpoints are worth trying (not that they will work)."""
        return self.session.has_credentials() or bool(self.config.bearer_token)

    def manual_token(self) -> str | None:
        token = (self.config.bearer_token or "").strip().removeprefix("Bearer ").strip()
        return token or None

    async def access_token(self, rejected: str | None = None) -> str:
        """The token to send, renewing the saved session first if it is near expiry.

        A signed-in session wins over a hand-pasted token, because only the
        session can renew itself.
        """
        if self.session.has_credentials():
            try:
                return await self.session.access_token(rejected=rejected)
            except auth.SignInRequired as exc:
                if not self.manual_token():
                    raise AuthError(str(exc)) from exc
            except auth.AuthFailure as exc:
                raise AuthError(str(exc)) from exc
        token = self.manual_token()
        if token is None:
            message = auth.NOT_SIGNED_IN
            if auth.legacy_profile_exists():
                message += (
                    " This version signs in differently from 0.2.0, so the old saved "
                    "browser session cannot be reused; sign in once more."
                )
            raise AuthError(message)
        if token.count(".") != 2:
            raise AuthError(
                "The pasted token is not a JWT (expected three dot-separated parts). "
                "Copy the full value of the `X-Api-Authorization` header, minus the "
                "leading `Bearer `. Or better, sign in with `fpl_login`."
            )
        return token

    async def _send(self, method: str, path: str, *, auth_required: bool, json_body=None) -> Any:
        http = await self._http()
        url = f"{BASE}/{path.lstrip('/')}"
        token = await self.access_token() if auth_required else None
        for attempt in (1, 2):
            headers = {"X-Api-Authorization": f"Bearer {token}"} if token else {}
            try:
                resp = await http.request(method, url, headers=headers, json=json_body)
            except httpx.HTTPError as exc:
                raise FPLError(f"Network error calling FPL API: {exc}") from exc
            # A 401 means the token was refused before anything happened, so one
            # retry with a renewed token is safe even for a POST.
            if (
                resp.status_code == 401
                and token
                and attempt == 1
                and self.session.has_credentials()
                and token != self.manual_token()
            ):
                renewed = await self.access_token(rejected=token)
                if renewed != token:
                    token = renewed
                    continue
            return self._unwrap(resp, path)

    async def _get(self, path: str, ttl: int = 0, auth: bool = False) -> Any:
        key = f"GET {path} auth={auth}"
        if ttl:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        data = await self._send("GET", path, auth_required=auth)
        if ttl:
            self._cache[key] = (time.monotonic(), data)
        return data

    async def _post(self, path: str, payload: dict) -> Any:
        return await self._send("POST", path, auth_required=True, json_body=payload)

    @staticmethod
    def _unwrap(resp: httpx.Response, path: str) -> Any:
        if resp.status_code == 401:
            raise AuthError(
                f"FPL rejected the session token on /{path} (401). Sign in again with "
                f"`fpl_login`. Server said: {resp.text[:300]}"
            )
        if resp.status_code == 403:
            raise AuthError(
                f"FPL refused access to /{path} (403). That endpoint only works for the "
                f"signed-in manager's own team; if this is your team, sign in again "
                f"with `fpl_login`. Server said: {resp.text[:300]}"
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

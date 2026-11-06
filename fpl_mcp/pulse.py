"""Client for the Premier League's own Pulse Live backend.

This is the service premierleague.com runs on. It is public and needs no
credentials, but it is **undocumented and unversioned** — paths and payloads can
change without notice, so every call here degrades to None rather than raising,
and callers treat missing Pulse data as "unknown", never as an error.

It supplies two things the Fantasy API does not:

* club managers (`officials`), which is the only structured source for
  managerial changes;
* confirmed team lists for a fixture, published about an hour before kickoff.

FPL joins to it on two keys it already ships: `team.pulse_id` for clubs, and
`element.opta_code` against a player's `altIds.opta`.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

BASE = "https://footballapi.pulselive.com/football"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Origin": "https://www.premierleague.com",
    "Referer": "https://www.premierleague.com/",
    "Accept": "application/json",
}

TTL_SEASON = 3600
TTL_STAFF = 900
TTL_FIXTURE = 120  # team lists appear ~1h before kickoff, so do not cache long


class PulseClient:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._client: httpx.AsyncClient | None = None
        self._season_id: int | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=HEADERS, timeout=httpx.Timeout(20.0), follow_redirects=True
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict | None = None, ttl: int = 0) -> Any:
        key = path + "?" + repr(sorted((params or {}).items()))
        if ttl:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        try:
            http = await self._http()
            resp = await http.get(BASE + path, params=params)
            if resp.status_code >= 400:
                return None
            data = resp.json()
        except Exception:
            return None  # Pulse is best-effort; absence is not an error
        if ttl:
            self._cache[key] = (time.monotonic(), data)
        return data

    async def current_season_id(self) -> int | None:
        """Highest compSeason id for the Premier League, i.e. the live season."""
        if self._season_id is not None:
            return self._season_id
        data = await self._get("/competitions/1/compseasons",
                               {"pageSize": 10}, ttl=TTL_SEASON)
        if not data:
            return None
        seasons = data.get("content") or []
        if not seasons:
            return None
        self._season_id = int(max(seasons, key=lambda s: s["id"])["id"])
        return self._season_id

    async def manager(self, pulse_team_id: int) -> dict | None:
        """The club's active manager, or None if Pulse does not say."""
        season = await self.current_season_id()
        if season is None:
            return None
        data = await self._get(
            "/teams/" + str(pulse_team_id) + "/compseasons/" + str(season) + "/staff",
            {"altIds": "true"}, ttl=TTL_STAFF,
        )
        if not data:
            return None
        officials = data.get("officials") or []
        managers = [o for o in officials if o.get("role") == "Manager"]
        active = [o for o in managers if o.get("active")] or managers
        if not active:
            return None
        m = active[0]
        return {
            "name": (m.get("name") or {}).get("display"),
            "role": m.get("role"),
            "active": m.get("active"),
            "age": m.get("age"),
            "nationality": ((m.get("birth") or {}).get("country") or {}).get("country"),
            "official_id": m.get("id"),
        }

    async def fixtures(self, gameweek: int | None = None,
                       statuses: str | None = None, page_size: int = 40) -> list[dict]:
        season = await self.current_season_id()
        if season is None:
            return []
        params: dict[str, Any] = {
            "comps": 1, "compSeasons": season, "pageSize": page_size,
            "sort": "asc", "altIds": "true",
        }
        if gameweek is not None:
            params["gameweekNumbers"] = gameweek
        if statuses:
            params["statuses"] = statuses
        data = await self._get("/fixtures", params, ttl=TTL_FIXTURE)
        return (data or {}).get("content") or []

    async def team_lists(self, fixture_id: int) -> list[dict] | None:
        """Confirmed lineups for a fixture, or None until they are published."""
        data = await self._get("/fixtures/" + str(fixture_id),
                               {"altIds": "true"}, ttl=TTL_FIXTURE)
        if not data:
            return None
        lists = data.get("teamLists") or []
        usable = [t for t in lists if t and (t.get("lineup") or t.get("substitutes"))]
        return usable or None


def lineup_players(team_list: dict) -> tuple[list[dict], list[dict]]:
    """Split a Pulse team list into (starters, substitutes) with opta codes."""
    def rows(group: str) -> list[dict]:
        out = []
        for p in team_list.get(group) or []:
            out.append({
                "name": (p.get("name") or {}).get("display"),
                "opta_code": (p.get("altIds") or {}).get("opta"),
                "match_position": p.get("matchPosition"),
                "shirt": p.get("matchShirtNumber"),
                "captain": bool(p.get("captain")),
            })
        return out
    return rows("lineup"), rows("substitutes")

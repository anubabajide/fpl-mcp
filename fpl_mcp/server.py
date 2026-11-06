"""MCP server exposing Fantasy Premier League team management tools."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from .analysis import (
    CHIP_LABELS,
    best_performers,
    describe_squad,
    differentials,
    fixture_ticker,
    league_ownership,
    squad_totals,
    validate_lineup,
    validate_squad,
)
from . import browser_auth
from .client import AuthError, FPLClient, FPLError
from .export import (
    LIVE_ROW_FIELDS,
    STANDARD_FIELDS,
    fixtures_by_gameweek,
    live_rows,
    player_rows,
    resolve_fields,
    to_csv,
)
from .config import Config
from .index import METRICS, Index, sort_players
from .lineups import predict_xi, start_rates
from .pulse import PulseClient, lineup_players

INSTRUCTIONS = """Manage a Fantasy Premier League team.

Read-only tools work with no credentials beyond a team id. Writing to the team
(`fpl_set_lineup`, `fpl_set_captain`, `fpl_make_transfers`) needs a signed-in FPL
session. `fpl_login` is a two-step tool: the first call opens a browser window on
the user's machine and returns immediately with status "waiting"; after they sign
in, call it a second time to store the session. Do not treat "waiting" as failure.
The session is then reused and renewed automatically.

Every write defaults to `dry_run=true`, which validates and previews the change
without submitting it. Always show the user that preview and get their agreement
before calling again with `dry_run=false`.
"""

server = MCPServer(
    name="fpl",
    title="Fantasy Premier League",
    version="0.5.1",
    instructions=INSTRUCTIONS,
)

_client: FPLClient | None = None
_pulse: PulseClient | None = None


def get_pulse() -> PulseClient:
    global _pulse
    if _pulse is None:
        _pulse = PulseClient()
    return _pulse


def get_client() -> FPLClient:
    global _client
    if _client is None:
        _client = FPLClient(Config.load())
    return _client


async def get_index() -> Index:
    return Index(await get_client().bootstrap())


def require_team_id(team_id: int | None) -> int:
    resolved = team_id or get_client().config.team_id
    if not resolved:
        raise ValueError(
            "No FPL team id set. Pass team_id, or store one with `fpl_setup`. "
            "Your id is the number in your team URL: "
            "fantasy.premierleague.com/entry/<TEAM_ID>/event/1"
        )
    return int(resolved)


def resolve_metric(index: Index, metric: str | None) -> str:
    if metric is None:
        return index.default_metric()
    if metric not in METRICS:
        raise ValueError(
            "Unknown metric " + repr(metric) + ". Choose from: " + ", ".join(sorted(METRICS))
        )
    return metric


def season_note(index: Index) -> dict:
    note: dict[str, Any] = {
        "season_stage": index.season_stage,
        "current_gameweek": index.current_event,
        "next_gameweek": index.next_event,
    }
    if index.season_stage == "preseason":
        note["caveat"] = (
            "The season has not kicked off yet. Until gameweek 1 completes, FPL "
            "keeps serving last season's totals, so points, minutes, goals and "
            "value figures describe the previous campaign, not this one. Form "
            "reads 0.00 for everyone. Treat these as a prior, and note that "
            "promoted-club and newly signed players have no numbers at all."
        )
    return note


# ============================================================ setup and auth
@server.tool()
async def fpl_setup(
    team_id: int | None = None,
    league_id: int | None = None,
) -> dict:
    """Store your FPL team id (and optionally a default mini-league id) on disk.

    Find your team id in the URL when viewing your team on the FPL site:
    fantasy.premierleague.com/entry/<TEAM_ID>/event/1
    """
    client = get_client()
    cfg = client.config
    if team_id is not None:
        cfg.team_id = int(team_id)
    if league_id is not None:
        cfg.default_league_id = int(league_id)
    cfg.save()

    out: dict[str, Any] = {"team_id": cfg.team_id, "default_league_id": cfg.default_league_id}
    if cfg.team_id:
        try:
            entry = await client.entry(cfg.team_id)
            out["team_name"] = entry.get("name")
            out["manager"] = (
                str(entry.get("player_first_name", "")) + " " + str(entry.get("player_last_name", ""))
            ).strip()
        except FPLError as exc:
            out["warning"] = "Could not verify that team id: " + str(exc)
    out["has_bearer_token"] = bool(cfg.bearer_token)
    return out


_login_task: "asyncio.Task[str] | None" = None


@server.tool()
async def fpl_login(wait_seconds: int = 15, timeout_seconds: int = 600) -> dict:
    """Sign in to FPL in a real browser window, and remember the session.

    This returns quickly rather than blocking while you sign in. The first call
    opens a browser window on the machine running this server and returns
    `status: "waiting"`. Sign in there, then **call this tool again** to finish
    and store the session.

    Signing in happens entirely in that window — password, two-factor and any bot
    check are handled by you, and no credentials pass through this server. Once
    stored, tokens renew silently, so this should be a rare, one-off step.
    """
    global _login_task
    client = get_client()

    def _finish(result: tuple) -> dict:
        token, channel = result
        client.config.bearer_token = token
        client.config.browser_session = True
        client.config.browser_channel = channel
        client.config.save()
        return {
            "signed_in": True,
            "status": "complete",
            "message": "Signed in and session saved. Future tokens renew automatically.",
            "browser_used": channel or "bundled chromium",
            "session_profile": str(browser_auth.PROFILE_DIR),
        }

    # A previous call left a finished attempt behind — collect its outcome.
    if _login_task is not None and _login_task.done():
        task, _login_task = _login_task, None
        try:
            return _finish(task.result())
        except browser_auth.BrowserAuthError as exc:
            return {
                "signed_in": False,
                "status": "failed",
                "error": str(exc),
                "next_step": "Call fpl_login again to open a fresh window.",
            }
        except asyncio.CancelledError:
            return {"signed_in": False, "status": "cancelled"}

    if _login_task is None:
        # Runs as an independent task so it survives this tool call returning,
        # or the client giving up on it.
        _login_task = asyncio.create_task(
            browser_auth.interactive_login(
                timeout_seconds=timeout_seconds,
                preferred_channel=client.config.browser_channel,
            )
        )

    # Shielded: a timeout here must not cancel a sign-in already in progress.
    try:
        token = await asyncio.wait_for(asyncio.shield(_login_task), timeout=wait_seconds)
    except asyncio.TimeoutError:
        return {
            "signed_in": False,
            "status": "waiting",
            "message": (
                "A browser window is open on the machine running this server. "
                "Sign in to FPL there, then call fpl_login again to finish."
            ),
            "window_open_for_seconds": timeout_seconds,
        }
    except browser_auth.BrowserAuthError as exc:
        _login_task = None
        return {
            "signed_in": False,
            "status": "failed",
            "error": str(exc),
            "next_step": "Call fpl_login again to open a fresh window.",
        }

    _login_task = None
    return _finish(token)


@server.tool()
async def fpl_logout() -> dict:
    """Forget the saved browser session and the stored token."""
    client = get_client()
    browser_auth.clear_profile()
    client.config.bearer_token = None
    client.config.browser_session = False
    client.config.save()
    return {"signed_out": True, "message": "Saved session and token cleared."}


@server.tool()
async def fpl_auth_status() -> dict:
    """Check whether the FPL session is usable, and renew the token if needed.

    Read-only tools never need this. Only lineup changes and transfers do.
    """
    client = get_client()
    hint = "Run the `fpl_login` tool — it opens a browser window for you to sign in."

    signed_in = client.config.browser_session and browser_auth.profile_exists()
    if not client.config.bearer_token and not signed_in:
        return {"authenticated": False, "reason": "Not signed in.", "next_step": hint}

    try:
        await client.ensure_token()
    except AuthError as exc:
        return {"authenticated": False, "reason": str(exc), "next_step": hint}

    seconds_left = client.token_seconds_left()
    result: dict[str, Any] = {"saved_session": signed_in}
    if client.config.browser_channel:
        result["browser_used"] = client.config.browser_channel
    if seconds_left is not None:
        result["token_expires_in_seconds"] = seconds_left

    try:
        me = await client.me()
    except AuthError as exc:
        return {"authenticated": False, "reason": str(exc), "next_step": hint}
    player = me.get("player") or {}
    result["authenticated"] = bool(player)
    if player:
        result["logged_in_as"] = player.get("first_name")
        result["entry_id"] = player.get("entry")
        if not client.config.team_id and player.get("entry"):
            client.config.team_id = player["entry"]
            client.config.save()
            result["team_id_saved"] = player["entry"]
    else:
        result["reason"] = "FPL did not recognise the token."
        result["next_step"] = hint
    return result


@server.tool()
async def fpl_set_token(token: str) -> dict:
    """Store a bearer token by hand. Fallback for when `fpl_login` cannot run.

    `fpl_login` is the normal route. Use this only if the browser session will
    not work on this machine: copy the `X-Api-Authorization` header value from a
    logged-in browser's DevTools Network tab, minus the leading `Bearer `. A
    hand-pasted token lasts about an hour and cannot renew itself.
    """
    client = get_client()
    client.config.bearer_token = token.strip().removeprefix("Bearer ").strip()
    client.config.save()
    return await fpl_auth_status()


# ================================================================ my team
@server.tool()
async def fpl_my_squad(team_id: int | None = None, gameweek: int | None = None) -> dict:
    """Show your current squad: starting XI, bench, captain, value and bank.

    With a valid token this reflects unsaved live state including selling prices
    and free transfers. Without one it falls back to your last confirmed squad.
    """
    client = get_client()
    index = await get_index()
    entry_id = require_team_id(team_id)

    result: dict[str, Any] = {"team_id": entry_id, **season_note(index)}
    try:
        entry = await client.entry(entry_id)
        result["team_name"] = entry.get("name")
        result["overall_rank"] = entry.get("summary_overall_rank")
        result["total_points"] = entry.get("summary_overall_points")
    except FPLError:
        pass

    picks: list[dict] = []
    if gameweek is None and client.config.bearer_token:
        try:
            live = await client.my_team(entry_id)
            picks = live.get("picks", [])
            transfers = live.get("transfers", {})
            result["source"] = "live (authenticated)"
            result["bank"] = transfers.get("bank", 0) / 10
            result["squad_value_incl_bank"] = transfers.get("value", 0) / 10
            result["free_transfers"] = transfers.get("limit")
            result["transfers_made_this_gw"] = transfers.get("made")
            result["chips_available"] = [
                CHIP_LABELS.get(c.get("name"), c.get("name"))
                for c in live.get("chips", [])
                if c.get("status_for_entry") == "available"
            ]
        except AuthError as exc:
            result["auth_warning"] = str(exc)

    if not picks:
        event = gameweek or index.current_event or index.next_event
        if event is None:
            raise FPLError("No gameweek is available yet for this season.")
        try:
            data = await client.entry_picks(entry_id, event)
        except FPLError as exc:
            raise FPLError(
                "Could not read your squad for gameweek " + str(event) + ". Before the "
                "season's first deadline FPL does not publish squads, so store a token "
                "with `fpl_set_token` to see your draft team. (" + str(exc) + ")"
            ) from exc
        picks = data.get("picks", [])
        result["source"] = "confirmed squad for gameweek " + str(event)
        result["gameweek"] = event
        history = data.get("entry_history", {})
        if history:
            result["gameweek_points"] = history.get("points")
            result["bank"] = history.get("bank", 0) / 10
            result["squad_value_incl_bank"] = history.get("value", 0) / 10
            result["points_on_bench"] = history.get("points_on_bench")

    result["squad"] = describe_squad(index, picks)
    result["totals"] = squad_totals(index, [p["element"] for p in picks])
    return result


@server.tool()
async def fpl_season_summary(team_id: int | None = None) -> dict:
    """Season overview for your team: points, rank movement, chips and transfers."""
    client = get_client()
    index = await get_index()
    entry_id = require_team_id(team_id)

    entry, history = await asyncio.gather(
        client.entry(entry_id), client.entry_history(entry_id)
    )
    current = history.get("current", [])
    result: dict[str, Any] = {
        "team_id": entry_id,
        "team_name": entry.get("name"),
        "manager": (
            str(entry.get("player_first_name", "")) + " " + str(entry.get("player_last_name", ""))
        ).strip(),
        "total_points": entry.get("summary_overall_points"),
        "overall_rank": entry.get("summary_overall_rank"),
        "gameweeks_played": len(current),
        **season_note(index),
    }

    if current:
        best = max(current, key=lambda g: g.get("points", 0))
        worst = min(current, key=lambda g: g.get("points", 0))
        result["best_gameweek"] = {"gw": best["event"], "points": best["points"]}
        result["worst_gameweek"] = {"gw": worst["event"], "points": worst["points"]}
        result["average_points_per_gw"] = round(
            sum(g.get("points", 0) for g in current) / len(current), 1
        )
        result["total_points_on_bench"] = sum(g.get("points_on_bench", 0) for g in current)
        result["total_transfer_hits"] = sum(g.get("event_transfers_cost", 0) for g in current)
        result["rank_by_gameweek"] = [
            {"gw": g["event"], "points": g["points"], "overall_rank": g.get("overall_rank")}
            for g in current
        ]

    result["chips_used"] = [
        {"chip": CHIP_LABELS.get(c.get("name"), c.get("name")), "gw": c.get("event")}
        for c in history.get("chips", [])
    ]
    result["past_seasons"] = history.get("past", [])
    return result


@server.tool()
async def fpl_gameweek_report(gameweek: int | None = None, team_id: int | None = None) -> dict:
    """Break down how each of your players scored in a given gameweek.

    Defaults to the current gameweek. Includes bench points, auto-subs and hits.
    """
    client = get_client()
    index = await get_index()
    entry_id = require_team_id(team_id)
    event = gameweek or index.current_event
    if event is None:
        raise FPLError(
            "No gameweek has started yet this season, so there is nothing to report on."
        )

    picks_data, live = await asyncio.gather(client.entry_picks(entry_id, event), client.live(event))
    live_by_element = {e["id"]: e for e in live.get("elements", [])}

    rows: list[dict] = []
    for pick in sorted(picks_data.get("picks", []), key=lambda p: p.get("position", 0)):
        player = index.players.get(pick["element"])
        if player is None:
            continue
        stats = (live_by_element.get(pick["element"]) or {}).get("stats", {})
        raw_points = stats.get("total_points", 0)
        multiplier = pick.get("multiplier", 1)
        rows.append(
            {
                "name": player.name,
                "team": player.team,
                "position": player.position,
                "slot": pick.get("position"),
                "started": pick.get("position", 99) <= 11,
                "captain": bool(pick.get("is_captain")),
                "multiplier": multiplier,
                "points": raw_points * multiplier,
                "raw_points": raw_points,
                "minutes": stats.get("minutes", 0),
                "goals": stats.get("goals_scored", 0),
                "assists": stats.get("assists", 0),
                "bonus": stats.get("bonus", 0),
            }
        )

    history = picks_data.get("entry_history", {})
    subs = [
        {
            "in": index.players[s["element_in"]].name,
            "out": index.players[s["element_out"]].name,
        }
        for s in picks_data.get("automatic_subs", [])
        if s.get("element_in") in index.players and s.get("element_out") in index.players
    ]
    played = [r for r in rows if r["started"]]
    return {
        "team_id": entry_id,
        "gameweek": event,
        "points": history.get("points"),
        "transfer_cost": history.get("event_transfers_cost"),
        "points_on_bench": history.get("points_on_bench"),
        "overall_rank": history.get("overall_rank"),
        "gameweek_rank": history.get("rank"),
        "average_entry_score": (index.event(event) or {}).get("average_entry_score"),
        "captain_returned": next(
            (r["points"] for r in rows if r["captain"]), None
        ),
        "automatic_subs": subs,
        "top_scorers": sorted(played, key=lambda r: r["points"], reverse=True)[:5],
        "blanks": [r["name"] for r in played if r["raw_points"] <= 1],
        "players": rows,
    }


@server.tool()
async def fpl_best_performers(
    metric: str | None = None,
    limit: int = 8,
    team_id: int | None = None,
) -> dict:
    """Rank the players in your own squad to see who is carrying the team.

    Metrics: total_points, form, points_per_game, value, ict_index, xgi,
    xgi_per_90, ep_next, minutes, goals, assists, bonus, defensive_contribution.
    """
    client = get_client()
    index = await get_index()
    entry_id = require_team_id(team_id)
    chosen = resolve_metric(index, metric)

    element_ids = await _current_squad_ids(client, index, entry_id)
    ranked = best_performers(index, element_ids, chosen, limit=limit)
    worst = best_performers(index, element_ids, chosen, limit=len(element_ids))[-3:]
    return {
        "team_id": entry_id,
        "metric": chosen,
        **season_note(index),
        "best": ranked,
        "worst": list(reversed(worst)),
        "totals": squad_totals(index, element_ids),
    }


async def _current_squad_ids(client: FPLClient, index: Index, entry_id: int) -> list[int]:
    """Best available view of the 15 players currently in a squad."""
    if client.config.bearer_token:
        try:
            live = await client.my_team(entry_id)
            return [p["element"] for p in live.get("picks", [])]
        except AuthError:
            pass
    event = index.current_event or index.next_event
    if event is None:
        raise FPLError("No gameweek data is available for this season yet.")
    try:
        data = await client.entry_picks(entry_id, event)
    except FPLError as exc:
        raise FPLError(
            "Could not read squad " + str(entry_id) + " for gameweek " + str(event)
            + ". FPL only publishes squads once a deadline has passed; store a token "
            "with `fpl_set_token` to read your draft squad before then. (" + str(exc) + ")"
        ) from exc
    return [p["element"] for p in data.get("picks", [])]


# =============================================================== player pool
@server.tool()
async def fpl_top_players(
    metric: str | None = None,
    position: str | None = None,
    team: str | None = None,
    max_price: float | None = None,
    min_price: float | None = None,
    min_minutes: int = 0,
    available_only: bool = True,
    limit: int = 15,
) -> dict:
    """Find the best performing players in the game, with optional filters.

    Filter by position (GKP/DEF/MID/FWD), club, and price. Metrics are the same
    set as `fpl_best_performers`.
    """
    index = await get_index()
    chosen = resolve_metric(index, metric)
    pool = index.filter(
        position=position,
        team=team,
        max_price=max_price,
        min_price=min_price,
        min_minutes=min_minutes,
        available_only=available_only,
    )
    ranked = sort_players(pool, chosen)[:limit]
    return {
        "metric": chosen,
        "pool_size": len(pool),
        **season_note(index),
        "players": [
            dict(p.brief(), **{chosen: METRICS[chosen](p)}) for p in ranked
        ],
    }


@server.tool()
async def fpl_search_players(query: str, limit: int = 10) -> dict:
    """Look up players by name to get their id, price and club."""
    index = await get_index()
    return {"query": query, "matches": [p.brief() for p in index.search(query, limit=limit)]}


@server.tool()
async def fpl_player(player: str, horizon: int = 5) -> dict:
    """Full detail on one player: season stats, upcoming fixtures and recent form.

    `player` accepts a name or an element id.
    """
    client = get_client()
    index = await get_index()
    target = index.resolve(player)
    summary = await client.element_summary(target.id)

    upcoming = [
        {
            "gw": f.get("event"),
            "opponent": index.teams[f["team_a"] if f.get("is_home") else f["team_h"]][
                "short_name"
            ],
            "venue": "H" if f.get("is_home") else "A",
            "difficulty": f.get("difficulty"),
        }
        for f in summary.get("fixtures", [])[:horizon]
    ]
    recent = [
        {
            "gw": h.get("round"),
            "opponent": index.teams.get(h.get("opponent_team"), {}).get("short_name"),
            "points": h.get("total_points"),
            "minutes": h.get("minutes"),
            "goals": h.get("goals_scored"),
            "assists": h.get("assists"),
            "bonus": h.get("bonus"),
        }
        for h in summary.get("history", [])[-horizon:]
    ]
    detail = target.detail()
    set_pieces = {
        k: target.raw.get(k)
        for k in ("penalties_text", "direct_freekicks_text", "corners_and_indirect_freekicks_text")
        if target.raw.get(k)
    }
    return {
        **detail,
        "set_pieces": set_pieces,
        "upcoming_fixtures": upcoming,
        "recent_gameweeks": recent,
        "past_seasons": summary.get("history_past", [])[-3:],
        **season_note(index),
    }


@server.tool()
async def fpl_compare_players(players: list[str], metric: str | None = None) -> dict:
    """Compare two or more players side by side on the headline stats."""
    index = await get_index()
    chosen = resolve_metric(index, metric)
    resolved = [index.resolve(p) for p in players]
    rows = [dict(p.detail(), **{"sort_metric": METRICS[chosen](p)}) for p in resolved]
    rows.sort(key=lambda r: r["sort_metric"], reverse=True)
    return {"metric": chosen, "players": rows, **season_note(index)}


@server.tool()
async def fpl_fixture_ticker(
    horizon: int = 5,
    start_gameweek: int | None = None,
    teams: list[str] | None = None,
    limit: int = 20,
) -> dict:
    """Rank clubs by how easy their next few fixtures are (lower is better)."""
    client = get_client()
    index = await get_index()
    start = start_gameweek or index.next_event or index.current_event or 1
    all_fixtures = await client.fixtures()

    team_ids = None
    if teams:
        team_ids = []
        for name in teams:
            rec = index.team_by_name(name)
            if rec is None:
                raise ValueError("Unknown team " + repr(name))
            team_ids.append(rec["id"])

    rows = fixture_ticker(index, all_fixtures, start, horizon, team_ids)
    return {
        "from_gameweek": start,
        "horizon": horizon,
        "note": "difficulty is FPL's 1-5 rating; lower means an easier fixture",
        "teams": rows[:limit],
    }


# ============================================================== differentials
@server.tool()
async def fpl_differentials(
    scope: str = "global",
    league_id: int | None = None,
    rivals: int = 30,
    metric: str | None = None,
    low_ownership_max: float = 10.0,
    high_ownership_min: float = 20.0,
    limit: int = 10,
    team_id: int | None = None,
) -> dict:
    """Find your differentials in both directions.

    `scope="global"` compares against ownership across all FPL managers.
    `scope="league"` compares against the top `rivals` teams in one mini-league,
    which is what actually decides your rank in that league — it also reports
    effective ownership, counting captaincy.

    Returns `my_differentials` (high performers few rivals own — where you gain)
    and `differentials_against_me` (high performers you are missing — where you
    bleed rank, so it doubles as a transfer shortlist).
    """
    client = get_client()
    index = await get_index()
    entry_id = require_team_id(team_id)
    chosen = resolve_metric(index, metric)
    my_ids = set(await _current_squad_ids(client, index, entry_id))

    result: dict[str, Any] = {
        "team_id": entry_id,
        "scope": scope,
        "metric": chosen,
        "thresholds": {
            "mine_owned_by_at_most_pct": low_ownership_max,
            "theirs_owned_by_at_least_pct": high_ownership_min,
        },
        **season_note(index),
    }

    if scope == "global":
        ownership = {p.id: p.selected_by for p in index.players.values()}
        result["comparison_set"] = "all " + str(index.total_players) + " FPL managers"
    elif scope == "league":
        target_league = league_id or client.config.default_league_id
        if not target_league:
            raise ValueError(
                "scope='league' needs a league_id. Find it in the URL of your "
                "mini-league page, or save a default with `fpl_setup`."
            )
        rival_squads, league_name = await _fetch_league_squads(
            client, index, int(target_league), rivals, exclude_entry=entry_id
        )
        stats = league_ownership(index, rival_squads, exclude_entry=entry_id)
        if not stats["sample_size"]:
            raise FPLError(
                "Could not read any rival squads in league " + str(target_league)
                + ". FPL only publishes squads after the first deadline has passed."
            )
        ownership = stats["owned_by"]
        sample = stats["sample_size"]
        result["comparison_set"] = str(sample) + " rivals in " + str(league_name)
        result["league_id"] = int(target_league)
        if sample < 20:
            result["sample_caveat"] = (
                "Only " + str(sample) + " rivals, so ownership moves in steps of "
                + str(round(100 / sample, 1)) + "%. A single rival owning a player "
                "already puts them at that figure — read these as rough signals and "
                "raise `rivals` if the league is bigger."
            )
        result["effective_ownership_pct"] = {
            index.player(eid).name: pct
            for eid, pct in sorted(
                stats["effective"].items(), key=lambda kv: kv[1], reverse=True
            )[:15]
            if eid in index.players
        }
    else:
        raise ValueError("scope must be either 'global' or 'league'")

    result.update(
        differentials(
            index,
            my_ids,
            ownership,
            chosen,
            low_ownership_max=low_ownership_max,
            high_ownership_min=high_ownership_min,
            limit=limit,
        )
    )
    return result


async def _fetch_league_squads(
    client: FPLClient, index: Index, league_id: int, rivals: int, exclude_entry: int
) -> tuple[list[dict], str]:
    """Pull the current picks for the top `rivals` teams in a classic league."""
    event = index.current_event or index.next_event
    if event is None:
        raise FPLError("No gameweek data is available for this season yet.")

    entries: list[dict] = []
    page = 1
    league_name = "league " + str(league_id)
    while len(entries) < rivals + 1 and page <= 5:
        data = await client.league_standings(league_id, page=page)
        league_name = (data.get("league") or {}).get("name", league_name)
        results = (data.get("standings") or {}).get("results", [])
        if not results:
            break
        entries.extend(results)
        if not (data.get("standings") or {}).get("has_next"):
            break
        page += 1

    wanted = [e for e in entries if e.get("entry") != exclude_entry][:rivals]
    semaphore = asyncio.Semaphore(6)

    async def fetch(row: dict) -> dict | None:
        async with semaphore:
            try:
                picks = await client.entry_picks(row["entry"], event)
            except FPLError:
                return None
        return {
            "entry": row["entry"],
            "entry_name": row.get("entry_name"),
            "picks": picks.get("picks", []),
        }

    fetched = await asyncio.gather(*(fetch(row) for row in wanted))
    return [f for f in fetched if f], league_name


@server.tool()
async def fpl_league_standings(
    league_id: int | None = None, limit: int = 25, team_id: int | None = None
) -> dict:
    """Show a classic mini-league table and where you sit in it."""
    client = get_client()
    target = league_id or client.config.default_league_id
    if not target:
        raise ValueError(
            "No league_id given. Find it in your mini-league URL, or save a "
            "default with `fpl_setup`."
        )
    data = await client.league_standings(int(target))
    results = (data.get("standings") or {}).get("results", [])
    me = get_client().config.team_id if team_id is None else team_id

    result: dict[str, Any] = {}
    if not results:
        result["note"] = (
            "This league has no standings yet — FPL only builds tables once the "
            "first gameweek has been scored."
        )
    elif me and not any(r.get("entry") == me for r in results):
        result["note"] = (
            "Your team is not on the first page of this league, so your_position "
            "is unknown; raise `limit` or check the league id."
        )

    return {
        **result,
        "league_id": int(target),
        "league_name": (data.get("league") or {}).get("name"),
        "your_position": next(
            (r["rank"] for r in results if r.get("entry") == me), None
        ),
        "standings": [
            {
                "rank": r.get("rank"),
                "team": r.get("entry_name"),
                "manager": r.get("player_name"),
                "gw_points": r.get("event_total"),
                "total": r.get("total"),
                "is_you": r.get("entry") == me,
            }
            for r in results[:limit]
        ],
    }


# ============================================================ bulk export
@server.tool()
async def fpl_export_players(
    fields: str | None = None,
    format: str = "csv",
    position: str | None = None,
    team: str | None = None,
    max_price: float | None = None,
    min_price: float | None = None,
    min_minutes: int = 0,
    available_only: bool = False,
) -> dict:
    """Dump the whole player pool for your own modelling — every player, many columns.

    Unlike `fpl_top_players` this does not rank or truncate: it returns the full
    pool so you can run your own optimiser (e.g. maximise projected points
    subject to budget, the 2/5/5/3 split and the three-per-club cap).

    `fields` defaults to a broad optimiser-oriented set: price, every scoring
    stat, the expected-goals columns, availability, and set-piece duty order.
    Pass "all" for every raw column, or a comma-separated list to narrow it.
    `format` is "csv" (default, far more compact) or "json".
    """
    index = await get_index()
    chosen = resolve_fields(index, fields)
    pool = index.filter(
        position=position, team=team, max_price=max_price, min_price=min_price,
        min_minutes=min_minutes, available_only=available_only,
    )
    pool.sort(key=lambda p: p.id)
    rows = player_rows(index, pool, chosen)

    result: dict[str, Any] = {
        "player_count": len(rows),
        "field_count": len(chosen),
        "fields": chosen,
        **season_note(index),
        "notes": [
            "price is in millions; FPL's raw now_cost is tenths.",
            "element_type: 1=GKP 2=DEF 3=MID 4=FWD. Squad rules: 2/5/5/3, "
            "max 3 per team_id, 100.0m budget.",
            "penalties_order / direct_freekicks_order / "
            "corners_and_indirect_freekicks_order: 1 means first choice, "
            "empty means no listed duty. The matching *_text fields are empty "
            "in this season's feed, so order is the only set-piece signal.",
        ],
    }
    if format.lower() == "json":
        result["players"] = rows
    else:
        result["format"] = "csv"
        result["csv"] = to_csv(rows, chosen)

    payload = result.get("csv") or str(result.get("players", ""))
    result["approx_tokens"] = len(payload) // 4
    if result["approx_tokens"] > 50_000:
        result["size_warning"] = (
            "This payload is roughly " + str(result["approx_tokens"] // 1000)
            + "k tokens. Narrow it with `fields`, `position` or `max_price` "
            "unless you genuinely need every column."
        )
    return result


@server.tool()
async def fpl_export_fixtures(horizon: int = 8, start_gameweek: int | None = None) -> dict:
    """Every fixture over a horizon, one row per team per match, as CSV.

    Join to the player export on `team_id` to build per-player, per-gameweek
    projections. Blanks and doubles fall out of the shape: a team with no row
    for a gameweek blanks it, two rows means a double gameweek.
    """
    client = get_client()
    index = await get_index()
    start = start_gameweek or index.next_event or index.current_event or 1
    rows = fixtures_by_gameweek(index, await client.fixtures(), start, horizon)

    counts: dict[int, int] = {}
    for row in rows:
        counts[row["team_id"]] = counts.get(row["team_id"], 0) + 1
    window = range(start, start + horizon)
    blanks = [index.teams[t]["short_name"] for t in index.teams
              if counts.get(t, 0) < len(list(window))]
    doubles = sorted({
        (r["gameweek"], r["team"]) for r in rows
        if sum(1 for x in rows if x["gameweek"] == r["gameweek"]
               and x["team_id"] == r["team_id"]) > 1
    })
    fields = list(rows[0].keys()) if rows else []
    return {
        "from_gameweek": start,
        "horizon": horizon,
        "row_count": len(rows),
        "teams_with_a_blank_in_window": blanks,
        "double_gameweeks": [{"gameweek": gw, "team": t} for gw, t in doubles],
        "notes": [
            "difficulty is FPL's 1-5 rating for this fixture; lower is easier.",
            "opponent_strength_overall is FPL's 2-5 rating for the opponent, "
            "picked for the side they are playing (their away rating when you "
            "are at home, and vice versa).",
            "FPL's strength_attack_* and strength_defence_* splits are all zero "
            "in this season's feed, so they are not exported. The team-level "
            "`strength` field is null for every club too.",
        ],
        "format": "csv",
        "csv": to_csv(rows, fields),
    }


@server.tool()
async def fpl_export_gameweek_history(
    start_gameweek: int = 1,
    end_gameweek: int | None = None,
    player_ids: list[int] | None = None,
) -> dict:
    """Per-player, per-gameweek stats across a range of finished gameweeks, as CSV.

    One row per player per gameweek: minutes, goals, assists, bonus, bps, the
    expected-goals columns and points. This is the series to fit form decay,
    home/away splits and minutes risk against, and to backtest on.

    Only finished gameweeks have data. Before the season's first deadline this
    returns nothing at all, because FPL publishes no per-gameweek data until
    matches have been played.
    """
    client = get_client()
    index = await get_index()

    finished = [e["id"] for e in index.events if e.get("finished")]
    if not finished:
        return {
            "row_count": 0,
            "gameweeks_returned": [],
            **season_note(index),
            "reason": (
                "No gameweek has finished yet this season, so FPL has no "
                "per-gameweek data to serve. Season totals currently carry over "
                "from last season, but per-gameweek history does not — the API "
                "exposes previous seasons only as aggregates, never split by "
                "gameweek. Backtesting against this source is not possible until "
                "gameweeks have been played."
            ),
        }

    last = end_gameweek or max(finished)
    wanted = [gw for gw in finished if start_gameweek <= gw <= last]
    wanted = wanted[:38]
    ids = set(player_ids) if player_ids else None

    semaphore = asyncio.Semaphore(4)

    async def fetch(gw: int) -> list[dict]:
        async with semaphore:
            return live_rows(index, gw, await client.live(gw), ids)

    batches = await asyncio.gather(*(fetch(gw) for gw in wanted))
    rows = [row for batch in batches for row in batch]
    rows.sort(key=lambda r: (r["id"], r["gameweek"]))

    return {
        "row_count": len(rows),
        "gameweeks_returned": wanted,
        "players_covered": len({r["id"] for r in rows}),
        **season_note(index),
        "note": "fixtures_played is 2 in a double gameweek; the stat columns "
                "are that gameweek's total across both matches.",
        "format": "csv",
        "csv": to_csv(rows, LIVE_ROW_FIELDS),
    }


# ========================================================= team news & XI
@server.tool()
async def fpl_team_news(
    team: str | None = None,
    signed_within_days: int = 30,
    include_headlines: bool = True,
    limit_headlines: int = 4,
) -> dict:
    """Club-level news: manager, new signings, departures and injury flags.

    Covers what changes a club's shape rather than one player's form. Managers
    come from the Premier League's own backend; signings, departures and
    injuries come from FPL's feed. Pass `team` for one club, or omit for all.

    Managers are not an FPL asset this season, so a managerial change matters
    indirectly — rotation, set-piece duty, formation — not as points.
    """
    import datetime as _dt

    index = await get_index()
    pulse = get_pulse()

    if team:
        rec = index.team_by_name(team)
        if rec is None:
            raise ValueError("Unknown team " + repr(team))
        teams = [rec]
    else:
        teams = sorted(index.teams.values(), key=lambda t: t["name"])

    cutoff = _dt.date.today() - _dt.timedelta(days=signed_within_days)
    semaphore = asyncio.Semaphore(5)

    async def manager_of(rec: dict):
        pulse_id = rec.get("pulse_id")
        if not pulse_id:
            return None
        async with semaphore:
            return await pulse.manager(int(pulse_id))

    managers = await asyncio.gather(*(manager_of(t) for t in teams))

    out: list[dict] = []
    for rec, manager in zip(teams, managers):
        squad = [p for p in index.players.values() if p.team_id == rec["id"]]

        signings = []
        for p in squad:
            joined = p.raw.get("team_join_date")
            if not joined:
                continue
            try:
                when = _dt.date.fromisoformat(str(joined)[:10])
            except ValueError:
                continue
            if when >= cutoff:
                signings.append({"name": p.name, "position": p.position,
                                 "price": p.price, "joined": str(when)})
        signings.sort(key=lambda r: r["joined"], reverse=True)

        departures, injuries = [], []
        for p in squad:
            if not p.news:
                continue
            lowered = p.news.lower()
            if "joined" in lowered or "loan" in lowered:
                departures.append({"name": p.name, "note": p.news})
            else:
                row = {"name": p.name, "position": p.position, "note": p.news,
                       "status": p.raw.get("status")}
                if p.chance_next is not None:
                    row["chance_of_playing_next_gw"] = p.chance_next
                injuries.append(row)

        out.append({
            "team": rec["name"],
            "short_name": rec["short_name"],
            "manager": manager or "unknown (Premier League backend did not answer)",
            "new_signings": signings,
            "departures_and_loans": departures,
            "injuries_and_doubts": injuries,
        })

    result: dict[str, Any] = {
        "teams": out,
        **season_note(index),
        "sources": {
            "manager": "Premier League Pulse backend (undocumented; when it does "
                       "not answer, manager reads 'unknown')",
            "signings": "FPL team_join_date within the last "
                        + str(signed_within_days) + " days",
            "departures_and_injuries": "FPL news text and availability flags",
        },
    }
    if include_headlines:
        result["headlines"] = await _fetch_headlines(limit_headlines)
        result["sources"]["headlines"] = (
            "Third-party RSS (BBC Sport, The Guardian). League-wide coverage, "
            "not filtered per club - context, not team news."
        )
    return result


async def _fetch_headlines(limit: int) -> list[dict]:
    """Best-effort league headlines from public RSS feeds."""
    import xml.etree.ElementTree as ET

    import httpx

    feeds = {
        "BBC Sport": "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml",
        "The Guardian": "https://www.theguardian.com/football/premierleague/rss",
    }
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=15,
                                 headers={"User-Agent": "fpl-mcp/0.5"}) as http:
        for source, url in feeds.items():
            taken = 0
            try:
                resp = await http.get(url)
                root = ET.fromstring(resp.text)
                for item in root.iter("item"):
                    title = " ".join((item.findtext("title") or "").split())
                    if not title:
                        continue
                    out.append({
                        "source": source,
                        "title": title,
                        "link": (item.findtext("link") or "").strip(),
                        "published": (item.findtext("pubDate") or "").strip(),
                    })
                    taken += 1
                    if taken >= limit:
                        break
            except Exception:
                continue  # a dead feed must not take the whole tool down
    return out


@server.tool()
async def fpl_predicted_lineups(
    team: str | None = None,
    gameweek: int | None = None,
    recent_gameweeks: int = 5,
) -> dict:
    """Predict each club's starting XI, or return the confirmed one if published.

    No API anywhere publishes predicted lineups; they are editorial judgement.
    This is a **derived heuristic** ranked on how often each player actually
    started recent gameweeks, minutes per appearance, availability, set-piece
    duty and price. Every pick shows its score breakdown, so you can disagree
    with the weighting.

    When the Premier League publishes real team lists - roughly an hour before
    kickoff - those are returned instead, marked source="confirmed".
    """
    client = get_client()
    index = await get_index()
    pulse = get_pulse()
    event = gameweek or index.next_event or index.current_event
    if event is None:
        raise FPLError("No gameweek is available for this season yet.")

    if team:
        rec = index.team_by_name(team)
        if rec is None:
            raise ValueError("Unknown team " + repr(team))
        teams = [rec]
    else:
        teams = sorted(index.teams.values(), key=lambda t: t["name"])

    confirmed: dict[int, dict] = {}
    by_opta = {str(p.raw.get("opta_code")): p for p in index.players.values()}
    for fixture in await pulse.fixtures(gameweek=event):
        lists = await pulse.team_lists(int(fixture["id"]))
        if not lists:
            continue
        for tl in lists:
            starters, subs = lineup_players(tl)
            confirmed[int(tl.get("teamId", -1))] = {
                "formation": (tl.get("formation") or {}).get("label"),
                "starters": starters,
                "substitutes": subs,
            }

    finished = [e["id"] for e in index.events if e.get("finished")]
    recent = finished[-recent_gameweeks:] if finished else []
    live_by_gw: dict[int, dict[int, dict]] = {}
    for gw in recent:
        payload = await client.live(gw)
        live_by_gw[gw] = {
            el["id"]: (el.get("stats") or {}) for el in (payload.get("elements") or [])
        }
    rates, mins = start_rates(live_by_gw, index.players)

    results = []
    for rec in teams:
        squad = [p for p in index.players.values() if p.team_id == rec["id"]]
        entry: dict[str, Any] = {"team": rec["name"], "short_name": rec["short_name"]}

        real = confirmed.get(int(rec.get("pulse_id") or -1))
        if real:
            mapped = []
            for row in real["starters"]:
                fpl = by_opta.get(str(row.get("opta_code")))
                mapped.append({
                    "name": row["name"],
                    "match_position": row["match_position"],
                    "captain": row["captain"],
                    "fpl_id": fpl.id if fpl else None,
                    "fpl_name": fpl.name if fpl else None,
                })
            entry["source"] = "confirmed"
            entry["formation"] = real["formation"]
            entry["starting_xi"] = mapped
            entry["substitutes"] = [r["name"] for r in real["substitutes"]]
        else:
            entry["source"] = "predicted"
            entry.update(predict_xi(index, squad, rates, mins))
        results.append(entry)

    notes = [
        "source=confirmed means the Premier League published that team list. "
        "source=predicted means it was derived here: a heuristic, not an "
        "expert prediction.",
        "likelihood_score is relative within a club only. It is not a "
        "probability and is not comparable across clubs.",
    ]
    if not recent:
        notes.append(
            "No gameweeks have finished, so the strongest signals - who actually "
            "started recently, and minutes per appearance - do not exist yet. "
            "These predictions rest on last season's minutes and cannot see new "
            "signings, new managers or pre-season form. Treat gameweek 1 "
            "predictions as weak."
        )
    return {
        "gameweek": event,
        "confirmed_teams": len(confirmed),
        "start_history_gameweeks": recent,
        "teams": results,
        "notes": notes,
        **season_note(index),
    }


# =================================================================== writes
@server.tool()
async def fpl_set_captain(
    captain: str,
    vice_captain: str | None = None,
    dry_run: bool = True,
    team_id: int | None = None,
) -> dict:
    """Change the captain (and optionally vice-captain), keeping the XI as-is.

    Runs as a preview by default. Show the preview to the user and get their
    agreement before calling again with dry_run=false.
    """
    client = get_client()
    index = await get_index()
    entry_id = require_team_id(team_id)

    live = await client.my_team(entry_id)
    picks = [dict(p) for p in live.get("picks", [])]
    new_captain = index.resolve(captain)
    new_vice = index.resolve(vice_captain) if vice_captain else None

    owned = {p["element"] for p in picks}
    if new_captain.id not in owned:
        raise ValueError(new_captain.name + " is not in your squad.")
    if new_vice and new_vice.id not in owned:
        raise ValueError(new_vice.name + " is not in your squad.")

    previous = {
        "captain": next(
            (index.player(p["element"]).name for p in picks if p.get("is_captain")), None
        ),
        "vice_captain": next(
            (index.player(p["element"]).name for p in picks if p.get("is_vice_captain")), None
        ),
    }
    existing_vice = next((p["element"] for p in picks if p.get("is_vice_captain")), None)
    vice_id = new_vice.id if new_vice else existing_vice
    if vice_id == new_captain.id:
        # The outgoing captain inherits the armband rather than leaving it unset.
        vice_id = next((p["element"] for p in picks if p.get("is_captain")), None)

    for pick in picks:
        pick["is_captain"] = pick["element"] == new_captain.id
        pick["is_vice_captain"] = pick["element"] == vice_id

    return await _submit_lineup(
        client,
        index,
        entry_id,
        picks,
        dry_run,
        extra={
            "change": "captaincy",
            "before": previous,
            "after": {
                "captain": new_captain.name,
                "vice_captain": index.player(vice_id).name if vice_id else None,
            },
        },
    )


@server.tool()
async def fpl_set_lineup(
    starting_xi: list[str],
    bench_order: list[str],
    captain: str,
    vice_captain: str,
    dry_run: bool = True,
    team_id: int | None = None,
) -> dict:
    """Set your full starting XI, bench order, captain and vice-captain.

    `starting_xi` is 11 players and `bench_order` is the remaining 4 with the
    substitute goalkeeper first, then outfielders in the order they should come
    on. Names or element ids both work.

    Runs as a preview by default. Show the preview to the user and get their
    agreement before calling again with dry_run=false.
    """
    client = get_client()
    index = await get_index()
    entry_id = require_team_id(team_id)

    live = await client.my_team(entry_id)
    owned = {p["element"] for p in live.get("picks", [])}

    xi = [index.resolve(p) for p in starting_xi]
    bench = [index.resolve(p) for p in bench_order]
    cap, vice = index.resolve(captain), index.resolve(vice_captain)

    ordered = xi + bench
    if len(ordered) != len(owned):
        raise ValueError(
            "Expected " + str(len(owned)) + " players in total, got "
            + str(len(ordered)) + " (" + str(len(xi)) + " starters + "
            + str(len(bench)) + " bench)."
        )
    missing = owned - {p.id for p in ordered}
    if missing:
        names = ", ".join(index.player(m).name for m in missing)
        raise ValueError("These squad players were not placed: " + names)

    picks = [
        {
            "element": player.id,
            "position": slot,
            "is_captain": player.id == cap.id,
            "is_vice_captain": player.id == vice.id,
        }
        for slot, player in enumerate(ordered, start=1)
    ]
    return await _submit_lineup(
        client, index, entry_id, picks, dry_run, extra={"change": "lineup"}
    )


async def _submit_lineup(
    client: FPLClient,
    index: Index,
    entry_id: int,
    picks: list[dict],
    dry_run: bool,
    extra: dict | None = None,
) -> dict:
    payload = [
        {
            "element": p["element"],
            "position": p["position"],
            "is_captain": bool(p.get("is_captain")),
            "is_vice_captain": bool(p.get("is_vice_captain")),
        }
        for p in picks
    ]
    problems = validate_lineup(index, payload)
    result: dict[str, Any] = {
        "team_id": entry_id,
        "dry_run": dry_run,
        "valid": not problems,
        "problems": problems,
        "squad": describe_squad(index, payload),
        **(extra or {}),
    }
    if problems:
        result["submitted"] = False
        result["message"] = "Not submitted — the lineup breaks FPL's rules."
        return result
    if dry_run:
        result["submitted"] = False
        result["message"] = (
            "Preview only. Confirm with the user, then call again with dry_run=false."
        )
        return result

    await client.post_lineup(entry_id, payload)
    result["submitted"] = True
    result["message"] = "Lineup saved to your FPL team."
    return result


@server.tool()
async def fpl_make_transfers(
    players_out: list[str],
    players_in: list[str],
    chip: str | None = None,
    dry_run: bool = True,
    team_id: int | None = None,
) -> dict:
    """Transfer players out of and into your squad.

    Pass matching lists of names or element ids. `chip` may be "wildcard" or
    "freehit" to play that chip with these transfers.

    Runs as a preview by default, reporting the cost in points, the resulting
    bank balance and any rule breaches. Show that preview to the user and get
    their agreement before calling again with dry_run=false.
    """
    client = get_client()
    index = await get_index()
    entry_id = require_team_id(team_id)

    if len(players_out) != len(players_in):
        raise ValueError(
            "players_out and players_in must be the same length (got "
            + str(len(players_out)) + " and " + str(len(players_in)) + ")."
        )
    if not players_out:
        raise ValueError("No transfers given.")
    if chip is not None and chip not in ("wildcard", "freehit"):
        raise ValueError("chip must be 'wildcard', 'freehit', or omitted.")

    event = index.next_event or index.current_event
    if event is None:
        raise FPLError("There is no upcoming gameweek to transfer into.")

    live = await client.my_team(entry_id)
    squad = {p["element"]: p for p in live.get("picks", [])}
    transfers_meta = live.get("transfers", {})
    bank = transfers_meta.get("bank", 0)

    outs = [index.resolve(p) for p in players_out]
    ins = [index.resolve(p) for p in players_in]

    transfers: list[dict] = []
    lines: list[dict] = []
    spend = 0
    for out_player, in_player in zip(outs, ins):
        if out_player.id not in squad:
            raise ValueError(out_player.name + " is not in your squad.")
        if in_player.id in squad:
            raise ValueError(in_player.name + " is already in your squad.")
        if out_player.position_id != in_player.position_id:
            raise ValueError(
                "Cannot swap " + out_player.name + " (" + out_player.position + ") for "
                + in_player.name + " (" + in_player.position + ") — FPL transfers must "
                "be like-for-like by position."
            )
        selling_price = squad[out_player.id].get("selling_price", int(out_player.price * 10))
        buy_price = in_player.raw["now_cost"]
        spend += buy_price - selling_price
        transfers.append(
            {
                "element_out": out_player.id,
                "element_in": in_player.id,
                "purchase_price": buy_price,
                "selling_price": selling_price,
            }
        )
        lines.append(
            {
                "out": out_player.name + " (" + out_player.team + ")",
                "sell_for": selling_price / 10,
                "in": in_player.name + " (" + in_player.team + ")",
                "buy_for": buy_price / 10,
                "net_cost": round((buy_price - selling_price) / 10, 1),
                "in_status": in_player.raw.get("status"),
                "in_news": in_player.news or None,
            }
        )

    new_squad = [eid for eid in squad if eid not in {p.id for p in outs}] + [p.id for p in ins]
    problems = validate_squad(index, new_squad)
    remaining_bank = bank - spend
    if remaining_bank < 0:
        problems.append(
            "Not enough money: this costs " + str(round(spend / 10, 1))
            + "m but you only have " + str(round(bank / 10, 1)) + "m in the bank."
        )

    free_transfers = transfers_meta.get("limit") or 0
    if chip in ("wildcard", "freehit"):
        hit = 0
    else:
        extra_transfers = max(0, len(transfers) - free_transfers)
        hit = extra_transfers * 4

    result: dict[str, Any] = {
        "team_id": entry_id,
        "gameweek": event,
        "dry_run": dry_run,
        "chip": chip,
        "transfers": lines,
        "free_transfers_available": free_transfers,
        "points_hit": hit,
        "bank_before": round(bank / 10, 1),
        "bank_after": round(remaining_bank / 10, 1),
        "valid": not problems,
        "problems": problems,
    }
    if problems:
        result["submitted"] = False
        result["message"] = "Not submitted — these transfers break FPL's rules."
        return result
    if dry_run:
        result["submitted"] = False
        result["message"] = (
            "Preview only. Confirm with the user"
            + (" (this costs " + str(hit) + " points)" if hit else "")
            + ", then call again with dry_run=false."
        )
        return result

    await client.post_transfers(entry_id, event, transfers, chip=chip)
    result["submitted"] = True
    result["message"] = "Transfers confirmed with FPL."
    return result


def main() -> None:
    """Entry point. Defaults to stdio; `--http` serves streamable HTTP instead.

    stdio is for local surfaces (Claude Code, Cowork). HTTP is for reaching
    claude.ai as a custom connector, which means the endpoint is exposed beyond
    this machine — so that mode refuses to start without a shared secret.
    """
    import os
    import sys

    # httpx logs every request at INFO; on stdio that is just noise in the client log.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if "--http" not in sys.argv:
        server.run(transport="stdio")
        return

    secret = (os.environ.get("FPL_MCP_SHARED_SECRET") or "").strip()
    if len(secret) < 16:
        raise SystemExit(
            "Refusing to start in HTTP mode without FPL_MCP_SHARED_SECRET set to at "
            "least 16 characters.\n"
            "These tools can transfer players and change your lineup, so an open "
            "endpoint would let anyone who finds the URL edit your team.\n"
            "Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    import uvicorn

    host = os.environ.get("FPL_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("FPL_MCP_PORT", "8000"))

    # The secret is enforced at the ASGI layer, in front of the MCP transport.
    app = server.streamable_http_app(host=host)
    app.add_middleware(_require_secret_middleware(secret))
    uvicorn.run(app, host=host, port=port, log_level="info")


def _require_secret_middleware(secret: str):
    """Build ASGI middleware that rejects requests without the shared secret."""
    import hmac

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class RequireSecret(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            supplied = request.headers.get("authorization", "")
            if supplied.lower().startswith("bearer "):
                supplied = supplied[7:]
            # Constant-time compare so the secret cannot be probed by timing.
            if not hmac.compare_digest(supplied.strip(), secret):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    return RequireSecret


if __name__ == "__main__":
    main()

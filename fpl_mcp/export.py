"""Bulk data export, for callers that want to model rather than be told.

Everything here returns whole datasets rather than a ranked top-N, so a client
can run its own optimiser. CSV is the default because a full player dump as
JSON costs roughly five times the tokens for the same information.
"""
from __future__ import annotations

import csv
import io
from typing import Any, Iterable

from .index import Index

# A compact default: identity, price, headline scoring, availability. The full
# optimiser set below is opt-in via fields="standard" because returning all 62
# columns for ~700 players overflows many clients' tool-result limits.
COMPACT_FIELDS: tuple[str, ...] = (
    "id", "web_name", "team", "team_id", "position", "element_type",
    "price", "total_points", "points_per_game", "form", "event_points",
    "ep_next", "minutes", "goals_scored", "assists", "clean_sheets", "bonus",
    "expected_goal_involvements", "selected_by_percent",
    "status", "chance_of_playing_next_round", "news",
)

# A broad, optimiser-oriented default: identity, price, every scoring stat,
# the underlying-performance columns, availability, and set-piece duty.
STANDARD_FIELDS: tuple[str, ...] = (
    "id", "web_name", "first_name", "second_name",
    "team", "team_id", "position", "element_type",
    "price", "cost_change_start", "cost_change_event",
    "total_points", "points_per_game", "form", "event_points",
    "ep_this", "ep_next",
    "minutes", "starts", "starts_per_90",
    "goals_scored", "assists", "clean_sheets", "goals_conceded",
    "own_goals", "penalties_saved", "penalties_missed",
    "yellow_cards", "red_cards", "saves", "saves_per_90",
    "bonus", "bps", "influence", "creativity", "threat", "ict_index",
    "expected_goals", "expected_assists", "expected_goal_involvements",
    "expected_goals_conceded",
    "expected_goals_per_90", "expected_assists_per_90",
    "expected_goal_involvements_per_90", "expected_goals_conceded_per_90",
    "clean_sheets_per_90",
    "defensive_contribution", "defensive_contribution_per_90",
    "tackles", "recoveries", "clearances_blocks_interceptions",
    "selected_by_percent", "transfers_in_event", "transfers_out_event",
    "status", "chance_of_playing_this_round", "chance_of_playing_next_round",
    "news", "news_added",
    "penalties_order", "direct_freekicks_order",
    "corners_and_indirect_freekicks_order",
)

# Derived columns that are not in the raw payload.
DERIVED = {
    "team": lambda p, idx: p.team,
    "position": lambda p, idx: p.position,
    "price": lambda p, idx: p.price,
    "team_id": lambda p, idx: p.team_id,
}


def player_rows(
    index: Index,
    players: Iterable,
    fields: tuple[str, ...] | list[str] = COMPACT_FIELDS,
) -> list[dict]:
    rows = []
    for p in players:
        row: dict[str, Any] = {}
        for field in fields:
            if field in DERIVED:
                row[field] = DERIVED[field](p, index)
            elif field == "cost_change_start":
                row[field] = p.raw.get("cost_change_start", 0) / 10
            elif field == "cost_change_event":
                row[field] = p.raw.get("cost_change_event", 0) / 10
            else:
                row[field] = p.raw.get(field)
        rows.append(row)
    return rows


def resolve_fields(index: Index, requested) -> list[str]:
    """`None` -> compact set, "standard" -> broad set, "all" -> every raw column."""
    if requested is None:
        return list(COMPACT_FIELDS)
    if isinstance(requested, str):
        lowered = requested.strip().lower()
        if lowered == "all":
            sample = next(iter(index.players.values()))
            return ["team", "position", "price"] + sorted(sample.raw.keys())
        if lowered in ("standard", "full", "optimiser", "optimizer"):
            return list(STANDARD_FIELDS)
        if lowered == "compact":
            return list(COMPACT_FIELDS)
        requested = [f.strip() for f in requested.split(",") if f.strip()]
    known = set(next(iter(index.players.values())).raw.keys()) | set(DERIVED)
    unknown = [f for f in requested if f not in known]
    if unknown:
        raise ValueError(
            "Unknown field(s): " + ", ".join(unknown)
            + ". Pass fields='all' to see everything available."
        )
    return list(requested)


def to_csv(rows: list[dict], fields: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue()


def fixtures_by_gameweek(
    index: Index, fixtures: list[dict], start: int, horizon: int
) -> list[dict]:
    """One row per team per fixture — the join key for per-player projections.

    Blanks and doubles fall out of this naturally: a team with no row for a
    gameweek blanks; two rows means a double.
    """
    window = set(range(start, start + horizon))
    rows: list[dict] = []
    for fx in fixtures:
        event = fx.get("event")
        if event not in window:
            continue
        home, away = fx.get("team_h"), fx.get("team_a")
        if home is None or away is None:
            continue
        for team_id, opp_id, venue, diff in (
            (home, away, "H", fx.get("team_h_difficulty")),
            (away, home, "A", fx.get("team_a_difficulty")),
        ):
            rows.append({
                "gameweek": event,
                "team_id": team_id,
                "team": index.teams[team_id]["short_name"],
                "opponent_id": opp_id,
                "opponent": index.teams[opp_id]["short_name"],
                "venue": venue,
                "difficulty": diff,
                "kickoff_time": fx.get("kickoff_time"),
                "finished": fx.get("finished"),
                # Side-adjusted overall rating for the opponent. FPL's
                # strength_attack_* / strength_defence_* splits are all zero in
                # this season's feed, so they are deliberately not exported.
                "opponent_strength_overall": index.teams[opp_id].get(
                    "strength_overall_away" if venue == "H" else "strength_overall_home"),
            })
    rows.sort(key=lambda r: (r["gameweek"], r["team"]))
    return rows


LIVE_STAT_FIELDS = (
    "minutes", "goals_scored", "assists", "clean_sheets", "goals_conceded",
    "own_goals", "penalties_saved", "penalties_missed", "yellow_cards",
    "red_cards", "saves", "bonus", "bps", "influence", "creativity", "threat",
    "ict_index", "starts", "expected_goals", "expected_assists",
    "expected_goal_involvements", "expected_goals_conceded",
    "defensive_contribution", "total_points",
)


def live_rows(index: Index, event: int, payload: dict,
              element_ids: set[int] | None = None) -> list[dict]:
    """Flatten one gameweek's live feed into per-player rows."""
    rows = []
    for element in payload.get("elements", []):
        eid = element.get("id")
        if element_ids is not None and eid not in element_ids:
            continue
        player = index.players.get(eid)
        if player is None:
            continue
        stats = element.get("stats", {}) or {}
        row = {
            "gameweek": event,
            "id": eid,
            "web_name": player.name,
            "team": player.team,
            "position": player.position,
            "price": player.price,
        }
        for field in LIVE_STAT_FIELDS:
            row[field] = stats.get(field)
        # A player can have two fixtures in one gameweek; record how many.
        row["fixtures_played"] = len(element.get("explain", []) or [])
        rows.append(row)
    return rows


LIVE_ROW_FIELDS = (
    ["gameweek", "id", "web_name", "team", "position", "price"]
    + list(LIVE_STAT_FIELDS) + ["fixtures_played"]
)

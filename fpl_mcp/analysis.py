"""Squad analytics: differentials, fixture ticker, and squad-legality checks."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .index import Index, Player, sort_players

# FPL's own labels for the chip codes returned by the API.
CHIP_LABELS = {
    "3xc": "Triple Captain",
    "bboost": "Bench Boost",
    "freehit": "Free Hit",
    "wildcard": "Wildcard",
    "manager": "Assistant Manager",
}


# --------------------------------------------------------------------- squads
def describe_squad(index: Index, picks: list[dict]) -> dict:
    """Turn a raw picks array into a readable starting XI + bench."""
    starters: list[dict] = []
    bench: list[dict] = []
    captain = vice = None

    for pick in sorted(picks, key=lambda p: p.get("position", 0)):
        try:
            player = index.player(pick["element"])
        except KeyError:
            continue
        row = player.brief()
        row["squad_slot"] = pick.get("position")
        multiplier = pick.get("multiplier", 1)
        if pick.get("is_captain"):
            row["role"] = "captain"
            captain = player
        elif pick.get("is_vice_captain"):
            row["role"] = "vice-captain"
            vice = player
        if multiplier and multiplier > 1:
            row["multiplier"] = multiplier
        if "selling_price" in pick:
            row["selling_price"] = pick["selling_price"] / 10
            row["purchase_price"] = pick.get("purchase_price", 0) / 10
        if pick.get("position", 0) <= 11:
            starters.append(row)
        else:
            bench.append(row)

    squad = {
        "starting_xi": starters,
        "bench": bench,
        "captain": captain.name if captain else None,
        "vice_captain": vice.name if vice else None,
    }
    if starters:
        squad["formation"] = _formation(index, starters)
    return squad


def _formation(index: Index, starters: list[dict]) -> str:
    counts: dict[int, int] = defaultdict(int)
    for row in starters:
        counts[index.player(row["id"]).position_id] += 1
    return "-".join(str(counts.get(pid, 0)) for pid in (2, 3, 4))


def squad_totals(index: Index, element_ids: Iterable[int]) -> dict:
    players = [index.player(pid) for pid in element_ids]
    if not players:
        return {}
    return {
        "squad_value": round(sum(p.price for p in players), 1),
        "total_points": sum(p.total_points for p in players),
        "average_form": round(sum(p.form for p in players) / len(players), 2),
        "average_ownership_pct": round(sum(p.selected_by for p in players) / len(players), 1),
        "flagged_players": [
            {"name": p.name, "status": p.raw.get("status"), "news": p.news}
            for p in players
            if not p.available
        ],
    }


def best_performers(index: Index, element_ids: Iterable[int], metric: str, limit: int = 5) -> list[dict]:
    players = [index.player(pid) for pid in element_ids]
    ranked = sort_players(players, metric)[:limit]
    return [_with_metric(p, metric) for p in ranked]


def _with_metric(player: Player, metric: str) -> dict:
    from .index import METRICS

    row = player.brief()
    row[metric] = METRICS[metric](player)
    return row


# ---------------------------------------------------------------- ownership
def league_ownership(
    index: Index, rival_picks: list[dict], exclude_entry: int | None = None
) -> dict:
    """Ownership and effective ownership across a set of rival squads.

    `rival_picks` is a list of {"entry": id, "entry_name": str, "picks": [...]}.
    Effective ownership counts a captain twice, which is how a rival's score
    actually moves relative to yours.
    """
    squads = [r for r in rival_picks if r.get("entry") != exclude_entry]
    n = len(squads)
    if not n:
        return {"sample_size": 0, "owned_by": {}, "effective": {}, "started_by": {}}

    owned: dict[int, int] = defaultdict(int)
    started: dict[int, int] = defaultdict(int)
    effective: dict[int, float] = defaultdict(float)

    for squad in squads:
        for pick in squad.get("picks", []):
            eid = pick["element"]
            owned[eid] += 1
            multiplier = pick.get("multiplier")
            if multiplier is None:
                multiplier = 1 if pick.get("position", 99) <= 11 else 0
            if multiplier > 0:
                started[eid] += 1
                effective[eid] += multiplier

    return {
        "sample_size": n,
        "owned_by": {k: round(100 * v / n, 1) for k, v in owned.items()},
        "started_by": {k: round(100 * v / n, 1) for k, v in started.items()},
        "effective": {k: round(100 * v / n, 1) for k, v in effective.items()},
    }


def differentials(
    index: Index,
    my_ids: set[int],
    ownership: dict[int, float],
    metric: str,
    low_ownership_max: float,
    high_ownership_min: float,
    limit: int,
    min_minutes: int = 0,
) -> dict:
    """Split the player pool into the two directions that actually matter.

    * `my_differentials` — players I own that few rivals do. These are where I
      gain ground when they return.
    * `differentials_against_me` — players rivals own that I do not. These are
      where I bleed rank, so they double as a transfer shortlist.
    """
    mine: list[dict] = []
    against: list[dict] = []

    for player in index.players.values():
        if player.raw.get("removed"):
            continue
        own = ownership.get(player.id, 0.0)
        if player.id in my_ids:
            if own <= low_ownership_max:
                row = _differential_row(player, own, metric)
                mine.append(row)
        else:
            if own >= high_ownership_min and player.minutes >= min_minutes:
                against.append(_differential_row(player, own, metric))

    mine.sort(key=lambda r: r["_score"], reverse=True)
    against.sort(key=lambda r: (r["_score"], r["owned_by_pct"]), reverse=True)
    for row in mine + against:
        row.pop("_score", None)

    return {
        "my_differentials": mine[:limit],
        "differentials_against_me": against[:limit],
    }


def _differential_row(player: Player, ownership: float, metric: str) -> dict:
    from .index import METRICS

    row = player.brief()
    row["owned_by_pct"] = ownership
    row[metric] = METRICS[metric](player)
    row["_score"] = METRICS[metric](player)
    return row


# ----------------------------------------------------------------- fixtures
def fixture_ticker(
    index: Index,
    fixtures: list[dict],
    start_event: int,
    horizon: int,
    team_ids: Iterable[int] | None = None,
) -> list[dict]:
    """Average fixture difficulty per team over the next `horizon` gameweeks."""
    window = range(start_event, start_event + horizon)
    per_team: dict[int, list[dict]] = defaultdict(list)

    for fx in fixtures:
        event = fx.get("event")
        if event is None or event not in window:
            continue
        home, away = fx.get("team_h"), fx.get("team_a")
        if home is None or away is None:
            continue
        per_team[home].append(
            {
                "gw": event,
                "opponent": index.teams[away]["short_name"],
                "venue": "H",
                "difficulty": fx.get("team_h_difficulty"),
            }
        )
        per_team[away].append(
            {
                "gw": event,
                "opponent": index.teams[home]["short_name"],
                "venue": "A",
                "difficulty": fx.get("team_a_difficulty"),
            }
        )

    wanted = set(team_ids) if team_ids is not None else set(index.teams)
    rows: list[dict] = []
    for tid in wanted:
        games = sorted(per_team.get(tid, []), key=lambda g: g["gw"])
        scores = [g["difficulty"] for g in games if g["difficulty"] is not None]
        rows.append(
            {
                "team": index.teams[tid]["short_name"],
                "team_name": index.teams[tid]["name"],
                "games": len(games),
                "avg_difficulty": round(sum(scores) / len(scores), 2) if scores else None,
                "fixtures": [
                    g["opponent"] + " (" + g["venue"] + ", " + str(g["difficulty"]) + ")"
                    for g in games
                ],
            }
        )
    rows.sort(key=lambda r: (r["avg_difficulty"] is None, r["avg_difficulty"], -r["games"]))
    return rows


# ---------------------------------------------------------------- validation
def validate_squad(index: Index, element_ids: list[int]) -> list[str]:
    """Check the 15-man squad rules. Returns a list of human-readable problems."""
    problems: list[str] = []

    if len(element_ids) != len(set(element_ids)):
        problems.append("Squad contains duplicate players.")

    expected_total = sum(pt["squad_select"] for pt in index.positions.values())
    if len(element_ids) != expected_total:
        problems.append(
            "Squad has " + str(len(element_ids)) + " players, expected " + str(expected_total) + "."
        )

    by_pos: dict[int, int] = defaultdict(int)
    by_club: dict[int, int] = defaultdict(int)
    for eid in element_ids:
        try:
            player = index.player(eid)
        except KeyError:
            problems.append("Unknown player id " + str(eid) + ".")
            continue
        by_pos[player.position_id] += 1
        by_club[player.team_id] += 1

    for pid, meta in index.positions.items():
        want = meta["squad_select"]
        got = by_pos.get(pid, 0)
        if got != want:
            problems.append(
                "Need " + str(want) + " " + meta["plural_name"] + ", squad has " + str(got) + "."
            )

    limit = index.raw.get("game_settings", {}).get("squad_team_limit", 3)
    for tid, count in by_club.items():
        if count > limit:
            problems.append(
                "Too many players from " + index.teams[tid]["name"]
                + " (" + str(count) + ", limit " + str(limit) + ")."
            )
    return problems


def validate_lineup(index: Index, picks: list[dict]) -> list[str]:
    """Check formation legality for an ordered picks payload."""
    problems: list[str] = []
    slots = [p.get("position") for p in picks]
    expected_slots = list(range(1, len(picks) + 1))
    if sorted(s for s in slots if s is not None) != expected_slots:
        problems.append("Squad slots must be a complete 1.." + str(len(picks)) + " sequence.")

    captains = [p for p in picks if p.get("is_captain")]
    vices = [p for p in picks if p.get("is_vice_captain")]
    if len(captains) != 1:
        problems.append("Exactly one captain required, got " + str(len(captains)) + ".")
    if len(vices) != 1:
        problems.append("Exactly one vice-captain required, got " + str(len(vices)) + ".")
    if captains and vices and captains[0]["element"] == vices[0]["element"]:
        problems.append("Captain and vice-captain must be different players.")
    for pick in captains + vices:
        if (pick.get("position") or 99) > 11:
            problems.append("Captain and vice-captain must both start.")
            break

    starters = [p for p in picks if (p.get("position") or 99) <= 11]
    by_pos: dict[int, int] = defaultdict(int)
    for pick in starters:
        try:
            by_pos[index.player(pick["element"]).position_id] += 1
        except KeyError:
            problems.append("Unknown player id " + str(pick.get("element")) + ".")

    for pid, meta in index.positions.items():
        got = by_pos.get(pid, 0)
        lo, hi = meta.get("squad_min_play", 0), meta.get("squad_max_play", 11)
        if got < lo or got > hi:
            problems.append(
                "Starting XI has " + str(got) + " " + meta["plural_name"]
                + " (allowed " + str(lo) + "-" + str(hi) + ")."
            )

    # Outfield bench order is meaningful; the goalkeeper bench slot is fixed.
    if len(picks) > 11:
        bench_gk = [
            p for p in picks
            if (p.get("position") or 0) > 11 and index.players.get(p["element"])
            and index.player(p["element"]).position_id == 1
        ]
        if bench_gk and bench_gk[0].get("position") != 12:
            problems.append("The benched goalkeeper must occupy slot 12.")
    return problems

"""Predicting a club's starting XI.

There is no API anywhere that publishes predicted lineups — they are editorial
judgement, not data. What follows is therefore a **derived heuristic**, not
somebody's expert prediction, and it says so in its own output. Its inputs are
all visible so a caller can disagree with the weighting.

Confirmed lineups always win. Once Pulse publishes team lists (about an hour
before kickoff) the prediction is discarded in favour of the real thing.

Ranking signal, strongest first:

* how often the player actually started recent gameweeks (from FPL's own live
  feed, which carries a `starts` flag per player per gameweek);
* minutes per appearance, which separates starters from cameo substitutes;
* availability — anyone injured or suspended is excluded outright, and a
  doubtful player is scaled by FPL's stated chance of playing;
* set-piece duty, a good proxy for being nailed on;
* price, as a last-resort proxy for squad status.

Pre-season the first two signals are empty, so predictions lean on last
season's minutes and are correspondingly weak.

Recent signings are handled separately, because scoring them on club minutes
buries them: a player who arrived in July has none, and if he came from abroad
FPL records none at all. For those, transfer fee stands in — clubs rarely buy
expensive players to bench them — and the row is flagged `new_signing` so the
caller can see the pick rests on fee rather than observed minutes. It is the
weakest inference here, and the one most worth overriding by hand.

What none of this can see is a new manager's preferences, pre-season friendlies,
or anything said in a press conference.
"""
from __future__ import annotations

from typing import Iterable

from .index import Index, Player

# FPL's legal formations: one keeper plus ten outfielders within these bounds.
MIN_DEF, MAX_DEF = 3, 5
MIN_MID, MAX_MID = 2, 5
MIN_FWD, MAX_FWD = 1, 3


def legal_formations() -> list[tuple[int, int, int]]:
    out = []
    for d in range(MIN_DEF, MAX_DEF + 1):
        for m in range(MIN_MID, MAX_MID + 1):
            for f in range(MIN_FWD, MAX_FWD + 1):
                if d + m + f == 10:
                    out.append((d, m, f))
    return out


def availability_factor(player: Player) -> float:
    """0 means cannot play; 1 means no doubt expressed."""
    if player.status in ("i", "s", "u", "n"):
        return 0.0
    chance = player.chance_next
    if chance is not None:
        return max(0.0, min(1.0, chance / 100))
    return 1.0


def is_new_signing(player: Player, within_days: int = 120) -> bool:
    """Did this player join recently enough that club minutes understate them?"""
    import datetime as dt

    joined = player.raw.get("team_join_date")
    if not joined:
        return False
    try:
        when = dt.date.fromisoformat(str(joined)[:10])
    except ValueError:
        return False
    return (dt.date.today() - when).days <= within_days


def score_player(player: Player, start_rate: float | None,
                 minutes_per_start: float | None,
                 new_signing: bool = False) -> tuple[float, dict]:
    """Score a player's likelihood of starting, and show the working."""
    parts: dict[str, float] = {}

    if start_rate is not None:
        parts["recent_start_rate"] = round(60.0 * start_rate, 2)
    if minutes_per_start is not None:
        parts["minutes_per_appearance"] = round(20.0 * min(minutes_per_start / 90.0, 1.0), 2)

    # Season prior, split into rate and volume — they mean different things.
    #
    # Total minutes alone conflates "was injured" with "is not a starter". A
    # player who starts every game he is fit for, but misses half the season,
    # racks up the same total as a rotation player who is always available. So
    # score how completely he plays when picked (minutes per start) separately
    # from how often he was picked (starts), and weight the rate more heavily:
    # fitness returns, a squad role does not.
    #
    # A recent signing's minutes were earned at another club — often abroad,
    # where FPL records none at all — so neither term describes him. For those,
    # lean on fee instead: clubs rarely buy expensive players to bench them. The
    # row is flagged so a caller can see the pick rests on fee, not on minutes.
    starts = player.raw.get("starts") or 0
    if new_signing and player.minutes < 900:
        parts["new_signing_price_proxy"] = round(12.0 * min(player.price / 12.0, 1.0), 2)
    else:
        if starts:
            minutes_per_start = player.minutes / starts
            parts["minutes_per_start"] = round(
                14.0 * min(minutes_per_start / 90.0, 1.0), 2)
        parts["starts_volume"] = round(9.0 * min(starts / 32.0, 1.0), 2)

    if player.raw.get("penalties_order") == 1:
        parts["penalty_taker"] = 4.0
    elif player.raw.get("penalties_order"):
        parts["penalty_duty"] = 1.5
    if player.raw.get("direct_freekicks_order") == 1:
        parts["freekick_taker"] = 2.0
    if player.raw.get("corners_and_indirect_freekicks_order") == 1:
        parts["corner_taker"] = 2.0

    parts["price_proxy"] = round(min(player.price / 15.0, 1.0) * 5.0, 2)

    raw_total = sum(parts.values())
    factor = availability_factor(player)
    parts["availability_multiplier"] = factor
    return raw_total * factor, parts


def start_rates(
    live_by_gameweek: dict[int, dict[int, dict]], element_ids: Iterable[int]
) -> tuple[dict[int, float], dict[int, float]]:
    """Per-player start rate and minutes-per-appearance over recent gameweeks."""
    rates: dict[int, float] = {}
    mins: dict[int, float] = {}
    gameweeks = list(live_by_gameweek)
    if not gameweeks:
        return rates, mins

    for eid in element_ids:
        started = appearances = total_minutes = 0
        for gw in gameweeks:
            stats = live_by_gameweek[gw].get(eid)
            if not stats:
                continue
            if stats.get("starts"):
                started += 1
            minutes = stats.get("minutes") or 0
            if minutes > 0:
                appearances += 1
                total_minutes += minutes
        rates[eid] = started / len(gameweeks)
        if appearances:
            mins[eid] = total_minutes / appearances
    return rates, mins


def predict_xi(
    index: Index,
    squad: list[Player],
    rates: dict[int, float],
    mins: dict[int, float],
) -> dict:
    """Pick the highest-scoring legal XI from a club's available players."""
    scored: dict[int, tuple[float, dict]] = {}
    newcomers: dict[int, bool] = {}
    for p in squad:
        newcomers[p.id] = is_new_signing(p)
        scored[p.id] = score_player(p, rates.get(p.id), mins.get(p.id), newcomers[p.id])

    available = [p for p in squad if scored[p.id][0] > 0]
    by_pos: dict[int, list[Player]] = {1: [], 2: [], 3: [], 4: []}
    for p in available:
        by_pos.setdefault(p.position_id, []).append(p)
    for pid in by_pos:
        by_pos[pid].sort(key=lambda p: scored[p.id][0], reverse=True)

    if not by_pos.get(1):
        return {"error": "No available goalkeeper for this club."}

    best: tuple[float, tuple[int, int, int], list[Player]] | None = None
    for d, m, f in legal_formations():
        if len(by_pos.get(2, [])) < d or len(by_pos.get(3, [])) < m \
                or len(by_pos.get(4, [])) < f:
            continue
        picked = (by_pos[1][:1] + by_pos[2][:d] + by_pos[3][:m] + by_pos[4][:f])
        total = sum(scored[p.id][0] for p in picked)
        if best is None or total > best[0]:
            best = (total, (d, m, f), picked)

    if best is None:
        return {"error": "Could not build a legal XI from the available players."}

    _, formation, picked = best
    picked_ids = {p.id for p in picked}

    def row(p: Player, starting: bool) -> dict:
        score, parts = scored[p.id]
        out = {
            "id": p.id,
            "name": p.name,
            "position": p.position,
            "price": p.price,
            "likelihood_score": round(score, 1),
            "starts_recent_gws": (None if p.id not in rates
                                  else round(100 * rates[p.id]) ),
            "season_minutes": p.minutes,
        }
        if newcomers.get(p.id):
            out["new_signing"] = True
            out["joined"] = str(p.raw.get("team_join_date"))[:10]
        if starting:
            out["score_breakdown"] = {k: v for k, v in parts.items() if v}
        if not p.available:
            out["status"] = p.raw.get("status")
            if p.news:
                out["news"] = p.news
        elif p.chance_next is not None and p.chance_next < 100:
            out["chance_of_playing"] = p.chance_next
        return out

    bench = [p for p in available if p.id not in picked_ids]
    bench.sort(key=lambda p: scored[p.id][0], reverse=True)
    excluded = [p for p in squad if scored[p.id][0] <= 0]

    # Recent signings rank low by construction: the model scores evidence, and
    # they have none in this league yet. Left to the ordering alone they would
    # drop off the bottom and read as "not considered", when the truth is
    # "cannot be judged". Surface them separately so the gap is visible.
    unproven = [
        row(p, False) for p in bench
        if newcomers.get(p.id) and p.id not in picked_ids
    ]

    out = {
        "formation": "-".join(str(x) for x in formation),
        "predicted_xi": [row(p, True) for p in picked],
        "next_in_line": [row(p, False) for p in bench[:7]],
        "unavailable": [
            {"name": p.name, "status": p.raw.get("status"), "news": p.news}
            for p in excluded
        ],
    }
    if unproven:
        out["new_signings_not_predicted"] = unproven
        out["new_signing_caveat"] = (
            "These joined recently and have little or no record at this club, so "
            "they score low on evidence rather than on merit. Any of them could "
            "walk into the side. Judge them on the transfer, not on this ranking."
        )
    return out

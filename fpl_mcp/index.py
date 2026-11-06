"""Normalisation layer: turns bootstrap-static into convenient lookups."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

STATUS_LABELS = {
    "a": "available",
    "d": "doubtful",
    "i": "injured",
    "s": "suspended",
    "u": "unavailable",
    "n": "ineligible",
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Player:
    id: int
    name: str
    full_name: str
    team_id: int
    team: str
    position: str
    position_id: int
    price: float
    total_points: int
    form: float
    points_per_game: float
    minutes: int
    goals: int
    assists: int
    clean_sheets: int
    bonus: int
    xg: float
    xa: float
    xgi: float
    xgi_per_90: float
    ict: float
    defensive_contribution: int
    selected_by: float
    ep_next: float
    event_points: int
    status: str
    news: str
    chance_next: int | None
    cost_change_event: float
    transfers_in_event: int
    transfers_out_event: int
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def available(self) -> bool:
        return self.status == "a"

    @property
    def value_score(self) -> float:
        """Points per million — the classic value yardstick."""
        return round(self.total_points / self.price, 2) if self.price else 0.0

    def brief(self) -> dict:
        out = {
            "id": self.id,
            "name": self.name,
            "team": self.team,
            "position": self.position,
            "price": self.price,
            "total_points": self.total_points,
            "form": self.form,
            "ppg": self.points_per_game,
            "minutes": self.minutes,
            "selected_by_pct": self.selected_by,
        }
        if not self.available:
            out["status"] = STATUS_LABELS.get(self.status, self.status)
            if self.news:
                out["news"] = self.news
        return out

    def detail(self) -> dict:
        out = self.brief()
        out.update(
            {
                "full_name": self.full_name,
                "goals": self.goals,
                "assists": self.assists,
                "clean_sheets": self.clean_sheets,
                "bonus": self.bonus,
                "xg": self.xg,
                "xa": self.xa,
                "xgi": self.xgi,
                "xgi_per_90": self.xgi_per_90,
                "ict_index": self.ict,
                "defensive_contribution": self.defensive_contribution,
                "points_per_million": self.value_score,
                "expected_points_next_gw": self.ep_next,
                "last_gw_points": self.event_points,
                "price_change_this_gw": self.cost_change_event,
                "net_transfers_this_gw": self.transfers_in_event - self.transfers_out_event,
                "status": STATUS_LABELS.get(self.status, self.status),
            }
        )
        if self.news:
            out["news"] = self.news
        if self.chance_next is not None and self.chance_next < 100:
            out["chance_of_playing_next_gw"] = self.chance_next
        return out


class Index:
    """Wraps one bootstrap-static payload."""

    def __init__(self, bootstrap: dict) -> None:
        self.raw = bootstrap
        self.teams: dict[int, dict] = {t["id"]: t for t in bootstrap["teams"]}
        self.positions: dict[int, dict] = {p["id"]: p for p in bootstrap["element_types"]}
        self.events: list[dict] = bootstrap["events"]
        self.total_players: int = bootstrap.get("total_players", 0)
        self.players: dict[int, Player] = {}
        for raw in bootstrap["elements"]:
            player = self._to_player(raw)
            self.players[player.id] = player

    def _to_player(self, e: dict) -> Player:
        team = self.teams.get(e["team"], {})
        pos = self.positions.get(e["element_type"], {})
        web = e.get("web_name") or e.get("known_name") or ""
        first = e.get("first_name") or ""
        second = e.get("second_name") or ""
        return Player(
            id=e["id"],
            name=web,
            full_name=(first + " " + second).strip(),
            team_id=e["team"],
            team=team.get("short_name", "???"),
            position=pos.get("singular_name_short", "?"),
            position_id=e["element_type"],
            price=e["now_cost"] / 10,
            total_points=e.get("total_points", 0),
            form=_f(e.get("form")),
            points_per_game=_f(e.get("points_per_game")),
            minutes=e.get("minutes", 0),
            goals=e.get("goals_scored", 0),
            assists=e.get("assists", 0),
            clean_sheets=e.get("clean_sheets", 0),
            bonus=e.get("bonus", 0),
            xg=_f(e.get("expected_goals")),
            xa=_f(e.get("expected_assists")),
            xgi=_f(e.get("expected_goal_involvements")),
            xgi_per_90=_f(e.get("expected_goal_involvements_per_90")),
            ict=_f(e.get("ict_index")),
            defensive_contribution=e.get("defensive_contribution", 0) or 0,
            selected_by=_f(e.get("selected_by_percent")),
            ep_next=_f(e.get("ep_next")),
            event_points=e.get("event_points", 0),
            status=e.get("status", "a"),
            news=(e.get("news") or "").strip(),
            chance_next=e.get("chance_of_playing_next_round"),
            cost_change_event=e.get("cost_change_event", 0) / 10,
            transfers_in_event=e.get("transfers_in_event", 0),
            transfers_out_event=e.get("transfers_out_event", 0),
            raw=e,
        )

    # ------------------------------------------------------------------ events
    @property
    def current_event(self) -> int | None:
        for ev in self.events:
            if ev.get("is_current"):
                return ev["id"]
        return None

    @property
    def next_event(self) -> int | None:
        for ev in self.events:
            if ev.get("is_next"):
                return ev["id"]
        return None

    def event(self, event_id: int) -> dict | None:
        return next((e for e in self.events if e["id"] == event_id), None)

    @property
    def season_stage(self) -> str:
        if self.current_event is None:
            return "preseason"
        if all(e.get("finished") for e in self.events):
            return "finished"
        return "in_season"

    def default_metric(self) -> str:
        """Total points is the right default all year.

        Before the first kickoff the API keeps serving the previous season's
        totals, which still rank players usefully. FPL's own `ep_next` is far
        too coarse pre-season to sort on (hundreds of players share a value).
        """
        return "total_points"

    # ----------------------------------------------------------------- lookups
    def player(self, element_id: int) -> Player:
        p = self.players.get(element_id)
        if p is None:
            raise KeyError("Unknown player id " + str(element_id))
        return p

    def team_by_name(self, name: str) -> dict | None:
        needle = name.strip().lower()
        for t in self.teams.values():
            if needle in (t["name"].lower(), t["short_name"].lower()):
                return t
        for t in self.teams.values():
            if needle in t["name"].lower():
                return t
        return None

    def position_id_by_name(self, name: str) -> int | None:
        needle = name.strip().lower()
        aliases = {
            "gk": 1, "gkp": 1, "goalkeeper": 1, "keeper": 1,
            "def": 2, "defender": 2, "d": 2,
            "mid": 3, "midfielder": 3, "m": 3,
            "fwd": 4, "forward": 4, "striker": 4, "f": 4, "att": 4,
        }
        if needle in aliases:
            return aliases[needle]
        for p in self.positions.values():
            if needle in (p["singular_name"].lower(), p["singular_name_short"].lower()):
                return p["id"]
        return None

    def search(self, query: str, limit: int = 10) -> list[Player]:
        needle = query.strip().lower()
        if not needle:
            return []
        scored: list[tuple[int, Player]] = []
        for p in self.players.values():
            name = p.name.lower()
            full = p.full_name.lower()
            if name == needle or full == needle:
                rank = 0
            elif name.startswith(needle) or full.startswith(needle):
                rank = 1
            elif needle in name or needle in full:
                rank = 2
            else:
                continue
            scored.append((rank, p))
        scored.sort(key=lambda item: (item[0], -item[1].total_points, -item[1].selected_by))
        return [p for _, p in scored[:limit]]

    def resolve(self, ref) -> Player:
        """Accept an element id or a name fragment and return exactly one player."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            return self.player(int(ref))
        matches = self.search(str(ref), limit=6)
        if not matches:
            raise KeyError("No player matches " + repr(ref))
        if len(matches) > 1 and matches[0].name.lower() != str(ref).strip().lower():
            options = ", ".join(
                m.name + " (" + m.team + ", id " + str(m.id) + ")" for m in matches
            )
            raise KeyError(repr(ref) + " is ambiguous — did you mean one of: " + options + "?")
        return matches[0]

    def filter(
        self,
        position: str | None = None,
        team: str | None = None,
        max_price: float | None = None,
        min_price: float | None = None,
        min_minutes: int = 0,
        available_only: bool = False,
    ) -> list[Player]:
        pos_id = self.position_id_by_name(position) if position else None
        if position and pos_id is None:
            raise ValueError("Unknown position " + repr(position) + " (use GKP, DEF, MID or FWD)")
        team_rec = self.team_by_name(team) if team else None
        if team and team_rec is None:
            raise ValueError("Unknown team " + repr(team))

        out: list[Player] = []
        for p in self.players.values():
            if p.raw.get("removed"):
                continue
            if pos_id and p.position_id != pos_id:
                continue
            if team_rec and p.team_id != team_rec["id"]:
                continue
            if max_price is not None and p.price > max_price:
                continue
            if min_price is not None and p.price < min_price:
                continue
            if p.minutes < min_minutes:
                continue
            if available_only and not p.available:
                continue
            out.append(p)
        return out


METRICS: dict[str, Callable[[Player], float]] = {
    "total_points": lambda p: p.total_points,
    "form": lambda p: p.form,
    "points_per_game": lambda p: p.points_per_game,
    "value": lambda p: p.value_score,
    "ict_index": lambda p: p.ict,
    "xgi": lambda p: p.xgi,
    "xgi_per_90": lambda p: p.xgi_per_90,
    "ep_next": lambda p: p.ep_next,
    "minutes": lambda p: p.minutes,
    "selected_by": lambda p: p.selected_by,
    "goals": lambda p: p.goals,
    "assists": lambda p: p.assists,
    "bonus": lambda p: p.bonus,
    "defensive_contribution": lambda p: p.defensive_contribution,
}


def sort_players(players: Iterable[Player], metric: str) -> list[Player]:
    if metric not in METRICS:
        raise ValueError(
            "Unknown metric " + repr(metric) + ". Choose from: " + ", ".join(sorted(METRICS))
        )
    key = METRICS[metric]
    return sorted(players, key=lambda p: (key(p), p.total_points), reverse=True)

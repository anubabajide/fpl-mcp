"""Regression tests for the connector bug report (BUG-1 … BUG-7, FR-1)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from fpl_mcp import server as server_module
from fpl_mcp.analysis import (
    CHIP_LABELS,
    TRANSFER_CHIPS,
    chip_state,
    normalise_chip,
    transfer_allowance,
)
from fpl_mcp.export import COMPACT_FIELDS
from fpl_mcp.index import Index, deadline_info
from fpl_mcp.server import server


def make_index(*, event_id: int = 1, finished: bool = False) -> Index:
    """A tiny but complete bootstrap payload for tool-level tests."""
    return Index(
        {
            "teams": [
                {"id": 1, "name": "Alpha", "short_name": "ALP"},
                {"id": 2, "name": "Beta", "short_name": "BET"},
            ],
            "element_types": [
                {"id": 3, "singular_name": "Midfielder", "singular_name_short": "MID"},
                {"id": 4, "singular_name": "Forward", "singular_name_short": "FWD"},
            ],
            "events": [
                {
                    "id": event_id,
                    "name": "Gameweek " + str(event_id),
                    "is_current": True,
                    "is_next": False,
                    "finished": finished,
                    "deadline_time": "2026-11-07T11:00:00Z",
                    "average_entry_score": 55,
                }
            ],
            "elements": [
                {
                    "id": 1,
                    "web_name": "One",
                    "first_name": "Player",
                    "second_name": "One",
                    "team": 1,
                    "element_type": 4,
                    "now_cost": 50,
                    "form": "2.0",
                    "total_points": 20,
                    "status": "a",
                },
                {
                    "id": 2,
                    "web_name": "Two",
                    "first_name": "Player",
                    "second_name": "Two",
                    "team": 2,
                    "element_type": 4,
                    "now_cost": 60,
                    "form": "8.0",
                    "total_points": 40,
                    "status": "a",
                },
                {
                    "id": 3,
                    "web_name": "Three",
                    "first_name": "Player",
                    "second_name": "Three",
                    "team": 1,
                    "element_type": 3,
                    "now_cost": 45,
                    "form": "5.0",
                    "total_points": 30,
                    "status": "a",
                },
            ],
        }
    )


def patch_backend(monkeypatch, index: Index, client) -> None:
    async def fake_index() -> Index:
        return index

    monkeypatch.setattr(server_module, "get_index", fake_index)
    monkeypatch.setattr(server_module, "get_client", lambda: client)


def test_transfer_allowance_counts_transfers_already_made() -> None:
    # One free transfer granted and already used: the next transfer costs 4.
    state = transfer_allowance({"limit": 1, "made": 1, "cost": 0}, 1)

    assert state["free_transfers_available"] == 0
    assert state["points_hit"] == 4
    assert state["points_hit_total"] == 4

    # The bug report's two-transfer dry run should cost 8, not 4.
    two = transfer_allowance({"limit": 1, "made": 1, "cost": 0}, 2)
    assert two["points_hit"] == 8
    assert two["points_hit_total"] == 8


def test_transfer_allowance_does_not_double_count_earlier_hits() -> None:
    # Two transfers already made with one free transfer: 4 points banked.
    state = transfer_allowance({"limit": 1, "made": 2, "cost": 4}, 1)

    assert state["points_hit_already"] == 4
    assert state["points_hit"] == 4
    assert state["points_hit_total"] == 8


def test_transfer_allowance_with_unused_free_transfers() -> None:
    meta = {"limit": 2, "made": 0, "cost": 0}

    assert transfer_allowance(meta, 2)["points_hit"] == 0
    assert transfer_allowance(meta, 3)["points_hit"] == 4
    assert transfer_allowance(meta, 3)["free_transfers_remaining_after"] == 0


def test_wildcard_and_free_hit_never_charge_points() -> None:
    meta = {"limit": 1, "made": 1, "cost": 4}

    for chip in TRANSFER_CHIPS:
        state = transfer_allowance(meta, 5, chip=chip)
        assert state["points_hit"] == 0


def test_chip_state_keeps_held_chips_while_another_is_active() -> None:
    live = {
        "chips": [
            {"name": "3xc", "status_for_entry": "active"},
            {"name": "bboost", "status_for_entry": "unavailable"},
            {"name": "wildcard", "status_for_entry": "played"},
        ]
    }

    state = chip_state(live)

    assert state["active_chip"] == CHIP_LABELS["3xc"]
    assert state["chips_available"] == [CHIP_LABELS["bboost"]]
    assert state["chips_playable_this_gw"] == []


def test_chip_state_lists_playable_chips_when_none_is_active() -> None:
    live = {
        "chips": [
            {"name": "bboost", "status_for_entry": "available"},
            {"name": "3xc", "status_for_entry": "available"},
            {"name": "freehit", "status_for_entry": "unavailable"},
        ]
    }

    state = chip_state(live)

    assert state["active_chip"] is None
    assert state["chips_available"] == ["Bench Boost", "Triple Captain", "Free Hit"]
    assert state["chips_playable_this_gw"] == ["Bench Boost", "Triple Captain"]


def test_chip_names_are_normalised_and_scoped() -> None:
    assert normalise_chip("Bench Boost") == "bboost"
    assert normalise_chip("TC") == "3xc"
    assert normalise_chip(None) is None

    with pytest.raises(ValueError, match="cannot be played here"):
        normalise_chip("Bench Boost", TRANSFER_CHIPS)


def test_deadline_info_returns_utc_and_uk_time() -> None:
    info = deadline_info({"id": 11, "deadline_time": "2026-11-07T11:00:00Z"})

    assert info is not None
    assert info["deadline_time_utc"] == "2026-11-07T11:00:00Z"
    assert info["deadline_time_uk"].endswith("+00:00")

    summer = deadline_info({"id": 1, "deadline_time": "2026-08-15T10:00:00Z"})
    assert summer is not None
    assert summer["deadline_time_uk"].endswith("+01:00")


def test_tool_schemas_accept_ids_chips_and_sort_by() -> None:
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    transfers = tools["fpl_make_transfers"].parameters["properties"]["players_out"]
    assert transfers["items"]["anyOf"] == [{"type": "integer"}, {"type": "string"}]

    lineup = tools["fpl_set_lineup"].parameters["properties"]
    assert "chip" in lineup

    top = tools["fpl_top_players"].parameters["properties"]
    assert "sort_by" in top

    export = tools["fpl_export_players"].parameters["properties"]
    assert {"ids", "offset", "limit"} <= set(export)


def test_my_squad_reports_remaining_transfers_and_held_chips(monkeypatch) -> None:
    index = make_index()

    class FakeClient:
        config = SimpleNamespace(team_id=1)

        def has_credentials(self) -> bool:
            return True

        async def entry(self, entry_id: int) -> dict:
            return {
                "name": "Test FC",
                "summary_overall_rank": 10,
                "summary_overall_points": 500,
            }

        async def my_team(self, entry_id: int) -> dict:
            return {
                "picks": [
                    {"element": 1, "position": 1, "multiplier": 1, "is_captain": True},
                    {"element": 2, "position": 2, "multiplier": 1},
                    {"element": 3, "position": 12, "multiplier": 1},
                ],
                "transfers": {"limit": 1, "made": 1, "cost": 4, "bank": 10, "value": 1000},
                "chips": [
                    {"name": "3xc", "status_for_entry": "active"},
                    {"name": "bboost", "status_for_entry": "unavailable"},
                    {"name": "wildcard", "status_for_entry": "played"},
                ],
            }

    patch_backend(monkeypatch, index, FakeClient())

    result = asyncio.run(server_module.fpl_my_squad())

    assert result["free_transfers"] == 0
    assert result["free_transfers_granted"] == 1
    assert result["transfers_made_this_gw"] == 1
    assert result["free_transfers_remaining"] == 0
    assert result["active_chip"] == "Triple Captain"
    assert result["chips_available"] == ["Bench Boost"]
    assert result["next_deadline_utc"] == "2026-11-07T11:00:00Z"


def test_gameweek_report_headline_agrees_with_player_rows(monkeypatch) -> None:
    index = make_index(event_id=7)

    class FakeClient:
        config = SimpleNamespace(team_id=1)

        async def entry_picks(self, entry_id: int, event: int) -> dict:
            return {
                "picks": [
                    {"element": 1, "position": 1, "multiplier": 1},
                    {"element": 2, "position": 2, "multiplier": 2, "is_captain": True},
                ],
                "entry_history": {
                    "points": 47,
                    "overall_rank": 2114490,
                    "event_transfers_cost": 4,
                    "points_on_bench": 3,
                },
                "automatic_subs": [],
            }

        async def live(self, event: int) -> dict:
            return {
                "elements": [
                    {"id": 1, "stats": {"total_points": 10, "minutes": 90}},
                    {"id": 2, "stats": {"total_points": 33, "minutes": 90}},
                ]
            }

        async def entry(self, entry_id: int) -> dict:
            return {"summary_overall_rank": 1462596}

    patch_backend(monkeypatch, index, FakeClient())

    result = asyncio.run(server_module.fpl_gameweek_report())

    assert result["is_live"] is True
    assert result["points_from_players"] == 76
    assert result["points"] == 72  # 76 player points minus the 4-point hit
    assert result["points_confirmed"] == 47
    assert result["overall_rank"] == 1462596
    assert result["overall_rank_confirmed"] == 2114490
    assert result["as_of"].endswith("Z")


def test_top_players_accepts_sort_by(monkeypatch) -> None:
    patch_backend(monkeypatch, make_index(), SimpleNamespace(config=SimpleNamespace(team_id=1)))

    result = asyncio.run(
        server_module.fpl_top_players(position="FWD", limit=2, sort_by="form")
    )

    assert result["metric"] == "form"
    assert [p["name"] for p in result["players"]] == ["Two", "One"]


def test_top_players_rejects_conflicting_metric_and_sort_by(monkeypatch) -> None:
    patch_backend(monkeypatch, make_index(), SimpleNamespace(config=SimpleNamespace(team_id=1)))

    with pytest.raises(ValueError, match="either metric or sort_by"):
        asyncio.run(
            server_module.fpl_top_players(metric="form", sort_by="total_points")
        )


def test_export_players_defaults_to_compact_fields_and_pages(monkeypatch) -> None:
    patch_backend(monkeypatch, make_index(), SimpleNamespace(config=SimpleNamespace(team_id=1)))

    first = asyncio.run(server_module.fpl_export_players(limit=2))
    assert first["player_count"] == 2
    assert first["field_count"] == len(COMPACT_FIELDS)
    assert "first_name" not in first["fields"]
    assert first["has_more"] is True
    assert first["next_offset"] == 2

    page = asyncio.run(server_module.fpl_export_players(offset=2, limit=2))
    assert page["player_count"] == 1
    assert page["has_more"] is False

    selected = asyncio.run(server_module.fpl_export_players(ids=[2]))
    assert selected["player_count"] == 1
    assert "Two" in selected["csv"]

    standard = asyncio.run(server_module.fpl_export_players(fields="standard", ids=[2]))
    assert "first_name" in standard["fields"]


def test_export_fixtures_includes_gameweek_deadlines(monkeypatch) -> None:
    index = make_index(event_id=8)

    class FakeClient:
        config = SimpleNamespace(team_id=1)

        async def fixtures(self) -> list[dict]:
            return []

    patch_backend(monkeypatch, index, FakeClient())

    result = asyncio.run(server_module.fpl_export_fixtures(horizon=1))

    assert result["first_deadline_utc"] == "2026-11-07T11:00:00Z"
    assert result["gameweek_deadlines"][0]["gameweek"] == 8
    assert result["next_deadline_utc"] == "2026-11-07T11:00:00Z"


def test_export_players_rejects_unknown_fields(monkeypatch) -> None:
    patch_backend(monkeypatch, make_index(), SimpleNamespace(config=SimpleNamespace(team_id=1)))

    with pytest.raises(ValueError, match="Unknown field"):
        asyncio.run(server_module.fpl_export_players(fields="not_a_field"))


def test_tool_result_json_stays_compact(monkeypatch) -> None:
    """The compact default must not balloon back to the old ~158k payload."""
    patch_backend(monkeypatch, make_index(), SimpleNamespace(config=SimpleNamespace(team_id=1)))

    result = asyncio.run(server_module.fpl_export_players())
    payload = json.dumps(result, default=str)
    # Three rows only here, but the compact field list is the real guard.
    assert result["field_count"] <= 25
    assert len(payload) < 20_000

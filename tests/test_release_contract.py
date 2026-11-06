"""Release metadata and protocol smoke tests that need no FPL credentials."""
from __future__ import annotations

import json
from pathlib import Path

from fpl_mcp import __version__
from fpl_mcp.server import server


ROOT = Path(__file__).parents[1]


def test_registry_manifest_matches_package_version() -> None:
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "io.github.anubabajide/fpl"
    assert manifest["version"] == __version__
    assert manifest["packages"][0]["identifier"] == "fantasy-premier-league-mcp"
    assert manifest["packages"][0]["version"] == __version__


def test_readme_has_registry_ownership_marker() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "mcp-name: io.github.anubabajide/fpl" in readme


def test_server_advertises_core_tools() -> None:
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert {"fpl_setup", "fpl_top_players", "fpl_make_transfers"} <= names

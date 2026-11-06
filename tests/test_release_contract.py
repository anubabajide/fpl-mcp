"""Release metadata and protocol smoke tests that need no FPL credentials."""
from __future__ import annotations

import json
import asyncio
from pathlib import Path

from fpl_mcp import __version__
from fpl_mcp import config as config_module
from fpl_mcp import server as server_module
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


def test_config_never_persists_a_bearer_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "config.json")
    cfg = config_module.Config(team_id=123, bearer_token="header.payload.signature")

    cfg.save()

    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "bearer_token" not in saved
    assert config_module.Config.load().bearer_token is None


def test_browser_login_saves_the_session_in_the_background(tmp_path, monkeypatch) -> None:
    from urllib.parse import parse_qs, urlsplit

    from fpl_mcp import auth
    from fpl_mcp.client import FPLClient

    class FakeConfig:
        team_id = None
        bearer_token = None
        browser_channel = None
        saves = 0

        def save(self) -> None:
            self.saves += 1

    async def scenario() -> None:
        gate = asyncio.Event()

        async def fake_browser(authorize_url, **_kwargs):
            await gate.wait()
            state = parse_qs(urlsplit(authorize_url).query)["state"][0]
            return auth.REDIRECT_URI + "?code=test-code&state=" + state, "chrome"

        config = FakeConfig()
        store = auth.TokenStore(tmp_path / "credentials.json", tmp_path / "credentials.lock")
        session = auth.Session(store=store, browser=fake_browser)

        async def fake_token_request(_form):
            return {
                "access_token": "header.payload.signature",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "scope": auth.SCOPE,
            }

        session._token_request = fake_token_request  # type: ignore[method-assign]
        client = FPLClient(config=config, session=session)
        monkeypatch.setattr(server_module, "get_client", lambda: client)

        result = await server_module.fpl_login(wait_seconds=0)
        assert result["status"] == "awaiting_browser"

        gate.set()
        assert await session.wait_for_login(1)

        saved = store.load()
        assert saved is not None
        assert saved.access_token == "header.payload.signature"
        assert saved.refresh_token == "refresh-token"
        assert config.browser_channel == "chrome"
        assert config.saves == 1

    asyncio.run(scenario())

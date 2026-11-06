"""Configuration and credential storage for the FPL MCP server."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("FPL_MCP_HOME") or (Path.home() / ".fpl-mcp"))
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = CONFIG_DIR / "cache"


@dataclass
class Config:
    """Persisted settings. Env vars always win over the config file."""

    team_id: int | None = None
    bearer_token: str | None = None
    default_league_id: int | None = None
    browser_session: bool = False
    browser_channel: str | None = None

    @classmethod
    def load(cls) -> "Config":
        data: dict = {}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}

        cfg = cls(
            team_id=_int_or_none(data.get("team_id")),
            bearer_token=data.get("bearer_token") or None,
            default_league_id=_int_or_none(data.get("default_league_id")),
            browser_session=bool(data.get("browser_session")),
            browser_channel=data.get("browser_channel") or None,
        )

        if os.environ.get("FPL_TEAM_ID"):
            cfg.team_id = _int_or_none(os.environ["FPL_TEAM_ID"])
        if os.environ.get("FPL_BEARER_TOKEN"):
            cfg.bearer_token = os.environ["FPL_BEARER_TOKEN"].strip()
        if os.environ.get("FPL_LEAGUE_ID"):
            cfg.default_league_id = _int_or_none(os.environ["FPL_LEAGUE_ID"])
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        try:  # best effort: keep the token file owner-only
            CONFIG_PATH.chmod(0o600)
        except OSError:
            pass


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

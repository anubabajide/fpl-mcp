"""Configuration storage for the FPL MCP server.

Credentials do not live here: a signed-in FPL session is kept by `auth.TokenStore`
in its own owner-only file. This file holds only non-sensitive settings.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("FPL_MCP_HOME") or (Path.home() / ".fpl-mcp"))
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = CONFIG_DIR / "cache"


@dataclass
class Config:
    """Settings for one local FPL user. Env vars always win over the file."""

    team_id: int | None = None
    default_league_id: int | None = None
    # Which browser last opened the sign-in window successfully, tried first next time.
    browser_channel: str | None = None
    # A bearer token pasted by hand (`fpl_set_token`) or set via FPL_BEARER_TOKEN.
    # Held in memory only: it cannot renew itself, so persisting it buys nothing.
    bearer_token: str | None = None

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
            default_league_id=_int_or_none(data.get("default_league_id")),
            browser_channel=data.get("browser_channel") or None,
        )
        # Version 0.2.0 wrote the bearer token into this file. Rewrite it without.
        if "bearer_token" in data or "browser_session" in data:
            try:
                cfg.save()
            except OSError:
                pass

        if os.environ.get("FPL_TEAM_ID"):
            cfg.team_id = _int_or_none(os.environ["FPL_TEAM_ID"])
        if os.environ.get("FPL_BEARER_TOKEN"):
            cfg.bearer_token = os.environ["FPL_BEARER_TOKEN"].strip()
        if os.environ.get("FPL_LEAGUE_ID"):
            cfg.default_league_id = _int_or_none(os.environ["FPL_LEAGUE_ID"])
        return cfg

    def save(self) -> None:
        write_private_json(
            CONFIG_PATH,
            {
                "team_id": self.team_id,
                "default_league_id": self.default_league_id,
                "browser_channel": self.browser_channel,
            },
        )


def write_private_json(path: Path, data: dict) -> None:
    """Write JSON readable only by the current user, replacing the file atomically.

    The atomic replace means a process stopped mid-write can never leave a
    truncated file behind. Permissions are best effort: on Windows the file
    inherits the ACL of the user's profile directory instead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        try:
            tmp_path.chmod(0o600)  # before any content is written
        except OSError:
            pass
        tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

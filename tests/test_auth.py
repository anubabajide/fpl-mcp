"""Unit tests for the OAuth session, without touching FPL or a browser."""
from __future__ import annotations

import asyncio
import time

import pytest

from fpl_mcp import auth


def test_pending_sign_in_only_accepts_its_own_state() -> None:
    pending = auth.PendingSignIn.start()

    code = pending.code_from(
        auth.REDIRECT_URI + "?code=the-code&state=" + pending.state
    )
    assert code == "the-code"

    with pytest.raises(auth.SignInError, match="different sign-in attempt"):
        pending.code_from(auth.REDIRECT_URI + "?code=the-code&state=someone-else")

    with pytest.raises(auth.SignInError, match="no sign-in code"):
        pending.code_from(auth.REDIRECT_URI + "?state=" + pending.state)


def test_token_store_round_trips_and_keeps_credentials_private(tmp_path) -> None:
    store = auth.TokenStore(tmp_path / "credentials.json", tmp_path / "credentials.lock")
    tokens = auth.Tokens(
        access_token="header.payload.signature",
        refresh_token="refresh-token",
        expires_at=int(time.time()) + 3600,
        scope=auth.SCOPE,
    )

    store.save(tokens)

    loaded = store.load()
    assert loaded == tokens
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_renewal_omitting_a_refresh_token_keeps_the_old_one() -> None:
    previous = auth.Tokens("old.access.token", "old-refresh", time.time() + 60)
    fresh = auth.Tokens.from_response(
        {"access_token": "new.access.token", "expires_in": 3600},
        previous=previous,
    )

    assert fresh.access_token == "new.access.token"
    assert fresh.refresh_token == "old-refresh"


def test_access_token_renews_an_expired_session(tmp_path) -> None:
    store = auth.TokenStore(tmp_path / "credentials.json", tmp_path / "credentials.lock")
    store.save(auth.Tokens("stale.access.token", "refresh-token", time.time() - 10))
    session = auth.Session(store=store)

    async def fake_token_request(form):
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "refresh-token"
        return {
            "access_token": "fresh.access.token",
            "refresh_token": "rotated-refresh-token",
            "expires_in": 3600,
            "scope": auth.SCOPE,
        }

    session._token_request = fake_token_request  # type: ignore[method-assign]

    assert asyncio.run(session.access_token()) == "fresh.access.token"
    saved = store.load()
    assert saved is not None
    assert saved.refresh_token == "rotated-refresh-token"

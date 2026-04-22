"""HTTP-layer tests for /api/sip/hold-toggle and /api/sip/hold-state.

These don't talk to the real sip-bridge container — they verify the
request shape, response shape, and that mute-toggle still routes for
backwards-compat with un-upgraded radios.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient


@pytest_asyncio.fixture(autouse=True)
async def _refresh_features_cache():
    """The features module keeps a process-level cache. Other tests in
    the suite flip flags off and on; refresh from the freshly-seeded
    DB before each hold-api test so the "sip" feature gate is green."""
    from server import features as _features
    from server.database import async_session

    async with async_session() as db:
        await _features.refresh_cache(db)
    yield


@pytest.mark.asyncio
async def test_hold_toggle_no_auth_required(client: AsyncClient):
    """POST /api/sip/hold-toggle is device-trusted (no auth)."""
    with patch("server.api.sip._signal_sip_bridge") as mock_signal:
        r = await client.post(
            "/api/sip/hold-toggle",
            json={"username": "harro"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "hold-toggle"
    assert body["username"] == "harro"
    mock_signal.assert_called_once_with("SIGUSR2")


@pytest.mark.asyncio
async def test_mute_toggle_alias_still_works(client: AsyncClient):
    """POST /api/sip/mute-toggle remains for one release as an alias."""
    with patch("server.api.sip._signal_sip_bridge") as mock_signal:
        r = await client.post(
            "/api/sip/mute-toggle",
            json={"username": "harro"},
        )
    assert r.status_code == 200
    mock_signal.assert_called_once_with("SIGUSR2")


@pytest.mark.asyncio
async def test_hold_state_returns_false_when_no_file(client: AsyncClient):
    """No state file exists → response is {'holding': false}."""
    with patch("server.api.sip._read_hold_state_from_bridge") as mock_read:
        mock_read.return_value = None
        r = await client.get("/api/sip/hold-state")
    assert r.status_code == 200
    assert r.json() == {"holding": False}


@pytest.mark.asyncio
async def test_hold_state_returns_full_payload_when_held(client: AsyncClient):
    """Bridge state file says holding → endpoint surfaces it verbatim."""
    payload = {"holding": True, "slot": 2, "held_for_seconds": 47}
    with patch("server.api.sip._read_hold_state_from_bridge") as mock_read:
        mock_read.return_value = payload
        r = await client.get("/api/sip/hold-state")
    assert r.status_code == 200
    assert r.json() == payload

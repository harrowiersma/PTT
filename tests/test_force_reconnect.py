"""Tests for the force-reconnect admin endpoint + registration fields
in UserResponse. The force-reconnect button in the dashboard calls
POST /api/call-groups/force-reconnect which restarts the Murmur
container, kicking every connected user. Used as a catch-all when
ACL state drifts — after a restart, clients re-read Murmur's acl
table and honour it fresh.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient

from server.models import User


@pytest.mark.asyncio
async def test_force_reconnect_requires_admin(client: AsyncClient):
    r = await client.post("/api/call-groups/force-reconnect")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_force_reconnect_restarts_murmur(admin_client: AsyncClient):
    """Admin POST restarts the Murmur container exactly once."""
    async def _run(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch(
        "server.murmur.admin_sqlite.restart_murmur"
    ) as m_restart, patch(
        "asyncio.to_thread", side_effect=_run,
    ):
        r = await admin_client.post("/api/call-groups/force-reconnect")
    assert r.status_code == 200
    m_restart.assert_called_once()


@pytest.mark.asyncio
async def test_user_response_exposes_registration_fields(
    admin_client: AsyncClient, db_session,
):
    """UserResponse must include mumble_cert_hash + mumble_registered_user_id
    so the dashboard can render the 'pending cert' / 'registered' pill."""
    u = User(
        username="alice",
        mumble_password="x",
        mumble_cert_hash="abc123",
        mumble_registered_user_id=42,
    )
    db_session.add(u)
    await db_session.commit()

    r = await admin_client.get("/api/users")
    assert r.status_code == 200
    body = r.json()
    alice = next(u for u in body if u["username"] == "alice")
    assert alice["mumble_cert_hash"] == "abc123"
    assert alice["mumble_registered_user_id"] == 42


@pytest.mark.asyncio
async def test_user_response_pending_user_has_null_fields(
    admin_client: AsyncClient, db_session,
):
    u = User(username="bob", mumble_password="x")  # no capture yet
    db_session.add(u)
    await db_session.commit()

    r = await admin_client.get("/api/users")
    bob = next(u for u in r.json() if u["username"] == "bob")
    assert bob["mumble_cert_hash"] is None
    assert bob["mumble_registered_user_id"] is None

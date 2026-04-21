import pytest
from httpx import AsyncClient

from server.models import DispatchEvent


@pytest.mark.asyncio
async def test_recent_empty(admin_client: AsyncClient):
    r = await admin_client.get("/api/dispatch/recent")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_recent_returns_in_reverse_chronological_order(admin_client: AsyncClient, db_session):
    db_session.add_all([
        DispatchEvent(target_username="alice", message="first", latitude=1, longitude=1),
        DispatchEvent(target_username="bob", message="second", latitude=2, longitude=2),
        DispatchEvent(target_username="carol", message="third", latitude=3, longitude=3),
    ])
    await db_session.commit()
    r = await admin_client.get("/api/dispatch/recent?limit=2")
    body = r.json()
    assert len(body) == 2
    # Most recent first
    assert body[0]["target_username"] == "carol"
    assert body[1]["target_username"] == "bob"


@pytest.mark.asyncio
async def test_recent_requires_admin(client: AsyncClient):
    r = await client.get("/api/dispatch/recent")
    assert r.status_code == 401

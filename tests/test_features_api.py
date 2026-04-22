from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_features_requires_admin(client: AsyncClient):
    r = await client.get("/api/admin/features")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_features_admin_ok(admin_client: AsyncClient):
    r = await admin_client.get("/api/admin/features")
    assert r.status_code == 200
    data = r.json()
    assert {f["key"] for f in data} == {
        "lone_worker", "sip", "dispatch", "weather", "sos",
        "call_groups_hiding",
    }
    assert all("enabled" in f and "updated_at" in f for f in data)


@pytest.mark.asyncio
async def test_update_feature_toggles_cache(admin_client: AsyncClient):
    from server.features import is_enabled, refresh_cache

    # Sync the cache to reflect the seeded DB state for this test DB.
    # (The module-level cache is shared across tests; setup_db reseeds the
    # DB each run but doesn't touch the cache.)
    from server.database import async_session
    async with async_session() as _db:
        await refresh_cache(_db)

    assert is_enabled("dispatch") is True  # pre-condition from seed
    r = await admin_client.put(
        "/api/admin/features/dispatch", json={"enabled": False}
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert is_enabled("dispatch") is False  # cache updated synchronously

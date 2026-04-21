import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_settings_no_auth(client: AsyncClient):
    r = await client.get("/api/dispatch/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["map_home_lat"] == pytest.approx(38.72, abs=0.01)
    assert body["max_workers"] == 10
    assert body["search_radius_m"] is None


@pytest.mark.asyncio
async def test_put_settings_requires_admin(client: AsyncClient):
    r = await client.put("/api/dispatch/settings", json={"max_workers": 5})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_put_settings_updates_fields(admin_client: AsyncClient):
    r = await admin_client.put("/api/dispatch/settings", json={
        "map_home_lat": 41.15, "map_home_lng": -8.61, "map_home_zoom": 12,
        "max_workers": 5, "search_radius_m": 3000,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["map_home_lat"] == pytest.approx(41.15, abs=0.01)
    assert body["max_workers"] == 5
    assert body["search_radius_m"] == 3000


@pytest.mark.asyncio
async def test_put_settings_clamps_max_workers(admin_client: AsyncClient):
    r = await admin_client.put("/api/dispatch/settings", json={"max_workers": 999})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_settings_rejects_negative_radius(admin_client: AsyncClient):
    r = await admin_client.put("/api/dispatch/settings", json={"search_radius_m": -5})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_settings_rejects_invalid_lat(admin_client: AsyncClient):
    r = await admin_client.put("/api/dispatch/settings", json={"map_home_lat": 999})
    assert r.status_code == 422

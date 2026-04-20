from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_capabilities_no_auth_required(client: AsyncClient):
    r = await client.get("/api/status/capabilities")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_capabilities_shape(client: AsyncClient):
    r = await client.get("/api/status/capabilities")
    data = r.json()
    assert "features" in data
    assert set(data["features"].keys()) == {
        "lone_worker", "sip", "dispatch", "weather", "sos",
    }
    assert all(isinstance(v, bool) for v in data["features"].values())
    assert "server_version" in data

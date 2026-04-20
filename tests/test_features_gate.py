from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from server import features as _features
from server.database import async_session
from server.models import FeatureFlag


@pytest.mark.asyncio
async def test_disabled_feature_returns_503(admin_client: AsyncClient):
    # Flip the row in the DB and refresh the cache so the next request
    # through the admin_client is rejected by the router dependency.
    async with async_session() as db:
        await db.execute(
            update(FeatureFlag)
            .where(FeatureFlag.key == "weather")
            .values(enabled=False)
        )
        await db.commit()
        await _features.refresh_cache(db)

    r = await admin_client.post("/api/weather", json={"location": "Paris"})
    assert r.status_code == 503
    assert "disabled" in r.json()["detail"].lower()

    # Re-enable so downstream tests that share the module-level cache
    # aren't left with weather off.
    async with async_session() as db:
        await db.execute(
            update(FeatureFlag)
            .where(FeatureFlag.key == "weather")
            .values(enabled=True)
        )
        await db.commit()
        await _features.refresh_cache(db)

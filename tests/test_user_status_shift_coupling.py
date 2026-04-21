import pytest
from sqlalchemy import select
from httpx import AsyncClient

from server import features as _features
from server.models import FeatureFlag, User


async def _enable_lw(db_session):
    row = (await db_session.execute(
        select(FeatureFlag).where(FeatureFlag.key == "lone_worker")
    )).scalar_one()
    row.enabled = True
    await db_session.commit()
    await _features.refresh_cache(db_session)


async def _make_lone_worker(db_session, name="wanda"):
    u = User(username=name, mumble_password="x", is_lone_worker=True)
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest.mark.asyncio
async def test_shift_start_forces_online(client: AsyncClient, db_session):
    await _enable_lw(db_session)
    u = await _make_lone_worker(db_session, "wanda")
    u.status_label = "busy"
    await db_session.commit()

    r = await client.post("/api/loneworker/shift/start", json={"username": "wanda"})
    assert r.status_code == 200

    await db_session.refresh(u)
    assert u.status_label == "online"


@pytest.mark.asyncio
async def test_shift_start_without_lone_worker_feature_is_noop(
    client: AsyncClient, db_session,
):
    lw = (await db_session.execute(
        select(FeatureFlag).where(FeatureFlag.key == "lone_worker")
    )).scalar_one()
    lw.enabled = False
    await db_session.commit()
    await _features.refresh_cache(db_session)
    u = await _make_lone_worker(db_session, "wanda")
    u.status_label = "busy"
    await db_session.commit()

    r = await client.post("/api/loneworker/shift/start", json={"username": "wanda"})
    # Feature-gated router returns 503 when the flag is off.
    assert r.status_code == 503

    await db_session.refresh(u)
    assert u.status_label == "busy"  # untouched

    # Restore the cache so downstream tests sharing the module-level feature
    # cache aren't left with lone_worker disabled.
    lw.enabled = True
    await db_session.commit()
    await _features.refresh_cache(db_session)

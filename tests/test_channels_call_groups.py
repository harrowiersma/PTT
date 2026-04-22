import pytest
from httpx import AsyncClient
from server.models import CallGroup


async def _seed_group(db_session, name="Sales"):
    g = CallGroup(name=name)
    db_session.add(g)
    await db_session.commit()
    await db_session.refresh(g)
    return g.id


@pytest.mark.asyncio
async def test_channel_response_includes_call_group_id(
    admin_client: AsyncClient, db_session,
):
    gid = await _seed_group(db_session)
    r = await admin_client.post("/api/channels", json={
        "name": "SalesChan",
        "call_group_id": gid,
    })
    assert r.status_code == 201
    assert r.json()["call_group_id"] == gid

    r = await admin_client.get("/api/channels")
    sc = next(x for x in r.json() if x["name"] == "SalesChan")
    assert sc["call_group_id"] == gid


@pytest.mark.asyncio
async def test_channel_create_without_call_group_id_is_null(
    admin_client: AsyncClient,
):
    r = await admin_client.post("/api/channels", json={"name": "PublicChan"})
    assert r.json()["call_group_id"] is None


@pytest.mark.asyncio
async def test_channel_patch_clears_call_group_id(
    admin_client: AsyncClient, db_session,
):
    gid = await _seed_group(db_session)
    r = await admin_client.post("/api/channels", json={
        "name": "PrivateChan", "call_group_id": gid,
    })
    cid = r.json()["id"]
    r = await admin_client.patch(f"/api/channels/{cid}", json={"call_group_id": None})
    assert r.json()["call_group_id"] is None

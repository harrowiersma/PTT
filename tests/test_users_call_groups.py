import pytest
from httpx import AsyncClient
from sqlalchemy import select
from server.models import CallGroup, UserCallGroup, User


async def _seed_groups(db_session, *names):
    ids = []
    for name in names:
        g = CallGroup(name=name)
        db_session.add(g)
        await db_session.commit()
        await db_session.refresh(g)
        ids.append(g.id)
    return ids


@pytest.mark.asyncio
async def test_user_response_includes_call_group_ids(
    admin_client: AsyncClient, db_session,
):
    g_ids = await _seed_groups(db_session, "Sales", "Ops")
    u = User(username="alice", mumble_password="x")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    db_session.add(UserCallGroup(user_id=u.id, call_group_id=g_ids[0]))
    db_session.add(UserCallGroup(user_id=u.id, call_group_id=g_ids[1]))
    await db_session.commit()

    r = await admin_client.get("/api/users")
    alice = next(x for x in r.json() if x["username"] == "alice")
    assert sorted(alice["call_group_ids"]) == sorted(g_ids)


@pytest.mark.asyncio
async def test_user_create_assigns_call_groups(
    admin_client: AsyncClient, db_session,
):
    g_ids = await _seed_groups(db_session, "Sales")
    r = await admin_client.post("/api/users", json={
        "username": "bob",
        "password": "shhh",
        "call_group_ids": g_ids,
    })
    assert r.status_code == 201
    assert r.json()["call_group_ids"] == g_ids


@pytest.mark.asyncio
async def test_user_update_replaces_call_groups(
    admin_client: AsyncClient, db_session,
):
    g_ids = await _seed_groups(db_session, "Sales", "Ops")
    r = await admin_client.post("/api/users", json={
        "username": "carol", "password": "shhh", "call_group_ids": [g_ids[0]],
    })
    uid = r.json()["id"]

    r = await admin_client.patch(f"/api/users/{uid}", json={
        "call_group_ids": [g_ids[1]],
    })
    assert r.status_code == 200
    assert r.json()["call_group_ids"] == [g_ids[1]]


@pytest.mark.asyncio
async def test_user_update_with_empty_call_groups_clears(
    admin_client: AsyncClient, db_session,
):
    g_ids = await _seed_groups(db_session, "Sales")
    r = await admin_client.post("/api/users", json={
        "username": "dave", "password": "shhh", "call_group_ids": g_ids,
    })
    uid = r.json()["id"]

    r = await admin_client.patch(f"/api/users/{uid}", json={"call_group_ids": []})
    assert r.json()["call_group_ids"] == []

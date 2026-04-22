import pytest
from httpx import AsyncClient
from sqlalchemy import select
from server.models import CallGroup, UserCallGroup, User


@pytest.mark.asyncio
async def test_list_empty(admin_client: AsyncClient):
    r = await admin_client.get("/api/call-groups")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_then_list(admin_client: AsyncClient):
    r = await admin_client.post("/api/call-groups", json={
        "name": "Sales", "description": "Sales team",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Sales"
    assert body["member_count"] == 0
    assert body["channel_count"] == 0

    r = await admin_client.get("/api/call-groups")
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_create_requires_admin(client: AsyncClient):
    r = await client.post("/api/call-groups", json={"name": "Sales"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_duplicate_name_rejected(admin_client: AsyncClient):
    await admin_client.post("/api/call-groups", json={"name": "Sales"})
    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_patch_renames(admin_client: AsyncClient):
    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]
    r = await admin_client.patch(f"/api/call-groups/{gid}", json={
        "name": "Sales EMEA", "description": "Europe team",
    })
    assert r.status_code == 200
    assert r.json()["name"] == "Sales EMEA"


@pytest.mark.asyncio
async def test_delete_cascades_join_rows(admin_client: AsyncClient, db_session):
    """Deleting a group removes its user_call_groups rows."""
    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]
    u = User(username="alice", mumble_password="x")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    db_session.add(UserCallGroup(user_id=u.id, call_group_id=gid))
    await db_session.commit()

    r = await admin_client.delete(f"/api/call-groups/{gid}")
    assert r.status_code == 204

    rows = (await db_session.execute(
        select(UserCallGroup).where(UserCallGroup.call_group_id == gid)
    )).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_put_members_replaces_wholesale(admin_client: AsyncClient, db_session):
    """PUT /members swaps the member set entirely."""
    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]

    u1 = User(username="alice", mumble_password="x")
    u2 = User(username="bob", mumble_password="x")
    u3 = User(username="carol", mumble_password="x")
    db_session.add_all([u1, u2, u3])
    await db_session.commit()
    for u in (u1, u2, u3):
        await db_session.refresh(u)

    # Initial: alice + bob.
    r = await admin_client.put(f"/api/call-groups/{gid}/members",
                                json={"user_ids": [u1.id, u2.id]})
    assert r.status_code == 200
    assert r.json()["member_count"] == 2

    # Replace with bob + carol.
    r = await admin_client.put(f"/api/call-groups/{gid}/members",
                                json={"user_ids": [u2.id, u3.id]})
    body = r.json()
    assert body["member_count"] == 2

    rows = (await db_session.execute(
        select(UserCallGroup).where(UserCallGroup.call_group_id == gid)
    )).scalars().all()
    user_ids = {row.user_id for row in rows}
    assert user_ids == {u2.id, u3.id}
    assert u1.id not in user_ids


@pytest.mark.asyncio
async def test_get_detail_includes_members_and_channels(
    admin_client: AsyncClient, db_session,
):
    from server.models import Channel
    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]

    u = User(username="alice", mumble_password="x")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    db_session.add(UserCallGroup(user_id=u.id, call_group_id=gid))
    db_session.add(Channel(name="SalesChan", call_group_id=gid))
    await db_session.commit()

    r = await admin_client.get(f"/api/call-groups/{gid}")
    body = r.json()
    assert body["name"] == "Sales"
    assert body["members"] == [{"id": u.id, "username": "alice"}]
    assert body["channels"][0]["name"] == "SalesChan"

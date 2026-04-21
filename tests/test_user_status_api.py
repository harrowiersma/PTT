import pytest
from httpx import AsyncClient
from sqlalchemy import select
from server.models import User, AuditLog


async def _make_user(db_session, name="charlie"):
    u = User(username=name, mumble_password="x")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest.mark.asyncio
async def test_get_status_returns_null_for_new_user(client: AsyncClient, db_session):
    await _make_user(db_session, "charlie")
    r = await client.get("/api/users/status?username=charlie")
    assert r.status_code == 200
    body = r.json()
    assert body["label"] is None
    assert body["effective_label"] == "offline"


@pytest.mark.asyncio
async def test_post_self_sets_label_and_writes_audit(client: AsyncClient, db_session):
    await _make_user(db_session, "charlie")
    r = await client.post("/api/users/status", json={"username": "charlie", "label": "busy"})
    assert r.status_code == 200
    assert r.json()["label"] == "busy"

    row = (await db_session.execute(select(User).where(User.username == "charlie"))).scalar_one()
    assert row.status_label == "busy"
    assert row.status_updated_at is not None

    audit = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.status_change")
    )).scalars().all()
    assert len(audit) == 1
    assert audit[0].target_id == "charlie"
    assert "\"source\": \"self\"" in audit[0].details
    assert "\"to\": \"busy\"" in audit[0].details


@pytest.mark.asyncio
async def test_post_rejects_unknown_label(client: AsyncClient, db_session):
    await _make_user(db_session, "charlie")
    r = await client.post("/api/users/status", json={"username": "charlie", "label": "zzz"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_post_audibility_only_updates_is_audible(client: AsyncClient, db_session):
    """Audibility can be posted alone — no label change, no audit row."""
    await _make_user(db_session, "charlie")
    r = await client.post("/api/users/status", json={"username": "charlie", "is_audible": False})
    assert r.status_code == 200
    assert r.json()["is_audible"] is False

    row = (await db_session.execute(select(User).where(User.username == "charlie"))).scalar_one()
    assert row.is_audible is False
    assert row.is_audible_updated_at is not None
    assert row.status_label is None

    audit = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.status_change")
    )).scalars().all()
    assert audit == []


@pytest.mark.asyncio
async def test_post_empty_body_rejected(client: AsyncClient, db_session):
    await _make_user(db_session, "charlie")
    r = await client.post("/api/users/status", json={"username": "charlie"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_post_label_and_audibility_together(client: AsyncClient, db_session):
    await _make_user(db_session, "charlie")
    r = await client.post("/api/users/status", json={
        "username": "charlie", "label": "busy", "is_audible": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "busy"
    assert body["is_audible"] is True


@pytest.mark.asyncio
async def test_post_404_for_missing_user(client: AsyncClient):
    r = await client.post("/api/users/status", json={"username": "nope", "label": "online"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_requires_admin(client: AsyncClient, db_session):
    u = await _make_user(db_session, "charlie")
    r = await client.patch(f"/api/users/{u.id}/status", json={"label": "online"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_patch_admin_sets_label_source_admin(admin_client: AsyncClient, db_session):
    u = await _make_user(db_session, "charlie")
    r = await admin_client.patch(f"/api/users/{u.id}/status", json={"label": "offline"})
    assert r.status_code == 200
    audit = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.status_change")
    )).scalars().all()
    assert audit[-1].admin_username == "testadmin"  # SPEC CORRECTION
    assert "\"source\": \"admin\"" in audit[-1].details

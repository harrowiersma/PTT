"""Tests for /api/admin/audit target_id filter (Task 8)."""

import pytest
from httpx import AsyncClient

from server.models import AuditLog


@pytest.mark.asyncio
async def test_audit_log_filters_by_target_id(admin_client: AsyncClient, db_session):
    """/api/admin/audit?target_id=X returns only rows matching target_id=X."""
    db_session.add_all([
        AuditLog(admin_username="testadmin", action="user.status_change",
                 target_type="user", target_id="alice", details="{}"),
        AuditLog(admin_username="testadmin", action="user.status_change",
                 target_type="user", target_id="bob", details="{}"),
        AuditLog(admin_username="testadmin", action="user.status_change",
                 target_type="user", target_id="alice", details="{}"),
    ])
    await db_session.commit()

    r = await admin_client.get("/api/admin/audit?target_id=alice&action=user.status_change")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(row["target_id"] == "alice" for row in rows)


@pytest.mark.asyncio
async def test_audit_log_without_target_id_returns_all(admin_client: AsyncClient, db_session):
    db_session.add_all([
        AuditLog(admin_username="testadmin", action="user.status_change",
                 target_type="user", target_id="alice", details="{}"),
        AuditLog(admin_username="testadmin", action="user.status_change",
                 target_type="user", target_id="bob", details="{}"),
    ])
    await db_session.commit()

    r = await admin_client.get("/api/admin/audit?action=user.status_change")
    assert r.status_code == 200
    assert len(r.json()) >= 2

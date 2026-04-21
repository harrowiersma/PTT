# User Status / Presence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship a three-state presence signal (Online / Busy / Offline) that radio users cycle with the orange top button, plus a device-audibility flag derived from Android's `AudioManager`. Dashboards display both. Dispatch respects Online+connected. Lone-worker shifts couple to status changes.

**Architecture:** Four columns on `users` (label + timestamp, audibility + timestamp); three HTTP endpoints (self GET/POST, admin PATCH), label writes audit-logged, audibility piggy-backs on the same POST and isn't audited; a `PYMUMBLE_CLBK_USERCREATED` hook in `MurmurClient` promotes to Online on connect; `find_nearest` gains an `online AND connected` predicate; dashboard gets a pill column on two tables, a muted icon next to the pill when audibility is false, and a dropdown in the user-edit modal; the Android app intercepts `KEYCODE_F4` (orange button) in `MumlaActivity.dispatchKeyEvent`, cycles state, speaks the new value over TTS, ships audibility alongside every status POST, and shows a pill (with optional muted icon) on the carousel.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (backend), pymumble 1.6.1 (Murmur integration), vanilla HTML/CSS/JS (dashboard), Java + Android (openPTT-app).

**Companion design doc:** `docs/plans/2026-04-21-user-status-presence-design.md`

---

## Pre-flight — bring up the pytest sidecar

All pytest runs below go through the long-lived `ptt-pytest` container (system Python is 3.9, the code needs 3.11):

```bash
docker run -d --name ptt-pytest -v $PWD:/app -w /app \
  python:3.11-slim sleep infinity
docker exec ptt-pytest sh -c "apt-get update -qq && \
  apt-get install -y -qq libopus0 libsndfile1 curl && \
  pip install -q -r requirements-test.txt"
```

Verify green baseline before starting Task 1:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -v"
```

Expected: all existing tests PASS.

---

## Phases

1. **Task 1** — `users.status_label` + `users.status_updated_at` columns + migration.
2. **Task 2** — Status endpoints (GET, POST self, PATCH admin) + audit writes.
3. **Task 3** — `/api/loneworker/shift/start` force-sets Online when lone-worker applies.
4. **Task 4** — Picking Offline auto-ends an active shift (same gate).
5. **Task 5** — `PYMUMBLE_CLBK_USERCREATED` hook in `MurmurClient` → auto-Online.
6. **Task 6** — `/api/dispatch/nearest` filters to `online AND connected`.
7. **Task 7** — Dashboard: status pill column in Live Ops + Directory.
8. **Task 8** — Dashboard: user-edit modal Status dropdown + "last changed" line.
9. **Task 9** — openPTT-app: intercept `KEYCODE_F4` in `MumlaActivity`, cycle + POST + TTS.
10. **Task 10** — openPTT-app: carousel status pill + hydrate on connect / resume.
11. **Task 11** — Deploy to `ptt.harro.ch` + manual verification on both P50s.

Each task is independently committable. Hard-stop after Task 11 to confirm prod + radios are healthy.

---

## Task 1: `users` status + audibility columns + migration

**Files:**
- Modify: `server/models.py` (append columns to `User`)
- Create: `server/alembic/versions/f2a9c3b7e4d1_user_status.py`
- Test: `tests/test_user_status_model.py`

**Step 1: Write the failing test**

```python
# tests/test_user_status_model.py
import pytest
from sqlalchemy import select
from server.models import User


@pytest.mark.asyncio
async def test_user_has_status_columns(db_session):
    """All four new columns exist and default to NULL for existing rows."""
    u = User(username="alice", mumble_password="x")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    assert u.status_label is None
    assert u.status_updated_at is None
    assert u.is_audible is None
    assert u.is_audible_updated_at is None


@pytest.mark.asyncio
async def test_user_status_label_accepts_values(db_session):
    u = User(
        username="bob", mumble_password="x",
        status_label="online", is_audible=True,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    assert u.status_label == "online"
    assert u.is_audible is True
```

**Step 2: Run test, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_user_status_model.py -v"
```

Expected: `AttributeError: 'User' object has no attribute 'status_label'`.

**Step 3: Add the columns**

In `server/models.py`, extend the `User` class — append after the existing `last_seen` column (line ~65):

```python
    # Presence intent. NULL = never-set (treated as 'offline').
    # Values: 'online' | 'busy' | 'offline'.
    status_label: Mapped[str] = mapped_column(String(16), nullable=True)
    status_updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Device audibility — piggy-backs on status POSTs from the app.
    # True = ringer normal + voice-call stream volume > 0; False = muted/silent;
    # NULL = never reported.
    is_audible: Mapped[bool] = mapped_column(Boolean, nullable=True)
    is_audible_updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

**Step 4: Create the Alembic migration**

Confirm current head:

```bash
docker compose up -d admin
docker exec ptt-admin-1 alembic current
```

Expected: `e1c4f8a3b5d6` (dispatch settings).

Create `server/alembic/versions/f2a9c3b7e4d1_user_status.py`:

```python
"""user status + audibility columns

Revision ID: f2a9c3b7e4d1
Revises: e1c4f8a3b5d6
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a9c3b7e4d1"
down_revision = "e1c4f8a3b5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("status_label", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("is_audible", sa.Boolean, nullable=True))
    op.add_column("users", sa.Column("is_audible_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "is_audible_updated_at")
    op.drop_column("users", "is_audible")
    op.drop_column("users", "status_updated_at")
    op.drop_column("users", "status_label")
```

**Step 5: Apply migration + re-run tests**

```bash
docker compose up -d --build admin
docker exec ptt-admin-1 alembic upgrade head
docker exec ptt-admin-1 alembic current  # expect f2a9c3b7e4d1
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_user_status_model.py -v"
```

Expected: PASS (2/2).

**Step 6: Commit**

```bash
git add server/models.py server/alembic/versions/f2a9c3b7e4d1_user_status.py tests/test_user_status_model.py
git commit -m "status: users.status_label + audibility columns"
```

---

## Task 2: Status endpoints (GET self, POST self, PATCH admin) + audit

**Files:**
- Create: `server/api/user_status.py`
- Modify: `server/main.py` (register router)
- Modify: `server/api/admin.py` (export `log_audit` if not already module-scoped — it already is)
- Test: `tests/test_user_status_api.py`

**Step 1: Write the failing test**

```python
# tests/test_user_status_api.py
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

    # Verify persisted
    row = (await db_session.execute(select(User).where(User.username == "charlie"))).scalar_one()
    assert row.status_label == "busy"
    assert row.status_updated_at is not None

    # Audit row written with source='self'
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
    assert row.status_label is None  # label untouched

    audit = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.status_change")
    )).scalars().all()
    assert audit == []  # audibility is not audited


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
    assert audit[-1].admin_username == "admin"
    assert "\"source\": \"admin\"" in audit[-1].details
```

**Step 2: Run test, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_user_status_api.py -v"
```

Expected: 404s / attribute errors everywhere.

**Step 3: Implement the router**

Create `server/api/user_status.py`:

```python
"""Three-state presence signal for radio users.

- GET /api/users/status?username=X   — read (no auth; dashboard + app use it).
- POST /api/users/status              — self set (device-trusted, no auth).
- PATCH /api/users/{id}/status        — admin override.

Every write path records an AuditLog entry with the actor + source so the
operator can see who changed what. Shift coupling and Murmur connect hooks
all funnel through `set_status()` below.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.admin import log_audit
from server.auth import get_current_admin
from server.database import get_db
from server.models import AuditLog, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["user-status"])


ALLOWED = ("online", "busy", "offline")
StatusLabel = Literal["online", "busy", "offline"]


class StatusBody(BaseModel):
    username: str
    label: StatusLabel | None = None
    is_audible: bool | None = None


class AdminStatusBody(BaseModel):
    label: StatusLabel


class StatusResponse(BaseModel):
    username: str
    label: str | None
    updated_at: datetime | None
    effective_label: str  # 'offline' if not mumble-connected
    is_audible: bool | None
    is_audible_updated_at: datetime | None


async def _effective(user: User) -> str:
    """Collapse stored label + live connection state into what the UI shows."""
    from server.murmur.client import murmur_client  # lazy import to avoid circular
    connected = False
    try:
        if murmur_client and murmur_client.has_mumble:
            connected = any(
                u["name"].lower() == user.username.lower()
                for u in murmur_client._mumble.users.values()
            )
    except Exception:
        connected = False
    if not connected:
        return "offline"
    return user.status_label or "offline"


async def set_status(
    db: AsyncSession, user: User, new_label: str, *,
    actor: str, source: str,
) -> User:
    """Shared status-write path used by all three endpoints + the Murmur
    auto-connect hook + shift coupling. Writes the audit row on the same
    transaction as the column update so either both land or neither does."""
    if new_label not in ALLOWED:
        raise HTTPException(status_code=422, detail=f"label must be one of {ALLOWED}")
    old = user.status_label
    if old == new_label:
        return user  # no-op; don't spam audit log
    user.status_label = new_label
    user.status_updated_at = datetime.now(timezone.utc)
    await log_audit(
        db, actor, "user.status_change",
        target_type="user", target_id=user.username,
        details=json.dumps({"from": old, "to": new_label, "source": source}),
    )
    await db.commit()
    await db.refresh(user)
    logger.info("status: %s %s -> %s (actor=%s, source=%s)",
                user.username, old, new_label, actor, source)
    return user


@router.get("/status", response_model=StatusResponse)
async def get_status(username: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return StatusResponse(
        username=row.username,
        label=row.status_label,
        updated_at=row.status_updated_at,
        effective_label=await _effective(row),
        is_audible=row.is_audible,
        is_audible_updated_at=row.is_audible_updated_at,
    )


@router.post("/status", response_model=StatusResponse)
async def post_status(body: StatusBody, db: AsyncSession = Depends(get_db)):
    if body.label is None and body.is_audible is None:
        raise HTTPException(status_code=422, detail="Must supply at least one of: label, is_audible")
    row = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    if body.label is not None:
        row = await set_status(db, row, body.label, actor=body.username, source="self")
    if body.is_audible is not None:
        # Audibility is high-churn + low-value → no audit row, direct write.
        row.is_audible = body.is_audible
        row.is_audible_updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
    # Shift coupling on Offline — handled in Task 4.
    return StatusResponse(
        username=row.username,
        label=row.status_label,
        updated_at=row.status_updated_at,
        effective_label=await _effective(row),
        is_audible=row.is_audible,
        is_audible_updated_at=row.is_audible_updated_at,
    )


@router.patch("/{user_id}/status", response_model=StatusResponse)
async def patch_status(
    user_id: int, body: AdminStatusBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    row = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    row = await set_status(db, row, body.label, actor=admin["sub"], source="admin")
    return StatusResponse(
        username=row.username,
        label=row.status_label,
        updated_at=row.status_updated_at,
        effective_label=await _effective(row),
        is_audible=row.is_audible,
        is_audible_updated_at=row.is_audible_updated_at,
    )
```

**Step 4: Register the router**

In `server/main.py`, add to the imports near the other `server.api.*` imports:

```python
from server.api.user_status import router as user_status_router
```

And register it alongside the other routers:

```python
app.include_router(user_status_router)
```

**Step 5: Run tests**

```bash
docker compose up -d --build admin
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_user_status_api.py -v"
```

Expected: PASS (6/6).

**Step 6: Commit**

```bash
git add server/api/user_status.py server/main.py tests/test_user_status_api.py
git commit -m "status: GET/POST self + PATCH admin endpoints with audit"
```

---

## Task 3: `/shift/start` force-sets Online when lone-worker applies

**Files:**
- Modify: `server/api/loneworker.py` (`shift_start`)
- Test: `tests/test_user_status_shift_coupling.py` (create; Task 4 adds the reverse case)

**Step 1: Write the failing test**

```python
# tests/test_user_status_shift_coupling.py
import pytest
from sqlalchemy import select
from httpx import AsyncClient
from server.models import FeatureFlag, User


async def _enable_lw(db_session):
    row = (await db_session.execute(
        select(FeatureFlag).where(FeatureFlag.key == "lone_worker")
    )).scalar_one()
    row.enabled = True
    await db_session.commit()


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
    # Feature flag defaults enabled in the fixture; disable here.
    lw = (await db_session.execute(
        select(FeatureFlag).where(FeatureFlag.key == "lone_worker")
    )).scalar_one()
    lw.enabled = False
    await db_session.commit()
    u = await _make_lone_worker(db_session, "wanda")
    u.status_label = "busy"
    await db_session.commit()

    r = await client.post("/api/loneworker/shift/start", json={"username": "wanda"})
    # Feature-gated router returns 503 when the flag is off.
    assert r.status_code == 503

    await db_session.refresh(u)
    assert u.status_label == "busy"  # untouched
```

**Step 2: Run test, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_user_status_shift_coupling.py -v"
```

Expected: first test fails — `status_label` stays `"busy"` because nothing sets it.

**Step 3: Wire the side-effect**

In `server/api/loneworker.py`, at the very bottom of `shift_start` (just before `return _shift_dict(shift, user)` on the success path), add:

```python
    # Status coupling (design §Decisions #5): if user is a lone worker and the
    # feature is enabled, a shift start force-sets status to Online. The
    # feature-enabled gate is implicit — requires_feature("lone_worker") on
    # the router already 503s when disabled.
    if user.is_lone_worker:
        from server.api.user_status import set_status
        await set_status(db, user, "online", actor="system", source="shift_start")
```

**Step 4: Run tests**

```bash
docker compose up -d --build admin
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_user_status_shift_coupling.py::test_shift_start_forces_online \
  tests/test_user_status_shift_coupling.py::test_shift_start_without_lone_worker_feature_is_noop -v"
```

Expected: PASS (2/2).

**Step 5: Commit**

```bash
git add server/api/loneworker.py tests/test_user_status_shift_coupling.py
git commit -m "status: shift start force-sets Online (lone-worker gate)"
```

---

## Task 4: Picking Offline ends an active shift (same gate)

**Files:**
- Modify: `server/api/user_status.py` (extend `post_status` + `patch_status`)
- Modify: `tests/test_user_status_shift_coupling.py` (append tests)

**Step 1: Write the failing tests**

Append to `tests/test_user_status_shift_coupling.py`:

```python
@pytest.mark.asyncio
async def test_offline_ends_active_shift(client: AsyncClient, db_session):
    await _enable_lw(db_session)
    u = await _make_lone_worker(db_session, "wanda")

    r = await client.post("/api/loneworker/shift/start", json={"username": "wanda"})
    assert r.status_code == 200
    shift_id = r.json()["id"]

    r = await client.post("/api/users/status", json={"username": "wanda", "label": "offline"})
    assert r.status_code == 200

    # Shift should be ended.
    from server.models import LoneWorkerShift
    shift = (await db_session.execute(
        select(LoneWorkerShift).where(LoneWorkerShift.id == shift_id)
    )).scalar_one()
    assert shift.ended_at is not None
    assert shift.end_reason == "user_offline"


@pytest.mark.asyncio
async def test_offline_without_lone_worker_feature_leaves_shift_alone(
    client: AsyncClient, db_session,
):
    # Start a shift while lone-worker is on, THEN flip the flag off and mark Offline.
    await _enable_lw(db_session)
    u = await _make_lone_worker(db_session, "wanda")
    r = await client.post("/api/loneworker/shift/start", json={"username": "wanda"})
    shift_id = r.json()["id"]

    lw = (await db_session.execute(
        select(FeatureFlag).where(FeatureFlag.key == "lone_worker")
    )).scalar_one()
    lw.enabled = False
    await db_session.commit()

    r = await client.post("/api/users/status", json={"username": "wanda", "label": "offline"})
    assert r.status_code == 200

    from server.models import LoneWorkerShift
    shift = (await db_session.execute(
        select(LoneWorkerShift).where(LoneWorkerShift.id == shift_id)
    )).scalar_one()
    assert shift.ended_at is None  # untouched


@pytest.mark.asyncio
async def test_admin_patch_offline_also_ends_shift(
    admin_client: AsyncClient, client: AsyncClient, db_session,
):
    await _enable_lw(db_session)
    u = await _make_lone_worker(db_session, "wanda")
    await client.post("/api/loneworker/shift/start", json={"username": "wanda"})

    r = await admin_client.patch(f"/api/users/{u.id}/status", json={"label": "offline"})
    assert r.status_code == 200

    from server.models import LoneWorkerShift
    shift = (await db_session.execute(
        select(LoneWorkerShift).where(LoneWorkerShift.user_id == u.id)
    )).scalar_one()
    assert shift.ended_at is not None
    assert shift.end_reason == "user_offline"
```

**Step 2: Run tests, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_user_status_shift_coupling.py -v"
```

Expected: the three new tests fail — the shift never ends.

**Step 3: Add the coupling helper + wire it in**

In `server/api/user_status.py`, add a helper just above `post_status`:

```python
async def _maybe_end_shift_on_offline(db: AsyncSession, user: User) -> None:
    """If lone-worker feature is enabled AND this user is a lone worker AND
    they have an active shift, end it with reason='user_offline'. No-op otherwise."""
    if not user.is_lone_worker:
        return
    # Feature-flag check — read from FeatureFlag directly (dispatch reads it
    # the same way from within a handler).
    from server.models import FeatureFlag, LoneWorkerShift
    flag = (await db.execute(
        select(FeatureFlag).where(FeatureFlag.key == "lone_worker")
    )).scalar_one_or_none()
    if flag is None or not flag.enabled:
        return
    shift = (await db.execute(
        select(LoneWorkerShift).where(
            LoneWorkerShift.user_id == user.id,
            LoneWorkerShift.ended_at.is_(None),
        )
    )).scalar_one_or_none()
    if shift is None:
        return
    shift.ended_at = datetime.now(timezone.utc)
    shift.end_reason = "user_offline"
    await log_audit(
        db, user.username, "shift.stop",
        target_type="user", target_id=user.username,
        details=json.dumps({"reason": "user_offline"}),
    )
    await db.commit()
    await db.refresh(shift)
```

In `post_status`, replace the `# Shift coupling on Offline — handled in Task 4.` comment with:

```python
    if row.status_label == "offline":
        await _maybe_end_shift_on_offline(db, row)
```

In `patch_status`, add the same call after the `set_status(...)` call:

```python
    if row.status_label == "offline":
        await _maybe_end_shift_on_offline(db, row)
```

**Step 4: Run tests**

```bash
docker compose up -d --build admin
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_user_status_shift_coupling.py -v"
```

Expected: PASS (5/5 total).

**Step 5: Commit**

```bash
git add server/api/user_status.py tests/test_user_status_shift_coupling.py
git commit -m "status: Offline ends active shift (lone-worker gate)"
```

---

## Task 5: `PYMUMBLE_CLBK_USERCREATED` auto-Online hook

**Files:**
- Modify: `server/murmur/client.py` (add callback + handler)
- Test: `tests/test_murmur_auto_online.py`

**Step 1: Write the failing test**

```python
# tests/test_murmur_auto_online.py
import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import select
from server.models import User


@pytest.mark.asyncio
async def test_user_created_callback_sets_online(db_session):
    """Simulate USERCREATED on a DB-registered user; verify status flips to online."""
    u = User(username="harry", mumble_password="x")
    db_session.add(u)
    await db_session.commit()

    from server.murmur.client import _on_user_created_sync

    # Fake pymumble event: dict-like with 'name' key.
    event = {"name": "harry"}
    _on_user_created_sync(event)  # sync path used by pymumble thread

    await db_session.refresh(u)
    assert u.status_label == "online"


@pytest.mark.asyncio
async def test_user_created_skips_bot_usernames(db_session):
    from server.murmur.client import _on_user_created_sync

    # Bot users must be ignored — no DB work, no errors.
    for name in ("PTTAdmin", "PTTWeather", "PTTPhone-1"):
        _on_user_created_sync({"name": name})


@pytest.mark.asyncio
async def test_user_created_ignores_unknown_username(db_session):
    from server.murmur.client import _on_user_created_sync
    # Username not in DB — must no-op, not raise.
    _on_user_created_sync({"name": "ghost"})
```

**Step 2: Run tests, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_murmur_auto_online.py -v"
```

Expected: `ImportError` — `_on_user_created_sync` doesn't exist.

**Step 3: Implement the hook**

In `server/murmur/client.py`:

(a) Near the top with other imports, add:

```python
import json
from datetime import datetime, timezone
```

(b) Register the callback inside `connect()`, right after the existing `PYMUMBLE_CLBK_USERUPDATED` registration (around line 117):

```python
            self._mumble.callbacks.set_callback(
                pymumble.constants.PYMUMBLE_CLBK_USERCREATED,
                lambda user: _on_user_created_sync(user),
            )
```

(c) Add the sync handler at module scope (bottom of the file):

```python
def _on_user_created_sync(event) -> None:
    """PYMUMBLE_CLBK_USERCREATED fires on pymumble's sync thread whenever a
    user joins the server (including reconnects). Promote the matching DB
    user to status_label='online'. Bots are skipped; unknown usernames no-op.

    Uses a short-lived sync engine so we don't fight the async main loop
    (same pattern as loneworker._run_shift_cycle).
    """
    try:
        name = event.get("name") if isinstance(event, dict) else getattr(event, "name", None)
    except Exception:
        return
    if not name or _is_bot_username(name):
        return

    try:
        from sqlalchemy import create_engine, select as _select
        from sqlalchemy.orm import sessionmaker
        from server.config import settings
        from server.models import AuditLog, User

        engine = create_engine(settings.database_url_sync, echo=False)
        Session = sessionmaker(engine, expire_on_commit=False)
        with Session() as db:
            user = db.execute(_select(User).where(User.username == name)).scalar_one_or_none()
            if user is None:
                return
            if user.status_label == "online":
                return
            old = user.status_label
            user.status_label = "online"
            user.status_updated_at = datetime.now(timezone.utc)
            db.add(AuditLog(
                admin_username="system",
                action="user.status_change",
                target_type="user", target_id=user.username,
                details=json.dumps({"from": old, "to": "online", "source": "auto_connect"}),
            ))
            db.commit()
            logger.info("status: %s auto-online on connect", user.username)
        engine.dispose()
    except Exception as e:
        logger.error("auto-online hook failed for %s: %s", name, e)
```

**Step 4: Run tests**

```bash
docker compose up -d --build admin
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_murmur_auto_online.py -v"
```

Expected: PASS (3/3).

**Step 5: Commit**

```bash
git add server/murmur/client.py tests/test_murmur_auto_online.py
git commit -m "status: auto-Online on Mumble connect (USERCREATED hook)"
```

---

## Task 6: `/api/dispatch/nearest` filters to `online AND connected`

**Files:**
- Modify: `server/api/dispatch.py` (`find_nearest`)
- Test: `tests/test_dispatch_filter_status.py`

**Step 1: Write the failing test**

```python
# tests/test_dispatch_filter_status.py
import pytest
from unittest.mock import MagicMock, patch
from httpx import AsyncClient
from server.models import User


class _FakePos:
    def __init__(self, device_id, name, lat, lng):
        self.device_id = device_id
        self.device_name = name
        self.latitude = lat
        self.longitude = lng
        self.timestamp = "2026-04-21T12:00:00Z"
        self.accuracy = 5
        self.battery = 90


def _positions():
    base = (38.72, -9.14)
    return [
        _FakePos(1, "alice", base[0] + 0.001, base[1]),
        _FakePos(2, "bob", base[0] + 0.002, base[1]),
        _FakePos(3, "carol", base[0] + 0.003, base[1]),
    ]


async def _seed(db_session, label_by_name):
    for name, label in label_by_name.items():
        u = User(username=name, mumble_password="x", status_label=label)
        db_session.add(u)
    await db_session.commit()


@pytest.mark.asyncio
async def test_nearest_excludes_busy_and_offline(
    admin_client: AsyncClient, db_session,
):
    await _seed(db_session, {"alice": "online", "bob": "busy", "carol": "offline"})
    # Fake all three as Mumble-connected.
    with patch("server.api.dispatch.TraccarClient") as MockTC, \
         patch("server.api.dispatch._connected_usernames",
               return_value={"alice", "bob", "carol"}):
        MockTC.return_value.get_positions.return_value = _positions()
        from server.traccar_client import TraccarClient as RealTC
        MockTC.haversine_distance = staticmethod(RealTC.haversine_distance)
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    assert r.status_code == 200
    names = [w["username"] for w in r.json()]
    assert names == ["alice"]


@pytest.mark.asyncio
async def test_nearest_excludes_online_but_disconnected(
    admin_client: AsyncClient, db_session,
):
    await _seed(db_session, {"alice": "online", "bob": "online"})
    with patch("server.api.dispatch.TraccarClient") as MockTC, \
         patch("server.api.dispatch._connected_usernames", return_value={"bob"}):
        MockTC.return_value.get_positions.return_value = _positions()[:2]
        from server.traccar_client import TraccarClient as RealTC
        MockTC.haversine_distance = staticmethod(RealTC.haversine_distance)
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    names = [w["username"] for w in r.json()]
    assert names == ["bob"]


@pytest.mark.asyncio
async def test_nearest_excludes_null_status(
    admin_client: AsyncClient, db_session,
):
    await _seed(db_session, {"alice": None, "bob": "online"})
    with patch("server.api.dispatch.TraccarClient") as MockTC, \
         patch("server.api.dispatch._connected_usernames",
               return_value={"alice", "bob"}):
        MockTC.return_value.get_positions.return_value = _positions()[:2]
        from server.traccar_client import TraccarClient as RealTC
        MockTC.haversine_distance = staticmethod(RealTC.haversine_distance)
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    names = [w["username"] for w in r.json()]
    assert names == ["bob"]
```

**Step 2: Run tests, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_dispatch_filter_status.py -v"
```

Expected: at least two tests fail — Busy/Offline/NULL still appear in the output.

**Step 3: Extend `find_nearest`**

In `server/api/dispatch.py`, add this helper at module scope (above `find_nearest`):

```python
def _connected_usernames() -> set[str]:
    """Lowercased set of Mumble-connected human usernames. Patchable in tests."""
    try:
        from server.murmur.client import murmur_client, BOT_USERNAMES
        if not murmur_client or not murmur_client.has_mumble:
            return set()
        return {
            u["name"].lower()
            for u in murmur_client._mumble.users.values()
            if u["name"] not in BOT_USERNAMES
        }
    except Exception:
        return set()
```

Modify `find_nearest` — extend the existing user-map query to also pull `status_label`, and add the two-part filter:

```python
    # Pull usernames + status_label keyed by lowercase-username and by
    # linked traccar_device_id. Both paths feed the same filter below.
    result = await db.execute(select(User))
    db_users = result.scalars().all()
    device_to_username = {u.traccar_device_id: u.username for u in db_users if u.traccar_device_id}
    status_by_username = {u.username.lower(): u.status_label for u in db_users}

    connected = _connected_usernames()
    radius = settings["search_radius_m"]
    results = []
    for p in positions:
        if p.latitude == 0 and p.longitude == 0:
            continue
        username = device_to_username.get(p.device_id, p.device_name)
        # Online + connected is the dispatch predicate (design §Dispatch filter).
        if username.lower() not in connected:
            continue
        if status_by_username.get(username.lower()) != "online":
            continue
        distance = TraccarClient.haversine_distance(lat, lng, p.latitude, p.longitude)
        if radius is not None and distance > radius:
            continue
        results.append({
            "username": username,
            "distance_m": round(distance),
            "latitude": p.latitude,
            "longitude": p.longitude,
            "timestamp": p.timestamp,
        })
    results.sort(key=lambda x: x["distance_m"])
    return results[: settings["max_workers"]]
```

**Step 4: Run tests**

```bash
docker compose up -d --build admin
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_dispatch_filter_status.py \
                  tests/test_dispatch_nearest_settings.py -v"
```

Expected: PASS (all). The existing nearest-settings tests already patch `TraccarClient` and populate rows with `status_label='online'` via the test helpers — if they fail, update those fixtures to also set `status_label="online"` and mock `_connected_usernames` to include the test usernames.

**Step 5: Commit**

```bash
git add server/api/dispatch.py tests/test_dispatch_filter_status.py
git commit -m "dispatch: /nearest excludes busy/offline/disconnected workers"
```

---

## Task 7: Dashboard — status pill column in Live Ops + Directory

**Files:**
- Modify: `server/dashboard/index.html`

**Step 1: Add a `renderStatusPill(label, connected)` helper**

Near the other render helpers in the `<script>` block (search for `function esc(` — add directly below it):

```js
function renderStatusPill(label, connected, isAudible) {
    // Effective-label rule: disconnected users always show Offline
    // regardless of stored label. Null stored label also = offline.
    const eff = !connected ? 'offline' : (label || 'offline');
    const map = {
        online:  { dot: 'var(--color-success)',  text: 'Online'  },
        busy:    { dot: 'var(--amber, #FFBF00)', text: 'Busy'    },
        offline: { dot: 'var(--color-muted, #999)', text: 'Offline' },
    };
    const s = map[eff] || map.offline;
    const mutedIcon = isAudible === false
        ? ' <span class="muted-icon" title="Device muted — TTS and ring won\'t be audible" aria-label="muted">&#128263;</span>'
        : '';
    return '<span class="status-pill" title="Stored: ' + (label || 'none') + '">' +
           '<span class="status-dot" style="background:' + s.dot + '"></span>' +
           s.text + '</span>' + mutedIcon;
}
```

Add the matching CSS in the `<style>` block near the other `.badge`/`.dot` rules:

```css
.status-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 2px 8px; border-radius: 999px;
    background: var(--surface-sunk, rgba(0,0,0,0.05));
    font-size: 0.8125rem; font-family: var(--font-data);
    border: 1px solid var(--border);
}
.status-dot { width: 6px; height: 6px; border-radius: 50%; }
.muted-icon { font-size: 0.875rem; color: var(--color-danger, #c33); margin-left: 4px; }
```

**Step 2: Add the Status column to the Live Ops online table**

Find the `<thead>` of `#onlineTable` (around line 815 — search for the `<th>Username</th>` row directly above `#onlineTable`). Insert a new `<th>Status</th>` cell between Username and Channel:

```html
<tr>
    <th>Username</th>
    <th>Status</th>     <!-- NEW -->
    <th>Channel</th>
    <!-- ...rest unchanged -->
</tr>
```

Update `renderOverview` (around line 1768 — the `data.users.forEach(u => { ... })` loop). Insert the pill cell between the username `<td>` and the channel `<td>`:

```js
rows.push('<tr>' +
    '<td><strong>' + esc(u.username) + '</strong>' + muteIcon + '</td>' +
    '<td>' + renderStatusPill(u.status_label || null, true, u.is_audible) + '</td>' +  // NEW
    '<td>' + esc(u.channel_name || 'Root') + '</td>' +
    // ...rest unchanged
    '</tr>');
```

And in the offline-users branch (the `allUsers.forEach` loop at line 1787), insert the pill cell too, always with `connected=false`:

```js
rows.push('<tr style="opacity:0.6">' +
    '<td>' + esc(u.username) + '</td>' +
    '<td>' + renderStatusPill(u.status_label || null, false, u.is_audible) + '</td>' +  // NEW (disconnected: muted icon still shown if last-reported false)
    '<td>\u2014</td>' +
    // ...rest unchanged
    '</tr>');
```

**Step 3: Update the colspan on empty-state rows**

Search for `colspan="6"` on the online table and bump to `colspan="7"` (the two empty-state `<tr>` rows around lines 1762 and 1818). Same for `#usersTable` in the next step.

**Step 4: Add the Status column to Directory → Users**

In the `<thead>` of `#usersTable` (around line 940 — search for `<th>Username</th>` directly above `#usersTable`), add `<th>Status</th>` between Display Name and Linked Device.

In `renderUsers` (around line 1820), insert the pill cell:

```js
tbody.innerHTML = users.map(u => '<tr>' +
    '<td>' + esc(u.username) + '</td>' +
    '<td>' + esc(u.display_name || '\u2014') + '</td>' +
    '<td>' + renderStatusPill(u.status_label || null, false /* list has no live presence info */, u.is_audible) + '</td>' +  // NEW
    '<td>' + (u.traccar_device_id ? ... ) + '</td>' +
    // ...rest unchanged
    '</tr>').join('');
```

Note: the Directory user list doesn't have live Mumble-connection info. The pill shows the stored label with `connected=false`, so it collapses to Offline visually when stored is NULL — matching what the Overview would show for disconnected users. That's fine; the Overview is the live view.

**Step 5: Verify `/api/users` returns `status_label` + `is_audible`**

Check `server/api/users.py` — the `UserResponse` Pydantic model probably uses `model_config = {"from_attributes": True}`. If so, both new fields are serialised once the columns exist. If not, add:

```python
class UserResponse(BaseModel):
    # ...existing fields
    status_label: str | None = None
    is_audible: bool | None = None
```

Also ensure the Overview's `data.users.forEach(u => ...)` gets `u.status_label` and `u.is_audible`. The server-side `_status.users` is assembled in `server/api/status.py` — it currently includes `username`, `channel_name`, `latitude`, etc. Add both fields to that response, pulled from the DB: inside the existing user-list loop, join against `users` (or reuse the same per-username lookup already used for battery / GPS) to populate `status_label` and `is_audible`.

**Step 6: Manual smoke**

```bash
docker compose up -d --build admin
# Open http://localhost:8000/dashboard/  and log in.
# Verify: Live Ops → Overview has a Status column with pills.
# Verify: Directory → Users has a Status column.
```

**Step 7: Commit**

```bash
git add server/dashboard/index.html server/api/status.py server/api/users.py
git commit -m "dashboard: status pill + muted icon in Live Ops + Directory tables"
```

---

## Task 8: Dashboard — user-edit modal Status dropdown + "last changed" line

**Files:**
- Modify: `server/dashboard/index.html`

**Step 1: Add the Status dropdown to the user-edit modal**

Find the `#editUserModal` block (search for `id="editUserModal"`). Insert this form group just above the "Can answer calls" toggle (search for `can_answer_calls` in the modal):

```html
<label style="display:block;margin-top:var(--space-md)">Status
    <select id="editUserStatus" style="width:100%">
        <option value="">— Unset —</option>
        <option value="online">Online</option>
        <option value="busy">Busy</option>
        <option value="offline">Offline</option>
    </select>
    <div id="editUserStatusMeta" style="font-size:0.75rem;color:var(--fg-muted);margin-top:4px"></div>
</label>
```

**Step 2: Populate the dropdown on modal open**

Find `openEditUser(...)` (around line 1828 — the function triggered from the Edit button). Extend the signature to accept `status_label` and `status_updated_at` (or fetch it fresh):

Simplest: inside `openEditUser`, after setting the other fields, fetch current status:

```js
// Populate status dropdown + "last changed" line.
fetch('/api/users/status?username=' + encodeURIComponent(username))
    .then(r => r.ok ? r.json() : null)
    .then(data => {
        document.getElementById('editUserStatus').value = data?.label || '';
        const meta = document.getElementById('editUserStatusMeta');
        if (data?.updated_at) {
            meta.textContent = 'Last changed ' + timeAgo(new Date(data.updated_at));
            // Fetch most-recent audit entry for a richer label.
            fetch('/api/admin/audit-log?target_id=' + encodeURIComponent(username) + '&action=user.status_change&limit=1')
                .then(r => r.ok ? r.json() : null)
                .then(audit => {
                    if (audit && audit.length) {
                        const a = audit[0];
                        try {
                            const details = JSON.parse(a.details || '{}');
                            meta.textContent = 'Last changed ' + timeAgo(new Date(a.timestamp)) +
                                ' by ' + a.admin_username + ' (' + (details.source || 'unknown') + ')';
                        } catch (e) { /* keep the plain timestamp */ }
                    }
                });
        } else {
            meta.textContent = 'Never set';
        }
    });
```

**Step 3: Wire the Save button to PATCH status**

Find the user-edit save handler (`saveEditUser` or similar). After the existing user PATCH succeeds, if the dropdown value differs from what was fetched, call:

```js
const newStatus = document.getElementById('editUserStatus').value;
if (newStatus) {
    await api('/api/users/' + userId + '/status', {
        method: 'PATCH',
        body: JSON.stringify({ label: newStatus }),
    });
}
```

Only PATCH if `newStatus` is one of the three allowed labels (empty string = skip; the dashboard can't "unset" a label via this path — deliberate, since NULL is only meaningful for brand-new users).

**Step 4: Verify `/api/admin/audit-log` accepts the filter params**

Check `server/api/admin.py::get_audit_log`. If it already accepts `target_id` + `action` + `limit` query params, done. If not, extend:

```python
@router.get("/audit-log", response_model=list[AuditLogResponse])
async def get_audit_log(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
    target_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
):
    q = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
    if target_id:
        q = q.where(AuditLog.target_id == target_id)
    if action:
        q = q.where(AuditLog.action == action)
    result = await db.execute(q)
    return result.scalars().all()
```

**Step 5: Manual smoke**

```bash
docker compose up -d --build admin
# Open a user's edit modal. Verify:
# - Status dropdown pre-selects the stored label (or "— Unset —").
# - "Last changed ..." line appears (or "Never set").
# - Changing the dropdown + Save → reload list → pill reflects the new state.
# - An audit row appears in System → Audit Log with action='user.status_change'.
```

**Step 6: Commit**

```bash
git add server/dashboard/index.html server/api/admin.py
git commit -m "dashboard: user-edit modal Status dropdown + audit-sourced history line"
```

---

## Task 9: openPTT-app — intercept `KEYCODE_F4`, cycle + POST + TTS

**Files:**
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/app/MumlaActivity.java` (dispatchKeyEvent + cycleStatus helper)
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/service/MumlaService.java` (add a `postStatus()` API call helper, parallel to the existing `postSipAnswered()` / `checkIn()` HTTP helpers)

**Step 1: Add `postStatus()` to `MumlaService`**

Find the existing HTTP helpers in `MumlaService.java` (grep for `sip/answered` or `/api/loneworker/checkin`). Below whichever one you land on, add:

```java
/**
 * POST /api/users/status. Either field may be null but not both.
 * - label != null: advance the presence state (orange-button path).
 * - isAudible != null: report device audibility (piggy-backs on every
 *   status POST; also callable alone for audibility-only heartbeats).
 */
public void postStatus(final String label, final Boolean isAudible,
                       final Runnable onSuccess, final Runnable onError) {
    final String username = mSettings.getMumbleUsername(); // or whichever getter is already used
    final String adminBase = mSettings.getAdminBaseUrl();
    if (username == null || adminBase == null || (label == null && isAudible == null)) {
        if (onError != null) onError.run();
        return;
    }
    new Thread(() -> {
        try {
            java.net.URL url = new java.net.URL(adminBase + "/api/users/status");
            java.net.HttpURLConnection conn = (java.net.HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setDoOutput(true);
            conn.setConnectTimeout(4000);
            conn.setReadTimeout(6000);
            StringBuilder sb = new StringBuilder();
            sb.append("{\"username\":\"").append(username).append("\"");
            if (label != null) sb.append(",\"label\":\"").append(label).append("\"");
            if (isAudible != null) sb.append(",\"is_audible\":").append(isAudible);
            sb.append("}");
            conn.getOutputStream().write(sb.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
            int code = conn.getResponseCode();
            conn.disconnect();
            if (code >= 200 && code < 300) {
                if (label != null) mCurrentStatus = label;
                if (isAudible != null) mCurrentAudible = isAudible;
                if (onSuccess != null) onSuccess.run();
            } else {
                if (onError != null) onError.run();
            }
        } catch (Exception e) {
            if (onError != null) onError.run();
        }
    }, "postStatus").start();
}

/** Compute device audibility from AudioManager. Silent + vibrate both count
 *  as not-audible. The voice-call stream is used because Humla + TTS both
 *  route through it on the P50. */
public boolean computeAudible() {
    android.media.AudioManager am =
        (android.media.AudioManager) getSystemService(android.content.Context.AUDIO_SERVICE);
    if (am == null) return true;  // conservative default
    return am.getRingerMode() == android.media.AudioManager.RINGER_MODE_NORMAL
        && am.getStreamVolume(android.media.AudioManager.STREAM_VOICE_CALL) > 0;
}

private volatile String mCurrentStatus = null;
private volatile Boolean mCurrentAudible = null;

public String getCurrentStatus() { return mCurrentStatus; }
public void setCurrentStatus(String s) { mCurrentStatus = s; }
public Boolean getCurrentAudible() { return mCurrentAudible; }
```

**Step 2: Add `cycleStatus()` to `MumlaActivity`**

Near `isInActivePhoneCall()` (around line 485 in `MumlaActivity.java`), add:

```java
private void cycleStatus() {
    if (mService == null) return;
    final String cur = mService.getCurrentStatus();
    final String next;
    if ("online".equals(cur)) next = "busy";
    else if ("busy".equals(cur)) next = "offline";
    else next = "online";  // null or 'offline' → online

    mService.postStatus(next, mService.computeAudible(),
        /* onSuccess */ () -> runOnUiThread(() -> {
            if (mTts != null) {
                mTts.speak(capitalize(next), android.speech.tts.TextToSpeech.QUEUE_FLUSH, null, "status");
            }
            if (mCarouselFragment != null) {
                mCarouselFragment.refreshStatusPill();  // Task 10 adds this method
            }
        }),
        /* onError */ () -> runOnUiThread(() -> {
            if (mTts != null) {
                mTts.speak("Status change failed", android.speech.tts.TextToSpeech.QUEUE_FLUSH, null, "statusErr");
            }
        })
    );
}

private static String capitalize(String s) {
    if (s == null || s.isEmpty()) return s;
    return Character.toUpperCase(s.charAt(0)) + s.substring(1);
}
```

(If `mTts` doesn't already exist in `MumlaActivity`, the app already sets up a `TextToSpeech` engine in `MumlaService` for shift / SIP / mute announcements — import it from there, or add a simple `TextToSpeech mTts` field here; use the same voice/pitch config as the shift-toggle path in `MumlaService.detectTripleTap`.)

**Step 3: Wire `dispatchKeyEvent` to consume F2 + F4**

In `MumlaActivity.dispatchKeyEvent` (line ~500), insert this block near the top — BEFORE the PTT check and the channel-knob check — so that nothing else tries to interpret F2/F4:

```java
    // Orange top button (P50): emits KEYCODE_F2 (scan 60) AND KEYCODE_F4
    // (scan 62) together per physical press. The ROM's lone-worker handler
    // passes both to dispatchKeyEvent as Unhandled Keys. We act on F4 only
    // and silently swallow F2 to prevent double-firing. Design doc §P50 app.
    if (keyCode == KeyEvent.KEYCODE_F4) {
        if (event.getAction() == KeyEvent.ACTION_DOWN && event.getRepeatCount() == 0) {
            cycleStatus();
        }
        return true;  // consume both DOWN and UP
    }
    if (keyCode == KeyEvent.KEYCODE_F2) {
        return true;  // swallow paired press, no action
    }
```

**Step 4: Build + install**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew clean :app:assembleFossRelease
# If the foss variant isn't set up yet, fall back to:
# ./gradlew clean :app:assembleFossDebug

adb -s R259060623 install -r app/build/outputs/apk/foss/release/openptt-foss-release.apk
# or the debug output path if that's what you built
```

**Step 5: Manual smoke**

- App shows connected status in Mumble.
- Press orange button → TTS says "Busy" → dashboard `/api/users/status?username=harro` shows `label=busy`.
- Press again → "Offline" → dashboard reflects.
- Press again → "Online" → back to start.

**Step 6: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/java/se/lublin/mumla/app/MumlaActivity.java \
        app/src/main/java/se/lublin/mumla/service/MumlaService.java
git commit -m "app: cycle status on orange top button (KEYCODE_F4) with TTS"
```

---

## Task 10: openPTT-app — carousel status pill + hydrate on connect / resume

**Files:**
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/ChannelCarouselFragment.java`
- Modify: `openPTT-app/app/src/main/res/layout/fragment_channel_carousel.xml` (or whichever layout the carousel uses)
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/app/MumlaActivity.java` (hydration on resume)
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/service/MumlaService.java` (hydration on connect + `fetchStatus()` helper)

**Step 1: Add the pill to the carousel layout**

Find the carousel layout XML file (`find app/src/main/res/layout -name '*carousel*'`). Below the active-channel TextView, add:

```xml
<LinearLayout
    android:id="@+id/status_pill"
    android:layout_width="wrap_content"
    android:layout_height="wrap_content"
    android:orientation="horizontal"
    android:gravity="center_vertical"
    android:paddingStart="8dp"
    android:paddingEnd="8dp"
    android:paddingTop="2dp"
    android:paddingBottom="2dp"
    android:background="@drawable/bg_status_pill"
    android:visibility="gone"
    android:layout_marginTop="4dp"
    android:layout_gravity="center_horizontal">

    <View
        android:id="@+id/status_dot"
        android:layout_width="6dp"
        android:layout_height="6dp"
        android:background="@drawable/circle_success"
        android:layout_marginEnd="6dp"/>

    <TextView
        android:id="@+id/status_label"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:text="ONLINE"
        android:textSize="12sp"
        android:textAllCaps="true"
        android:textColor="?android:attr/textColorPrimary"
        android:fontFamily="@font/jetbrains_mono_medium"/>

    <ImageView
        android:id="@+id/status_muted_icon"
        android:layout_width="12dp"
        android:layout_height="12dp"
        android:layout_marginStart="6dp"
        android:src="@drawable/ic_volume_off"
        android:tint="?attr/colorError"
        android:contentDescription="@string/status_muted_desc"
        android:visibility="gone"/>
</LinearLayout>
```

Drawables:

`app/src/main/res/drawable/bg_status_pill.xml`:
```xml
<shape xmlns:android="http://schemas.android.com/apk/res/android" android:shape="rectangle">
    <corners android:radius="999dp"/>
    <solid android:color="?attr/colorSurface"/>
    <stroke android:width="1dp" android:color="?attr/colorControlHighlight"/>
</shape>
```

`app/src/main/res/drawable/circle_success.xml`, `circle_amber.xml`, `circle_muted.xml`: three shape drawables with the colour attrs; the Fragment swaps between them.

`app/src/main/res/drawable/ic_volume_off.xml`: reuse the Material "volume_off" vector drawable (already in the Android SDK — can be copy-pasted, or referenced as `@android:drawable/ic_lock_silent_mode` on older SDK levels).

`app/src/main/res/values/strings.xml` — add `<string name="status_muted_desc">Device muted</string>`.

**Step 2: Add `refreshStatusPill()` to `ChannelCarouselFragment`**

```java
public void refreshStatusPill() {
    if (getView() == null || mService == null) return;
    String label = mService.getCurrentStatus();
    Boolean audible = mService.getCurrentAudible();
    android.view.View pill = getView().findViewById(R.id.status_pill);
    android.view.View dot = getView().findViewById(R.id.status_dot);
    android.widget.TextView tv = getView().findViewById(R.id.status_label);
    android.view.View mutedIcon = getView().findViewById(R.id.status_muted_icon);
    if (label == null) {
        pill.setVisibility(android.view.View.GONE);
        return;
    }
    pill.setVisibility(android.view.View.VISIBLE);
    switch (label) {
        case "online":
            dot.setBackgroundResource(R.drawable.circle_success);
            tv.setText("Online");
            break;
        case "busy":
            dot.setBackgroundResource(R.drawable.circle_amber);
            tv.setText("Busy");
            break;
        case "offline":
        default:
            dot.setBackgroundResource(R.drawable.circle_muted);
            tv.setText("Offline");
            break;
    }
    // Show muted icon only when explicitly not-audible (null = unknown, hide).
    if (mutedIcon != null) {
        mutedIcon.setVisibility(Boolean.FALSE.equals(audible)
                ? android.view.View.VISIBLE : android.view.View.GONE);
    }
}
```

Call `refreshStatusPill()` from `onViewCreated` and whenever `mService` binds.

**Step 3: Add `fetchStatus()` to `MumlaService`**

```java
public void fetchStatus(final Runnable onDone) {
    final String username = mSettings.getMumbleUsername();
    final String adminBase = mSettings.getAdminBaseUrl();
    if (username == null || adminBase == null) {
        if (onDone != null) onDone.run();
        return;
    }
    new Thread(() -> {
        try {
            java.net.URL url = new java.net.URL(adminBase + "/api/users/status?username=" + java.net.URLEncoder.encode(username, "UTF-8"));
            java.net.HttpURLConnection conn = (java.net.HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(4000);
            conn.setReadTimeout(6000);
            if (conn.getResponseCode() == 200) {
                java.io.InputStream is = conn.getInputStream();
                java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
                byte[] tmp = new byte[256]; int n;
                while ((n = is.read(tmp)) > 0) buf.write(tmp, 0, n);
                String json = buf.toString("UTF-8");
                org.json.JSONObject o = new org.json.JSONObject(json);
                mCurrentStatus = o.isNull("label") ? null : o.optString("label", null);
                mCurrentAudible = o.isNull("is_audible") ? null : o.optBoolean("is_audible");
            }
            conn.disconnect();
        } catch (Exception e) {
            // Leave cached values as-is
        } finally {
            if (onDone != null) onDone.run();
        }
    }, "fetchStatus").start();
}
```

**Step 4: Wire hydration**

Three trigger points:

(a) In `MumlaService`, at the end of `onServerConnected` (or whichever handler fires on Mumble connect success), call `fetchStatus(() -> { /* notify UI */ })`, then immediately `postStatus(null, computeAudible(), null, null)` so the server gets this device's current audibility from first connect.

(b) In `MumlaActivity.onResume()`, call `if (mService != null) mService.fetchStatus(() -> runOnUiThread(() -> { if (mCarouselFragment != null) mCarouselFragment.refreshStatusPill(); }));`.

(c) Optional (v1.1): register a `BroadcastReceiver` for `android.media.RINGER_MODE_CHANGED` and `android.media.VOLUME_CHANGED_ACTION` that fires a debounced `postStatus(null, computeAudible(), ...)`. Skipped in v1 — audibility only updates on connect + each orange-button press, which is good enough for the operator to know "can Sarah hear a TTS whisper right now".

**Step 5: Build + install**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew clean :app:assembleFossRelease
adb -s R259060623 install -r app/build/outputs/apk/foss/release/openptt-foss-release.apk
```

**Step 6: Manual smoke**

- Launch the app, connect to Mumble.
- Pill appears on carousel showing "Online" (auto-set by server on connect).
- Press orange → pill updates to "Busy" → background, then foreground the app → pill still shows "Busy".
- Admin overrides to "Offline" via dashboard → foreground the app → pill refreshes to "Offline" on resume.
- Turn the P50 volume all the way down OR flip to silent → press orange to trigger a status POST → carousel pill now shows the muted icon (or any subsequent `fetchStatus` call reflects it) → dashboard row shows the 🔇 icon next to the pill.

**Step 7: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/res/ app/src/main/java/se/lublin/mumla/channel/ChannelCarouselFragment.java \
        app/src/main/java/se/lublin/mumla/service/MumlaService.java \
        app/src/main/java/se/lublin/mumla/app/MumlaActivity.java
git commit -m "app: carousel status pill + hydration on connect + resume"
```

---

## Task 11: Deploy + verify on `ptt.harro.ch` and both P50s

**Step 1: Push server-side changes**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git push origin main
```

**Step 2: Deploy on the VPS**

```bash
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch
cd /opt/ptt
git pull
docker compose up -d --build admin
docker exec ptt-admin-1 alembic upgrade head
docker exec ptt-admin-1 alembic current  # expect f2a9c3b7e4d1
exit
```

**Step 3: Push the Android app**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git push origin main
# Flash both P50s.
adb devices  # expect R259060623 + R259060618
adb -s R259060623 install -r app/build/outputs/apk/foss/release/openptt-foss-release.apk
adb -s R259060618 install -r app/build/outputs/apk/foss/release/openptt-foss-release.apk
```

**Step 4: End-to-end verification**

Log in to `https://ptt.harro.ch` (`admin` / `xYf3huRzivc6Zl6`):

1. **Migration check** — `docker exec ptt-admin-1 alembic current` → `f2a9c3b7e4d1`.
2. **Live Ops** — both P50s connected → both show green Online pills after they connect.
3. **Orange button on P50 #1** — one press → TTS "Busy" → dashboard pill flips amber.
4. **Orange button on P50 #1** — press → "Offline" → pill grey. If that P50 is a lone worker AND lone_worker feature is enabled, the active shift (if any) ends with reason `user_offline` — verify in `/api/loneworker/shift/active` and in the audit log.
5. **Cycle wraps** — one more press → "Online" → pill green.
6. **Admin override** — from the dashboard, edit the other P50's user → set Status to Busy → save. On the P50: background + foreground the app → pill shows Busy.
7. **Dispatch filter** — open Live Ops → Dispatch → search a location → only Online+connected workers appear in Nearest Workers. Flip one to Busy → rerun search → that worker disappears.
8. **Audit log** — System → Audit Log shows `user.status_change` entries with `source` values `self`, `admin`, `auto_connect`, `shift_start`, `shift_stop_offline` as appropriate.
9. **Reconnect auto-Online** — on a P50 set to Busy, disconnect Mumble (toggle airplane mode) → reconnect → pill returns to Online (server auto-promotes on USERCREATED).

**Step 5: Update `docs/open_issues.md`**

Move the **User status / presence** item from "Still open" to "Resolved" with the commit hashes of Tasks 1-10 + the deploy verification note.

**Step 6: Commit the doc update**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add docs/open_issues.md
git commit -m "docs: user status/presence resolved (2026-04-21)"
git push origin main
```

---

## Verification checklist (all phases)

After Task 11:

- [ ] `docker exec ptt-admin-1 alembic current` → `f2a9c3b7e4d1`.
- [ ] `curl https://ptt.harro.ch/api/users/status?username=harro` returns `label`, `effective_label`, and `is_audible`.
- [ ] Live Ops + Directory → Users both show the status pill column.
- [ ] User edit modal has the Status dropdown + "last changed" line.
- [ ] Orange button on both P50s cycles through the three states with TTS confirmation.
- [ ] Carousel status pill reflects the current state on both radios.
- [ ] Shift start force-sets Online (verify with a lone-worker user).
- [ ] Picking Offline ends an active shift (same user).
- [ ] `/api/dispatch/nearest` excludes Busy / Offline / never-set / disconnected workers.
- [ ] Audit Log shows all five `source` values at least once each. Audibility changes do NOT appear in the audit log.
- [ ] Muting a P50 and pressing the orange button causes the 🔇 icon to appear next to that user's pill on the dashboard; unmuting + cycling clears it.
- [ ] All pytest tests pass: `docker exec ptt-pytest sh -c "rm -f test.db && python -m pytest tests/ --ignore=tests/sip_bridge -v"`.

All eleven = user-status-presence ships.

---

## Open questions / deferred

Carried over from the design doc:

1. **Busy-survives-reconnect?** Current design erases Busy on reconnect (auto-Online). Revisit only if users complain.
2. **Dedicated per-user audit-history timeline in the edit modal** — beyond the one-line "last changed" summary. Deferred.
3. **Dashboard-driven custom status text** — rejected; re-open only with a specific operator ask.
4. **Status pill visible on ALL app screens** — deferred; carousel is the app's home.

---

## Dependencies + parallelization

- Tasks 1 → 2 → {3, 4} → 5 must run in order (column → endpoint → side-effects → hook).
- Task 6 depends on Task 5 (needs the stored label to be reliably set).
- Tasks 7, 8 depend on Task 2 (endpoint exists + returns `status_label`).
- Tasks 9, 10 depend on Task 2 (radio needs the POST and GET endpoints).
- Task 11 is the final deploy gate.

Solo execution: run in order. Estimated 4-6 hours of focused work end-to-end.

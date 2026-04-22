# Call Groups Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Scope each Mumble user's joinable channel set to one or more **call groups**, enforced via the same bounce-on-entry pattern that today's Phone-ACL uses.

**Architecture:** New `call_groups` + `user_call_groups` tables; nullable `channels.call_group_id` column. `MurmurClient` gains a second bounce check after the existing phone-ACL one, fed by a 30 s lifespan refresh loop that mirrors today's phone-eligible refresh. Admin-CRUD endpoints for groups + extensions to user/channel endpoints for the multi-/single-select. Dashboard adds a Directory → Call Groups sub-tab and modal extensions. Server-only — no app changes.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (backend), pymumble (Murmur ACL hook), vanilla HTML/CSS/JS (dashboard).

**Companion design doc:** `docs/plans/2026-04-22-call-groups-design.md`

---

## Pre-flight

```bash
# Sidecar + admin running
docker ps --format "{{.Names}}" | grep -E "ptt-pytest|ptt-admin"
# Expected: both listed

# Pytest baseline (will tick up as we add tests)
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q 2>&1 | tail -3"
# Expected: 114 passed (matches end of 2026-04-22 hide-offline ship; True
# Call Hold may add more by the time this plan runs in parallel — that's
# fine, just note the new baseline before Task 1)

# Alembic head
docker exec -w /app/server ptt-admin-1 alembic current 2>&1 | tail -1
# Expected: f2a9c3b7e4d1 (head)
```

If the True Call Hold session has already pushed and bumped the alembic head, treat its head as the new `down_revision` chain — but check first. The two designs deliberately don't share migrations.

---

## Phases

1. **Task 1** — schema (`call_groups`, `user_call_groups`, `channels.call_group_id`) + migration + model tests.
2. **Task 2** — `server/api/call_groups.py` admin-CRUD endpoints + `PUT /members` + tests.
3. **Task 3** — extend `UserResponse`/`UserCreate`/`UserUpdate` and `ChannelResponse`/`ChannelCreate`/`ChannelUpdate` with the new fields + tests.
4. **Task 4** — `MurmurClient` bridge state (`_user_call_groups`, `_channel_call_group`, `_user_is_admin`) + `_call_group_check` + `_on_user_updated` extension + generalised `_bounce_from_channel` + tests mirroring `tests/test_phone_acl.py`.
5. **Task 5** — lifespan refresh task in `server/main.py` parallel to `_refresh_phone_eligibles`.
6. **Task 6** — dashboard sub-tab + modal extensions + member-management UX.
7. **Task 7** — deploy + manual smoke (create group, assign user, tag channel, observe bounce).

Each task ships independently. Hard-stop after Task 7 to confirm prod is healthy.

---

## Task 1: Schema + migration + model tests

**Files:**
- Modify: `server/models.py` (append `CallGroup`, `UserCallGroup`; add `call_group_id` to `Channel`)
- Create: `server/alembic/versions/g3b8d4f6e2a9_call_groups.py`
- Modify: `tests/conftest.py` (only if seeding is needed — likely no)
- Test: `tests/test_call_groups_model.py`

**Step 1: Write the failing test**

Create `tests/test_call_groups_model.py`:

```python
import pytest
from sqlalchemy import select
from server.models import CallGroup, UserCallGroup, User, Channel


@pytest.mark.asyncio
async def test_call_group_create(db_session):
    cg = CallGroup(name="Sales", description="Sales team")
    db_session.add(cg)
    await db_session.commit()
    await db_session.refresh(cg)
    assert cg.id is not None
    assert cg.name == "Sales"


@pytest.mark.asyncio
async def test_user_call_groups_join(db_session):
    """User in a group → join-table row visible via direct query."""
    cg = CallGroup(name="Sales")
    u = User(username="alice", mumble_password="x")
    db_session.add_all([cg, u])
    await db_session.commit()
    await db_session.refresh(cg)
    await db_session.refresh(u)

    db_session.add(UserCallGroup(user_id=u.id, call_group_id=cg.id))
    await db_session.commit()

    rows = (await db_session.execute(
        select(UserCallGroup).where(UserCallGroup.user_id == u.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].call_group_id == cg.id


@pytest.mark.asyncio
async def test_channel_call_group_id_nullable(db_session):
    """A channel without a group_id is unrestricted (NULL)."""
    c = Channel(name="Root", description="default")
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.call_group_id is None


@pytest.mark.asyncio
async def test_channel_with_call_group(db_session):
    cg = CallGroup(name="Sales")
    db_session.add(cg)
    await db_session.commit()
    await db_session.refresh(cg)
    c = Channel(name="SalesChan", description="", call_group_id=cg.id)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.call_group_id == cg.id


@pytest.mark.asyncio
async def test_call_group_unique_name(db_session):
    """name is unique — second insert raises."""
    db_session.add(CallGroup(name="Sales"))
    await db_session.commit()

    db_session.add(CallGroup(name="Sales"))
    with pytest.raises(Exception):  # IntegrityError or InvalidRequestError
        await db_session.commit()
    await db_session.rollback()
```

**Step 2: Run, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_call_groups_model.py -v"
```

Expected: `ImportError: cannot import name 'CallGroup'`.

**Step 3: Add the models**

Append to `server/models.py`:

```python
class CallGroup(Base):
    """Per-user channel-access scoping. Channels with call_group_id set
    are joinable only by users who belong to that group (or by users
    with is_admin=True). NULL on the channel side = unrestricted."""
    __tablename__ = "call_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserCallGroup(Base):
    """Join table — composite PK enforces uniqueness."""
    __tablename__ = "user_call_groups"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
```

Modify the `Channel` model — append `call_group_id` after `max_users`:

```python
    # Optional FK to call_groups.id. NULL = unrestricted (any user can join).
    # ON DELETE SET NULL handled in the migration.
    call_group_id: Mapped[int] = mapped_column(Integer, nullable=True)
```

(SQLAlchemy doesn't strictly need `ForeignKey()` here for tests since we're not relying on relationship loading — the migration enforces the FK constraint on the DB side.)

**Step 4: Create the migration**

Create `server/alembic/versions/g3b8d4f6e2a9_call_groups.py`:

```python
"""call_groups + user_call_groups + channels.call_group_id

Revision ID: g3b8d4f6e2a9
Revises: f2a9c3b7e4d1
Create Date: 2026-04-22
"""
from alembic import op
import sqlalchemy as sa

revision = "g3b8d4f6e2a9"
down_revision = "f2a9c3b7e4d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_groups",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "user_call_groups",
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("call_group_id", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "call_group_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_group_id"], ["call_groups.id"], ondelete="CASCADE"),
    )

    op.add_column(
        "channels",
        sa.Column("call_group_id", sa.Integer, nullable=True),
    )
    op.create_foreign_key(
        "fk_channels_call_group_id",
        "channels", "call_groups",
        ["call_group_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_channels_call_group_id", "channels", type_="foreignkey")
    op.drop_column("channels", "call_group_id")
    op.drop_table("user_call_groups")
    op.drop_table("call_groups")
```

**Step 5: Apply migration + run tests**

```bash
docker compose up -d --build admin
docker exec -w /app/server ptt-admin-1 alembic upgrade head
docker exec -w /app/server ptt-admin-1 alembic current
# Expected: g3b8d4f6e2a9 (head)

docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_call_groups_model.py -v"
```

Expected: PASS (5/5).

Full suite for regressions:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 119 passed (114 baseline + 5 new). Adjust if True Call Hold landed first and bumped the baseline.

**Step 6: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add server/models.py \
        server/alembic/versions/g3b8d4f6e2a9_call_groups.py \
        tests/test_call_groups_model.py
git commit -m "$(cat <<'EOF'
call-groups: schema (call_groups + user_call_groups + channels FK)

Adds the two new tables plus a nullable channels.call_group_id FK
(ON DELETE SET NULL — deleting a group converts its channels back to
visible-to-all rather than orphaning them). Existing channels default
NULL, so today's "everyone sees everything" behaviour is preserved
exactly. CRUD endpoints + bridge state come in Tasks 2 + 4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `server/api/call_groups.py` admin CRUD + tests

**Files:**
- Create: `server/api/call_groups.py`
- Modify: `server/main.py` (register router)
- Test: `tests/test_call_groups_api.py`

**Step 1: Write the failing tests**

Create `tests/test_call_groups_api.py`:

```python
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
```

**Step 2: Run, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_call_groups_api.py -v"
```

Expected: every test 404s — router doesn't exist.

**Step 3: Implement the router**

Create `server/api/call_groups.py`:

```python
"""Admin-CRUD for call groups + per-user membership.

Per-user channel-access scoping: channels with a call_group_id can be
joined only by users in that group (or by users.is_admin=true). NULL
on the channel side means unrestricted. Bounce enforcement lives in
MurmurClient (Task 4); this module is the data-plane.

PUT /api/call-groups/{id}/members replaces the membership set wholesale
— matches the dashboard's checkbox-list save shape (the form sends the
current full state). Add/remove deltas are wordier and need conflict
handling; not worth the API surface for typically-low-N membership.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.admin import log_audit
from server.auth import get_current_admin
from server.database import get_db
from server.models import CallGroup, Channel, User, UserCallGroup

router = APIRouter(prefix="/api/call-groups", tags=["call-groups"])


class CallGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=256)


class CallGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=256)


class CallGroupResponse(BaseModel):
    id: int
    name: str
    description: str | None
    created_at: datetime
    member_count: int
    channel_count: int


class MemberRef(BaseModel):
    id: int
    username: str


class ChannelRef(BaseModel):
    id: int
    name: str


class CallGroupDetail(CallGroupResponse):
    members: list[MemberRef]
    channels: list[ChannelRef]


class MembershipReplace(BaseModel):
    user_ids: list[int]


async def _to_response(db: AsyncSession, group: CallGroup) -> CallGroupResponse:
    member_count = (await db.execute(
        select(func.count()).select_from(UserCallGroup)
        .where(UserCallGroup.call_group_id == group.id)
    )).scalar_one()
    channel_count = (await db.execute(
        select(func.count()).select_from(Channel)
        .where(Channel.call_group_id == group.id)
    )).scalar_one()
    return CallGroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        created_at=group.created_at,
        member_count=member_count,
        channel_count=channel_count,
    )


@router.get("", response_model=list[CallGroupResponse])
async def list_call_groups(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    rows = (await db.execute(select(CallGroup).order_by(CallGroup.name))).scalars().all()
    return [await _to_response(db, g) for g in rows]


@router.post("", response_model=CallGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_call_group(
    body: CallGroupCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    existing = (await db.execute(
        select(CallGroup).where(CallGroup.name == body.name)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Call group name already exists")
    group = CallGroup(name=body.name, description=body.description)
    db.add(group)
    await log_audit(db, admin["sub"], "call_group.create",
                     target_type="call_group", target_id=body.name)
    await db.commit()
    await db.refresh(group)
    return await _to_response(db, group)


@router.get("/{group_id}", response_model=CallGroupDetail)
async def get_call_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    group = (await db.execute(
        select(CallGroup).where(CallGroup.id == group_id)
    )).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Call group not found")
    base = await _to_response(db, group)
    members = (await db.execute(
        select(User).join(UserCallGroup, UserCallGroup.user_id == User.id)
        .where(UserCallGroup.call_group_id == group_id)
        .order_by(User.username)
    )).scalars().all()
    channels = (await db.execute(
        select(Channel).where(Channel.call_group_id == group_id).order_by(Channel.name)
    )).scalars().all()
    return CallGroupDetail(
        **base.model_dump(),
        members=[MemberRef(id=u.id, username=u.username) for u in members],
        channels=[ChannelRef(id=c.id, name=c.name) for c in channels],
    )


@router.patch("/{group_id}", response_model=CallGroupResponse)
async def update_call_group(
    group_id: int, body: CallGroupUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    group = (await db.execute(
        select(CallGroup).where(CallGroup.id == group_id)
    )).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Call group not found")
    if body.name is not None and body.name != group.name:
        clash = (await db.execute(
            select(CallGroup).where(CallGroup.name == body.name)
        )).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status_code=409, detail="Call group name already exists")
        group.name = body.name
    if body.description is not None:
        group.description = body.description
    await log_audit(db, admin["sub"], "call_group.update",
                     target_type="call_group", target_id=str(group.id))
    await db.commit()
    await db.refresh(group)
    return await _to_response(db, group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_call_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    group = (await db.execute(
        select(CallGroup).where(CallGroup.id == group_id)
    )).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Call group not found")
    await log_audit(db, admin["sub"], "call_group.delete",
                     target_type="call_group", target_id=str(group.id))
    await db.delete(group)
    await db.commit()


@router.put("/{group_id}/members", response_model=CallGroupResponse)
async def replace_members(
    group_id: int, body: MembershipReplace,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    group = (await db.execute(
        select(CallGroup).where(CallGroup.id == group_id)
    )).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Call group not found")
    # Wipe + reinsert. Cheap for typically-low-N memberships.
    await db.execute(
        delete(UserCallGroup).where(UserCallGroup.call_group_id == group_id)
    )
    for uid in body.user_ids:
        db.add(UserCallGroup(user_id=uid, call_group_id=group_id))
    await log_audit(db, admin["sub"], "call_group.members_replace",
                     target_type="call_group", target_id=str(group.id))
    await db.commit()
    return await _to_response(db, group)
```

**Step 4: Register the router in `server/main.py`**

Add to the `server.api.*` import block:

```python
from server.api.call_groups import router as call_groups_router
```

And register alongside the other routers:

```python
app.include_router(call_groups_router)
```

**Step 5: Run tests**

```bash
docker compose up -d --build admin
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_call_groups_api.py -v"
```

Expected: 8 passed.

Full suite:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 127 passed (119 + 8 new).

**Step 6: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add server/api/call_groups.py server/main.py tests/test_call_groups_api.py
git commit -m "$(cat <<'EOF'
call-groups: admin CRUD endpoints + PUT /members wholesale-replace

GET / POST / GET-detail / PATCH / DELETE on /api/call-groups + a
PUT /{id}/members that swaps the membership set in one shot. All
admin-auth, all audit-logged. PUT-replace matches the dashboard's
checkbox-list save shape (Task 6); cheap for low-N memberships.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Extend `UserResponse` + `ChannelResponse` (and Create/Update siblings)

**Files:**
- Modify: `server/api/schemas.py`
- Modify: `server/api/users.py` (load + apply call_group_ids on create/update; expose on responses)
- Modify: `server/api/channels.py` (load + apply call_group_id on create/update)
- Test: `tests/test_users_call_groups.py`
- Test: `tests/test_channels_call_groups.py`

**Step 1: Write the failing tests**

Create `tests/test_users_call_groups.py`:

```python
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
        "password": "shh",
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
        "username": "carol", "password": "shh", "call_group_ids": [g_ids[0]],
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
        "username": "dave", "password": "shh", "call_group_ids": g_ids,
    })
    uid = r.json()["id"]

    r = await admin_client.patch(f"/api/users/{uid}", json={"call_group_ids": []})
    assert r.json()["call_group_ids"] == []
```

Create `tests/test_channels_call_groups.py`:

```python
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
```

**Step 2: Run, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_users_call_groups.py tests/test_channels_call_groups.py -v"
```

Expected: validation errors / missing fields on the responses.

**Step 3: Extend the schemas**

In `server/api/schemas.py`, find `UserResponse` and add the new field:

```python
    call_group_ids: list[int] = []
```

Find `UserCreate` and `UserUpdate` (Pydantic models for the body). Add the optional field to each:

```python
    call_group_ids: list[int] | None = None
```

Find `ChannelResponse` (with `model_config = {"from_attributes": True}`) and add:

```python
    call_group_id: int | None = None
```

Find `ChannelCreate` (and `ChannelUpdate` if it exists separately — the codebase may share a single body model). Add:

```python
    call_group_id: int | None = None
```

**Step 4: Wire `call_group_ids` into the user endpoints**

In `server/api/users.py`:

The `UserResponse` `from_attributes=True` won't auto-populate `call_group_ids` (it's not a column on `User`). Override the response by hand. Find the `list_users` and `create_user` and `update_user` handlers; before returning each, fetch the join-table rows and stuff them into the response model.

A small shared helper near the top of the file:

```python
from server.models import UserCallGroup

async def _load_call_group_ids(db: AsyncSession, user_id: int) -> list[int]:
    rows = (await db.execute(
        select(UserCallGroup.call_group_id)
        .where(UserCallGroup.user_id == user_id)
    )).all()
    return [r[0] for r in rows]


async def _replace_call_groups(db: AsyncSession, user_id: int,
                                group_ids: list[int]) -> None:
    """Wipe-and-reinsert the join rows for this user."""
    from sqlalchemy import delete
    await db.execute(
        delete(UserCallGroup).where(UserCallGroup.user_id == user_id)
    )
    for gid in group_ids:
        db.add(UserCallGroup(user_id=user_id, call_group_id=gid))
```

In `list_users` (currently returning `result.scalars().all()` directly), reshape:

```python
@router.get("", response_model=list[UserResponse])
async def list_users(...):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    out = []
    for u in users:
        resp = UserResponse.model_validate(u)
        resp.call_group_ids = await _load_call_group_ids(db, u.id)
        out.append(resp)
    return out
```

In `create_user`, after the new user is committed:

```python
if user_data.call_group_ids is not None:
    await _replace_call_groups(db, user.id, user_data.call_group_ids)
    await db.commit()

resp = UserResponse.model_validate(user)
resp.call_group_ids = await _load_call_group_ids(db, user.id)
return resp
```

In `update_user`, similarly — if `data.call_group_ids is not None`, call `_replace_call_groups` and commit, then load + return.

**Step 5: Wire `call_group_id` into the channel endpoints**

In `server/api/channels.py`, the existing handlers likely just create a `Channel(name=..., description=..., max_users=...)`. Add:

```python
new_channel = Channel(
    name=body.name,
    description=body.description,
    max_users=body.max_users or 0,
    call_group_id=body.call_group_id,
)
```

For PATCH:
```python
if body.call_group_id is not None or "call_group_id" in body.model_fields_set:
    channel.call_group_id = body.call_group_id
```

(The `model_fields_set` check lets clients explicitly send `null` to clear.)

`from_attributes=True` on `ChannelResponse` will auto-populate `call_group_id` since it IS a column on `Channel` now.

**Step 6: Run tests**

```bash
docker compose up -d --build admin
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_users_call_groups.py tests/test_channels_call_groups.py -v"
```

Expected: 7 passed.

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 134 passed (127 + 7 new).

**Step 7: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add server/api/schemas.py server/api/users.py server/api/channels.py \
        tests/test_users_call_groups.py tests/test_channels_call_groups.py
git commit -m "$(cat <<'EOF'
call-groups: extend user + channel endpoints with group fields

UserResponse gains call_group_ids[]; UserCreate/Update accept it
(replace-wholesale on save). ChannelResponse gains call_group_id;
ChannelCreate/Update accept it (null clears). Bridge enforcement +
lifespan refresh come in Tasks 4 + 5.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `MurmurClient` bridge state + bounce check + tests

**Files:**
- Modify: `server/murmur/client.py`
- Test: `tests/test_call_group_acl.py`

**Step 1: Write the failing tests**

Create `tests/test_call_group_acl.py` mirroring `tests/test_phone_acl.py` shape. The exact fixtures depend on what's in `test_phone_acl.py`; the key behaviours to assert:

```python
"""Bounce-on-entry tests for the call-group ACL.

Mirrors tests/test_phone_acl.py — same MurmurClient under test, same
USERUPDATED simulation. The Phone-ACL tests are the precedent for the
fixture pattern; copy that shape for any pymumble mocking helpers.
"""
import pytest
from unittest.mock import MagicMock
from server.murmur.client import MurmurClient


def _make_client():
    """Bare MurmurClient with a mocked _mumble for move_in / send_text."""
    c = MurmurClient(host="x", port=0, secret="x", mumble_host="x", mumble_port=0)
    c._mumble = MagicMock()
    return c


def test_call_group_check_admin_bypasses():
    c = _make_client()
    c._user_call_groups = {"alice": set()}
    c._channel_call_group = {5: 1}  # channel 5 restricted to group 1
    c._user_is_admin = {"alice": True}
    assert c._call_group_check("alice", 5) is True


def test_call_group_check_unrestricted_channel_allowed():
    c = _make_client()
    c._user_call_groups = {"alice": set()}
    c._channel_call_group = {5: None}
    c._user_is_admin = {"alice": False}
    assert c._call_group_check("alice", 5) is True


def test_call_group_check_member_allowed():
    c = _make_client()
    c._user_call_groups = {"alice": {1, 2}}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {"alice": False}
    assert c._call_group_check("alice", 5) is True


def test_call_group_check_non_member_denied():
    c = _make_client()
    c._user_call_groups = {"alice": {2}}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {"alice": False}
    assert c._call_group_check("alice", 5) is False


def test_call_group_check_unknown_user_denied():
    c = _make_client()
    c._user_call_groups = {}  # alice has never been refreshed
    c._channel_call_group = {5: 1}
    c._user_is_admin = {}
    assert c._call_group_check("alice", 5) is False


def test_update_call_group_state_atomic():
    """update_call_group_state replaces all three dicts wholesale."""
    c = _make_client()
    c.update_call_group_state(
        user_groups={"alice": {1}},
        channel_groups={5: 1},
        user_admin={"alice": False},
    )
    assert c._user_call_groups == {"alice": {1}}
    assert c._channel_call_group == {5: 1}
    assert c._user_is_admin == {"alice": False}


def test_user_updated_bounces_non_member(monkeypatch):
    """USERUPDATED with a non-member entering a restricted channel triggers
    a bounce. Reuses the same callback as the Phone-ACL test pattern."""
    c = _make_client()
    c._user_call_groups = {"alice": set()}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {"alice": False}
    # alice's previous channel (so the move-detection logic fires).
    c._user_last_channel = {42: 0}

    bounced = []
    monkeypatch.setattr(
        c, "_bounce_from_channel",
        lambda sid, prev, deny: bounced.append((sid, prev)),
    )

    c._on_user_updated(
        user={"name": "alice", "session": 42, "channel_id": 5},
        actions=None,
    )
    assert bounced == [(42, 0)]


def test_user_updated_admin_not_bounced(monkeypatch):
    """is_admin user is not bounced from a restricted channel."""
    c = _make_client()
    c._user_call_groups = {"alice": set()}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {"alice": True}
    c._user_last_channel = {42: 0}

    bounced = []
    monkeypatch.setattr(
        c, "_bounce_from_channel",
        lambda sid, prev, deny: bounced.append((sid, prev)),
    )

    c._on_user_updated(
        user={"name": "alice", "session": 42, "channel_id": 5},
        actions=None,
    )
    assert bounced == []
```

**Step 2: Run, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_call_group_acl.py -v"
```

Expected: AttributeError on `_user_call_groups`, `_call_group_check`, `update_call_group_state`, `_bounce_from_channel`.

**Step 3: Generalise `_bounce_from_phone` → `_bounce_from_channel`**

In `server/murmur/client.py`, find `_bounce_from_phone(self, session_id, previous_channel_id)` (line ~570). Rename + add a `deny_pcm` param:

```python
def _bounce_from_channel(
    self,
    session_id: int,
    previous_channel_id: int,
    deny_pcm: Optional[bytes],
) -> None:
    """Move `session_id` back to `previous_channel_id` and whisper why."""
    # ... existing body, but use the passed-in deny_pcm instead of
    # self._render_phone_deny()
```

Update the existing phone-acl call site (around line 566) to pass the phone deny PCM:

```python
self._bounce_from_channel(
    session_id, prev_channel_id, self._render_phone_deny(),
)
```

(Keep `_bounce_from_phone` as a one-line shim if it's referenced from tests, OR rename references throughout — check `tests/test_phone_acl.py` first.)

**Step 4: Add the call-group state + check + extension**

Add these fields to `MurmurClient.__init__` (near the existing `_phone_eligible`):

```python
        # Call-group ACL state, refreshed every 30 s by the lifespan task.
        self._user_call_groups: dict[str, set[int]] = {}
        self._channel_call_group: dict[int, int | None] = {}
        self._user_is_admin: dict[str, bool] = {}
        self._call_group_deny_pcm: bytes | None = None
```

Add the public update method (alongside `update_phone_eligible`):

```python
    def update_call_group_state(
        self,
        user_groups: dict[str, set[int]],
        channel_groups: dict[int, int | None],
        user_admin: dict[str, bool],
    ) -> None:
        """Refresh the in-memory call-group state. Atomic swap."""
        self._user_call_groups = user_groups
        self._channel_call_group = channel_groups
        self._user_is_admin = user_admin
```

Add the check helper (near `_resolve_phone_and_children`):

```python
    def _call_group_check(self, name: str, new_channel_id: int) -> bool:
        """True if `name` may join `new_channel_id` per call-group rules."""
        lc = name.lower()
        if self._user_is_admin.get(lc, False):
            return True
        chan_group = self._channel_call_group.get(new_channel_id)
        if chan_group is None:
            return True
        return chan_group in self._user_call_groups.get(lc, set())
```

Add the call-group deny TTS renderer (near `_render_phone_deny`):

```python
    CALL_GROUP_ACL_DENY_TEXT = "This channel requires call group membership"

    def _render_call_group_deny(self) -> Optional[bytes]:
        if self._call_group_deny_pcm is not None:
            return self._call_group_deny_pcm
        try:
            from server.weather_bot import text_to_audio_pcm
            pcm = text_to_audio_pcm(self.CALL_GROUP_ACL_DENY_TEXT)
            if pcm:
                self._call_group_deny_pcm = pcm
            return pcm
        except Exception as e:
            logger.error("failed to render call-group-deny TTS: %s", e)
            return None
```

Extend `_on_user_updated` — after the existing phone-acl block (right before the `except Exception as e:` of the outer `try`), add:

```python
            # Call-group ACL — second check, same bounce mechanic, different
            # deny TTS. Returns early once handled so we don't fall through.
            if not self._call_group_check(name, new_channel_id):
                logger.info(
                    "call-group: bouncing '%s' (session=%s) from channel %d",
                    name, session_id, new_channel_id,
                )
                self._bounce_from_channel(
                    session_id, prev_channel_id, self._render_call_group_deny(),
                )
                return
```

**Step 5: Run tests**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_call_group_acl.py tests/test_phone_acl.py -v"
```

Expected: 8 new + 14 phone-acl all pass. If phone-acl tests fail, the bounce-rename touched a code path they exercise — adjust them to use `_bounce_from_channel(...)` with the phone deny PCM.

Full suite:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 142 passed (134 + 8 new).

**Step 6: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add server/murmur/client.py tests/test_call_group_acl.py
git commit -m "$(cat <<'EOF'
call-groups: bridge state + bounce check (mirrors phone-acl)

MurmurClient gains _user_call_groups + _channel_call_group +
_user_is_admin dicts, refreshed wholesale by update_call_group_state.
_on_user_updated extends the existing phone-acl block with a
_call_group_check; non-members are bounced via the generalised
_bounce_from_channel helper (renamed from _bounce_from_phone) with a
distinct deny TTS. is_admin users bypass.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Lifespan refresh task in `server/main.py`

**Files:**
- Modify: `server/main.py` (add `_refresh_call_groups` parallel to `_refresh_phone_eligibles`)

**Step 1: Add the refresh coroutine**

Inside the `lifespan(app)` block, alongside the existing `_refresh_phone_eligibles` (around line 162), add:

```python
        from server.models import Channel, UserCallGroup

        async def _refresh_call_groups():
            while True:
                try:
                    async with async_session() as db:
                        # users + admin flag + their groups
                        rows = (await db.execute(
                            select(User.username, User.is_admin, UserCallGroup.call_group_id)
                            .outerjoin(UserCallGroup, UserCallGroup.user_id == User.id)
                        )).all()
                        user_groups: dict[str, set[int]] = {}
                        user_admin: dict[str, bool] = {}
                        for username, is_admin, gid in rows:
                            lc = username.lower()
                            user_admin[lc] = bool(is_admin)
                            if gid is not None:
                                user_groups.setdefault(lc, set()).add(gid)
                            else:
                                user_groups.setdefault(lc, set())

                        # channels + their group_id (mumble_id is what the
                        # bridge keys on for the bounce check)
                        crows = (await db.execute(
                            select(Channel.mumble_id, Channel.call_group_id)
                            .where(Channel.mumble_id.isnot(None))
                        )).all()
                        channel_groups = {mid: gid for (mid, gid) in crows}

                    client.update_call_group_state(user_groups, channel_groups, user_admin)
                except Exception as e:
                    logger.warning("call-groups: refresh failed: %s", e)
                await asyncio.sleep(30)

        try:
            call_group_task = asyncio.create_task(_refresh_call_groups())
            app.state.call_group_task = call_group_task
            logger.info("Call-group state poller started (30 s)")
        except Exception as e:
            logger.warning("Call-group poller failed to start: %s", e)
```

(Reuses `asyncio` + `async_session` + `select` already imported in this scope by the phone-acl block. Just import `Channel` and `UserCallGroup` at the top of the inner block.)

**Step 2: Cancel on shutdown**

Find the existing shutdown block where `phone_acl_task.cancel()` lives. Add right after:

```python
    if hasattr(app.state, "call_group_task") and app.state.call_group_task is not None:
        app.state.call_group_task.cancel()
        try:
            await app.state.call_group_task
        except asyncio.CancelledError:
            pass
```

**Step 3: Verify the bridge picks up state — semi-integration smoke**

There's no clean unit test for the lifespan task itself; verify by manual inspection of the logs after deploy in Task 7. For now, just confirm the build is clean:

```bash
docker compose up -d --build admin 2>&1 | tail -5
docker logs ptt-admin-1 --tail 30 2>&1 | grep -iE "call-group|phone ACL"
```

Expected: lines like `Call-group state poller started (30 s)` and `Phone ACL eligible-set poller started (30 s)`.

Run the full suite (no new tests — sanity only):

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 142 passed (no change).

**Step 4: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add server/main.py
git commit -m "$(cat <<'EOF'
call-groups: 30s lifespan refresh task (mirrors phone-acl poller)

Polls users + their groups + admin flag + channels with their group
ids; pushes the three dicts into MurmurClient.update_call_group_state.
Cancelled cleanly on shutdown alongside the phone-acl task. Re-uses
the same async_session + 30 s cadence as the existing phone-acl
poller; cost is one outer-join + one channels select per cycle.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Dashboard sub-tab + modal extensions

**Files:**
- Modify: `server/dashboard/index.html`

**Step 1: Add the Call Groups sub-tab**

Find the `subTabsDirectory` block (around line 836). Insert a new sub-tab after Channels:

```html
                <div class="sub-tab" data-tab="call-groups" onclick="switchTab('call-groups')">Call Groups</div>
```

**Step 2: Add the `tab-call-groups` content block**

After the existing `tab-channels` content block (search for `id="tab-channels"`), insert:

```html
        <!-- Call Groups Tab -->
        <div class="content tab-content" id="tab-call-groups" style="display:none" role="tabpanel" aria-label="Call Groups">
            <div class="card">
                <h2>Call Groups</h2>
                <p style="font-size:0.875rem;color:var(--fg-muted);margin-bottom:var(--space-md)">
                    Restrict which channels each user can join. A channel with no
                    group is visible to everyone; a channel assigned to a group is
                    visible only to its members. Users marked as admin bypass these
                    restrictions.
                </p>
                <div class="form-row" style="margin-bottom:var(--space-md)">
                    <input type="text" id="newCallGroupName" placeholder="Group name" style="flex:1">
                    <input type="text" id="newCallGroupDesc" placeholder="Description (optional)" style="flex:2">
                    <button class="btn primary" onclick="addCallGroup()">Add Group</button>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Name</th><th>Description</th><th>Members</th><th>Channels</th><th>Actions</th></tr></thead>
                        <tbody id="callGroupsTable"></tbody>
                    </table>
                </div>
            </div>
        </div>
```

Edit modal (members management) — append near the end of `<body>`:

```html
<div id="callGroupModal" class="modal" style="display:none">
    <div class="modal-content" style="max-width:640px">
        <h2 id="callGroupModalTitle">Edit Call Group</h2>
        <label style="display:block;margin-top:var(--space-md)">Name
            <input type="text" id="editCallGroupName" style="width:100%">
        </label>
        <label style="display:block;margin-top:var(--space-md)">Description
            <input type="text" id="editCallGroupDesc" style="width:100%">
        </label>
        <h3 style="margin-top:var(--space-lg)">Members</h3>
        <div id="callGroupMembersList" style="max-height:240px;overflow:auto;border:1px solid var(--color-border);padding:var(--space-sm)"></div>
        <div class="form-row" style="margin-top:var(--space-md);justify-content:flex-end">
            <button class="btn" onclick="closeCallGroupModal()">Cancel</button>
            <button class="btn primary" onclick="saveCallGroup()">Save</button>
        </div>
    </div>
</div>
```

**Step 3: Add the JS — refresh + add + edit + delete**

Add these functions near the other `refresh*` helpers in the `<script>` block:

```js
let _editingCallGroupId = null;

async function refreshCallGroups() {
    const data = await api('/api/call-groups');
    if (!data) return;
    const tbody = document.getElementById('callGroupsTable');
    if (data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5"><div class="empty-state"><p>No call groups yet.</p></div></td></tr>';
        return;
    }
    tbody.innerHTML = data.map(g => '<tr>' +
        '<td><strong>' + esc(g.name) + '</strong></td>' +
        '<td>' + esc(g.description || '\u2014') + '</td>' +
        '<td style="font-family:var(--font-data)">' + g.member_count + '</td>' +
        '<td style="font-family:var(--font-data)">' + g.channel_count + '</td>' +
        '<td class="actions">' +
            '<button class="btn" onclick="openCallGroupModal(' + g.id + ')">Edit</button> ' +
            '<button class="btn danger" onclick="deleteCallGroup(' + g.id + ',\'' + esc(g.name) + '\')">Delete</button>' +
        '</td></tr>').join('');
}

async function addCallGroup() {
    const name = document.getElementById('newCallGroupName').value.trim();
    const description = document.getElementById('newCallGroupDesc').value.trim();
    if (!name) { showToast('Group name is required', 'error'); return; }
    const r = await api('/api/call-groups', { method: 'POST', body: JSON.stringify({ name, description }) });
    if (r) {
        showToast('Created call group "' + name + '"', 'success');
        document.getElementById('newCallGroupName').value = '';
        document.getElementById('newCallGroupDesc').value = '';
        refreshCallGroups();
    }
}

async function openCallGroupModal(groupId) {
    _editingCallGroupId = groupId;
    const detail = await api('/api/call-groups/' + groupId);
    if (!detail) return;
    document.getElementById('editCallGroupName').value = detail.name;
    document.getElementById('editCallGroupDesc').value = detail.description || '';

    // Load all users to render the checkbox list.
    const allUsers = await api('/api/users');
    const memberIds = new Set(detail.members.map(m => m.id));
    const list = document.getElementById('callGroupMembersList');
    list.innerHTML = (allUsers || []).map(u =>
        '<label style="display:block;padding:2px 0">' +
        '<input type="checkbox" data-user-id="' + u.id + '"' +
            (memberIds.has(u.id) ? ' checked' : '') + ' style="margin-right:6px">' +
        esc(u.username) + (u.display_name ? ' (' + esc(u.display_name) + ')' : '') +
        '</label>'
    ).join('');

    document.getElementById('callGroupModalTitle').textContent = 'Edit "' + detail.name + '"';
    document.getElementById('callGroupModal').style.display = 'flex';
}

function closeCallGroupModal() {
    document.getElementById('callGroupModal').style.display = 'none';
    _editingCallGroupId = null;
}

async function saveCallGroup() {
    if (_editingCallGroupId == null) return;
    const name = document.getElementById('editCallGroupName').value.trim();
    const description = document.getElementById('editCallGroupDesc').value.trim();
    const userIds = Array.from(
        document.querySelectorAll('#callGroupMembersList input[type=checkbox]:checked')
    ).map(cb => parseInt(cb.dataset.userId, 10));

    await api('/api/call-groups/' + _editingCallGroupId, {
        method: 'PATCH',
        body: JSON.stringify({ name, description }),
    });
    await api('/api/call-groups/' + _editingCallGroupId + '/members', {
        method: 'PUT',
        body: JSON.stringify({ user_ids: userIds }),
    });
    showToast('Saved call group "' + name + '"', 'success');
    closeCallGroupModal();
    refreshCallGroups();
}

async function deleteCallGroup(groupId, name) {
    if (!confirm('Delete call group "' + name + '"? Channels assigned to it will become visible to all users.')) return;
    await api('/api/call-groups/' + groupId, { method: 'DELETE' });
    showToast('Deleted "' + name + '"', 'success');
    refreshCallGroups();
}
```

Wire `refreshCallGroups` into `switchTab` — find the existing `if (name === 'channels') refreshChannels();` line (or similar) and append:

```js
if (name === 'call-groups') refreshCallGroups();
```

**Step 4: Extend the user-edit modal**

Find the user-edit modal (around line 1740 — `id="editUserModal"` in the HTML, with the `Status` dropdown added during the user-status work). Add a checkbox-list block above the Save button:

```html
<label style="display:block;margin-top:var(--space-md)">Call Groups
    <div id="editUserCallGroups" style="max-height:140px;overflow:auto;border:1px solid var(--color-border);padding:var(--space-sm)"></div>
</label>
```

In `openEditUser(...)` (the function triggered by the Edit button on the user list), after populating the existing fields, fetch all groups + the user's current memberships:

```js
const allGroups = await api('/api/call-groups');
const userDetail = await api('/api/users/' + userId);
const memberIds = new Set((userDetail && userDetail.call_group_ids) || []);
const ucgList = document.getElementById('editUserCallGroups');
ucgList.innerHTML = (allGroups || []).map(g =>
    '<label style="display:block;padding:2px 0">' +
    '<input type="checkbox" data-cg-id="' + g.id + '"' +
        (memberIds.has(g.id) ? ' checked' : '') + ' style="margin-right:6px">' +
    esc(g.name) +
    '</label>'
).join('');
```

In the user-edit save handler, after the existing PATCH succeeds:

```js
const newGroupIds = Array.from(
    document.querySelectorAll('#editUserCallGroups input[type=checkbox]:checked')
).map(cb => parseInt(cb.dataset.cgId, 10));
await api('/api/users/' + userId, {
    method: 'PATCH',
    body: JSON.stringify({ call_group_ids: newGroupIds }),
});
```

**Step 5: Extend the channel-edit modal**

Find the channel-edit modal (search for `editChannelModal` or `openEditChannel`). If channels currently lack an edit modal (they may only have create + delete), add a small one OR extend the create form to include the call-group select. Minimum viable:

In the create-channel form (`#tab-channels` block), add a select element next to the existing name + description inputs:

```html
<select id="newChannelCallGroup" style="flex:1">
    <option value="">— Visible to all —</option>
</select>
```

Populate the dropdown in `refreshChannels` (or a sibling helper called when the tab opens):

```js
async function refreshCallGroupsForChannelDropdown() {
    const groups = await api('/api/call-groups') || [];
    const sel = document.getElementById('newChannelCallGroup');
    sel.innerHTML = '<option value="">— Visible to all —</option>' +
        groups.map(g => '<option value="' + g.id + '">' + esc(g.name) + '</option>').join('');
}
```

In the create-channel handler (`addChannel()` or similar), include the value:

```js
const callGroupVal = document.getElementById('newChannelCallGroup').value;
const body = {
    name, description,
    call_group_id: callGroupVal ? parseInt(callGroupVal, 10) : null,
};
await api('/api/channels', { method: 'POST', body: JSON.stringify(body) });
```

Show the assigned group in the channels-table render — find `renderChannels` (or wherever the row HTML is built) and add a column:

```js
'<td>' + (c.call_group_id ? esc(<lookup>) : '<span style="color:var(--fg-muted)">All</span>') + '</td>' +
```

(For brevity, look up the group name from a cached `_callGroupsById` map populated by the dropdown refresh.)

If the existing channel row doesn't already have an Edit modal, you can either ship the create-only path now and defer in-place editing to a follow-up, OR add a minimal channel-edit modal that lets the admin re-assign the group. Operator's choice — pragmatic minimum for v1 is "set at create, delete + re-create to change."

**Step 6: Manual smoke**

```bash
docker compose up -d --build admin
# Open https://ptt.harro.ch/dashboard/ and log in.
# Verify:
# - Directory → Call Groups sub-tab appears.
# - Add a "Sales" group via the form.
# - Click Edit on it → modal opens with member checkboxes.
# - Tick harro → Save → row member count goes 0 → 1.
# - Edit a channel → assign Sales as call group → in DB:
#   docker exec -w /app/server ptt-admin-1 \
#     python -c "from sqlalchemy import select; ..." (or just inspect via psql)
# - Verify in the channel row the new "Group" column shows "Sales".
```

**Step 7: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add server/dashboard/index.html
git commit -m "$(cat <<'EOF'
call-groups: dashboard sub-tab + member modal + user/channel modal hooks

Directory → Call Groups: list + add + edit (modal with member
checkbox-list) + delete. User-edit modal gains a call-groups
checkbox-list. Channel-create form gains a "— Visible to all —" /
call-group dropdown. Channels table shows the assigned group.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Deploy + manual smoke

**Step 1: Push**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git push origin main
```

If the True Call Hold parallel session has pushed in between, rebase or merge as the situation requires. Both feature designs deliberately don't share files; the only conflict zone is `docs/open_issues.md` at Resolved-time, which we handle in Step 5.

**Step 2: Deploy**

```bash
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch "
    cd /opt/ptt && \
    git pull --ff-only && \
    docker compose up -d --build admin && \
    docker exec -w /app/server ptt-admin-1 alembic upgrade head && \
    docker exec -w /app/server ptt-admin-1 alembic current
" 2>&1 | tail -10
```

Expected: alembic current reports `g3b8d4f6e2a9 (head)`.

**Step 3: Verify the lifespan poller started**

```bash
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch \
    "docker logs ptt-admin-1 --tail 30 2>&1 | grep -i 'call-group'"
```

Expected: `Call-group state poller started (30 s)`.

**Step 4: End-to-end smoke**

1. Log in to `https://ptt.harro.ch/dashboard/` (`admin` / `xYf3huRzivc6Zl6`).
2. **Create a group** — Directory → Call Groups → Add "Sales".
3. **Assign a member** — Edit the Sales group → tick `harro` → Save. Member count goes 0 → 1.
4. **Tag a channel** — Directory → Channels → create a new channel "SalesChan" with call-group = Sales (or edit an existing one). The DB now has `channels.call_group_id != NULL`.
5. **Wait ~30 s** for the next refresh cycle so the bridge picks up the new state.
6. **Bounce test** — on R259060618 (yuliia, NOT in the Sales group), navigate to SalesChan via the carousel knob. Within ~1 s, yuliia should be bounced back and hear *"This channel requires call group membership"*. The dashboard's Live Ops shows yuliia briefly in SalesChan, then back in her previous channel.
7. **Member-allowed test** — on R259060623 (harro, IS in Sales), navigate to SalesChan. No bounce; the carousel sits there.
8. **Admin-bypass test** — temporarily set yuliia `is_admin=true` via the user edit form → wait 30 s → yuliia can now navigate to SalesChan without bounce. Revert the flag.
9. **Group-delete behaviour** — delete the Sales group. Verify SalesChan's `call_group_id` flips to NULL (channel becomes visible-to-all).

**Step 5: Update `docs/open_issues.md`**

Move the "Call groups — per-user channel-access scoping" entry from "Still open" to a Resolved bullet with today's commit hashes.

If True Call Hold landed first and you hit a merge conflict with its Resolved entry, just stack both bullets in chronological order. Keep both.

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
# Edit docs/open_issues.md per above
git add docs/open_issues.md
git commit -m "$(cat <<'EOF'
docs: call-groups resolved (2026-04-22)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

---

## Verification checklist (all phases)

After Task 7:

- [ ] `docker exec -w /app/server ptt-admin-1 alembic current` → `g3b8d4f6e2a9`.
- [ ] Full pytest suite → 142+ passed (no regressions).
- [ ] `docker logs ptt-admin-1 --tail 30 | grep call-group` shows the poller started.
- [ ] Dashboard Directory → Call Groups sub-tab works.
- [ ] User-edit modal has the call-groups checkbox-list.
- [ ] Channel-create form has the call-group dropdown.
- [ ] Smoke step 6 (non-member bounced) succeeds.
- [ ] Smoke step 7 (member allowed) succeeds.
- [ ] Smoke step 8 (admin bypass) succeeds.
- [ ] Smoke step 9 (group delete → channels become NULL-tagged) succeeds.
- [ ] `docs/open_issues.md` updated.

All eleven green = Call Groups ships.

---

## Open questions / deferred (carried from design)

1. **App-side carousel filter** — hide non-visible channels from the knob navigation. Defer.
2. **Per-channel ACL beyond group membership** — read-only vs talk. Defer.
3. **Group nesting / hierarchy** — defer.
4. **Bulk membership ops beyond `PUT /members`** (CSV import, etc.) — defer.

---

## Dependencies + parallelization

- Tasks 1 → 2 → 3 (schema → endpoints → user/channel extensions) are sequential.
- Task 4 depends on Task 1's models + `_bounce_from_phone` rename to `_bounce_from_channel` (which the phone-acl tests will need updated import paths for if the rename touches their call sites).
- Task 5 depends on Task 4's `update_call_group_state` signature.
- Task 6 depends on Tasks 2 + 3 (endpoints exist).
- Task 7 is the deploy gate.

Solo execution: ~3-4 hours of focused work.

**Coordination with True Call Hold parallel session**: both ship independently. The only file both touch is `docs/open_issues.md` at Resolved-time — easy merge (each appends its Resolved entry). Code-file overlap is zero (`server/api/sip.py` vs `server/api/call_groups.py`; `sip_bridge/*` vs `server/murmur/client.py`; different migrations).

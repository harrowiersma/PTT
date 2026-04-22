# Call Groups — True Hiding via Mumble ACL — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Replace the bounce-on-entry UX of the shipped call-groups feature with true server-side hiding via Murmur's native ACL. Restricted channels become invisible in the channel tree to non-members — "enterprise style."

**Architecture:** Admin captures each user's Mumble cert hash on first connect, registers them in Murmur's sqlite (`admin_sqlite.register_user`), and sets per-channel ACL (`Deny Enter+Traverse` for `@all`, `Allow Enter+Traverse+Speak` for specific member `user_id`s). The existing bounce + sweep stay as defense-in-depth. Server-only — no app changes.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (backend), pymumble (cert-hash capture), Murmur sqlite via `docker exec` (ACL write + restart).

**Companion design doc:** `docs/plans/2026-04-22-call-groups-hiding-design.md`

---

## Pre-flight

```bash
# Sidecar + admin running
docker ps --format "{{.Names}}" | grep -E "ptt-pytest|ptt-admin"

# Pytest baseline — expect 149 (call-groups feature + sweep fix already in)
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q 2>&1 | tail -3"

# Alembic head should be g3b8d4f6e2a9 (call_groups schema applied)
docker exec -w /app/server ptt-admin-1 alembic current 2>&1 | tail -1
```

If the pytest baseline differs or alembic isn't at `g3b8d4f6e2a9`, stop + report. This plan assumes the prior call-groups feature is deployed and working (bounce fires, ACL is just broken because PTTAdmin can't force-move — that's what we're fixing).

---

## Phases

1. **Task 1** — schema migration (add `users.mumble_cert_hash` + `users.mumble_registered_user_id`).
2. **Task 2** — bridge cert-hash capture in `MurmurClient._on_user_created_sync` + USERUPDATED extension.
3. **Task 3** — `admin_sqlite.register_user` + tests (docker-exec mocked).
4. **Task 4** — background auto-registration scheduler in `server/main.py` lifespan.
5. **Task 5** — `admin_sqlite.set_channel_acl` + `clear_channel_acl` + `batched_acl_apply` helpers + tests.
6. **Task 6** — wire ACL apply into `PUT /members`, `PUT /channels`, `DELETE /groups` endpoints.
7. **Task 7** — dashboard UX: registration badges + "force-all reconnect" button + feature flag toggle.
8. **Task 8** — deploy + staged rollout: land 1→2→(observe)→3→4→(observe)→5→6→7, smoke.

Each task ships independently. Tasks 1-4 are cert-hash data collection with no behaviour change — they can ship freely. Tasks 5-8 are behind the `call_groups_hiding` feature flag (default off).

---

## Task 1: Schema — `users.mumble_cert_hash` + `users.mumble_registered_user_id`

**Files:**
- Modify: `server/models.py` (append columns to `User`)
- Create: `server/alembic/versions/h4c9e5a7f3b2_call_groups_hiding.py`
- Test: `tests/test_call_groups_hiding_model.py`

**Step 1: Write the failing test**

Create `tests/test_call_groups_hiding_model.py`:

```python
import pytest
from sqlalchemy import select
from server.models import User


@pytest.mark.asyncio
async def test_user_cert_hash_defaults_null(db_session):
    u = User(username="alice", mumble_password="x")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    assert u.mumble_cert_hash is None
    assert u.mumble_registered_user_id is None


@pytest.mark.asyncio
async def test_user_cert_hash_roundtrip(db_session):
    u = User(username="bob", mumble_password="x",
             mumble_cert_hash="abc123" * 6,
             mumble_registered_user_id=42)
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    assert u.mumble_cert_hash == "abc123" * 6
    assert u.mumble_registered_user_id == 42


@pytest.mark.asyncio
async def test_cert_hash_unique_when_set(db_session):
    """Two users can't share a non-null cert hash."""
    db_session.add(User(username="u1", mumble_password="x",
                         mumble_cert_hash="deadbeef"))
    await db_session.commit()
    db_session.add(User(username="u2", mumble_password="x",
                         mumble_cert_hash="deadbeef"))
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_cert_hash_nulls_coexist(db_session):
    """Multiple users with NULL cert_hash are allowed (partial unique)."""
    db_session.add(User(username="u1", mumble_password="x"))
    db_session.add(User(username="u2", mumble_password="x"))
    await db_session.commit()  # should not raise
    rows = (await db_session.execute(
        select(User).where(User.mumble_cert_hash.is_(None))
    )).scalars().all()
    assert len(rows) >= 2
```

**Step 2: Run, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_call_groups_hiding_model.py -v"
```

Expected: `AttributeError: 'User' object has no attribute 'mumble_cert_hash'`.

**Step 3: Extend the model**

Append to `User` in `server/models.py`:

```python
    # Mumble cert SHA-1 hash, captured by the bridge on first connect.
    # Unique when set — one hash per device/cert.
    mumble_cert_hash: Mapped[str] = mapped_column(String(128), nullable=True)
    # The user_id assigned in mumble-server.sqlite after registration.
    # Populated by admin_sqlite.register_user; NULL means "not yet registered".
    mumble_registered_user_id: Mapped[int] = mapped_column(Integer, nullable=True)
```

**Step 4: Create the migration**

Create `server/alembic/versions/h4c9e5a7f3b2_call_groups_hiding.py`:

```python
"""users: mumble_cert_hash + mumble_registered_user_id

Revision ID: h4c9e5a7f3b2
Revises: g3b8d4f6e2a9
Create Date: 2026-04-22
"""
from alembic import op
import sqlalchemy as sa

revision = "h4c9e5a7f3b2"
down_revision = "g3b8d4f6e2a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mumble_cert_hash", sa.String(128), nullable=True))
    op.add_column("users", sa.Column("mumble_registered_user_id", sa.Integer, nullable=True))
    # Partial unique on Postgres; on SQLite, a plain unique still allows multiple NULLs.
    op.create_index(
        "uq_users_mumble_cert_hash",
        "users", ["mumble_cert_hash"],
        unique=True,
        postgresql_where=sa.text("mumble_cert_hash IS NOT NULL"),
    )
    op.create_index(
        "uq_users_mumble_registered_user_id",
        "users", ["mumble_registered_user_id"],
        unique=True,
        postgresql_where=sa.text("mumble_registered_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_mumble_registered_user_id", table_name="users")
    op.drop_index("uq_users_mumble_cert_hash", table_name="users")
    op.drop_column("users", "mumble_registered_user_id")
    op.drop_column("users", "mumble_cert_hash")
```

**Step 5: Apply migration + run tests**

```bash
docker compose up -d --build admin
docker exec -w /app/server ptt-admin-1 alembic upgrade head
docker exec -w /app/server ptt-admin-1 alembic current
# Expected: h4c9e5a7f3b2 (head)

docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_call_groups_hiding_model.py -v"
```

Expected: 4 passed.

Full suite:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 153 passed (149 + 4 new).

**Step 6: Commit + push**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add server/models.py \
        server/alembic/versions/h4c9e5a7f3b2_call_groups_hiding.py \
        tests/test_call_groups_hiding_model.py
git commit -m "$(cat <<'EOF'
call-groups-hiding: schema (users.mumble_cert_hash + registered_user_id)

Adds two nullable columns to the users table. Populated by the bridge's
cert-hash capture (Task 2) and the registration path (Task 3). Both
columns are unique when non-null (partial unique on Postgres, plain
unique on SQLite — NULL coexistence is the same).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

---

## Task 2: Bridge cert-hash capture

**Files:**
- Modify: `server/murmur/client.py` (extend `_on_user_created_sync` + add a USERUPDATED hook)
- Test: `tests/test_cert_hash_capture.py`

**Step 1: Write the failing test**

Create `tests/test_cert_hash_capture.py`, following the stub pattern from `tests/test_phone_acl.py`:

```python
"""Unit tests for cert-hash capture on USERCREATED/USERUPDATED."""
from __future__ import annotations

import sys, types
from unittest.mock import MagicMock

def _install_stubs():
    # Same stubs as test_phone_acl.py
    ...

_install_stubs()

from server.murmur.client import _capture_cert_hash_sync  # new helper


def test_capture_writes_hash_on_first_sighting(monkeypatch, tmp_path):
    """When a user connects and User.hash is set, admin users row is updated."""
    # Use in-memory sqlite for this test; patch settings.database_url_sync.
    ...


def test_capture_skip_when_no_hash(...):
    """If the user dict has no 'hash' field, nothing is written."""
    ...


def test_capture_no_overwrite(...):
    """Existing non-null mumble_cert_hash is not overwritten on reconnect
    unless the new hash differs."""
    ...


def test_capture_updates_on_hash_change(...):
    """If the hash changes (user re-provisioned), admin DB is updated."""
    ...
```

**Step 2: Run, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_cert_hash_capture.py -v"
```

Expected: `ImportError: cannot import name '_capture_cert_hash_sync'`.

**Step 3: Implement the capture helper**

In `server/murmur/client.py`, near `_on_user_created_sync`:

```python
def _capture_cert_hash_sync(event) -> None:
    """Extract cert hash from a pymumble user dict and persist it to
    admin's users.mumble_cert_hash. Runs on pymumble's sync thread via
    short-lived engine (same pattern as _on_user_created_sync).

    No-op when: the user has no hash, the hash matches the stored one,
    or the user is a bot.
    """
    try:
        name = event.get("name") if isinstance(event, dict) else getattr(event, "name", None)
        hash_ = event.get("hash") if isinstance(event, dict) else getattr(event, "hash", None)
    except Exception:
        return
    if not name or not hash_ or _is_bot_username(name):
        return

    try:
        from sqlalchemy import create_engine, select as _select
        from sqlalchemy.orm import sessionmaker
        from server.config import settings
        from server.models import User

        engine = create_engine(settings.database_url_sync, echo=False)
        Session = sessionmaker(engine, expire_on_commit=False)
        with Session() as db:
            user = db.execute(_select(User).where(User.username == name)).scalar_one_or_none()
            if user is None:
                return
            if user.mumble_cert_hash == hash_:
                return  # no change
            user.mumble_cert_hash = hash_
            # If the hash changed, invalidate the registration — user_id
            # becomes stale until re-registered with the new hash.
            user.mumble_registered_user_id = None
            db.commit()
            logger.info("cert-hash: captured for %s (was_new=%s)",
                        name, user.mumble_cert_hash is not None)
    except Exception as e:
        logger.error("cert-hash capture failed for %s: %s", name, e)
```

**Step 4: Wire it into the USERCREATED + USERUPDATED callbacks**

In `MurmurClient.connect()` (around line 120), after the existing `PYMUMBLE_CLBK_USERCREATED`:

```python
# Capture cert hash on both CREATED and UPDATED. UPDATED fires on
# many state changes; _capture_cert_hash_sync is idempotent + cheap
# when the hash matches.
_orig_created = lambda user: (_on_user_created_sync(user),
                               _capture_cert_hash_sync(user))
self._mumble.callbacks.set_callback(
    pymumble.constants.PYMUMBLE_CLBK_USERCREATED,
    _orig_created,
)
```

And extend `_on_user_updated` (on the main USERUPDATED path) to also call `_capture_cert_hash_sync(user)` — idempotent, cheap, catches the case where a user reconnects with a new cert.

**Step 5: Run tests**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_cert_hash_capture.py tests/test_phone_acl.py \
  tests/test_call_group_acl.py -v"
```

Expected: new tests pass (4) + existing bridge tests still pass (15).

Full suite:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 157 passed (153 + 4 new).

**Step 6: Commit + push**

```bash
git add server/murmur/client.py tests/test_cert_hash_capture.py
git commit -m "$(cat <<'EOF'
call-groups-hiding: capture Mumble cert hash on connect

Bridge's USERCREATED + USERUPDATED callbacks now read user.hash and
write it to admin DB users.mumble_cert_hash. Idempotent + no-op if
the hash matches the stored one. On hash change (cert rotation) the
mumble_registered_user_id is reset so the next registration pass
picks up the new hash.

No behaviour change yet — Task 3 uses the captured hash to register
users in mumble-server.sqlite.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

---

## Task 3: `admin_sqlite.register_user` + tests

**Files:**
- Modify: `server/murmur/admin_sqlite.py` (append `register_user` + `_next_user_id`)
- Test: `tests/test_admin_sqlite_register.py`

**Step 1: Write the failing tests**

`tests/test_admin_sqlite_register.py` — mocks `_sqlite_exec`:

```python
def test_register_user_inserts_users_and_user_info(monkeypatch):
    """Given a free user_id, INSERT runs against users + user_info."""
    ...

def test_register_user_picks_next_free_id(monkeypatch):
    """Max existing user_id + 1, skipping gaps is fine."""
    ...

def test_register_user_restarts_murmur(monkeypatch):
    """After the INSERT, docker container.restart() is invoked."""
    ...

def test_register_user_serialized(monkeypatch):
    """Two concurrent calls serialize under _admin_lock."""
    ...
```

**Step 2: Run, expect fail**

Will fail on import (`register_user` doesn't exist yet).

**Step 3: Implement `register_user`**

In `server/murmur/admin_sqlite.py`:

```python
def _next_mumble_user_id() -> int:
    """Highest user_id in Murmur sqlite + 1. SuperUser is always user_id 0."""
    out = _sqlite_exec(
        "SELECT COALESCE(MAX(user_id), 0) + 1 FROM users WHERE server_id = 1;"
    ).strip()
    return int(out) if out else 1


def register_user(username: str, cert_hash: str) -> int:
    """Register an app user in Murmur's sqlite with their cert hash.

    Inserts into `users` + `user_info` (key=user_hash). Bounces the
    murmur container so the new registration takes effect. Serialized
    under the admin lock.

    Returns the new Mumble user_id.
    """
    with _admin_lock:
        uid = _next_mumble_user_id()
        # Users row: pw/salt/kdfiterations all NULL — this is cert-auth.
        _sqlite_exec(
            f"INSERT INTO users (server_id, user_id, name, pw, salt, kdfiterations, "
            f"lastchannel, texture, last_active, last_disconnect) VALUES "
            f"(1, {uid}, {shlex.quote(username)!r}, NULL, NULL, NULL, 0, NULL, "
            f"datetime('now'), datetime('now'));"
        )
        _sqlite_exec(
            f"INSERT INTO user_info (server_id, user_id, key, value) VALUES "
            f"(1, {uid}, 'user_hash', {shlex.quote(cert_hash)!r});"
        )
        _restart_murmur_container()
        logger.info("Registered %s in Murmur sqlite (user_id=%d)", username, uid)
        return uid
```

(`_restart_murmur_container` is the same helper as `delete_user_and_restart` uses — refactor if it's inline there.)

**Step 4: Run tests**

Expected: 4 new passes. Full suite: 161 passed (157 + 4).

**Step 5: Commit + push**

```bash
git commit -am "call-groups-hiding: admin_sqlite.register_user helper + tests"
```

---

## Task 4: Auto-registration scheduler

**Files:**
- Modify: `server/main.py` (add `_run_pending_registrations` lifespan task)
- Test: `tests/test_registration_scheduler.py`

**Step 1: Test** — verify the scheduler picks up users with cert hash but no registered_user_id, calls `register_user`, writes the returned id back to the DB.

**Step 2: Implement**

In `server/main.py` lifespan, alongside the call-groups poller:

```python
async def _run_pending_registrations():
    while True:
        try:
            async with async_session() as db:
                pending = (await db.execute(
                    select(User).where(
                        User.mumble_cert_hash.is_not(None),
                        User.mumble_registered_user_id.is_(None),
                    ).limit(10)  # batch size
                )).scalars().all()
            for user in pending:
                try:
                    uid = await asyncio.get_event_loop().run_in_executor(
                        None, admin_sqlite.register_user,
                        user.username, user.mumble_cert_hash,
                    )
                    async with async_session() as db2:
                        u2 = await db2.get(User, user.id)
                        if u2 is not None:
                            u2.mumble_registered_user_id = uid
                            await db2.commit()
                except Exception as e:
                    logger.warning("auto-register failed for %s: %s",
                                   user.username, e)
        except Exception as e:
            logger.warning("auto-reg scheduler tick failed: %s", e)
        await asyncio.sleep(60)
```

Run every 60 s. Each registration costs a murmur restart, so batch hard — but expect few pending users in steady state.

**Step 3: Feature-gate**

Behind a new `FeatureFlag(key="call_groups_hiding", enabled=False)` row. The scheduler only runs ticks if the flag is enabled at the start of each iteration. Cert-hash capture (Task 2) still runs regardless — it's just data collection.

Seed the flag in `server/alembic/versions/c1a2b3d4e5f6_feature_flags.py` style — extend the existing feature-flags migration or use an UPSERT in a new step of `h4c9e5a7f3b2_call_groups_hiding.py`.

**Step 4: Commit + push**

```bash
git commit -am "call-groups-hiding: auto-registration scheduler (flag-gated, 60s)"
```

---

## Task 5: ACL helpers (`set_channel_acl` + `clear_channel_acl` + `batched_acl_apply`)

**Files:**
- Modify: `server/murmur/admin_sqlite.py`
- Test: `tests/test_admin_sqlite_acl.py`

**Step 1: Test** — mock `_sqlite_exec`, assert:
- `set_channel_acl(9, [42, 43])` issues a DELETE then three INSERTs (deny-@all, allow-42, allow-43).
- `clear_channel_acl(9)` only issues the DELETE.
- `batched_acl_apply([(9, [42, 43]), (10, [])])` issues the SQL for each + exactly one container restart.

**Step 2: Implement**

```python
# Mumble permission bits from Murmur source:
_PERM_TRAVERSE = 0x02
_PERM_ENTER = 0x04
_PERM_SPEAK = 0x08
_DENY_TRAVERSE_ENTER = _PERM_TRAVERSE | _PERM_ENTER
_GRANT_MEMBER = _PERM_TRAVERSE | _PERM_ENTER | _PERM_SPEAK


def set_channel_acl(mumble_channel_id: int, member_user_ids: list[int],
                     *, restart: bool = True) -> None:
    """Replace the ACL on a channel with the deny/allow pair."""
    with _admin_lock:
        _sqlite_exec(
            f"DELETE FROM acl WHERE server_id=1 AND channel_id={mumble_channel_id};"
        )
        _sqlite_exec(
            f"INSERT INTO acl (server_id, channel_id, priority, user_id, group_name, "
            f"apply_here, apply_sub, grantpriv, revokepriv) VALUES "
            f"(1, {mumble_channel_id}, 1, NULL, 'all', 1, 1, 0, {_DENY_TRAVERSE_ENTER});"
        )
        for i, uid in enumerate(member_user_ids):
            _sqlite_exec(
                f"INSERT INTO acl (server_id, channel_id, priority, user_id, group_name, "
                f"apply_here, apply_sub, grantpriv, revokepriv) VALUES "
                f"(1, {mumble_channel_id}, {2+i}, {uid}, NULL, 1, 1, {_GRANT_MEMBER}, 0);"
            )
        if restart:
            _restart_murmur_container()


def clear_channel_acl(mumble_channel_id: int, *, restart: bool = True) -> None:
    with _admin_lock:
        _sqlite_exec(
            f"DELETE FROM acl WHERE server_id=1 AND channel_id={mumble_channel_id};"
        )
        if restart:
            _restart_murmur_container()


def batched_acl_apply(changes: list[tuple[int, list[int] | None]]) -> None:
    """Apply a list of (channel_id, member_user_ids) changes with ONE restart.

    `member_user_ids=None` means clear the ACL entirely."""
    with _admin_lock:
        for cid, members in changes:
            if members is None:
                clear_channel_acl(cid, restart=False)
            else:
                set_channel_acl(cid, members, restart=False)
        _restart_murmur_container()
```

**Step 3: Tests pass + full suite 165 passed.**

**Step 4: Commit + push.**

---

## Task 6: Wire ACL into the call-group endpoints

**Files:**
- Modify: `server/api/call_groups.py` (PUT /members, PUT /channels, DELETE /group)

**Changes:**

In each endpoint, after `db.commit()`:

1. Collect the affected (channel, member_user_ids) changes.
2. Call `admin_sqlite.batched_acl_apply(changes)`.

Gate the whole call-site block behind a `_features.is_enabled("call_groups_hiding")` check. Flag off → falls back to today's bounce-only behaviour.

**Tests** — existing `test_call_groups_api.py` tests still pass. Add new tests that mock `admin_sqlite.batched_acl_apply` + assert the right shape of changes gets passed for each endpoint.

**Commit + push.**

---

## Task 7: Dashboard UX + feature-flag toggle

**Files:**
- Modify: `server/dashboard/index.html`

1. Call Groups tab — member checkbox list: show a `pending` pill next to users where `mumble_registered_user_id IS NULL`.
2. Users directory — add a "Registered" column (badge: "registered" / "pending cert" / "pending registration").
3. Features tab — add the `call_groups_hiding` toggle.
4. Call Groups tab — add a "Force all reconnect" button (hits a new endpoint that calls `admin_sqlite._restart_murmur_container()` directly).

**Commit + push.**

---

## Task 8: Deploy + staged smoke

**Step 1: Land Tasks 1-4 only.** Capture cert hashes, register users, but don't apply any ACL yet (feature flag off).

Deploy:

```bash
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch "
    cd /opt/ptt && git pull --ff-only && \
    docker compose up -d --build admin && \
    docker exec -w /app/server ptt-admin-1 alembic upgrade head && \
    docker exec -w /app/server ptt-admin-1 alembic current
"
```

Verify all deployed users reconnect within 24 h and get a `mumble_cert_hash` populated. Then let the scheduler register them (may need one manual murmur restart to trigger).

**Step 2: Wait 24+ h for full cert-hash capture.**

Query:
```bash
ssh ... "docker exec ptt-postgres-1 psql -U ptt -d ptt -c \
  \"SELECT COUNT(*) total, \
           COUNT(mumble_cert_hash) captured, \
           COUNT(mumble_registered_user_id) registered FROM users;\""
```

Expected: all three counts equal after 24 h of observation + one force-all-reconnect pass.

**Step 3: Land Tasks 5-7 (ACL apply + dashboard).**

Deploy, flip `call_groups_hiding = true`.

**Step 4: Smoke**

1. Dashboard → Features → enable `call_groups_hiding`.
2. Create "Sales" group, add harro, tag "SalesChan" with it.
3. Click "Apply ACL" (or let the `PUT /channels` handler do it automatically).
4. Wait for murmur restart (~3 s).
5. On R259060618 (yuliia, non-member): carousel should NOT show SalesChan. Navigate right — arrows skip it.
6. On R259060623 (harro, member): SalesChan appears as normal.
7. Remove harro from Sales → murmur restart → harro's carousel no longer shows SalesChan.
8. Delete the Sales group → SalesChan reverts to visible-to-all.

**Step 5: Update `docs/open_issues.md`.**

Move the "Call groups hide" entry from open → resolved with today's commit hashes.

---

## Verification checklist (all phases)

- [ ] `docker exec -w /app/server ptt-admin-1 alembic current` → `h4c9e5a7f3b2`.
- [ ] Pytest → 169+ passed (149 existing + ~20 new).
- [ ] `docker logs ptt-admin-1 --tail 30 | grep "cert-hash: captured"` shows recent captures.
- [ ] `docker logs ptt-admin-1 --tail 30 | grep "Registered .* in Murmur sqlite"` shows registrations.
- [ ] DB query: all active users have `mumble_cert_hash` + `mumble_registered_user_id` set.
- [ ] Murmur sqlite `acl` table populated for tagged channels.
- [ ] Smoke step 5 (yuliia's carousel doesn't show SalesChan) succeeds.
- [ ] Smoke step 6 (harro sees SalesChan) succeeds.
- [ ] Feature flag toggle off → ACL cleared → everyone sees everything.

---

## Rollback

If anything goes wrong:

1. Dashboard → Features → disable `call_groups_hiding`. Scheduler stops issuing new ACL changes.
2. `POST /api/admin/clear-all-call-group-acl` (new admin endpoint, Task 7 scope) — strips every row from `acl` table where `channel_id` matches a tagged channel. One restart. Restores visibility for everyone.
3. `alembic downgrade g3b8d4f6e2a9` — drops the two new columns. Registrations already written to `mumble-server.sqlite` stay (harmless — they just identify users by cert; no ACL references them).

---

## Open questions / deferred

1. App-side hiding as an additional layer (defense in depth, faster UI update than ACL + murmur restart). Defer.
2. Nested call groups / hierarchy (team within team, with inheritance). Defer.
3. Per-user read-only vs talk grants (user can see and listen but can't TX). Defer — current `_GRANT_MEMBER` includes Speak; reducing it to just Traverse+Enter on a per-user basis is a schema change.
4. Audit log for ACL diff per save. Defer — existing `AuditLog` entry per endpoint call is enough for now.
5. Migration of already-registered users (if some team's mumble-server.sqlite has pre-existing regs): skip; assume clean state.

---

## Dependencies + coordination

- **Depends on** `g3b8d4f6e2a9_call_groups` (already shipped).
- **No other active-session conflicts** expected — no overlap with True Call Hold, dispatch, etc.
- **Feature-flag gates** let this ship in observation mode without changing production behaviour until flag is lit.

Solo execution: ~4-6 hours focused work, split across two observation-window waits.

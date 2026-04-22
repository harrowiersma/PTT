"""Unit tests for cert-hash capture on USERCREATED/USERUPDATED.

Mirrors the in-process style of test_murmur_auto_online.py: uses the
shared sqlite test.db + db_session fixture, drives the sync helper
directly, verifies the persisted row.
"""
import pytest
from sqlalchemy import select
from server.models import User


@pytest.mark.asyncio
async def test_capture_writes_hash_on_first_sighting(db_session):
    """When a user connects with a `hash`, users.mumble_cert_hash is filled."""
    u = User(username="alice", mumble_password="x")
    db_session.add(u)
    await db_session.commit()

    from server.murmur.client import _capture_cert_hash_sync

    _capture_cert_hash_sync({"name": "alice", "hash": "deadbeef" * 5})

    await db_session.refresh(u)
    assert u.mumble_cert_hash == "deadbeef" * 5
    # First registration is still pending.
    assert u.mumble_registered_user_id is None


@pytest.mark.asyncio
async def test_capture_skip_when_no_hash(db_session):
    """No 'hash' key on the event → no DB write."""
    u = User(username="bob", mumble_password="x")
    db_session.add(u)
    await db_session.commit()

    from server.murmur.client import _capture_cert_hash_sync

    _capture_cert_hash_sync({"name": "bob"})
    _capture_cert_hash_sync({"name": "bob", "hash": ""})
    _capture_cert_hash_sync({"name": "bob", "hash": None})

    await db_session.refresh(u)
    assert u.mumble_cert_hash is None


@pytest.mark.asyncio
async def test_capture_no_overwrite_when_unchanged(db_session):
    """Identical hash on reconnect is a no-op (doesn't reset registered_user_id)."""
    u = User(
        username="carol",
        mumble_password="x",
        mumble_cert_hash="abc123",
        mumble_registered_user_id=7,
    )
    db_session.add(u)
    await db_session.commit()

    from server.murmur.client import _capture_cert_hash_sync

    _capture_cert_hash_sync({"name": "carol", "hash": "abc123"})

    await db_session.refresh(u)
    assert u.mumble_cert_hash == "abc123"
    # Registration preserved — nothing changed, so no invalidation.
    assert u.mumble_registered_user_id == 7


@pytest.mark.asyncio
async def test_capture_updates_on_hash_change(db_session):
    """Hash changes (cert rotated) → hash updated, registered_user_id cleared."""
    u = User(
        username="dave",
        mumble_password="x",
        mumble_cert_hash="oldhash",
        mumble_registered_user_id=9,
    )
    db_session.add(u)
    await db_session.commit()

    from server.murmur.client import _capture_cert_hash_sync

    _capture_cert_hash_sync({"name": "dave", "hash": "newhash"})

    await db_session.refresh(u)
    assert u.mumble_cert_hash == "newhash"
    # Registration must be invalidated — the stored user_id pointed at
    # the old cert; next scheduler tick re-registers with the new one.
    assert u.mumble_registered_user_id is None


@pytest.mark.asyncio
async def test_capture_skips_bot_usernames(db_session):
    """Bot usernames must be ignored — no DB work, no errors."""
    from server.murmur.client import _capture_cert_hash_sync

    for name in ("PTTAdmin", "PTTWeather", "PTTPhone-1"):
        _capture_cert_hash_sync({"name": name, "hash": "whatever"})


@pytest.mark.asyncio
async def test_capture_ignores_unknown_username(db_session):
    """Username not in DB → no-op, no raise."""
    from server.murmur.client import _capture_cert_hash_sync

    _capture_cert_hash_sync({"name": "ghost", "hash": "whatever"})

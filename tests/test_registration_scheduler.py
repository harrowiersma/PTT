"""Unit tests for the auto-registration scheduler.

The scheduler (server.murmur.registration.run_pending_registrations_once)
finds users with a captured cert_hash but no registered_user_id, calls
admin_sqlite.register_user for each, and writes the returned Mumble
user_id back to the DB. Gated on feature flag `call_groups_hiding`.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from server import features as _features
from server.models import User


@pytest.mark.asyncio
async def test_flag_disabled_no_registration(db_session):
    """Default state (flag off) → no work done even if users are pending."""
    u = User(username="alice", mumble_password="x", mumble_cert_hash="h1")
    db_session.add(u)
    await db_session.commit()

    from server.murmur.registration import run_pending_registrations_once

    with patch(
        "server.murmur.admin_sqlite.register_user"
    ) as m_register:
        count = await run_pending_registrations_once()
    assert count == 0
    m_register.assert_not_called()


@pytest.mark.asyncio
async def test_flag_enabled_registers_pending(db_session, monkeypatch):
    """Flag on + pending user → register_user called, uid written back."""
    monkeypatch.setitem(_features._cache, "call_groups_hiding", True)

    u = User(username="bob", mumble_password="x", mumble_cert_hash="h2")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)

    from server.murmur.registration import run_pending_registrations_once

    with patch(
        "server.murmur.admin_sqlite.register_user", return_value=17
    ) as m_register:
        count = await run_pending_registrations_once()

    assert count == 1
    m_register.assert_called_once_with("bob", "h2")
    await db_session.refresh(u)
    assert u.mumble_registered_user_id == 17


@pytest.mark.asyncio
async def test_skips_users_without_cert_hash(db_session, monkeypatch):
    """Users with no cert_hash OR already-registered are not touched."""
    monkeypatch.setitem(_features._cache, "call_groups_hiding", True)

    u1 = User(username="no_hash", mumble_password="x")  # no cert
    u2 = User(
        username="has_reg",
        mumble_password="x",
        mumble_cert_hash="h",
        mumble_registered_user_id=5,
    )  # already registered
    db_session.add_all([u1, u2])
    await db_session.commit()

    from server.murmur.registration import run_pending_registrations_once

    with patch(
        "server.murmur.admin_sqlite.register_user"
    ) as m_register:
        count = await run_pending_registrations_once()

    assert count == 0
    m_register.assert_not_called()


@pytest.mark.asyncio
async def test_continues_on_per_user_failure(db_session, monkeypatch):
    """One user's register_user raising must not stop the others."""
    monkeypatch.setitem(_features._cache, "call_groups_hiding", True)

    u_fail = User(username="fail_user", mumble_password="x", mumble_cert_hash="hx")
    u_ok = User(username="ok_user", mumble_password="x", mumble_cert_hash="hy")
    db_session.add_all([u_fail, u_ok])
    await db_session.commit()
    await db_session.refresh(u_fail)
    await db_session.refresh(u_ok)

    from server.murmur.registration import run_pending_registrations_once

    def _side_effect(name, cert_hash):
        if name == "fail_user":
            raise RuntimeError("murmur restart timed out")
        return 99

    with patch(
        "server.murmur.admin_sqlite.register_user",
        side_effect=_side_effect,
    ) as m_register:
        count = await run_pending_registrations_once()

    # Exactly one successful registration; failed one remains pending.
    assert count == 1
    assert m_register.call_count == 2
    await db_session.refresh(u_fail)
    await db_session.refresh(u_ok)
    assert u_fail.mumble_registered_user_id is None
    assert u_ok.mumble_registered_user_id == 99


def test_call_groups_hiding_in_feature_keys():
    """The new flag must appear in FEATURE_KEYS so the PUT endpoint
    allows toggling it."""
    from server.features import FEATURE_KEYS
    assert "call_groups_hiding" in FEATURE_KEYS


def test_call_groups_hiding_defaults_false():
    """Before any DB seed or refresh, is_enabled must return False for
    the hiding flag so the feature stays dormant on a clean install."""
    from server.features import FEATURE_DEFAULTS
    assert FEATURE_DEFAULTS["call_groups_hiding"] is False

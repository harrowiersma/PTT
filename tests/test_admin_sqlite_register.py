"""Unit tests for server.murmur.admin_sqlite.register_user.

Mirrors test_admin_sqlite.py — mocks _sqlite_exec and restart_murmur
so nothing touches real docker.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest


@pytest.fixture
def fake_sqlite():
    calls: list[str] = []
    responses: list[str] = []

    def _fake(sql: str) -> str:
        calls.append(sql)
        if not responses:
            return ""
        return responses.pop(0)

    with patch("server.murmur.admin_sqlite._sqlite_exec", side_effect=_fake):
        yield calls, responses


@pytest.fixture
def fake_restart():
    with patch("server.murmur.admin_sqlite.restart_murmur") as m:
        yield m


def test_register_user_inserts_users_and_user_info(fake_sqlite, fake_restart):
    calls, responses = fake_sqlite
    # SELECT MAX(user_id)+1 → "5" ; INSERT users → "" ; INSERT user_info → ""
    responses.extend(["5", "", ""])
    from server.murmur.admin_sqlite import register_user

    uid = register_user("alice", "deadbeef" * 5)

    assert uid == 5
    # One users INSERT + one user_info INSERT.
    users_ins = [s for s in calls if "INSERT INTO users" in s]
    info_ins = [s for s in calls if "INSERT INTO user_info" in s]
    assert len(users_ins) == 1
    assert len(info_ins) == 1
    # Both reference the new user_id.
    assert ", 5," in users_ins[0]
    assert ", 5," in info_ins[0]
    # The user_info row stores the cert hash under key='user_hash'.
    assert "'user_hash'" in info_ins[0]
    assert "'" + "deadbeef" * 5 + "'" in info_ins[0]
    # Username quoted correctly.
    assert "'alice'" in users_ins[0]
    # Murmur restarted once.
    fake_restart.assert_called_once()


def test_register_user_picks_next_free_id(fake_sqlite, fake_restart):
    calls, responses = fake_sqlite
    # MAX query returns 0 → first user id is 1 (SuperUser is 0).
    responses.extend(["1", "", ""])
    from server.murmur.admin_sqlite import register_user

    uid = register_user("bob", "hashb")
    assert uid == 1


def test_register_user_escapes_apostrophes(fake_sqlite, fake_restart):
    """Usernames with apostrophes must be safely quoted."""
    calls, responses = fake_sqlite
    responses.extend(["3", "", ""])
    from server.murmur.admin_sqlite import register_user

    uid = register_user("O'Brien", "hashc")
    assert uid == 3
    users_ins = next(s for s in calls if "INSERT INTO users" in s)
    assert "'O''Brien'" in users_ins


def test_register_user_serialized(fake_sqlite, fake_restart):
    """Two concurrent register_user calls must serialize under _admin_lock —
    they can't both read the same MAX and collide on the same user_id."""
    calls, responses = fake_sqlite
    # Six responses total: two SELECTs + four INSERT/INFO.
    responses.extend(["5", "", "", "6", "", ""])
    from server.murmur.admin_sqlite import register_user

    results: list[int] = []

    def _go(name, h):
        results.append(register_user(name, h))

    t1 = threading.Thread(target=_go, args=("u1", "h1"))
    t2 = threading.Thread(target=_go, args=("u2", "h2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both threads finished and got distinct ids.
    assert sorted(results) == [5, 6]
    # Both caused a restart — two calls total.
    assert fake_restart.call_count == 2

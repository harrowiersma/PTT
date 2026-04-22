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


def test_register_user_inserts_users_row(fake_sqlite, fake_restart):
    calls, responses = fake_sqlite
    # SELECT MAX(user_id)+1 → "5" ; INSERT users → ""
    responses.extend(["5", ""])
    from server.murmur.admin_sqlite import register_user

    uid = register_user("alice", "hunter2")

    assert uid == 5
    # One users INSERT, no user_info (no cert_hash passed).
    users_ins = [s for s in calls if "INSERT INTO users" in s]
    info_ins = [s for s in calls if "INSERT INTO user_info" in s]
    assert len(users_ins) == 1
    assert len(info_ins) == 0
    assert ", 5," in users_ins[0]
    assert "'alice'" in users_ins[0]
    # Password hash is hex-encoded 48 bytes (96 hex chars) with
    # 8000 iterations and a hex-encoded 16-char salt.
    import re
    pw_matches = re.findall(r"'([0-9a-f]{96})'", users_ins[0])
    salt_matches = re.findall(r"'([0-9a-f]{16})'", users_ins[0])
    assert len(pw_matches) == 1
    assert len(salt_matches) == 1
    assert "8000" in users_ins[0]  # kdfiterations
    fake_restart.assert_called_once()


def test_register_user_with_cert_hash_writes_user_info(fake_sqlite, fake_restart):
    calls, responses = fake_sqlite
    responses.extend(["7", "", ""])
    from server.murmur.admin_sqlite import register_user

    uid = register_user("bob", "pw", cert_hash="deadbeef" * 5)
    assert uid == 7
    info_ins = [s for s in calls if "INSERT INTO user_info" in s]
    assert len(info_ins) == 1
    assert "'user_hash'" in info_ins[0]
    assert "'" + "deadbeef" * 5 + "'" in info_ins[0]


def test_register_user_password_hash_matches_mumble_algorithm():
    """PBKDF2-HMAC-SHA384, UTF-8 password, raw-bytes salt, 48-byte dk
    stored as lowercase hex — the exact recipe in
    src/murmur/PBKDF2.cpp. This test pins the algorithm so we can't
    silently drift."""
    import hashlib
    from server.murmur.admin_sqlite import _mumble_hash_password, _MURMUR_KDF_ITERATIONS

    pw_hex, salt_hex, iters = _mumble_hash_password("hunter2")
    assert iters == _MURMUR_KDF_ITERATIONS
    assert len(pw_hex) == 96  # 48 bytes of output
    assert len(salt_hex) == 16  # 8 bytes of salt
    # Recompute and compare.
    expected = hashlib.pbkdf2_hmac(
        "sha384", b"hunter2", bytes.fromhex(salt_hex), iters, dklen=48,
    ).hex()
    assert pw_hex == expected


def test_register_user_picks_next_free_id(fake_sqlite, fake_restart):
    calls, responses = fake_sqlite
    # MAX query returns 0 → first user id is 1 (SuperUser is 0).
    responses.extend(["1", ""])
    from server.murmur.admin_sqlite import register_user

    uid = register_user("bob", "pw")
    assert uid == 1


def test_register_user_escapes_apostrophes(fake_sqlite, fake_restart):
    """Usernames with apostrophes must be safely quoted."""
    calls, responses = fake_sqlite
    responses.extend(["3", ""])
    from server.murmur.admin_sqlite import register_user

    uid = register_user("O'Brien", "pw")
    assert uid == 3
    users_ins = next(s for s in calls if "INSERT INTO users" in s)
    assert "'O''Brien'" in users_ins


def test_register_user_serialized(fake_sqlite, fake_restart):
    """Two concurrent register_user calls must serialize under _admin_lock —
    they can't both read the same MAX and collide on the same user_id."""
    calls, responses = fake_sqlite
    # Four responses: two SELECTs + two INSERTs (no cert_hash, no user_info).
    responses.extend(["5", "", "6", ""])
    from server.murmur.admin_sqlite import register_user

    results: list[int] = []

    def _go(name, pw):
        results.append(register_user(name, pw))

    t1 = threading.Thread(target=_go, args=("u1", "p1"))
    t2 = threading.Thread(target=_go, args=("u2", "p2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both threads finished and got distinct ids.
    assert sorted(results) == [5, 6]
    # Both caused a restart — two calls total.
    assert fake_restart.call_count == 2

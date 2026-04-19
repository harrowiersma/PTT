"""Unit tests for server.murmur.admin_sqlite.

admin_sqlite runs its edits via `docker exec sqlite3` inside the murmur
container, so we mock `_sqlite_exec` to simulate Murmur's sqlite
responses. restart_murmur() is exercised end-to-end during deploy.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def fake_sqlite():
    """Patch _sqlite_exec so tests don't hit docker.

    Each test configures `calls` (list of recorded SQL) and
    `responses` (list of stdout strings to return in order).
    """
    calls: list[str] = []
    responses: list[str] = []

    def _fake(sql: str) -> str:
        calls.append(sql)
        if not responses:
            return ""
        return responses.pop(0)

    with patch("server.murmur.admin_sqlite._sqlite_exec", side_effect=_fake):
        yield calls, responses


def test_ensure_channel_creates_when_missing(fake_sqlite):
    calls, responses = fake_sqlite
    # SELECT existing → empty ; SELECT MAX(id) → "4" ; INSERT → ""
    responses.extend(["", "4", ""])
    from server.murmur.admin_sqlite import ensure_channel_exists
    cid = ensure_channel_exists("Phone")
    assert cid == 5
    assert any("INSERT INTO channels" in sql for sql in calls)
    assert any("'Phone'" in sql for sql in calls)


def test_ensure_channel_returns_existing_id(fake_sqlite):
    calls, responses = fake_sqlite
    # SELECT existing → "3" (Weather channel is at id=3 on prod)
    responses.extend(["3"])
    from server.murmur.admin_sqlite import ensure_channel_exists
    cid = ensure_channel_exists("Weather")
    assert cid == 3
    # No INSERT should have happened.
    assert not any("INSERT" in sql for sql in calls)


def test_ensure_channel_respects_parent(fake_sqlite):
    calls, responses = fake_sqlite
    responses.extend(["", "10", ""])
    from server.murmur.admin_sqlite import ensure_channel_exists
    cid = ensure_channel_exists("Sub", parent_id=3)
    assert cid == 11
    insert_sql = next(sql for sql in calls if "INSERT" in sql)
    # parent_id=3 should be in the VALUES tuple.
    assert ", 3, 'Sub', 1" in insert_sql


def test_delete_channel_present(fake_sqlite):
    calls, responses = fake_sqlite
    # SELECT 1 → "1" ; DELETE → ""
    responses.extend(["1", ""])
    from server.murmur.admin_sqlite import delete_channel
    assert delete_channel("SmokeTest") is True
    assert any("DELETE FROM channels" in sql for sql in calls)


def test_delete_channel_missing(fake_sqlite):
    calls, responses = fake_sqlite
    responses.extend([""])  # SELECT returns nothing
    from server.murmur.admin_sqlite import delete_channel
    assert delete_channel("Ghost") is False
    assert not any("DELETE" in sql for sql in calls)


def test_delete_user_registration_present(fake_sqlite):
    calls, responses = fake_sqlite
    responses.extend(["1", ""])
    from server.murmur.admin_sqlite import delete_user_registration
    assert delete_user_registration("testuser") is True
    assert any("DELETE FROM users" in sql for sql in calls)
    assert any("'testuser'" in sql for sql in calls)


def test_delete_user_registration_missing(fake_sqlite):
    calls, responses = fake_sqlite
    responses.extend([""])
    from server.murmur.admin_sqlite import delete_user_registration
    assert delete_user_registration("ghost") is False
    assert not any("DELETE" in sql for sql in calls)


def test_sql_quote_escapes_apostrophes():
    from server.murmur.admin_sqlite import _sql_quote
    assert _sql_quote("O'Brien") == "'O''Brien'"
    assert _sql_quote("plain") == "'plain'"
    assert _sql_quote("") == "''"

"""Unit tests for server.murmur.admin_sqlite.

Uses a temporary sqlite file seeded with Murmur's real channels + users
schema so we can exercise the helper without touching a live Murmur
container. restart_murmur() is not covered here — it talks to the
docker socket and is verified during deploy.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# Canonical Murmur schema (extracted via
#   docker exec ptt-murmur-1 sqlite3 /data/mumble-server.sqlite ".schema"
# during Phase 2b-audio). We recreate it here so tests don't depend on
# the image having been booted.
_MURMUR_SCHEMA_SQL = """
CREATE TABLE channels (
    server_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    parent_id INTEGER,
    name TEXT,
    inheritacl INTEGER
);
CREATE UNIQUE INDEX channel_id ON channels (server_id, channel_id);
CREATE TABLE users (
    server_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    pw TEXT,
    lastchannel INTEGER,
    texture BLOB,
    last_active DATE
);
CREATE UNIQUE INDEX users_name ON users (server_id, name);
CREATE TABLE user_info (
    server_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    key INTEGER,
    value TEXT
);
"""


@pytest.fixture
def murmur_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mumble-server.sqlite"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(_MURMUR_SCHEMA_SQL)
    # Seed with the stock channels our test Murmur had.
    conn.executemany(
        "INSERT INTO channels (server_id, channel_id, parent_id, name, inheritacl) VALUES (?, ?, ?, ?, ?)",
        [
            (1, 0, None, "Root", 1),
            (1, 1, 0, "Internal", 1),
            (1, 3, 0, "Weather", 1),
            (1, 4, 0, "Emergency", 1),
        ],
    )
    # Seed one registered user.
    conn.execute(
        "INSERT INTO users (server_id, user_id, name) VALUES (1, 42, 'testuser')"
    )
    conn.close()

    # Reload the module against this temp DB location.
    monkeypatch.setenv("MURMUR_DATA_DIR", str(tmp_path))
    import importlib
    import server.murmur.admin_sqlite as admin_sqlite
    importlib.reload(admin_sqlite)
    yield admin_sqlite, db_path
    # Leave cleanup to pytest's tmp_path.


def _channel_names(db_path: Path) -> list[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return [row[0] for row in conn.execute(
            "SELECT name FROM channels ORDER BY channel_id"
        ).fetchall()]


def _user_names(db_path: Path) -> list[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return [row[0] for row in conn.execute(
            "SELECT name FROM users ORDER BY user_id"
        ).fetchall()]


class TestEnsureChannelExists:
    def test_creates_new_channel(self, murmur_db):
        admin_sqlite, db_path = murmur_db
        cid = admin_sqlite.ensure_channel_exists("Phone")
        assert cid > 0
        assert "Phone" in _channel_names(db_path)

    def test_idempotent_on_existing(self, murmur_db):
        admin_sqlite, db_path = murmur_db
        # "Weather" is seeded at id=3.
        cid = admin_sqlite.ensure_channel_exists("Weather")
        assert cid == 3
        # No duplicate row.
        assert _channel_names(db_path).count("Weather") == 1

    def test_assigns_next_available_id(self, murmur_db):
        admin_sqlite, db_path = murmur_db
        # Seeded ids: 0,1,3,4 → next MAX+1 = 5.
        cid = admin_sqlite.ensure_channel_exists("NewChan")
        assert cid == 5

    def test_respects_parent_id(self, murmur_db):
        admin_sqlite, db_path = murmur_db
        cid = admin_sqlite.ensure_channel_exists("Sub", parent_id=3)
        with sqlite3.connect(str(db_path)) as conn:
            parent = conn.execute(
                "SELECT parent_id FROM channels WHERE channel_id=?", (cid,)
            ).fetchone()[0]
        assert parent == 3


class TestDeleteChannel:
    def test_deletes_existing(self, murmur_db):
        admin_sqlite, db_path = murmur_db
        assert admin_sqlite.delete_channel("Internal") is True
        assert "Internal" not in _channel_names(db_path)

    def test_noop_for_missing(self, murmur_db):
        admin_sqlite, db_path = murmur_db
        assert admin_sqlite.delete_channel("DoesNotExist") is False
        # Other channels untouched.
        assert "Root" in _channel_names(db_path)
        assert "Weather" in _channel_names(db_path)


class TestDeleteUserRegistration:
    def test_deletes_existing(self, murmur_db):
        admin_sqlite, db_path = murmur_db
        assert admin_sqlite.delete_user_registration("testuser") is True
        assert "testuser" not in _user_names(db_path)

    def test_noop_for_missing(self, murmur_db):
        admin_sqlite, db_path = murmur_db
        assert admin_sqlite.delete_user_registration("ghost") is False
        assert "testuser" in _user_names(db_path)


class TestConnectErrors:
    def test_raises_when_sqlite_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MURMUR_DATA_DIR", str(tmp_path))  # empty dir
        import importlib
        import server.murmur.admin_sqlite as admin_sqlite
        importlib.reload(admin_sqlite)
        with pytest.raises(admin_sqlite.AdminSqliteError):
            admin_sqlite.ensure_channel_exists("X")

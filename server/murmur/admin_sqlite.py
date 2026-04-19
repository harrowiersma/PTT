"""Direct Murmur sqlite admin helpers.

Pymumble is a client API — it can't perform SuperUser operations like
creating a channel on a server whose Root ACL doesn't grant MakeChannel
to anonymous users, or deleting a registered user. So we edit Murmur's
sqlite file directly and bounce the container to reload.

The admin container mounts the `murmur-data` Docker volume at
`/murmur-data` and the host Docker socket at `/var/run/docker.sock`
(see docker-compose.yml). This file is the single place that touches
that sqlite; callers (MurmurClient, dashboard endpoints) go through here.

Trade-offs:
  - Each admin op causes ~3 s of Mumble downtime during the restart.
    Acceptable for rare ops (channel create ≈ weekly, user reset ≈
    monthly). Users auto-reconnect.
  - A per-process asyncio lock serializes operations; concurrent admin
    actions would otherwise race on the sqlite file.
  - This tightly couples admin to the murmur filesystem layout. If we
    ever split admin onto a different host, migrate to Ice or gRPC.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Mount point for the murmur-data Docker volume. Matches docker-compose.yml.
MURMUR_DATA_DIR = Path(os.environ.get("MURMUR_DATA_DIR", "/murmur-data"))
MURMUR_SQLITE = MURMUR_DATA_DIR / "mumble-server.sqlite"

# Docker container name for Murmur. Lives alongside admin in the compose stack.
MURMUR_CONTAINER_NAME = os.environ.get("MURMUR_CONTAINER_NAME", "ptt-murmur-1")

# Server id in the sqlite — compose only has one virtual server.
_SERVER_ID = 1

# Serialize all admin operations so we never have two concurrent sqlite
# edits + restarts in flight. Thread-level lock because the sqlite3 calls
# and docker-py calls are blocking.
_admin_lock = threading.Lock()


class AdminSqliteError(RuntimeError):
    """Raised when an admin-sqlite operation fails."""


def _connect() -> sqlite3.Connection:
    if not MURMUR_SQLITE.exists():
        raise AdminSqliteError(
            f"Murmur sqlite not found at {MURMUR_SQLITE}. "
            "Is the murmur-data volume mounted into admin?"
        )
    # isolation_level=None → autocommit; we wrap our edits in explicit
    # transactions where needed.
    return sqlite3.connect(str(MURMUR_SQLITE), isolation_level=None)


def ensure_channel_exists(name: str, parent_id: int = 0) -> int:
    """Ensure a channel with the given name exists in Murmur; return its id.

    Idempotent — returns the existing id if the channel is already present.
    Caller is responsible for calling `restart_murmur()` afterwards if the
    channel was newly created (Murmur reads channels.sqlite at startup;
    live changes aren't picked up without a reload).
    """
    with _admin_lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT channel_id FROM channels WHERE server_id=? AND name=?",
                (_SERVER_ID, name),
            ).fetchone()
            if row is not None:
                logger.info("channel %r already exists at id=%d", name, row[0])
                return int(row[0])

            max_id = conn.execute(
                "SELECT COALESCE(MAX(channel_id), 0) FROM channels WHERE server_id=?",
                (_SERVER_ID,),
            ).fetchone()[0]
            new_id = int(max_id) + 1
            conn.execute(
                "INSERT INTO channels (server_id, channel_id, parent_id, name, inheritacl) "
                "VALUES (?, ?, ?, ?, 1)",
                (_SERVER_ID, new_id, parent_id, name),
            )
            logger.info("inserted channel %r with id=%d (parent=%d)", name, new_id, parent_id)
            return new_id


def delete_channel(name: str) -> bool:
    """Remove a channel from Murmur's sqlite. Returns True if a row was
    deleted. Murmur's DELETE trigger cascades to child channels and
    related rows (channel_info, groups, acl, channel_links, listeners).
    Caller still needs to bounce murmur to drop in-memory state.
    """
    with _admin_lock:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM channels WHERE server_id=? AND name=?",
                (_SERVER_ID, name),
            )
            deleted = cur.rowcount > 0
            if deleted:
                logger.info("deleted channel %r", name)
            else:
                logger.info("delete_channel: %r not present (no-op)", name)
            return deleted


def delete_user_registration(username: str) -> bool:
    """Remove a registered user from Murmur. Returns True if a row was
    deleted. Users table is the root; related rows (user_info, acl,
    channel_listeners, last-channel references) cascade via Murmur's
    triggers. Caller must bounce murmur for the SuperUser session cache
    to forget the user.
    """
    with _admin_lock:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM users WHERE server_id=? AND name=?",
                (_SERVER_ID, username),
            )
            deleted = cur.rowcount > 0
            if deleted:
                logger.info("deleted user registration %r", username)
            else:
                logger.info("delete_user_registration: %r not registered (no-op)", username)
            return deleted


def restart_murmur(timeout: int = 10) -> None:
    """Restart the murmur container so sqlite edits take effect.

    Uses the Docker socket mounted into admin at /var/run/docker.sock.
    Disruption is brief (~3 s); every Mumble client auto-reconnects.
    """
    with _admin_lock:
        try:
            import docker  # local import so tests on machines without docker still pass
        except ImportError as e:
            raise AdminSqliteError(f"docker SDK not available: {e}")
        try:
            client = docker.from_env()
            container = client.containers.get(MURMUR_CONTAINER_NAME)
            logger.info("restarting murmur container %s (timeout=%ds)",
                        MURMUR_CONTAINER_NAME, timeout)
            container.restart(timeout=timeout)
            logger.info("murmur restart complete")
        except Exception as e:
            raise AdminSqliteError(f"murmur restart failed: {e}") from e


async def ensure_channel_and_restart(name: str, parent_id: int = 0) -> tuple[int, bool]:
    """Convenience wrapper: ensure + restart only when a new channel was
    actually created. Returns (channel_id, was_created).
    Runs blocking sqlite/docker work in a thread so the asyncio event
    loop stays responsive.
    """
    def _sync() -> tuple[int, bool]:
        with _connect() as conn:
            row = conn.execute(
                "SELECT channel_id FROM channels WHERE server_id=? AND name=?",
                (_SERVER_ID, name),
            ).fetchone()
            if row is not None:
                return int(row[0]), False

        new_id = ensure_channel_exists(name, parent_id)
        restart_murmur()
        return new_id, True

    return await asyncio.to_thread(_sync)


async def delete_channel_and_restart(name: str) -> bool:
    """Convenience wrapper: delete + restart. Returns True if deleted."""
    def _sync() -> bool:
        deleted = delete_channel(name)
        if deleted:
            restart_murmur()
        return deleted

    return await asyncio.to_thread(_sync)


async def delete_user_and_restart(username: str) -> bool:
    """Convenience wrapper: delete user registration + restart. Returns
    True if a row was deleted."""
    def _sync() -> bool:
        deleted = delete_user_registration(username)
        if deleted:
            restart_murmur()
        return deleted

    return await asyncio.to_thread(_sync)

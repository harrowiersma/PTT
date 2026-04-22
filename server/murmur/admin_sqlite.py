"""Murmur sqlite admin helpers via docker-exec into the murmur container.

Pymumble is a client API — it can't perform SuperUser operations like
creating a channel on a server whose Root ACL doesn't grant MakeChannel
to anonymous users, or deleting a registered user. So we edit Murmur's
sqlite file and bounce the container.

Design: rather than mount Murmur's data volume into admin and fight
file-perm mismatches (Murmur writes 640; admin is uid 999 not in the
murmur group's owner-uid), we `docker exec` sqlite3 INSIDE the murmur
container via the mounted Docker socket. The edit then runs as murmur's
own user and always has the correct perms. Admin only needs
`/var/run/docker.sock`.

Trade-offs:
  - Each admin op causes ~3 s of Mumble downtime during the restart.
    Acceptable for rare ops (channel create ≈ weekly, user reset ≈
    monthly). Users auto-reconnect.
  - A per-process thread lock serializes operations; concurrent admin
    actions would otherwise race on the sqlite file.
  - Tight coupling to the murmur container name + its sqlite path. If
    we ever split admin onto a different host, migrate to Ice or gRPC.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import shlex
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Mumble's PBKDF2 config (from src/murmur/PBKDF2.cpp):
#   - HMAC-SHA384
#   - 8-byte random salt (stored as 16-char hex)
#   - 48-byte derived key (stored as 96-char hex)
#   - iterations: auto-benchmarked on first boot (~10 ms CPU budget);
#     floor is 1000. The admin path uses a fixed value matching what
#     fresh Murmur installs typically pick on modern hardware — any
#     value >= 1000 is valid, Murmur honours whatever is in the row.
_MURMUR_KDF_ITERATIONS = 8000
_MURMUR_SALT_BYTES = 8
_MURMUR_DK_LEN = 48

# Docker container name for Murmur. Lives alongside admin in the compose stack.
MURMUR_CONTAINER_NAME = os.environ.get("MURMUR_CONTAINER_NAME", "ptt-murmur-1")
# Path of the sqlite file INSIDE the murmur container.
MURMUR_SQLITE_IN_CONTAINER = os.environ.get(
    "MURMUR_SQLITE_IN_CONTAINER", "/data/mumble-server.sqlite"
)
# Server id in the sqlite — compose only has one virtual server.
_SERVER_ID = 1

# Serialize all admin operations so two concurrent sqlite edits + restarts
# never race. Thread-level lock because the docker SDK is blocking.
_admin_lock = threading.Lock()


class AdminSqliteError(RuntimeError):
    """Raised when an admin-sqlite operation fails."""


def _docker_client():
    try:
        import docker
    except ImportError as e:
        raise AdminSqliteError(f"docker SDK not available: {e}")
    try:
        return docker.from_env()
    except Exception as e:
        raise AdminSqliteError(f"docker.from_env() failed: {e}") from e


def _sqlite_exec(sql: str) -> str:
    """Run one sqlite3 statement inside the murmur container and return stdout.

    Raises AdminSqliteError on any sqlite or docker failure.
    """
    client = _docker_client()
    try:
        container = client.containers.get(MURMUR_CONTAINER_NAME)
    except Exception as e:
        raise AdminSqliteError(f"murmur container {MURMUR_CONTAINER_NAME} not found: {e}") from e
    cmd = ["sqlite3", MURMUR_SQLITE_IN_CONTAINER, sql]
    try:
        exit_code, output = container.exec_run(cmd, demux=False)
    except Exception as e:
        raise AdminSqliteError(f"docker exec failed: {e}") from e
    stdout = output.decode("utf-8", errors="replace") if output else ""
    if exit_code != 0:
        raise AdminSqliteError(f"sqlite3 exited {exit_code}: {stdout.strip()}")
    return stdout


def _sql_quote(s: str) -> str:
    """Quote a string literal for sqlite (doubling embedded apostrophes)."""
    return "'" + s.replace("'", "''") + "'"


def ensure_channel_exists(name: str, parent_id: int = 0) -> int:
    """Ensure a channel with the given name exists in Murmur; return its id.

    Idempotent — returns the existing id if the channel is already present.
    Caller is responsible for `restart_murmur()` afterwards if the channel
    was newly created (Murmur only reads the file at startup).
    """
    with _admin_lock:
        # Look for an existing row.
        existing = _sqlite_exec(
            f"SELECT channel_id FROM channels "
            f"WHERE server_id={_SERVER_ID} AND name={_sql_quote(name)};"
        ).strip()
        if existing:
            cid = int(existing)
            logger.info("channel %r already exists at id=%d", name, cid)
            return cid

        # Compute next id and insert.
        max_row = _sqlite_exec(
            f"SELECT COALESCE(MAX(channel_id), 0) FROM channels "
            f"WHERE server_id={_SERVER_ID};"
        ).strip()
        new_id = int(max_row) + 1
        _sqlite_exec(
            f"INSERT INTO channels (server_id, channel_id, parent_id, name, inheritacl) "
            f"VALUES ({_SERVER_ID}, {new_id}, {int(parent_id)}, {_sql_quote(name)}, 1);"
        )
        logger.info("inserted channel %r with id=%d (parent=%d)", name, new_id, parent_id)
        return new_id


def delete_channel(name: str) -> bool:
    """Remove a channel from Murmur's sqlite. Returns True if a row was
    deleted. Murmur's DELETE trigger cascades to child channels.
    """
    with _admin_lock:
        # Check existence first so we can return the right boolean.
        hit = _sqlite_exec(
            f"SELECT 1 FROM channels WHERE server_id={_SERVER_ID} "
            f"AND name={_sql_quote(name)};"
        ).strip()
        if not hit:
            logger.info("delete_channel: %r not present (no-op)", name)
            return False
        _sqlite_exec(
            f"DELETE FROM channels WHERE server_id={_SERVER_ID} "
            f"AND name={_sql_quote(name)};"
        )
        logger.info("deleted channel %r", name)
        return True


def delete_user_registration(username: str) -> bool:
    """Remove a registered user from Murmur. Returns True if a row was
    deleted. Related rows (user_info, etc.) cascade via Murmur's triggers.
    """
    with _admin_lock:
        hit = _sqlite_exec(
            f"SELECT 1 FROM users WHERE server_id={_SERVER_ID} "
            f"AND name={_sql_quote(username)};"
        ).strip()
        if not hit:
            logger.info("delete_user_registration: %r not registered (no-op)", username)
            return False
        _sqlite_exec(
            f"DELETE FROM users WHERE server_id={_SERVER_ID} "
            f"AND name={_sql_quote(username)};"
        )
        logger.info("deleted user registration %r", username)
        return True


def _next_mumble_user_id() -> int:
    """Highest user_id in Murmur sqlite + 1. SuperUser is always user_id=0,
    so the first real user registration lands at user_id=1."""
    out = _sqlite_exec(
        f"SELECT COALESCE(MAX(user_id), 0) + 1 FROM users "
        f"WHERE server_id={_SERVER_ID};"
    ).strip()
    return int(out) if out else 1


def _mumble_hash_password(password: str) -> tuple[str, str, int]:
    """Produce (pw_hex, salt_hex, iterations) in Murmur's wire format.

    Murmur's `users.pw` column stores PBKDF2-HMAC-SHA384 with UTF-8
    password bytes and a fresh random 8-byte salt. The derived key is
    48 bytes, stored as lowercase hex; the salt is stored as lowercase
    hex. Iterations go in `kdfiterations`. See
    src/murmur/PBKDF2.cpp::getHash in upstream Mumble.
    """
    salt = secrets.token_bytes(_MURMUR_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha384",
        password.encode("utf-8"),
        salt,
        _MURMUR_KDF_ITERATIONS,
        dklen=_MURMUR_DK_LEN,
    )
    return dk.hex(), salt.hex(), _MURMUR_KDF_ITERATIONS


def register_user(
    username: str,
    password: str,
    cert_hash: str | None = None,
) -> int:
    """Register an app user in Murmur's sqlite with their password.

    Murmur accepts cert-based auth (cert hash in user_info.user_hash)
    OR password auth (PBKDF2-SHA384 hash in users.pw + users.salt +
    users.kdfiterations). Our P50 clients auto-registered historically
    by supplying name+password on first connect, so the admin DB
    carries the plaintext in users.mumble_password. Passing that
    through here means Mumble's existing password-auth path keeps
    working unchanged — the ACL feature just adds a Mumble user_id
    that ACL rows can reference.

    When cert_hash is provided we also write the user_info.user_hash
    row so a matching client cert can log the user in without a
    password prompt. That path has had format-match issues in the
    past, so password auth is the primary path and cert is additive.

    Returns the newly-assigned Mumble user_id.
    """
    pw_hex, salt_hex, iters = _mumble_hash_password(password)
    # Serialize the SELECT/INSERT sequence so two concurrent calls don't
    # both pick the same _next_mumble_user_id(). restart_murmur is
    # called AFTER releasing the lock — it takes its own lock, so we
    # can't nest (threading.Lock is non-reentrant).
    with _admin_lock:
        uid = _next_mumble_user_id()
        _sqlite_exec(
            f"INSERT INTO users (server_id, user_id, name, pw, salt, "
            f"kdfiterations, lastchannel, texture, last_active, "
            f"last_disconnect) VALUES "
            f"({_SERVER_ID}, {uid}, {_sql_quote(username)}, "
            f"{_sql_quote(pw_hex)}, {_sql_quote(salt_hex)}, {int(iters)}, "
            f"0, NULL, datetime('now'), datetime('now'));"
        )
        if cert_hash:
            _sqlite_exec(
                f"INSERT INTO user_info (server_id, user_id, key, value) VALUES "
                f"({_SERVER_ID}, {uid}, 'user_hash', {_sql_quote(cert_hash)});"
            )
        logger.info(
            "registered %s in Murmur sqlite (user_id=%d, cert=%s)",
            username, uid, bool(cert_hash),
        )
    restart_murmur()
    return uid


# --- Mumble ACL bit constants (from Mumble's Permission enum) -------
_PERM_TRAVERSE = 0x02
_PERM_ENTER = 0x04
_PERM_SPEAK = 0x08
# Bits we strip from @all so non-members can't see or join the channel.
_DENY_TRAVERSE_ENTER = _PERM_TRAVERSE | _PERM_ENTER
# Bits we grant per-member so they can see, join, and talk. Priority
# ordering ensures the per-user grant overrides the @all deny.
_GRANT_MEMBER = _PERM_TRAVERSE | _PERM_ENTER | _PERM_SPEAK


def _set_channel_acl_no_lock(
    mumble_channel_id: int, member_user_ids: list[int],
) -> None:
    """ACL replace — caller must hold _admin_lock + own the restart."""
    _sqlite_exec(
        f"DELETE FROM acl WHERE server_id={_SERVER_ID} "
        f"AND channel_id={int(mumble_channel_id)};"
    )
    # Deny-@all at priority=1 — revokes Traverse+Enter so non-members
    # see nothing in the channel tree.
    _sqlite_exec(
        f"INSERT INTO acl (server_id, channel_id, priority, user_id, "
        f"group_name, apply_here, apply_sub, grantpriv, revokepriv) VALUES "
        f"({_SERVER_ID}, {int(mumble_channel_id)}, 1, NULL, 'all', 1, 1, "
        f"0, {_DENY_TRAVERSE_ENTER});"
    )
    # Per-member allow at priority=2+i — Mumble evaluates ACLs in
    # priority order and grants override revokes on later entries for
    # the matching user.
    for i, uid in enumerate(member_user_ids):
        _sqlite_exec(
            f"INSERT INTO acl (server_id, channel_id, priority, user_id, "
            f"group_name, apply_here, apply_sub, grantpriv, revokepriv) VALUES "
            f"({_SERVER_ID}, {int(mumble_channel_id)}, {2 + i}, {int(uid)}, "
            f"NULL, 1, 1, {_GRANT_MEMBER}, 0);"
        )


def _clear_channel_acl_no_lock(mumble_channel_id: int) -> None:
    """ACL wipe — caller must hold _admin_lock + own the restart."""
    _sqlite_exec(
        f"DELETE FROM acl WHERE server_id={_SERVER_ID} "
        f"AND channel_id={int(mumble_channel_id)};"
    )


def set_channel_acl(
    mumble_channel_id: int,
    member_user_ids: list[int],
    *,
    restart: bool = True,
) -> None:
    """Replace the ACL on a Mumble channel with a deny-@all + per-member
    allow pair. After the rows are written, restart Murmur so the new
    ACL takes effect (Murmur only reads the sqlite at startup).

    Pass restart=False when orchestrating a multi-channel batch —
    batched_acl_apply uses that to fold many changes into one restart.
    """
    with _admin_lock:
        _set_channel_acl_no_lock(mumble_channel_id, member_user_ids)
        logger.info(
            "set ACL on channel %d (members=%d)",
            mumble_channel_id, len(member_user_ids),
        )
    if restart:
        restart_murmur()


def clear_channel_acl(
    mumble_channel_id: int,
    *,
    restart: bool = True,
) -> None:
    """Remove every ACL row for a channel. The channel reverts to its
    parent's inherited permissions — in practice, visible-to-all."""
    with _admin_lock:
        _clear_channel_acl_no_lock(mumble_channel_id)
        logger.info("cleared ACL on channel %d", mumble_channel_id)
    if restart:
        restart_murmur()


def batched_acl_apply(
    changes: list[tuple[int, list[int] | None]],
) -> None:
    """Apply a list of channel ACL changes with exactly one Murmur restart.

    Each change is ``(mumble_channel_id, member_user_ids)``. When
    ``member_user_ids`` is None the channel's ACL is cleared entirely
    (back to visible-to-all); otherwise it's replaced with the deny/allow
    pair. Empty ``changes`` → no sqlite, no restart.
    """
    if not changes:
        return
    with _admin_lock:
        for cid, members in changes:
            if members is None:
                _clear_channel_acl_no_lock(cid)
            else:
                _set_channel_acl_no_lock(cid, members)
        logger.info("batched ACL apply: %d change(s)", len(changes))
    restart_murmur()


def restart_murmur(timeout: int = 10) -> None:
    """Restart the murmur container so sqlite edits take effect.
    Uses the Docker socket mounted at /var/run/docker.sock. Disruption
    is brief (~3 s); every Mumble client auto-reconnects.
    """
    with _admin_lock:
        client = _docker_client()
        try:
            container = client.containers.get(MURMUR_CONTAINER_NAME)
            logger.info("restarting murmur container %s (timeout=%ds)",
                        MURMUR_CONTAINER_NAME, timeout)
            container.restart(timeout=timeout)
            logger.info("murmur restart complete")
        except Exception as e:
            raise AdminSqliteError(f"murmur restart failed: {e}") from e


async def ensure_channel_and_restart(name: str, parent_id: int = 0) -> tuple[int, bool]:
    """Convenience: ensure + restart only when a new channel was created.
    Returns (channel_id, was_created). Runs blocking work in a thread.
    """
    def _sync() -> tuple[int, bool]:
        existing = _sqlite_exec(
            f"SELECT channel_id FROM channels "
            f"WHERE server_id={_SERVER_ID} AND name={_sql_quote(name)};"
        ).strip()
        if existing:
            return int(existing), False
        new_id = ensure_channel_exists(name, parent_id)
        restart_murmur()
        return new_id, True

    return await asyncio.to_thread(_sync)


async def ensure_phone_slots_and_restart(slot_count: int) -> dict[str, int]:
    """Ensure Phone + Phone/Call-1..Phone/Call-N sub-channels exist.

    Used by the sip-bridge at startup to provision per-call sub-channels
    (Priority 7 — multi-caller). One restart at the end covers any new
    rows. Returns a {name: channel_id} map covering all requested slots
    plus "Phone" itself. Safe to call every sip-bridge restart: idempotent.
    """
    def _sync() -> dict[str, int]:
        phone_id = ensure_channel_exists("Phone", parent_id=0)
        result: dict[str, int] = {"Phone": phone_id}
        created_any = False
        for n in range(1, slot_count + 1):
            name = f"Call-{n}"
            existing = _sqlite_exec(
                f"SELECT channel_id FROM channels "
                f"WHERE server_id={_SERVER_ID} AND name={_sql_quote(name)} "
                f"AND parent_id={int(phone_id)};"
            ).strip()
            if existing:
                result[name] = int(existing)
                continue
            cid = ensure_channel_exists(name, parent_id=phone_id)
            result[name] = cid
            created_any = True
        if created_any:
            restart_murmur()
        return result

    return await asyncio.to_thread(_sync)


async def delete_channel_and_restart(name: str) -> bool:
    def _sync() -> bool:
        deleted = delete_channel(name)
        if deleted:
            restart_murmur()
        return deleted

    return await asyncio.to_thread(_sync)


async def delete_user_and_restart(username: str) -> bool:
    def _sync() -> bool:
        deleted = delete_user_registration(username)
        if deleted:
            restart_murmur()
        return deleted

    return await asyncio.to_thread(_sync)

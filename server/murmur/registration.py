"""Auto-registration of app users into Murmur's sqlite.

Users connect to Murmur with a client certificate; the bridge captures
the cert SHA-1 hash into users.mumble_cert_hash (see
server.murmur.client._capture_cert_hash_sync). A registered user in
Murmur's sqlite lets the per-channel ACL reference them by user_id —
the foundation for "hide this channel from non-members" behaviour.

This module drives registration. Every TICK_SECONDS the loop looks for
users with a captured cert_hash but no Murmur user_id and registers
them via admin_sqlite.register_user (one murmur restart per user —
BATCH_SIZE keeps the blast radius bounded). Gated on the
`call_groups_hiding` feature flag so cert-hash capture runs freely on
prod while the restart-per-user cost stays dormant until ops is ready.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from server.database import async_session
from server.models import User

logger = logging.getLogger(__name__)

# Per-tick registration cap. Each register_user costs one murmur
# restart (~3 s disruption), so on a fleet reconnect we don't want to
# loop through 100 users in one tick.
BATCH_SIZE = 10
TICK_SECONDS = 60


async def run_pending_registrations_once() -> int:
    """Register any pending users. Returns the count successfully registered.

    No-op when the `call_groups_hiding` feature flag is off — cert-hash
    capture keeps running regardless, but the restart-per-user pass
    stays dormant.
    """
    # Imported inside the function so the feature cache is read at tick
    # time (not import time) and tests can patch admin_sqlite freely.
    from server import features as _features
    from server.murmur import admin_sqlite

    if not _features.is_enabled("call_groups_hiding"):
        return 0

    async with async_session() as db:
        pending = (await db.execute(
            select(User).where(
                User.mumble_cert_hash.is_not(None),
                User.mumble_registered_user_id.is_(None),
            ).limit(BATCH_SIZE)
        )).scalars().all()

    registered = 0
    for user in pending:
        if not user.mumble_password:
            # Historical rows without a plaintext Mumble password can't
            # be registered via our path — skip rather than register
            # with a blank password. Operator can re-provision the user.
            logger.warning(
                "auto-register: %s has no mumble_password; skipping",
                user.username,
            )
            continue
        try:
            uid = await asyncio.to_thread(
                admin_sqlite.register_user,
                user.username,
                user.mumble_password,
                user.mumble_cert_hash,
            )
        except Exception as e:
            logger.warning(
                "auto-register failed for %s: %s", user.username, e,
            )
            continue

        try:
            async with async_session() as db2:
                u2 = await db2.get(User, user.id)
                if u2 is not None:
                    u2.mumble_registered_user_id = uid
                    await db2.commit()
                    registered += 1
        except Exception as e:
            logger.warning(
                "auto-register: failed to persist uid for %s: %s",
                user.username, e,
            )
    return registered


async def scheduler_loop() -> None:
    """Run run_pending_registrations_once in a loop, one tick per
    TICK_SECONDS. Caller owns the task lifecycle — use asyncio.create_task
    in the lifespan and cancel it at shutdown."""
    while True:
        try:
            n = await run_pending_registrations_once()
            if n:
                logger.info(
                    "auto-register: registered %d pending user(s) this tick", n,
                )
        except Exception as e:
            logger.warning("auto-register tick failed: %s", e)
        await asyncio.sleep(TICK_SECONDS)

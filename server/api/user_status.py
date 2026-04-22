"""Three-state presence signal for radio users.

- GET /api/users/status?username=X   — read (no auth; dashboard + app use it).
- POST /api/users/status              — self set (device-trusted, no auth).
- PATCH /api/users/{id}/status        — admin override.

Every write path records an AuditLog entry with the actor + source so the
operator can see who changed what. Shift coupling and Murmur connect hooks
all funnel through `set_status()` below.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.admin import log_audit
from server.auth import get_current_admin
from server.database import get_db
from server.dependencies import get_murmur_client
from server.models import AuditLog, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["user-status"])


ALLOWED = ("online", "busy", "offline")
StatusLabel = Literal["online", "busy", "offline"]


class StatusBody(BaseModel):
    username: str
    label: StatusLabel | None = None
    is_audible: bool | None = None


class AdminStatusBody(BaseModel):
    label: StatusLabel


class StatusResponse(BaseModel):
    username: str
    label: str | None
    updated_at: datetime | None
    effective_label: str  # 'offline' if not mumble-connected
    is_audible: bool | None
    is_audible_updated_at: datetime | None


async def _effective(user: User, murmur) -> str:
    """Collapse stored label + live connection state into what the UI shows."""
    if not murmur or not getattr(murmur, "has_mumble", False):
        return "offline"
    try:
        connected = any(
            u["name"].lower() == user.username.lower()
            for u in murmur._mumble.users.values()
        )
    except Exception:
        connected = False
    if not connected:
        return "offline"
    return user.status_label or "offline"


async def set_status(
    db: AsyncSession, user: User, new_label: str, *,
    actor: str, source: str,
) -> User:
    """Shared status-write path used by all three endpoints + the Murmur
    auto-connect hook + shift coupling. Writes the audit row on the same
    transaction as the column update so either both land or neither does."""
    if new_label not in ALLOWED:
        raise HTTPException(status_code=422, detail=f"label must be one of {ALLOWED}")
    old = user.status_label
    if old == new_label:
        return user  # no-op; don't spam audit log
    user.status_label = new_label
    user.status_updated_at = datetime.now(timezone.utc)
    await log_audit(
        db, actor, "user.status_change",
        target_type="user", target_id=user.username,
        details=json.dumps({"from": old, "to": new_label, "source": source}),
    )
    await db.commit()
    await db.refresh(user)
    logger.info("status: %s %s -> %s (actor=%s, source=%s)",
                user.username, old, new_label, actor, source)
    return user


@router.get("/status", response_model=StatusResponse)
async def get_status(
    username: str,
    db: AsyncSession = Depends(get_db),
    murmur = Depends(get_murmur_client),
):
    row = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return StatusResponse(
        username=row.username,
        label=row.status_label,
        updated_at=row.status_updated_at,
        effective_label=await _effective(row, murmur),
        is_audible=row.is_audible,
        is_audible_updated_at=row.is_audible_updated_at,
    )


async def _maybe_end_shift_on_offline(db: AsyncSession, user: User) -> None:
    """If lone-worker feature is enabled AND this user is a lone worker AND
    they have an active shift, end it with reason='user_offline'. No-op otherwise."""
    if not user.is_lone_worker:
        return
    from server.models import FeatureFlag, LoneWorkerShift
    flag = (await db.execute(
        select(FeatureFlag).where(FeatureFlag.key == "lone_worker")
    )).scalar_one_or_none()
    if flag is None or not flag.enabled:
        return
    shift = (await db.execute(
        select(LoneWorkerShift).where(
            LoneWorkerShift.user_id == user.id,
            LoneWorkerShift.ended_at.is_(None),
        )
    )).scalar_one_or_none()
    if shift is None:
        return
    shift.ended_at = datetime.now(timezone.utc)
    shift.end_reason = "user_offline"
    await log_audit(
        db, user.username, "shift.stop",
        target_type="user", target_id=user.username,
        details=json.dumps({"reason": "user_offline"}),
    )
    await db.commit()
    await db.refresh(shift)


@router.post("/status", response_model=StatusResponse)
async def post_status(
    body: StatusBody,
    db: AsyncSession = Depends(get_db),
    murmur = Depends(get_murmur_client),
):
    if body.label is None and body.is_audible is None:
        raise HTTPException(status_code=422, detail="Must supply at least one of: label, is_audible")
    row = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    if body.label is not None:
        row = await set_status(db, row, body.label, actor=body.username, source="self")
    if body.is_audible is not None:
        # Audibility is high-churn + low-value → no audit row, direct write.
        row.is_audible = body.is_audible
        row.is_audible_updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
    if row.status_label == "offline":
        await _maybe_end_shift_on_offline(db, row)
    return StatusResponse(
        username=row.username,
        label=row.status_label,
        updated_at=row.status_updated_at,
        effective_label=await _effective(row, murmur),
        is_audible=row.is_audible,
        is_audible_updated_at=row.is_audible_updated_at,
    )


@router.patch("/{user_id}/status", response_model=StatusResponse)
async def patch_status(
    user_id: int, body: AdminStatusBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
    murmur = Depends(get_murmur_client),
):
    row = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    row = await set_status(db, row, body.label, actor=admin["sub"], source="admin")
    if row.status_label == "offline":
        await _maybe_end_shift_on_offline(db, row)
    return StatusResponse(
        username=row.username,
        label=row.status_label,
        updated_at=row.status_updated_at,
        effective_label=await _effective(row, murmur),
        is_audible=row.is_audible,
        is_audible_updated_at=row.is_audible_updated_at,
    )


class PresenceEntry(BaseModel):
    status_label: str | None
    is_audible: bool | None


# Lowercased usernames mirroring server/murmur/client.py::BOT_USERNAMES.
# Kept literal here (not imported) so the endpoint doesn't tug the murmur
# module into its dependency graph for one tuple.
_BOT_USERNAMES = {"PTTAdmin", "PTTWeather", "PTTPhone"}


def _is_bot_username(name: str) -> bool:
    """Mirror of MurmurClient._is_bot_username — keep both in sync."""
    if name in _BOT_USERNAMES:
        return True
    return name.startswith("PTTPhone-")


@router.get("/presence-map", response_model=dict[str, PresenceEntry])
async def get_presence_map(db: AsyncSession = Depends(get_db)):
    """Return every (non-bot) user's presence in one shot.

    The radio app polls this every 20 s to decide who to hide from its
    channel user list. Lowercased keys for case-insensitive lookup. No
    auth — matches the GET /status convention; the data leaked is the
    same shape any logged-in user would see.
    """
    rows = (await db.execute(select(User))).scalars().all()
    return {
        u.username.lower(): PresenceEntry(
            status_label=u.status_label,
            is_audible=u.is_audible,
        )
        for u in rows
        if not _is_bot_username(u.username)
    }

"""Lone Worker Timer: periodic check-in system for field worker safety.

Workers must check in every X minutes by pressing PTT or via the check-in API.
If a worker misses a check-in, the system auto-triggers SOS with their last GPS.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/loneworker", tags=["loneworker"])


class LoneWorkerConfig(BaseModel):
    check_in_interval_minutes: int = 30  # How often workers must check in
    warning_after_minutes: int = 25  # When to show yellow warning
    enabled: bool = True


class CheckInRequest(BaseModel):
    username: str


# In-memory config (per deployment, not per user for now)
_config = LoneWorkerConfig()
_last_check_ins: dict[str, datetime] = {}


@router.get("/config")
async def get_config(_admin: dict = Depends(get_current_admin)):
    return _config.model_dump()


@router.post("/config")
async def update_config(
    config: LoneWorkerConfig,
    _admin: dict = Depends(get_current_admin),
):
    global _config
    _config = config
    logger.info("Lone worker config updated: interval=%dm, warning=%dm, enabled=%s",
                config.check_in_interval_minutes, config.warning_after_minutes, config.enabled)
    return _config.model_dump()


@router.post("/checkin")
async def check_in(req: CheckInRequest):
    """Record a check-in for a worker. Called by the device or admin."""
    now = datetime.now(timezone.utc)
    _last_check_ins[req.username.lower()] = now
    logger.info("Check-in received from %s", req.username)
    return {"status": "ok", "username": req.username, "checked_in_at": now.isoformat()}


@router.get("/status")
async def get_status(
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get check-in status for all users. Shows green/yellow/red per user."""
    if not _config.enabled:
        return {"enabled": False, "users": []}

    now = datetime.now(timezone.utc)
    result = await db.execute(select(User).where(User.is_active == True))
    users = result.scalars().all()

    statuses = []
    for user in users:
        last = _last_check_ins.get(user.username.lower())
        if last is None:
            status = "unknown"
            minutes_ago = None
        else:
            minutes_ago = (now - last).total_seconds() / 60
            if minutes_ago <= _config.warning_after_minutes:
                status = "ok"
            elif minutes_ago <= _config.check_in_interval_minutes:
                status = "warning"
            else:
                status = "overdue"

        statuses.append({
            "username": user.username,
            "display_name": user.display_name,
            "status": status,
            "last_check_in": last.isoformat() if last else None,
            "minutes_ago": round(minutes_ago, 1) if minutes_ago is not None else None,
            "interval_minutes": _config.check_in_interval_minutes,
        })

    return {
        "enabled": True,
        "config": _config.model_dump(),
        "users": sorted(statuses, key=lambda x: (
            {"overdue": 0, "warning": 1, "unknown": 2, "ok": 3}[x["status"]],
            x["minutes_ago"] or 9999,
        )),
    }

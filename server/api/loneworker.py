"""Lone Worker Timer: periodic check-in system for field worker safety.

Workers must check in every X minutes by pressing PTT or via the check-in API.
If a worker misses a check-in, the system sends a TTS voice reminder.
Optionally auto-triggers SOS if configured.
"""

import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db, async_session
from server.features_gate import requires_feature
from server.models import User, LoneWorkerShift

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/loneworker",
    tags=["loneworker"],
    dependencies=[requires_feature("lone_worker")],
)


class LoneWorkerConfig(BaseModel):
    check_in_interval_minutes: int = 30  # How often workers must check in
    warning_after_minutes: int = 25  # When to show yellow warning
    enabled: bool = False  # OFF by default, enable per installation
    auto_sos_on_overdue: bool = False  # Auto-trigger SOS when a worker goes overdue
    default_shift_hours: int = 8  # Fallback when User.shift_duration_hours is NULL


class CheckInRequest(BaseModel):
    username: str


class ShiftRequest(BaseModel):
    username: str


# In-memory config (per deployment, not per user for now)
_config = LoneWorkerConfig()
_last_check_ins: dict[str, datetime] = {}
_reminded: set[str] = set()  # Users already reminded this cycle (prevent spam)
_murmur_client = None  # Set by start_overdue_checker()


def start_overdue_checker(murmur_client):
    """Start background thread that checks for overdue workers and sends TTS reminders.

    Only considers users with an active LoneWorkerShift — no shift, no pings.
    Also auto-closes shifts whose planned_end_at has passed.
    """
    global _murmur_client
    _murmur_client = murmur_client

    def _checker_loop():
        while True:
            _time.sleep(60)  # Check every 60 seconds
            if not _config.enabled:
                continue

            try:
                _run_shift_cycle()
            except Exception as e:
                logger.error("shift cycle failed: %s", e)

    thread = threading.Thread(target=_checker_loop, daemon=True)
    thread.start()
    logger.info("Lone worker overdue checker started (checks every 60s)")


def _run_shift_cycle():
    """One pass of the overdue loop: auto-expire shifts, ping overdue active users."""
    import asyncio
    from sqlalchemy import select as _select, update as _update
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import create_engine
    from server.config import settings

    # Use a short-lived sync connection so the background thread doesn't
    # fight the async engine's event loop. psycopg2 driver via database_url_sync.
    sync_engine = create_engine(settings.database_url_sync, echo=False)
    Session = sessionmaker(sync_engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)

    with Session() as db:
        # Auto-expire shifts that ran past their planned end.
        expired_rows = db.execute(
            _select(LoneWorkerShift).where(
                LoneWorkerShift.ended_at.is_(None),
                LoneWorkerShift.planned_end_at < now,
            )
        ).scalars().all()
        for shift in expired_rows:
            shift.ended_at = now
            shift.end_reason = "auto_expired"
            logger.info("Auto-expired shift id=%d user_id=%d", shift.id, shift.user_id)
        if expired_rows:
            db.commit()

        # For each active shift, check whether the user is overdue on check-in.
        active = db.execute(
            _select(LoneWorkerShift, User)
            .join(User, User.id == LoneWorkerShift.user_id)
            .where(LoneWorkerShift.ended_at.is_(None))
        ).all()

    sync_engine.dispose()

    for shift, user in active:
        uname = user.username.lower()
        last = _last_check_ins.get(uname)
        if last is None:
            # Never checked in this shift — use shift start as the baseline.
            last = shift.started_at
        minutes_ago = (now - last).total_seconds() / 60
        if minutes_ago > _config.check_in_interval_minutes and uname not in _reminded:
            _reminded.add(uname)
            _send_voice_reminder(user.username)


def _send_voice_reminder(username: str):
    """Send a TTS voice reminder to an overdue worker in their current channel."""
    if not _murmur_client or not _murmur_client.has_mumble:
        logger.warning("Cannot send voice reminder to %s: no Murmur connection", username)
        return

    try:
        from server.weather_bot import text_to_audio_pcm

        text = f"Automated check-in for {username}, please respond."
        logger.info("Sending voice check-in reminder to %s", username)

        # Find the user's current channel
        mm = _murmur_client._mumble
        user_channel = None
        for sid, user in mm.users.items():
            if user["name"].lower() == username.lower():
                user_channel = user.get("channel_id", 0)
                break

        if user_channel is None:
            # User not online, send text message to root channel instead
            _murmur_client.send_message(0, f"Check-in overdue for {username}. User is not connected.")
            return

        # Generate TTS audio
        pcm = text_to_audio_pcm(text)
        if not pcm:
            # Fallback to text message
            _murmur_client.send_message(user_channel, f"<b>CHECK-IN REMINDER:</b> {username}, please respond to confirm you are OK.")
            return

        # Move bot to user's channel, play audio, move back
        original_channel = None
        for cid, ch in mm.channels.items():
            if ch["name"] == "Weather":
                original_channel = cid
                break

        mm.users.myself.move_in(user_channel)
        _time.sleep(0.1)

        CHUNK_SIZE = 48000 * 2 * 20 // 1000
        for i in range(0, len(pcm), CHUNK_SIZE):
            chunk = pcm[i:i + CHUNK_SIZE]
            if len(chunk) < CHUNK_SIZE:
                chunk += b'\x00' * (CHUNK_SIZE - len(chunk))
            mm.sound_output.add_sound(chunk)
            _time.sleep(0.018)

        _time.sleep(0.5)
        if original_channel is not None:
            mm.users.myself.move_in(original_channel)

        logger.info("Voice check-in reminder sent to %s in channel %d", username, user_channel)

    except Exception as e:
        logger.error("Failed to send voice reminder to %s: %s", username, e)


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
    _reminded.discard(req.username.lower())  # Clear reminder flag so they get reminded again next time
    logger.info("Check-in received from %s", req.username)
    return {"status": "ok", "username": req.username, "checked_in_at": now.isoformat()}


def _resolve_shift_duration(user: User) -> int:
    """Resolve the planned duration in hours for a new shift: per-user override
    wins, else the global default from config."""
    if user.shift_duration_hours and user.shift_duration_hours > 0:
        return user.shift_duration_hours
    return _config.default_shift_hours


@router.post("/shift/start")
async def shift_start(
    req: ShiftRequest,
    db: AsyncSession = Depends(get_db),
):
    """Start a lone-worker shift for the given user.

    Idempotent: if an active shift already exists, returns it unchanged.
    Called by the device on long-press of the shift-control button; the
    admin dashboard can also trigger via this endpoint.
    """
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_lone_worker:
        raise HTTPException(
            status_code=400,
            detail="User is not marked as a lone worker",
        )

    # Idempotent — active shift wins.
    existing = await db.execute(
        select(LoneWorkerShift)
        .where(LoneWorkerShift.user_id == user.id, LoneWorkerShift.ended_at.is_(None))
    )
    active = existing.scalar_one_or_none()
    if active:
        logger.info("shift_start: user %s already has active shift id=%d", user.username, active.id)
        return _shift_dict(active, user)

    duration_hours = _resolve_shift_duration(user)
    started = datetime.now(timezone.utc)
    shift = LoneWorkerShift(
        user_id=user.id,
        started_at=started,
        planned_end_at=started + timedelta(hours=duration_hours),
    )
    db.add(shift)
    await db.commit()
    await db.refresh(shift)

    _last_check_ins[user.username.lower()] = started  # counts as a check-in
    _reminded.discard(user.username.lower())
    logger.info(
        "Shift started for %s: id=%d duration=%dh",
        user.username, shift.id, duration_hours,
    )

    # Status coupling (design §Decisions #5): if user is a lone worker, the
    # shift start force-sets status to Online. The feature-enabled gate is
    # implicit — requires_feature("lone_worker") on the router already 503s
    # when disabled.
    if user.is_lone_worker:
        from server.api.user_status import set_status
        await set_status(db, user, "online", actor="system", source="shift_start")

    return _shift_dict(shift, user)


@router.post("/shift/stop")
async def shift_stop(
    req: ShiftRequest,
    db: AsyncSession = Depends(get_db),
):
    """End the user's active shift, if any. No-op if none is active."""
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    existing = await db.execute(
        select(LoneWorkerShift)
        .where(LoneWorkerShift.user_id == user.id, LoneWorkerShift.ended_at.is_(None))
    )
    active = existing.scalar_one_or_none()
    if not active:
        return {"status": "no_active_shift", "username": user.username}

    active.ended_at = datetime.now(timezone.utc)
    active.end_reason = "user_ended"
    await db.commit()
    await db.refresh(active)
    _reminded.discard(user.username.lower())
    logger.info("Shift stopped for %s: id=%d", user.username, active.id)
    return _shift_dict(active, user)


@router.get("/shift/active")
async def shift_active(
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all currently active shifts. Used by the dashboard."""
    rows = await db.execute(
        select(LoneWorkerShift, User)
        .join(User, User.id == LoneWorkerShift.user_id)
        .where(LoneWorkerShift.ended_at.is_(None))
        .order_by(LoneWorkerShift.started_at.desc())
    )
    return [_shift_dict(shift, user) for shift, user in rows.all()]


def _shift_dict(shift: LoneWorkerShift, user: User) -> dict:
    return {
        "id": shift.id,
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "started_at": shift.started_at.isoformat() if shift.started_at else None,
        "planned_end_at": shift.planned_end_at.isoformat() if shift.planned_end_at else None,
        "ended_at": shift.ended_at.isoformat() if shift.ended_at else None,
        "end_reason": shift.end_reason,
        "active": shift.ended_at is None,
    }


@router.get("/status")
async def get_status(
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get check-in status for all users. Shows green/yellow/red per user."""
    if not _config.enabled:
        return {"enabled": False, "users": []}

    now = datetime.now(timezone.utc)
    result = await db.execute(select(User).where(User.is_active == True, User.is_lone_worker == True))
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

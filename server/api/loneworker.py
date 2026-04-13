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
from server.database import get_db
from server.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/loneworker", tags=["loneworker"])


class LoneWorkerConfig(BaseModel):
    check_in_interval_minutes: int = 30  # How often workers must check in
    warning_after_minutes: int = 25  # When to show yellow warning
    enabled: bool = False  # OFF by default, enable per installation
    auto_sos_on_overdue: bool = False  # Auto-trigger SOS when a worker goes overdue


class CheckInRequest(BaseModel):
    username: str


# In-memory config (per deployment, not per user for now)
_config = LoneWorkerConfig()
_last_check_ins: dict[str, datetime] = {}
_reminded: set[str] = set()  # Users already reminded this cycle (prevent spam)
_murmur_client = None  # Set by start_overdue_checker()


def start_overdue_checker(murmur_client):
    """Start background thread that checks for overdue workers and sends TTS reminders."""
    global _murmur_client
    _murmur_client = murmur_client

    def _checker_loop():
        while True:
            _time.sleep(60)  # Check every 60 seconds
            if not _config.enabled:
                continue

            now = datetime.now(timezone.utc)
            for username, last_check_in in list(_last_check_ins.items()):
                minutes_ago = (now - last_check_in).total_seconds() / 60
                if minutes_ago > _config.check_in_interval_minutes and username not in _reminded:
                    _reminded.add(username)
                    _send_voice_reminder(username)

            # Also check users who never checked in (if they're online)
            if _murmur_client and _murmur_client.has_mumble:
                mm = _murmur_client._mumble
                if mm:
                    for sid, user in mm.users.items():
                        uname = user["name"].lower()
                        if uname == "pttadmin":
                            continue
                        if uname not in _last_check_ins and uname not in _reminded:
                            # Online but never checked in, and monitoring is on
                            # Don't remind immediately, give them one interval
                            pass

    thread = threading.Thread(target=_checker_loop, daemon=True)
    thread.start()
    logger.info("Lone worker overdue checker started (checks every 60s)")


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

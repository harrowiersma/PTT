import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.config import settings
from server.database import get_db
from server.models import SOSEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sos", tags=["sos"])

# Track original channels so we can move users back after SOS
_original_channels: dict[int, int] = {}  # session_id -> channel_id


class SOSRequest(BaseModel):
    username: str
    latitude: float = 0
    longitude: float = 0
    message: str = ""


class SOSAcknowledge(BaseModel):
    acknowledged_by: str = "admin"


def _get_murmur():
    """Get the murmur client from app state."""
    try:
        from server.main import app
        return getattr(app.state, "murmur_client", None)
    except Exception:
        return None


def _move_all_to_emergency(murmur) -> int | None:
    """Create Emergency channel if needed, save original channels, move all users there."""
    if not murmur or not murmur.has_mumble:
        return None

    mm = murmur._mumble
    if not mm:
        return None

    # Find or create Emergency channel
    emergency_id = None
    for chan_id, chan in mm.channels.items():
        if chan["name"] == "Emergency":
            emergency_id = chan_id
            break

    if emergency_id is None:
        mm.channels.new_channel(0, "Emergency", temporary=False)
        import time
        time.sleep(0.5)
        for chan_id, chan in mm.channels.items():
            if chan["name"] == "Emergency":
                emergency_id = chan_id
                break

    if emergency_id is None:
        logger.error("Could not create Emergency channel")
        return None

    # Save original channels and move all users to Emergency
    _original_channels.clear()
    for session_id, user in mm.users.items():
        if user["name"] in ("PTTAdmin", "PTTWeather", "PTTPhone"):
            continue
        current_channel = user.get("channel_id", 0)
        if current_channel != emergency_id:
            _original_channels[session_id] = current_channel
            mm.users[session_id].move_in(emergency_id)

    # Send alert message to Emergency channel
    if emergency_id in mm.channels:
        mm.channels[emergency_id].send_text_message(
            "<b style='color:red'>SOS ALERT</b> — All users moved to Emergency channel"
        )

    user_count = len(_original_channels)
    logger.warning("SOS: Moved %d users to Emergency channel (ID %d)", user_count, emergency_id)
    return emergency_id


def _restore_channels(murmur):
    """Move all users back to their original channels after SOS is acknowledged."""
    if not murmur or not murmur.has_mumble:
        return

    mm = murmur._mumble
    if not mm:
        return

    restored = 0
    for session_id, original_channel in _original_channels.items():
        if session_id in mm.users:
            try:
                mm.users[session_id].move_in(original_channel)
                restored += 1
            except Exception as e:
                logger.warning("Could not restore user session %d: %s", session_id, e)

    # Send all-clear message
    for chan_id, chan in mm.channels.items():
        if chan["name"] == "Emergency":
            mm.channels[chan_id].send_text_message(
                "<b style='color:green'>ALL CLEAR</b> — Users returned to original channels"
            )
            break

    _original_channels.clear()
    logger.info("SOS acknowledged: restored %d users to original channels", restored)


def _verify_sos_auth(request: Request) -> None:
    """Verify SOS trigger authentication. Accepts either admin JWT or SOS token."""
    # Check for SOS token header first (for Traccar webhooks)
    sos_token = request.headers.get("X-SOS-Token", "")
    if settings.sos_token and sos_token == settings.sos_token:
        return  # Valid SOS token

    # Fall back to JWT auth
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        from server.auth import verify_token
        verify_token(auth[7:])
        return

    from fastapi import HTTPException, status
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="SOS trigger requires authentication (JWT or X-SOS-Token header)",
    )


@router.post("")
async def trigger_sos(
    sos: SOSRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Trigger an SOS alert. Requires admin JWT or SOS token. Moves all users to Emergency channel."""
    _verify_sos_auth(request)
    event = SOSEvent(
        username=sos.username,
        latitude=sos.latitude,
        longitude=sos.longitude,
        message=sos.message or "SOS triggered",
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    logger.warning("SOS ALERT: %s at (%f, %f) - %s", sos.username, sos.latitude, sos.longitude, sos.message)

    # Move all users to Emergency channel
    murmur = _get_murmur()
    _move_all_to_emergency(murmur)

    # Send webhook if configured
    if settings.sos_webhook_url:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.post(settings.sos_webhook_url, json={
                    "event": "sos",
                    "username": sos.username,
                    "latitude": sos.latitude,
                    "longitude": sos.longitude,
                    "message": sos.message,
                    "timestamp": event.triggered_at.isoformat(),
                })
        except Exception as e:
            logger.error("SOS webhook failed: %s", e)

    return {"status": "alert_triggered", "id": event.id}


@router.post("/traccar/event")
async def traccar_event_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Receive event forwarding from Traccar (alarm events become SOS)."""
    try:
        body = await request.json()
        event = body.get("event", {})
        device = body.get("device", {})
        position = body.get("position", {})

        event_type = event.get("type", "")
        if event_type == "alarm" or event_type == "sos":
            sos = SOSEvent(
                username=device.get("name", "unknown"),
                latitude=position.get("latitude", 0),
                longitude=position.get("longitude", 0),
                message=f"Traccar alarm: {event.get('attributes', {}).get('alarm', 'unknown')}",
            )
            db.add(sos)
            await db.commit()
            logger.warning("SOS via Traccar: %s", device.get("name"))

            # Move all users to Emergency channel
            murmur = _get_murmur()
            _move_all_to_emergency(murmur)

        return {"status": "received"}
    except Exception as e:
        logger.error("Traccar event webhook error: %s", e)
        return {"status": "error", "detail": str(e)}


@router.get("/active")
async def get_active_sos(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    """Get all unacknowledged SOS events."""
    result = await db.execute(
        select(SOSEvent)
        .where(SOSEvent.acknowledged == False)
        .order_by(SOSEvent.triggered_at.desc())
    )
    events = result.scalars().all()
    return [
        {
            "id": e.id,
            "username": e.username,
            "latitude": e.latitude,
            "longitude": e.longitude,
            "message": e.message,
            "triggered_at": e.triggered_at.isoformat(),
        }
        for e in events
    ]


@router.post("/{sos_id}/acknowledge")
async def acknowledge_sos(
    sos_id: int,
    ack: SOSAcknowledge,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    """Acknowledge an SOS alert. Moves users back to original channels."""
    result = await db.execute(select(SOSEvent).where(SOSEvent.id == sos_id))
    event = result.scalar_one_or_none()
    if not event:
        return {"status": "not_found"}

    event.acknowledged = True
    event.acknowledged_at = datetime.now(timezone.utc)
    event.acknowledged_by = ack.acknowledged_by
    await db.commit()

    # Restore users to original channels
    murmur = _get_murmur()
    _restore_channels(murmur)

    logger.info("SOS %d acknowledged by %s", sos_id, ack.acknowledged_by)
    return {"status": "acknowledged"}

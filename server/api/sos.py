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


class SOSRequest(BaseModel):
    username: str
    latitude: float = 0
    longitude: float = 0
    message: str = ""


class SOSAcknowledge(BaseModel):
    acknowledged_by: str = "admin"


@router.post("")
async def trigger_sos(
    sos: SOSRequest,
    db: AsyncSession = Depends(get_db),
):
    """Trigger an SOS alert. Can be called by Traccar webhook or directly."""
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
    """Acknowledge an SOS alert."""
    result = await db.execute(select(SOSEvent).where(SOSEvent.id == sos_id))
    event = result.scalar_one_or_none()
    if not event:
        return {"status": "not_found"}

    event.acknowledged = True
    event.acknowledged_at = datetime.now(timezone.utc)
    event.acknowledged_by = ack.acknowledged_by
    await db.commit()

    logger.info("SOS %d acknowledged by %s", sos_id, ack.acknowledged_by)
    return {"status": "acknowledged"}

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.models import DispatchEvent
from server.traccar_client import TraccarClient

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])


class DispatchRequest(BaseModel):
    target_username: str
    message: str
    latitude: float = 0
    longitude: float = 0


@router.get("/nearest")
async def find_nearest(
    lat: float,
    lng: float,
    _admin: dict = Depends(get_current_admin),
):
    """Find nearest workers to a given GPS location."""
    client = TraccarClient()
    nearest = await client.find_nearest(lat, lng)
    return nearest[:10]


@router.post("")
async def dispatch_worker(
    req: DispatchRequest,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    """Dispatch a worker: log the event and send them a message."""
    event = DispatchEvent(
        target_username=req.target_username,
        message=req.message,
        latitude=req.latitude,
        longitude=req.longitude,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    # TODO: Send text message to user via Mumble when ICE/pymumble is available
    # For now, the dispatch is logged and visible in dashboard

    return {
        "status": "dispatched",
        "id": event.id,
        "target": req.target_username,
        "message": req.message,
    }

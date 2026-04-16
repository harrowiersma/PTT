from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.dependencies import get_murmur_client
from server.models import DispatchEvent, User
from server.murmur.client import MurmurClient
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
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    """Find nearest workers to a given GPS location.
    Uses explicit user-device links where available, falls back to name matching."""
    client = TraccarClient()
    positions = await client.get_positions()

    # Build device_id -> username map from DB
    result = await db.execute(select(User).where(User.traccar_device_id.isnot(None)))
    device_to_user = {u.traccar_device_id: u.username for u in result.scalars().all()}

    results = []
    for p in positions:
        if p.latitude == 0 and p.longitude == 0:
            continue
        # Resolve username: explicit link first, then device name fallback
        username = device_to_user.get(p.device_id, p.device_name)
        distance = TraccarClient.haversine_distance(lat, lng, p.latitude, p.longitude)
        results.append({
            "username": username,
            "distance_m": round(distance),
            "latitude": p.latitude,
            "longitude": p.longitude,
            "timestamp": p.timestamp,
        })
    results.sort(key=lambda x: x["distance_m"])
    return results[:10]


@router.post("")
async def dispatch_worker(
    req: DispatchRequest,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
    murmur: MurmurClient | None = Depends(get_murmur_client),
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

    # Send text message to the user's current channel via Mumble
    if murmur and murmur.has_mumble:
        # Find which channel the target user is in
        target_channel = 0  # fallback to Root
        mm = murmur._mumble
        if mm:
            for sid, user in mm.users.items():
                if user["name"].lower() == req.target_username.lower():
                    target_channel = user.get("channel_id", 0)
                    break
        murmur.send_message(target_channel, f"DISPATCH: {req.target_username}, {req.message}")

    return {
        "status": "dispatched",
        "id": event.id,
        "target": req.target_username,
        "message": req.message,
    }

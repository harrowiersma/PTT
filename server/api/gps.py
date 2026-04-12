from fastapi import APIRouter, Depends

from server.auth import get_current_admin
from server.traccar_client import TraccarClient

router = APIRouter(prefix="/api/gps", tags=["gps"])


@router.get("/positions")
async def get_positions(_admin: dict = Depends(get_current_admin)):
    """Get latest GPS positions for all devices."""
    client = TraccarClient()
    positions = await client.get_positions()
    return [
        {
            "device_id": p.device_id,
            "username": p.device_name,
            "latitude": p.latitude,
            "longitude": p.longitude,
            "speed": p.speed,
            "accuracy": p.accuracy,
            "battery": p.battery_level,
            "timestamp": p.timestamp,
        }
        for p in positions
    ]

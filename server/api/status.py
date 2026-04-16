import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.schemas import ServerStatusResponse, UserOnline
from server.auth import get_current_admin
from server.database import get_db
from server.dependencies import get_murmur_client
from server.models import User
from server.murmur.client import MurmurClient
from server.traccar_client import TraccarClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("/server", response_model=ServerStatusResponse)
async def get_server_status(
    _admin: dict = Depends(get_current_admin),
    murmur: MurmurClient | None = Depends(get_murmur_client),
    db: AsyncSession = Depends(get_db),
):
    if not murmur or not murmur.is_connected:
        return ServerStatusResponse(
            is_running=False,
            users_online=0,
            max_users=0,
            murmur_connected=False,
            users=[],
        )

    status = murmur.get_status()

    # Build channel ID -> name map
    channel_names = {ch.id: ch.name for ch in status.channels}

    # Get GPS positions from Traccar (keyed by device ID)
    gps_by_device_id = {}
    gps_by_name = {}  # fallback for unlinked devices
    try:
        traccar = TraccarClient()
        positions = await traccar.get_positions()
        for p in positions:
            gps_by_device_id[p.device_id] = p
            gps_by_name[p.device_name.lower()] = p
    except Exception as e:
        logger.debug("Traccar positions unavailable: %s", e)

    # Build username -> traccar_device_id map from DB
    user_device_map = {}
    try:
        result = await db.execute(select(User).where(User.traccar_device_id.isnot(None)))
        for db_user in result.scalars().all():
            user_device_map[db_user.username.lower()] = db_user.traccar_device_id
    except Exception as e:
        logger.debug("Could not load user-device map: %s", e)

    # Update last_seen for all online users
    online_usernames = [u.name for u in status.users]
    if online_usernames:
        try:
            now = datetime.now(timezone.utc)
            await db.execute(
                update(User)
                .where(User.username.in_(online_usernames))
                .values(last_seen=now)
            )
            await db.commit()
        except Exception as e:
            logger.debug("Could not update last_seen: %s", e)

    users = []
    for u in status.users:
        # Try explicit device link first, fall back to name matching
        device_id = user_device_map.get(u.name.lower())
        gps = gps_by_device_id.get(device_id) if device_id else gps_by_name.get(u.name.lower())
        users.append(
            UserOnline(
                username=u.name,
                channel_id=u.channel_id,
                channel_name=channel_names.get(u.channel_id, f"Channel {u.channel_id}"),
                is_muted=u.is_muted,
                is_deaf=u.is_deaf,
                online_secs=u.online_secs,
                address=u.address,
                latitude=gps.latitude if gps else None,
                longitude=gps.longitude if gps else None,
                gps_timestamp=gps.timestamp if gps else None,
                battery=gps.battery_level if gps and gps.battery_level >= 0 else None,
                speed=gps.speed if gps else None,
            )
        )

    return ServerStatusResponse(
        is_running=status.is_running,
        users_online=status.users_online,
        max_users=status.max_users,
        murmur_connected=True,
        users=users,
    )


@router.get("/health")
async def health_check(
    murmur: MurmurClient | None = Depends(get_murmur_client),
):
    """Public health check endpoint for monitoring."""
    murmur_ok = murmur.is_connected if murmur else False
    return {
        "status": "healthy" if murmur_ok else "degraded",
        "murmur": "connected" if murmur_ok else "disconnected",
    }

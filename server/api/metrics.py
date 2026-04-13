"""Prometheus-compatible metrics endpoint."""

import time

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.database import get_db
from server.dependencies import get_murmur_client
from server.models import Channel, SOSEvent, User
from server.murmur.client import MurmurClient

router = APIRouter(tags=["metrics"])

_start_time = time.time()


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(
    db: AsyncSession = Depends(get_db),
    murmur: MurmurClient | None = Depends(get_murmur_client),
):
    """Prometheus-compatible metrics in text exposition format."""
    lines = []

    # Uptime
    uptime = time.time() - _start_time
    lines.append(f"# HELP ptt_uptime_seconds Admin service uptime in seconds")
    lines.append(f"# TYPE ptt_uptime_seconds gauge")
    lines.append(f"ptt_uptime_seconds {uptime:.0f}")

    # Murmur connection
    murmur_connected = 1 if (murmur and murmur.is_connected) else 0
    lines.append(f"# HELP ptt_murmur_connected Whether Murmur voice server is connected")
    lines.append(f"# TYPE ptt_murmur_connected gauge")
    lines.append(f"ptt_murmur_connected {murmur_connected}")

    # Online users (from Murmur)
    online = 0
    if murmur and murmur.is_connected:
        status = murmur.get_status()
        online = status.users_online
    lines.append(f"# HELP ptt_users_online Currently connected voice users")
    lines.append(f"# TYPE ptt_users_online gauge")
    lines.append(f"ptt_users_online {online}")

    # Total registered users
    result = await db.execute(select(func.count(User.id)))
    total_users = result.scalar() or 0
    lines.append(f"# HELP ptt_users_total Total registered device users")
    lines.append(f"# TYPE ptt_users_total gauge")
    lines.append(f"ptt_users_total {total_users}")

    # Total channels
    result = await db.execute(select(func.count(Channel.id)))
    total_channels = result.scalar() or 0
    lines.append(f"# HELP ptt_channels_total Total channels")
    lines.append(f"# TYPE ptt_channels_total gauge")
    lines.append(f"ptt_channels_total {total_channels}")

    # Active SOS events
    result = await db.execute(
        select(func.count(SOSEvent.id)).where(SOSEvent.acknowledged == False)
    )
    active_sos = result.scalar() or 0
    lines.append(f"# HELP ptt_sos_active Unacknowledged SOS events")
    lines.append(f"# TYPE ptt_sos_active gauge")
    lines.append(f"ptt_sos_active {active_sos}")

    # Total SOS events
    result = await db.execute(select(func.count(SOSEvent.id)))
    total_sos = result.scalar() or 0
    lines.append(f"# HELP ptt_sos_total Total SOS events ever triggered")
    lines.append(f"# TYPE ptt_sos_total counter")
    lines.append(f"ptt_sos_total {total_sos}")

    return "\n".join(lines) + "\n"

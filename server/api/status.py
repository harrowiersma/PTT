from fastapi import APIRouter, Depends

from server.api.schemas import DeviceHealthResponse, ServerStatusResponse, UserOnline
from server.auth import get_current_admin

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("/server", response_model=ServerStatusResponse)
async def get_server_status(_admin: dict = Depends(get_current_admin)):
    from server.main import murmur_client

    if not murmur_client or not murmur_client.is_connected:
        return ServerStatusResponse(
            is_running=False,
            users_online=0,
            max_users=0,
            murmur_connected=False,
            users=[],
        )

    status = murmur_client.get_status()
    users = [
        UserOnline(
            username=u.name,
            channel_id=u.channel_id,
            is_muted=u.is_muted,
            is_deaf=u.is_deaf,
            online_secs=u.online_secs,
            address=u.address,
        )
        for u in status.users
    ]

    return ServerStatusResponse(
        is_running=status.is_running,
        users_online=status.users_online,
        max_users=status.max_users,
        murmur_connected=True,
        users=users,
    )


@router.get("/health")
async def health_check():
    """Public health check endpoint for monitoring."""
    from server.main import murmur_client

    murmur_ok = murmur_client.is_connected if murmur_client else False
    return {
        "status": "healthy" if murmur_ok else "degraded",
        "murmur": "connected" if murmur_ok else "disconnected",
    }

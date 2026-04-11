from fastapi import APIRouter, Depends

from server.api.schemas import ServerStatusResponse, UserOnline
from server.auth import get_current_admin
from server.dependencies import get_murmur_client
from server.murmur.client import MurmurClient

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("/server", response_model=ServerStatusResponse)
async def get_server_status(
    _admin: dict = Depends(get_current_admin),
    murmur: MurmurClient | None = Depends(get_murmur_client),
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
async def health_check(
    murmur: MurmurClient | None = Depends(get_murmur_client),
):
    """Public health check endpoint for monitoring."""
    murmur_ok = murmur.is_connected if murmur else False
    return {
        "status": "healthy" if murmur_ok else "degraded",
        "murmur": "connected" if murmur_ok else "disconnected",
    }

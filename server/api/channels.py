from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.schemas import ChannelCreate, ChannelResponse
from server.auth import get_current_admin
from server.database import get_db
from server.models import Channel

router = APIRouter(prefix="/api/channels", tags=["channels"])


@router.get("", response_model=list[ChannelResponse])
async def list_channels(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(Channel).order_by(Channel.name))
    return result.scalars().all()


@router.post("", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    channel_data: ChannelCreate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    existing = await db.execute(select(Channel).where(Channel.name == channel_data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Channel name already exists",
        )

    # Create in Murmur if connected
    mumble_id = None
    from server.main import murmur_client

    if murmur_client and murmur_client.is_connected:
        mumble_id = murmur_client.create_channel(channel_data.name)

    channel = Channel(
        name=channel_data.name,
        description=channel_data.description,
        max_users=channel_data.max_users,
        mumble_id=mumble_id,
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return channel


@router.get("/{channel_id}", response_model=ChannelResponse)
async def get_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Remove from Murmur if connected
    from server.main import murmur_client

    if murmur_client and murmur_client.is_connected and channel.mumble_id is not None:
        murmur_client.remove_channel(channel.mumble_id)

    await db.delete(channel)
    await db.commit()

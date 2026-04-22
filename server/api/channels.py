from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.schemas import ChannelCreate, ChannelResponse, ChannelUpdate
from server.auth import get_current_admin
from server.database import get_db
from server.dependencies import get_murmur_client
from server.models import Channel
from server.murmur.client import MurmurClient

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
    murmur: MurmurClient | None = Depends(get_murmur_client),
):
    existing = await db.execute(select(Channel).where(Channel.name == channel_data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Channel name already exists",
        )

    mumble_id = None
    if murmur and murmur.is_connected:
        mumble_id = murmur.create_channel(channel_data.name)

    channel = Channel(
        name=channel_data.name,
        description=channel_data.description,
        max_users=channel_data.max_users,
        mumble_id=mumble_id,
        call_group_id=channel_data.call_group_id,
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


@router.patch("/{channel_id}", response_model=ChannelResponse)
async def update_channel(
    channel_id: int,
    body: ChannelUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    fields_set = body.model_fields_set
    if "description" in fields_set:
        channel.description = body.description
    if "max_users" in fields_set and body.max_users is not None:
        channel.max_users = body.max_users
    if "call_group_id" in fields_set:
        channel.call_group_id = body.call_group_id

    await db.commit()
    await db.refresh(channel)
    return channel


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
    murmur: MurmurClient | None = Depends(get_murmur_client),
):
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    if murmur and murmur.is_connected and channel.mumble_id is not None:
        murmur.remove_channel(channel.mumble_id)

    await db.delete(channel)
    await db.commit()

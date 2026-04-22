"""Admin-CRUD for call groups + per-user membership.

Per-user channel-access scoping: channels with a call_group_id can be
joined only by users in that group (or by users.is_admin=true). NULL
on the channel side means unrestricted. Bounce enforcement lives in
MurmurClient (Task 4); this module is the data-plane.

PUT /api/call-groups/{id}/members replaces the membership set wholesale
— matches the dashboard's checkbox-list save shape (the form sends the
current full state). Add/remove deltas are wordier and need conflict
handling; not worth the API surface for typically-low-N membership.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.admin import log_audit
from server.auth import get_current_admin
from server.database import get_db
from server.models import CallGroup, Channel, User, UserCallGroup

router = APIRouter(prefix="/api/call-groups", tags=["call-groups"])


class CallGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=256)


class CallGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=256)


class CallGroupResponse(BaseModel):
    id: int
    name: str
    description: str | None
    created_at: datetime
    member_count: int
    channel_count: int


class MemberRef(BaseModel):
    id: int
    username: str


class ChannelRef(BaseModel):
    id: int
    name: str


class CallGroupDetail(CallGroupResponse):
    members: list[MemberRef]
    channels: list[ChannelRef]


class MembershipReplace(BaseModel):
    user_ids: list[int]


class ChannelsReplace(BaseModel):
    channel_ids: list[int]


async def _to_response(db: AsyncSession, group: CallGroup) -> CallGroupResponse:
    member_count = (await db.execute(
        select(func.count()).select_from(UserCallGroup)
        .where(UserCallGroup.call_group_id == group.id)
    )).scalar_one()
    channel_count = (await db.execute(
        select(func.count()).select_from(Channel)
        .where(Channel.call_group_id == group.id)
    )).scalar_one()
    return CallGroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        created_at=group.created_at,
        member_count=member_count,
        channel_count=channel_count,
    )


@router.get("", response_model=list[CallGroupResponse])
async def list_call_groups(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    rows = (await db.execute(select(CallGroup).order_by(CallGroup.name))).scalars().all()
    return [await _to_response(db, g) for g in rows]


@router.post("", response_model=CallGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_call_group(
    body: CallGroupCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    existing = (await db.execute(
        select(CallGroup).where(CallGroup.name == body.name)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Call group name already exists")
    group = CallGroup(name=body.name, description=body.description)
    db.add(group)
    await log_audit(db, admin["sub"], "call_group.create",
                     target_type="call_group", target_id=body.name)
    await db.commit()
    await db.refresh(group)
    return await _to_response(db, group)


@router.get("/{group_id}", response_model=CallGroupDetail)
async def get_call_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    group = (await db.execute(
        select(CallGroup).where(CallGroup.id == group_id)
    )).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Call group not found")
    base = await _to_response(db, group)
    members = (await db.execute(
        select(User).join(UserCallGroup, UserCallGroup.user_id == User.id)
        .where(UserCallGroup.call_group_id == group_id)
        .order_by(User.username)
    )).scalars().all()
    channels = (await db.execute(
        select(Channel).where(Channel.call_group_id == group_id).order_by(Channel.name)
    )).scalars().all()
    return CallGroupDetail(
        **base.model_dump(),
        members=[MemberRef(id=u.id, username=u.username) for u in members],
        channels=[ChannelRef(id=c.id, name=c.name) for c in channels],
    )


@router.patch("/{group_id}", response_model=CallGroupResponse)
async def update_call_group(
    group_id: int, body: CallGroupUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    group = (await db.execute(
        select(CallGroup).where(CallGroup.id == group_id)
    )).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Call group not found")
    if body.name is not None and body.name != group.name:
        clash = (await db.execute(
            select(CallGroup).where(CallGroup.name == body.name)
        )).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status_code=409, detail="Call group name already exists")
        group.name = body.name
    if body.description is not None:
        group.description = body.description
    await log_audit(db, admin["sub"], "call_group.update",
                     target_type="call_group", target_id=str(group.id))
    await db.commit()
    await db.refresh(group)
    return await _to_response(db, group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_call_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    group = (await db.execute(
        select(CallGroup).where(CallGroup.id == group_id)
    )).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Call group not found")
    # Explicit join-row delete so SQLite (no FK-cascade by default) matches
    # Postgres. Redundant on prod because the migration has ON DELETE CASCADE.
    await db.execute(
        delete(UserCallGroup).where(UserCallGroup.call_group_id == group_id)
    )
    await log_audit(db, admin["sub"], "call_group.delete",
                     target_type="call_group", target_id=str(group.id))
    await db.delete(group)
    await db.commit()


@router.put("/{group_id}/members", response_model=CallGroupResponse)
async def replace_members(
    group_id: int, body: MembershipReplace,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    group = (await db.execute(
        select(CallGroup).where(CallGroup.id == group_id)
    )).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Call group not found")
    await db.execute(
        delete(UserCallGroup).where(UserCallGroup.call_group_id == group_id)
    )
    for uid in body.user_ids:
        db.add(UserCallGroup(user_id=uid, call_group_id=group_id))
    await log_audit(db, admin["sub"], "call_group.members_replace",
                     target_type="call_group", target_id=str(group.id))
    await db.commit()
    return await _to_response(db, group)


@router.put("/{group_id}/channels", response_model=CallGroupResponse)
async def replace_channels(
    group_id: int, body: ChannelsReplace,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """Assign the listed channels to this group (wholesale replace).

    Channels previously assigned to this group but not in the list get
    their call_group_id cleared (back to NULL = visible-to-all). Channels
    never linked here are untouched — this endpoint never steals a
    channel away from another group. To move a channel between groups,
    save both groups.
    """
    group = (await db.execute(
        select(CallGroup).where(CallGroup.id == group_id)
    )).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Call group not found")

    # Clear the ones currently assigned but dropped from the new list.
    await db.execute(
        update(Channel)
        .where(Channel.call_group_id == group_id)
        .where(~Channel.id.in_(body.channel_ids))
        .values(call_group_id=None)
    )
    # Assign the new set — overwrites whatever group they had before.
    if body.channel_ids:
        await db.execute(
            update(Channel)
            .where(Channel.id.in_(body.channel_ids))
            .values(call_group_id=group_id)
        )

    await log_audit(db, admin["sub"], "call_group.channels_replace",
                     target_type="call_group", target_id=str(group.id))
    await db.commit()
    return await _to_response(db, group)

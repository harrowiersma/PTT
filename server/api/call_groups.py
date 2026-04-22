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

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server import features as _features
from server.api.admin import log_audit
from server.auth import get_current_admin
from server.database import get_db
from server.models import CallGroup, Channel, User, UserCallGroup

logger = logging.getLogger(__name__)


async def _collect_member_uids(db: AsyncSession, group_id: int) -> list[int]:
    """Registered Mumble user_ids for every member of the group.

    Members without a mumble_registered_user_id are skipped — the ACL
    can only reference users Murmur knows about. They stay locked out
    until the scheduler registers them and the ACL gets re-applied on
    the next membership save.
    """
    rows = (await db.execute(
        select(User.mumble_registered_user_id)
        .join(UserCallGroup, UserCallGroup.user_id == User.id)
        .where(
            UserCallGroup.call_group_id == group_id,
            User.mumble_registered_user_id.is_not(None),
        )
    )).all()
    return [row[0] for row in rows]


async def _tagged_channel_mumble_ids(
    db: AsyncSession, group_id: int,
) -> list[int]:
    """Mumble channel ids for every channel tagged with this group.

    Channels not yet mirrored to Murmur (mumble_id IS NULL) are dropped
    — nothing to enforce on them.
    """
    rows = (await db.execute(
        select(Channel.mumble_id).where(
            Channel.call_group_id == group_id,
            Channel.mumble_id.is_not(None),
        )
    )).all()
    return [row[0] for row in rows]


async def _apply_acl_for_group(db: AsyncSession, group_id: int) -> None:
    """Recompute + apply ACL for every channel currently tagged with
    this group. No-op when the `call_groups_hiding` feature flag is off."""
    if not _features.is_enabled("call_groups_hiding"):
        return
    from server.murmur import admin_sqlite

    chan_mumble_ids = await _tagged_channel_mumble_ids(db, group_id)
    if not chan_mumble_ids:
        return  # nothing to enforce — skip the murmur restart
    members = await _collect_member_uids(db, group_id)
    changes: list[tuple[int, list[int] | None]] = [
        (cid, list(members)) for cid in chan_mumble_ids
    ]
    try:
        await asyncio.to_thread(admin_sqlite.batched_acl_apply, changes)
    except Exception as e:
        logger.error("ACL apply for group %d failed: %s", group_id, e)

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


@router.post("/force-reconnect", status_code=status.HTTP_200_OK)
async def force_reconnect(
    admin: dict = Depends(get_current_admin),
):
    """Restart the Murmur container — every connected client reconnects
    within a couple of seconds. Used as the operator escape hatch when
    ACL state drifts (e.g., a user registered mid-session but their
    group's ACL was computed before they had a user_id). After the
    restart, clients re-read Murmur's acl table fresh.
    """
    from server.murmur import admin_sqlite
    try:
        await asyncio.to_thread(admin_sqlite.restart_murmur)
    except Exception as e:
        logger.error("force-reconnect restart failed: %s", e)
        raise HTTPException(
            status_code=500, detail=f"Murmur restart failed: {e}",
        )
    return {"ok": True}


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
    # Snapshot the tagged channels BEFORE delete — after ON DELETE SET
    # NULL fires, we can't tell them apart from always-visible channels.
    affected_mumble_ids = await _tagged_channel_mumble_ids(db, group_id)
    # Explicit join-row delete so SQLite (no FK-cascade by default) matches
    # Postgres. Redundant on prod because the migration has ON DELETE CASCADE.
    await db.execute(
        delete(UserCallGroup).where(UserCallGroup.call_group_id == group_id)
    )
    await log_audit(db, admin["sub"], "call_group.delete",
                     target_type="call_group", target_id=str(group.id))
    await db.delete(group)
    await db.commit()

    # After the DB transaction commits, clear ACL on every previously-
    # tagged channel so they revert to visible-to-all. Flag-gated —
    # when hiding is off, there's no ACL to clean up.
    if _features.is_enabled("call_groups_hiding") and affected_mumble_ids:
        from server.murmur import admin_sqlite
        changes: list[tuple[int, list[int] | None]] = [
            (cid, None) for cid in affected_mumble_ids
        ]
        try:
            await asyncio.to_thread(admin_sqlite.batched_acl_apply, changes)
        except Exception as e:
            logger.error("ACL clear after delete of group %d failed: %s",
                         group_id, e)


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
    # Recompute ACL on every channel tagged with this group so the new
    # member set takes effect in Murmur.
    await _apply_acl_for_group(db, group_id)
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

    # Snapshot the current set BEFORE mutating so we can diff afterwards.
    # Channels dropped from the group get their ACL cleared; channels
    # newly added get ACL applied with the current member set.
    prev_mumble_ids = set(await _tagged_channel_mumble_ids(db, group_id))

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

    if _features.is_enabled("call_groups_hiding"):
        from server.murmur import admin_sqlite
        new_mumble_ids = set(await _tagged_channel_mumble_ids(db, group_id))
        removed = prev_mumble_ids - new_mumble_ids
        members = await _collect_member_uids(db, group_id)
        changes: list[tuple[int, list[int] | None]] = []
        # Cleared channels revert to visible-to-all.
        changes.extend((cid, None) for cid in removed)
        # Everything in the new set gets the current member list. We
        # apply to the whole set (not just newly-added) so an overlap
        # channel with a stale ACL from a prior group gets refreshed.
        changes.extend((cid, list(members)) for cid in new_mumble_ids)
        if changes:
            try:
                await asyncio.to_thread(
                    admin_sqlite.batched_acl_apply, changes,
                )
            except Exception as e:
                logger.error("ACL apply for group %d channels failed: %s",
                             group_id, e)

    return await _to_response(db, group)

"""CRUD for admin-managed canned dispatch messages.

The merged Dispatch page's send modal renders these as a dropdown so the
operator can pick "Pickup ready" instead of typing it every time. Free-text
fallback is always available.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.features_gate import requires_feature
from server.models import DispatchCannedMessage

router = APIRouter(
    prefix="/api/dispatch/messages",
    tags=["dispatch"],
    dependencies=[requires_feature("dispatch")],
)


class MessageCreate(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    sort_order: int = 0


class MessageUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=64)
    message: str | None = Field(default=None, min_length=1, max_length=500)
    sort_order: int | None = None


class MessageResponse(BaseModel):
    id: int
    label: str
    message: str
    sort_order: int

    model_config = {"from_attributes": True}


@router.get("", response_model=list[MessageResponse])
async def list_messages(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(
        select(DispatchCannedMessage)
        .order_by(DispatchCannedMessage.sort_order, DispatchCannedMessage.id)
    )
    return result.scalars().all()


@router.post("", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def create_message(
    body: MessageCreate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    msg = DispatchCannedMessage(
        label=body.label, message=body.message, sort_order=body.sort_order
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


@router.patch("/{message_id}", response_model=MessageResponse)
async def update_message(
    message_id: int,
    body: MessageUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(
        select(DispatchCannedMessage).where(DispatchCannedMessage.id == message_id)
    )
    msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if body.label is not None:
        msg.label = body.label
    if body.message is not None:
        msg.message = body.message
    if body.sort_order is not None:
        msg.sort_order = body.sort_order
    await db.commit()
    await db.refresh(msg)
    return msg


@router.delete("/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    message_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(
        select(DispatchCannedMessage).where(DispatchCannedMessage.id == message_id)
    )
    msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    await db.delete(msg)
    await db.commit()

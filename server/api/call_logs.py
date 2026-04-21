"""Admin API for browsing the call log. Lives outside sip.py so it
isn't gated on the ``sip`` feature flag — operators may want to audit
historical calls even after disabling the SIP gateway.
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.models import CallLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/call-logs", tags=["call-logs"])


class CallLogResponse(BaseModel):
    id: int
    caller_id: Optional[str]
    slot: Optional[int]
    started_at: datetime.datetime
    assigned_at: Optional[datetime.datetime]
    answered_at: Optional[datetime.datetime]
    answered_by: Optional[str]
    ended_at: Optional[datetime.datetime]
    duration_s: Optional[int]

    class Config:
        from_attributes = True


@router.get("", response_model=list[CallLogResponse])
async def list_call_logs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    """Most-recent-first list. Defaults to the latest 100; tune via
    ?limit= and ?offset= for pagination."""
    result = await db.execute(
        select(CallLog)
        .order_by(CallLog.started_at.desc(), CallLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()

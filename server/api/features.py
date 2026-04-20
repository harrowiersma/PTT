"""Admin feature-flag CRUD.

GET  /api/admin/features           — list all flags
PUT  /api/admin/features/{key}     — enable/disable one flag

Both require admin. PUT writes the DB row AND refreshes the in-process
cache so readers see the new value without a service restart.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server import features as _features
from server.auth import get_current_admin
from server.database import get_db
from server.models import FeatureFlag

router = APIRouter(prefix="/api/admin/features", tags=["features"])


class FeatureResponse(BaseModel):
    key: str
    enabled: bool
    updated_at: datetime
    updated_by: str | None

    model_config = {"from_attributes": True}


class FeatureUpdate(BaseModel):
    enabled: bool


@router.get("", response_model=list[FeatureResponse])
async def list_features(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(FeatureFlag).order_by(FeatureFlag.key))
    return result.scalars().all()


@router.put("/{key}", response_model=FeatureResponse)
async def update_feature(
    key: str,
    body: FeatureUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    if key not in _features.FEATURE_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown feature: {key}")
    result = await db.execute(
        update(FeatureFlag)
        .where(FeatureFlag.key == key)
        .values(enabled=body.enabled, updated_by=admin.get("sub"))
        .returning(FeatureFlag)
    )
    flag = result.scalar_one()
    await db.commit()
    await _features.refresh_cache(db)
    return flag

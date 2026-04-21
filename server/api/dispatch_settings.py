"""Singleton config for the dispatch feature.

GET is intentionally unauthenticated — the dashboard reads it before login
to centre the map correctly. The only thing leaked is the operator's chosen
map default; same risk profile as /api/status/capabilities.

PUT is admin-only and refreshes the in-process cache so /api/dispatch/nearest
sees the new values without a service restart.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.features_gate import requires_feature
from server.models import DispatchSettings

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/dispatch/settings",
    tags=["dispatch"],
    dependencies=[requires_feature("dispatch")],
)


_cache: dict | None = None


class SettingsResponse(BaseModel):
    map_home_lat: float
    map_home_lng: float
    map_home_zoom: int
    max_workers: int
    search_radius_m: int | None

    model_config = {"from_attributes": True}


class SettingsUpdate(BaseModel):
    map_home_lat: float | None = Field(default=None, ge=-90, le=90)
    map_home_lng: float | None = Field(default=None, ge=-180, le=180)
    map_home_zoom: int | None = Field(default=None, ge=1, le=19)
    max_workers: int | None = Field(default=None, ge=1, le=50)
    search_radius_m: int | None = Field(default=None, ge=0)


async def _load(db: AsyncSession) -> DispatchSettings:
    result = await db.execute(select(DispatchSettings).where(DispatchSettings.id == 1))
    row = result.scalar_one_or_none()
    if row is None:
        row = DispatchSettings(
            id=1, map_home_lat=38.72, map_home_lng=-9.14,
            map_home_zoom=11, max_workers=10, search_radius_m=None,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def get_cached(db: AsyncSession) -> dict:
    """Used by /api/dispatch/nearest to avoid a DB round-trip per request."""
    global _cache
    if _cache is None:
        row = await _load(db)
        _cache = {
            "map_home_lat": row.map_home_lat,
            "map_home_lng": row.map_home_lng,
            "map_home_zoom": row.map_home_zoom,
            "max_workers": row.max_workers,
            "search_radius_m": row.search_radius_m,
        }
    return _cache


def invalidate_cache() -> None:
    global _cache
    _cache = None


@router.get("", response_model=SettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    return await _load(db)


@router.put("", response_model=SettingsResponse)
async def update_settings(
    body: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    row = await _load(db)
    if body.map_home_lat is not None:
        row.map_home_lat = body.map_home_lat
    if body.map_home_lng is not None:
        row.map_home_lng = body.map_home_lng
    if body.map_home_zoom is not None:
        row.map_home_zoom = body.map_home_zoom
    if body.max_workers is not None:
        row.max_workers = body.max_workers
    if "search_radius_m" in body.model_fields_set:
        row.search_radius_m = body.search_radius_m
    row.updated_by = admin.get("username")
    await db.commit()
    await db.refresh(row)
    invalidate_cache()
    return row

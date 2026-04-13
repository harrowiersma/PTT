"""Saved dispatch locations for quick one-click dispatch."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.models import DispatchLocation

router = APIRouter(prefix="/api/dispatch/locations", tags=["dispatch"])


class LocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    latitude: float
    longitude: float
    description: str | None = Field(default=None, max_length=256)
    sort_order: int = 0


class LocationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    latitude: float | None = None
    longitude: float | None = None
    description: str | None = Field(default=None, max_length=256)
    sort_order: int | None = None


class LocationResponse(BaseModel):
    id: int
    name: str
    latitude: float
    longitude: float
    description: str | None
    sort_order: int

    model_config = {"from_attributes": True}


@router.get("", response_model=list[LocationResponse])
async def list_locations(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(
        select(DispatchLocation).order_by(DispatchLocation.sort_order, DispatchLocation.name)
    )
    return result.scalars().all()


@router.post("", response_model=LocationResponse, status_code=status.HTTP_201_CREATED)
async def create_location(
    loc: LocationCreate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    location = DispatchLocation(
        name=loc.name,
        latitude=loc.latitude,
        longitude=loc.longitude,
        description=loc.description,
        sort_order=loc.sort_order,
    )
    db.add(location)
    await db.commit()
    await db.refresh(location)
    return location


@router.patch("/{location_id}", response_model=LocationResponse)
async def update_location(
    location_id: int,
    loc: LocationUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(DispatchLocation).where(DispatchLocation.id == location_id))
    location = result.scalar_one_or_none()
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")

    if loc.name is not None:
        location.name = loc.name
    if loc.latitude is not None:
        location.latitude = loc.latitude
    if loc.longitude is not None:
        location.longitude = loc.longitude
    if loc.description is not None:
        location.description = loc.description
    if loc.sort_order is not None:
        location.sort_order = loc.sort_order

    await db.commit()
    await db.refresh(location)
    return location


@router.delete("/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_location(
    location_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(DispatchLocation).where(DispatchLocation.id == location_id))
    location = result.scalar_one_or_none()
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")

    await db.delete(location)
    await db.commit()

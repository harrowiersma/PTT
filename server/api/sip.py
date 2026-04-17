"""SIP gateway configuration API.

Admin CRUD for SipTrunk (provider account) and SipNumber (DID). The
sip-bridge container reads these rows directly to build its Baresip
config — credentials live in the DB, not in environment variables, so
the operator can add/remove DIDs without a redeploy.

Call-time endpoints (webhook, mute, active-calls) land here later when
the bridge container ships.

Also exposes /api/sip/internal/* endpoints for the sip-bridge container
to fetch config (including plaintext passwords). These are gated by a
shared secret in X-Internal-Auth and MUST NOT be forwarded by nginx —
the nginx config returns 404 for /api/sip/internal/*.
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.config import settings
from server.database import get_db
from server.models import SipTrunk, SipNumber
from server.api.schemas import (
    SipTrunkCreate, SipTrunkUpdate, SipTrunkResponse,
    SipNumberCreate, SipNumberUpdate, SipNumberResponse,
)


def _require_internal_auth(x_internal_auth: str | None = Header(default=None)) -> None:
    """Gate /api/sip/internal/* endpoints behind a shared secret.

    Disabled entirely if PTT_INTERNAL_API_SECRET is empty so these
    endpoints can't be reached by accident on a misconfigured deployment.
    """
    if not settings.internal_api_secret:
        raise HTTPException(status_code=404, detail="Not found")
    if x_internal_auth != settings.internal_api_secret:
        raise HTTPException(status_code=403, detail="Invalid internal auth")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sip", tags=["sip"])


# --- Trunks ---

@router.get("/trunks", response_model=list[SipTrunkResponse])
async def list_trunks(
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipTrunk).order_by(SipTrunk.id))
    return result.scalars().all()


@router.post("/trunks", response_model=SipTrunkResponse, status_code=status.HTTP_201_CREATED)
async def create_trunk(
    data: SipTrunkCreate,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    trunk = SipTrunk(**data.model_dump())
    db.add(trunk)
    await db.commit()
    await db.refresh(trunk)
    logger.info("SIP trunk created: id=%d label=%s host=%s auth=%s",
                trunk.id, trunk.label, trunk.sip_host,
                "user" if trunk.sip_user else "ip")
    return trunk


@router.get("/trunks/{trunk_id}", response_model=SipTrunkResponse)
async def get_trunk(
    trunk_id: int,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipTrunk).where(SipTrunk.id == trunk_id))
    trunk = result.scalar_one_or_none()
    if not trunk:
        raise HTTPException(status_code=404, detail="Trunk not found")
    return trunk


@router.patch("/trunks/{trunk_id}", response_model=SipTrunkResponse)
async def update_trunk(
    trunk_id: int,
    data: SipTrunkUpdate,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipTrunk).where(SipTrunk.id == trunk_id))
    trunk = result.scalar_one_or_none()
    if not trunk:
        raise HTTPException(status_code=404, detail="Trunk not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(trunk, field, value)
    await db.commit()
    await db.refresh(trunk)
    logger.info("SIP trunk updated: id=%d", trunk.id)
    return trunk


@router.delete("/trunks/{trunk_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trunk(
    trunk_id: int,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipTrunk).where(SipTrunk.id == trunk_id))
    trunk = result.scalar_one_or_none()
    if not trunk:
        raise HTTPException(status_code=404, detail="Trunk not found")
    # CASCADE handles sip_numbers.
    await db.delete(trunk)
    await db.commit()
    logger.info("SIP trunk deleted: id=%d", trunk_id)


# --- Numbers (DIDs) ---

@router.get("/numbers", response_model=list[SipNumberResponse])
async def list_numbers(
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipNumber).order_by(SipNumber.trunk_id, SipNumber.id))
    return result.scalars().all()


@router.post("/numbers", response_model=SipNumberResponse, status_code=status.HTTP_201_CREATED)
async def create_number(
    data: SipNumberCreate,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    # Validate the referenced trunk exists; the CASCADE only helps on delete.
    result = await db.execute(select(SipTrunk).where(SipTrunk.id == data.trunk_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="trunk_id does not exist")
    number = SipNumber(**data.model_dump())
    db.add(number)
    await db.commit()
    await db.refresh(number)
    logger.info("SIP number created: id=%d did=%s trunk_id=%d",
                number.id, number.did, number.trunk_id)
    return number


@router.patch("/numbers/{number_id}", response_model=SipNumberResponse)
async def update_number(
    number_id: int,
    data: SipNumberUpdate,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipNumber).where(SipNumber.id == number_id))
    number = result.scalar_one_or_none()
    if not number:
        raise HTTPException(status_code=404, detail="Number not found")
    if data.trunk_id is not None and data.trunk_id != number.trunk_id:
        trunk_chk = await db.execute(select(SipTrunk).where(SipTrunk.id == data.trunk_id))
        if not trunk_chk.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="trunk_id does not exist")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(number, field, value)
    await db.commit()
    await db.refresh(number)
    logger.info("SIP number updated: id=%d", number.id)
    return number


@router.delete("/numbers/{number_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_number(
    number_id: int,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipNumber).where(SipNumber.id == number_id))
    number = result.scalar_one_or_none()
    if not number:
        raise HTTPException(status_code=404, detail="Number not found")
    await db.delete(number)
    await db.commit()
    logger.info("SIP number deleted: id=%d", number_id)


# --- Internal endpoints (sip-bridge container only) ---

@router.get("/internal/config/trunks")
async def internal_list_trunks(
    _auth: None = Depends(_require_internal_auth),
    db: AsyncSession = Depends(get_db),
):
    """Full trunk config INCLUDING sip_password. For the bridge only."""
    result = await db.execute(select(SipTrunk).order_by(SipTrunk.id))
    rows = result.scalars().all()
    return [
        {
            "id": t.id,
            "label": t.label,
            "sip_host": t.sip_host,
            "sip_port": t.sip_port,
            "sip_user": t.sip_user,
            "sip_password": t.sip_password,
            "from_uri": t.from_uri,
            "transport": t.transport,
            "registration_interval_s": t.registration_interval_s,
            "enabled": t.enabled,
        }
        for t in rows
    ]


@router.get("/internal/config/numbers")
async def internal_list_numbers(
    _auth: None = Depends(_require_internal_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipNumber).order_by(SipNumber.id))
    rows = result.scalars().all()
    return [
        {
            "id": n.id,
            "trunk_id": n.trunk_id,
            "did": n.did,
            "label": n.label,
            "enabled": n.enabled,
        }
        for n in rows
    ]

import io
import logging
import secrets

import qrcode
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.schemas import QRResponse, UserCreate, UserResponse, UserUpdate
from server.auth import get_current_admin
from server.config import settings
from server.database import get_db
from server.dependencies import get_murmur_client
from server.models import User
from server.murmur.client import MurmurClient
from server.traccar_client import TraccarClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[UserResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return result.scalars().all()


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
    murmur: MurmurClient | None = Depends(get_murmur_client),
):
    existing = await db.execute(select(User).where(User.username == user_data.username))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )

    mumble_password = user_data.password or secrets.token_urlsafe(16)

    # Auto-create a Traccar device with uniqueId=<username> if one wasn't supplied.
    # openPTT app reports GPS via OsmAnd protocol using the mumble username as the
    # device's uniqueId, so creating the Traccar device here unifies identity
    # (mumble username == traccar uniqueId) and avoids a manual provisioning step.
    traccar_device_id = user_data.traccar_device_id
    if traccar_device_id is None:
        try:
            traccar = TraccarClient()
            display = user_data.display_name or user_data.username
            new_device_id = await traccar.create_device(
                name=display, unique_id=user_data.username
            )
            if new_device_id is not None:
                traccar_device_id = new_device_id
                logger.info(
                    "Auto-created Traccar device id=%s uniqueId=%s for user %s",
                    new_device_id, user_data.username, user_data.username,
                )
            else:
                logger.warning(
                    "Could not auto-create Traccar device for user %s; "
                    "user created without GPS link",
                    user_data.username,
                )
        except Exception as e:
            # Don't block user creation if Traccar is unreachable
            logger.error("Traccar device creation failed for %s: %s",
                         user_data.username, e)

    user = User(
        username=user_data.username,
        display_name=user_data.display_name,
        mumble_password=mumble_password,
        channel_id=user_data.channel_id,
        is_admin=user_data.is_admin,
        is_lone_worker=user_data.is_lone_worker,
        shift_duration_hours=user_data.shift_duration_hours,
        can_answer_calls=user_data.can_answer_calls,
        traccar_device_id=traccar_device_id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    if murmur and murmur.is_connected:
        murmur.register_user(user.username, mumble_password)

    return user


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user_data.display_name is not None:
        user.display_name = user_data.display_name
    if user_data.password is not None:
        user.mumble_password = user_data.password
    if user_data.is_admin is not None:
        user.is_admin = user_data.is_admin
    if user_data.is_lone_worker is not None:
        user.is_lone_worker = user_data.is_lone_worker
    if user_data.shift_duration_hours is not None:
        user.shift_duration_hours = user_data.shift_duration_hours
    if user_data.can_answer_calls is not None:
        user.can_answer_calls = user_data.can_answer_calls
    if user_data.traccar_device_id is not None:
        # Allow setting to 0/null to unlink
        user.traccar_device_id = user_data.traccar_device_id if user_data.traccar_device_id != 0 else None

    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Clean up the linked Traccar device so the uniqueId (username) can be reused
    if user.traccar_device_id is not None:
        try:
            traccar = TraccarClient()
            await traccar.delete_device(user.traccar_device_id)
        except Exception as e:
            logger.warning("Could not delete Traccar device %s for user %s: %s",
                           user.traccar_device_id, user.username, e)

    await db.delete(user)
    await db.commit()


@router.post("/{user_id}/migrate-traccar-uniqueid")
async def migrate_traccar_unique_id(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    """Change the linked Traccar device's uniqueId to match the mumble username.

    One-time migration for users provisioned before the openPTT app's built-in
    GPS integration (when devices used random numeric uniqueIds). After this,
    the openPTT app — which sends position with id=<username> — will be
    recognized by Traccar for this user.

    If no Traccar device is linked yet, auto-creates one with uniqueId=username.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    traccar = TraccarClient()

    if user.traccar_device_id is None:
        display = user.display_name or user.username
        new_device_id = await traccar.create_device(
            name=display, unique_id=user.username
        )
        if new_device_id is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to create Traccar device",
            )
        user.traccar_device_id = new_device_id
        await db.commit()
        return {
            "action": "created",
            "user_id": user.id,
            "username": user.username,
            "traccar_device_id": new_device_id,
            "unique_id": user.username,
        }

    ok = await traccar.update_device_unique_id(
        user.traccar_device_id, user.username
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to update Traccar device uniqueId",
        )
    return {
        "action": "updated",
        "user_id": user.id,
        "username": user.username,
        "traccar_device_id": user.traccar_device_id,
        "unique_id": user.username,
    }


@router.get("/{user_id}/qr", response_model=QRResponse)
async def get_user_qr(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    mumble_url = (
        f"mumble://{user.username}:{user.mumble_password}"
        f"@{settings.public_host}:{settings.public_port}/"
    )

    return QRResponse(
        username=user.username,
        mumble_url=mumble_url,
        qr_code_url=f"/api/users/{user_id}/qr.png",
    )


@router.get("/{user_id}/qr.png")
async def get_user_qr_image(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    mumble_url = (
        f"mumble://{user.username}:{user.mumble_password}"
        f"@{settings.public_host}:{settings.public_port}/"
    )

    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(mumble_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return StreamingResponse(buf, media_type="image/png")

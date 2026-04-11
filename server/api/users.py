import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.schemas import QRResponse, UserCreate, UserResponse
from server.auth import get_current_admin
from server.config import settings
from server.database import get_db
from server.models import User

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
):
    existing = await db.execute(select(User).where(User.username == user_data.username))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )

    mumble_password = user_data.password or secrets.token_urlsafe(16)

    user = User(
        username=user_data.username,
        display_name=user_data.display_name,
        mumble_password=mumble_password,
        channel_id=user_data.channel_id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Register in Murmur if connected
    from server.main import murmur_client

    if murmur_client and murmur_client.is_connected:
        murmur_client.register_user(user.username, mumble_password)

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

    await db.delete(user)
    await db.commit()


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
    import io

    import qrcode
    from fastapi.responses import StreamingResponse

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

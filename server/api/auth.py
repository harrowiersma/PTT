import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
import bcrypt as _bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.schemas import LoginRequest, TokenResponse
from server.auth import create_access_token
from server.config import settings
from server.database import get_db
from server.models import AdminUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Simple in-memory rate limiter
import time
from collections import defaultdict

_failed_attempts: dict[str, list[float]] = defaultdict(list)
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 300


def _check_rate_limit(ip: str) -> None:
    now = time.time()
    _failed_attempts[ip] = [t for t in _failed_attempts[ip] if now - t < _WINDOW_SECONDS]
    if len(_failed_attempts[ip]) >= _MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again in a few minutes.",
        )


def _record_failure(ip: str) -> None:
    _failed_attempts[ip].append(time.time())


@router.post("/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    req: Request,
    db: AsyncSession = Depends(get_db),
):
    client_ip = req.client.host if req.client else "unknown"
    _check_rate_limit(client_ip)

    # Try database admin accounts first
    result = await db.execute(
        select(AdminUser).where(
            AdminUser.username == request.username,
            AdminUser.is_active == True,
        )
    )
    admin = result.scalar_one_or_none()

    if admin and _bcrypt.checkpw(request.password.encode(), admin.password_hash.encode()):
        # Update last login
        admin.last_login = datetime.now(timezone.utc)
        await db.commit()
        _failed_attempts.pop(client_ip, None)
        token = create_access_token(data={"sub": admin.username, "role": admin.role})
        return TokenResponse(access_token=token)

    # Fallback: check .env admin credentials (for initial setup before first DB admin is created)
    if (
        request.username == settings.admin_username
        and request.password == settings.admin_password
        and settings.admin_password != "admin"  # Don't allow default password
    ):
        _failed_attempts.pop(client_ip, None)
        token = create_access_token(data={"sub": request.username, "role": "admin"})
        return TokenResponse(access_token=token)

    _record_failure(client_ip)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
    )

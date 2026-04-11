import secrets
import time
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request, status

from server.api.schemas import LoginRequest, TokenResponse
from server.auth import create_access_token
from server.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Simple in-memory rate limiter: max 5 failed attempts per IP per 5 minutes
_failed_attempts: dict[str, list[float]] = defaultdict(list)
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 300


def _check_rate_limit(ip: str) -> None:
    now = time.time()
    # Clean old entries
    _failed_attempts[ip] = [t for t in _failed_attempts[ip] if now - t < _WINDOW_SECONDS]
    if len(_failed_attempts[ip]) >= _MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again in a few minutes.",
        )


def _record_failure(ip: str) -> None:
    _failed_attempts[ip].append(time.time())


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest, req: Request):
    client_ip = req.client.host if req.client else "unknown"
    _check_rate_limit(client_ip)

    if not secrets.compare_digest(request.username, settings.admin_username) or \
       not secrets.compare_digest(request.password, settings.admin_password):
        _record_failure(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    # Clear failures on successful login
    _failed_attempts.pop(client_ip, None)

    token = create_access_token(data={"sub": request.username})
    return TokenResponse(access_token=token)

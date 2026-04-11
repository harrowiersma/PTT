from fastapi import APIRouter, HTTPException, status

from server.api.schemas import LoginRequest, TokenResponse
from server.auth import create_access_token
from server.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    if (
        request.username != settings.admin_username
        or request.password != settings.admin_password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    token = create_access_token(data={"sub": request.username})
    return TokenResponse(access_token=token)

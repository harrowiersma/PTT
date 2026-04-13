from datetime import datetime

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
    display_name: str | None = Field(default=None, max_length=128)
    password: str = Field(min_length=4, max_length=128)
    channel_id: int | None = None
    is_admin: bool = False


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, min_length=4, max_length=128)
    is_admin: bool | None = None


class UserResponse(BaseModel):
    id: int
    username: str
    display_name: str | None
    channel_id: int | None
    is_admin: bool
    is_active: bool
    created_at: datetime
    last_seen: datetime | None

    model_config = {"from_attributes": True}


class UserOnline(BaseModel):
    username: str
    channel_id: int
    channel_name: str = ""
    is_muted: bool
    is_deaf: bool
    online_secs: int
    address: str
    # GPS data from Traccar (if available)
    latitude: float | None = None
    longitude: float | None = None
    gps_timestamp: str | None = None
    battery: float | None = None
    speed: float | None = None


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_ -]+$')
    description: str | None = Field(default=None, max_length=512)
    max_users: int = 0


class ChannelResponse(BaseModel):
    id: int
    mumble_id: int | None
    name: str
    description: str | None
    max_users: int
    created_at: datetime

    model_config = {"from_attributes": True}


class DeviceHealthResponse(BaseModel):
    username: str
    is_online: bool
    ip_address: str | None
    latency_ms: int | None
    connected_since: datetime | None
    last_updated: datetime


class ServerStatusResponse(BaseModel):
    is_running: bool
    users_online: int
    max_users: int
    murmur_connected: bool
    users: list[UserOnline]


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class QRResponse(BaseModel):
    username: str
    mumble_url: str
    qr_code_url: str

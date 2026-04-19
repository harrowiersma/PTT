from datetime import datetime

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
    display_name: str | None = Field(default=None, max_length=128)
    password: str = Field(min_length=4, max_length=128)
    channel_id: int | None = None
    is_admin: bool = False
    is_lone_worker: bool = False
    shift_duration_hours: int | None = Field(default=None, ge=1, le=24)
    can_answer_calls: bool = False
    traccar_device_id: int | None = None


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, min_length=4, max_length=128)
    is_admin: bool | None = None
    is_lone_worker: bool | None = None
    shift_duration_hours: int | None = Field(default=None, ge=1, le=24)
    can_answer_calls: bool | None = None
    traccar_device_id: int | None = None


class UserResponse(BaseModel):
    id: int
    username: str
    display_name: str | None
    channel_id: int | None
    is_admin: bool
    is_lone_worker: bool
    shift_duration_hours: int | None = None
    can_answer_calls: bool = False
    traccar_device_id: int | None
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


# --- SIP gateway ---

class SipTrunkCreate(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    sip_host: str = Field(min_length=1, max_length=256)
    sip_port: int = Field(default=5060, ge=1, le=65535)
    sip_user: str | None = Field(default=None, max_length=128)
    sip_password: str | None = Field(default=None, max_length=256)
    from_uri: str | None = Field(default=None, max_length=256)
    transport: str = Field(default="udp", pattern=r'^(udp|tcp|tls)$')
    registration_interval_s: int = Field(default=3600, ge=60, le=86400)
    enabled: bool = True
    greeting_text: str | None = Field(default=None, max_length=2000)


class SipTrunkUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=128)
    sip_host: str | None = Field(default=None, max_length=256)
    sip_port: int | None = Field(default=None, ge=1, le=65535)
    sip_user: str | None = Field(default=None, max_length=128)
    sip_password: str | None = Field(default=None, max_length=256)
    from_uri: str | None = Field(default=None, max_length=256)
    transport: str | None = Field(default=None, pattern=r'^(udp|tcp|tls)$')
    registration_interval_s: int | None = Field(default=None, ge=60, le=86400)
    enabled: bool | None = None
    greeting_text: str | None = Field(default=None, max_length=2000)


class SipTrunkResponse(BaseModel):
    id: int
    label: str
    sip_host: str
    sip_port: int
    sip_user: str | None
    # Password intentionally excluded from responses. The bridge reads
    # directly from the DB; the dashboard never sees it after creation.
    from_uri: str | None
    transport: str
    registration_interval_s: int
    enabled: bool
    greeting_text: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SipGreetingUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class SipNumberCreate(BaseModel):
    trunk_id: int
    did: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    enabled: bool = True


class SipNumberUpdate(BaseModel):
    trunk_id: int | None = None
    did: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None


class SipNumberResponse(BaseModel):
    id: int
    trunk_id: int
    did: str
    label: str | None
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Device provisioning ---

class ProvisioningTokenCreate(BaseModel):
    """Body for POST /api/provisioning/tokens.

    The plaintext Mumble password is supplied by the admin at generation
    time (we only store a bcrypt hash on ``users.mumble_password``, and
    Humla needs the plaintext to seed its sqlite row). The admin already
    knows it because they set it when they created the account.
    """
    user_id: int = Field(ge=1)
    password: str = Field(min_length=4, max_length=256)


class ProvisioningTokenResponse(BaseModel):
    slug: str
    url: str
    user_id: int
    username: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    os_fetched: str | None = None

    model_config = {"from_attributes": True}


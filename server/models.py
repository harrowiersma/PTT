import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from server.database import Base


class AdminUser(Base):
    """Admin dashboard users with bcrypt-hashed passwords."""
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="admin")  # admin, viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_login: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuditLog(Base):
    """Audit trail for all admin actions."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    admin_username: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=True)  # user, channel, sos, etc.
    target_id: Mapped[str] = mapped_column(String(64), nullable=True)
    details: Mapped[str] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=True)
    mumble_password: Mapped[str] = mapped_column(String(256), nullable=False)
    channel_id: Mapped[int] = mapped_column(Integer, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_lone_worker: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-user shift length for the lone-worker system. Nullable; falls back to
    # LoneWorkerConfig.default_shift_hours when NULL.
    shift_duration_hours: Mapped[int] = mapped_column(Integer, nullable=True)
    # ACL for entering Phone channels (SIP gateway). Default false; dashboard
    # toggles per user.
    can_answer_calls: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    traccar_device_id: Mapped[int] = mapped_column(Integer, nullable=True)  # linked Traccar device
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SipTrunk(Base):
    """A SIP trunk (provider account). One trunk can have many DIDs.

    Credentials are stored plaintext for v1 (same pattern as User.mumble_password);
    tighten to AES-at-rest when the broader secrets story lands.
    """
    __tablename__ = "sip_trunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    sip_host: Mapped[str] = mapped_column(String(256), nullable=False)
    sip_port: Mapped[int] = mapped_column(Integer, default=5060, nullable=False)
    # NULL means IP-auth (allowlisted carrier sends INVITEs without REGISTER).
    sip_user: Mapped[str] = mapped_column(String(128), nullable=True)
    sip_password: Mapped[str] = mapped_column(String(256), nullable=True)
    # If NULL, bridge derives from sip:<user>@<host>.
    from_uri: Mapped[str] = mapped_column(String(256), nullable=True)
    # udp | tcp | tls
    transport: Mapped[str] = mapped_column(String(8), default="udp", nullable=False)
    registration_interval_s: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class SipNumber(Base):
    """A DID owned by a SIP trunk. All DIDs ring into the shared Phone channel."""
    __tablename__ = "sip_numbers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trunk_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    did: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class LoneWorkerShift(Base):
    """A bounded work session for the lone-worker system.

    When a shift is active, the overdue-check loop considers the user. When
    no active shift exists, the loop skips them — no 24/7 pings. Shifts are
    created from a long-press on the device side, and auto-end on expiry.
    """
    __tablename__ = "lone_worker_shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    planned_end_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # user_ended | auto_expired | admin_ended
    end_reason: Mapped[str] = mapped_column(String(32), nullable=True)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mumble_id: Mapped[int] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=True)
    max_users: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SOSEvent(Base):
    __tablename__ = "sos_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    latitude: Mapped[float] = mapped_column(nullable=True, default=0)
    longitude: Mapped[float] = mapped_column(nullable=True, default=0)
    message: Mapped[str] = mapped_column(String(512), nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged_by: Mapped[str] = mapped_column(String(64), nullable=True)
    acknowledged_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    triggered_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DispatchEvent(Base):
    __tablename__ = "dispatch_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_username: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    latitude: Mapped[float] = mapped_column(nullable=True, default=0)
    longitude: Mapped[float] = mapped_column(nullable=True, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DispatchLocation(Base):
    """Pre-configured dispatch locations for quick selection."""
    __tablename__ = "dispatch_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeviceHealth(Base):
    __tablename__ = "device_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=True)
    connected_since: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_updated: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

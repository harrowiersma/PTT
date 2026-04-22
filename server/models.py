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
    # Presence intent. NULL = never-set (treated as 'offline').
    # Values: 'online' | 'busy' | 'offline'.
    status_label: Mapped[str] = mapped_column(String(16), nullable=True)
    status_updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Device audibility — piggy-backs on status POSTs from the app.
    # True = ringer normal + voice-call stream volume > 0; False = muted/silent;
    # NULL = never reported.
    is_audible: Mapped[bool] = mapped_column(Boolean, nullable=True)
    is_audible_updated_at: Mapped[datetime.datetime] = mapped_column(
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
    # Piper TTS greeting the caller hears on answer. NULL → sip-bridge falls
    # back to the GREETING_TEXT env var. Admin edits via the dashboard;
    # a save triggers immediate regeneration + push to sip-bridge's
    # asterisk sounds dir, so the next call uses the new audio without
    # a container restart.
    greeting_text: Mapped[str] = mapped_column(Text, nullable=True)
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
    # Optional FK to call_groups.id. NULL = unrestricted (any user can join).
    # ON DELETE SET NULL handled in the migration.
    call_group_id: Mapped[int] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CallGroup(Base):
    """Per-user channel-access scoping. Channels with call_group_id set
    are joinable only by users who belong to that group (or by users
    with is_admin=True). NULL on the channel side = unrestricted."""
    __tablename__ = "call_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserCallGroup(Base):
    """Join table — composite PK enforces uniqueness."""
    __tablename__ = "user_call_groups"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_group_id: Mapped[int] = mapped_column(Integer, primary_key=True)


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


class DispatchCannedMessage(Base):
    """Admin-managed canned messages for the dispatch modal dropdown."""
    __tablename__ = "dispatch_canned_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DispatchSettings(Base):
    """Singleton config row for the dispatch feature.

    Always exactly one row with id=1 (seeded by migration). Holds the map
    default, the per-request worker cap, and the optional radius filter
    applied by /api/dispatch/nearest.
    """
    __tablename__ = "dispatch_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    map_home_lat: Mapped[float] = mapped_column(Float, nullable=False)
    map_home_lng: Mapped[float] = mapped_column(Float, nullable=False)
    map_home_zoom: Mapped[int] = mapped_column(Integer, nullable=False, default=11)
    max_workers: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    search_radius_m: Mapped[int] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=True)


class DeviceProvisioningToken(Base):
    """One-click provisioning short-link for a P50 handset.

    Field tech opens ``ptt.harro.ch/p/<slug>`` on their laptop, the server
    sniffs their User-Agent and serves the OS-appropriate ADB-driven setup
    script with per-device config baked in. Single-use, 24 h TTL — ``used_at``
    is stamped by a POST-back from the completed script, not on the initial
    script GET (so a botched run can be retried from the same link within
    its TTL).

    ``mumble_password_plaintext`` lives here because the primary ``users``
    row only has a bcrypt hash and the Humla Mumble client needs the
    plaintext to seed its sqlite server row. Reveal is one-shot at token
    creation; after that the only code path that reads it is the template
    renderer inside this container.
    """
    __tablename__ = "device_provisioning_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    mumble_password_plaintext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    used_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # "macos" | "windows" | "linux" | NULL — stamped on first GET of /p/<slug>
    os_fetched: Mapped[str] = mapped_column(String(16), nullable=True)


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


class FeatureFlag(Base):
    """Admin-configurable feature toggle. One row per module.

    Enabled flags propagate to: (a) lifespan task startup, (b) route
    dependencies, (c) /api/status/capabilities for downstream clients.
    """
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=True)


class CallLog(Base):
    """One row per inbound SIP call. Lifecycle:

    ``started``    (row INSERT, caller_id from Asterisk)
    ``assigned``   (slot known — sip-bridge posted /internal/call-assigned)
    ``answered``   (operator tapped Answer — app posted /api/sip/answered)
    ``ended``      (sip-bridge cleanup posted /internal/call-ended)

    A call without an ``answered_by`` means nobody picked it up. A call
    without ``ended_at`` is still in flight (or the sip-bridge crashed
    before cleanup — handle via an "orphan" sweep if that becomes real).
    """

    __tablename__ = "call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    caller_id: Mapped[str] = mapped_column(String(64), nullable=True)
    slot: Mapped[int] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    assigned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    answered_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    answered_by: Mapped[str] = mapped_column(String(64), nullable=True)
    ended_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_s: Mapped[int] = mapped_column(Integer, nullable=True)

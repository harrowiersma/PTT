import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from server.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=True)
    mumble_password: Mapped[str] = mapped_column(String(256), nullable=False)
    channel_id: Mapped[int] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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

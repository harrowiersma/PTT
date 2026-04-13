"""Admin user management + audit log endpoints."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from passlib.hash import bcrypt
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.models import AdminUser, AuditLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- Schemas ---

class AdminCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
    password: str = Field(min_length=8, max_length=128)
    role: str = "admin"  # admin or viewer


class AdminResponse(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: datetime
    last_login: datetime | None

    model_config = {"from_attributes": True}


class AuditLogResponse(BaseModel):
    id: int
    timestamp: datetime
    admin_username: str
    action: str
    target_type: str | None
    target_id: str | None
    details: str | None


# --- Audit helper ---

async def log_audit(
    db: AsyncSession, admin_username: str, action: str,
    target_type: str = None, target_id: str = None,
    details: str = None, ip_address: str = None,
):
    """Log an admin action to the audit trail."""
    entry = AuditLog(
        admin_username=admin_username,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
        ip_address=ip_address,
    )
    db.add(entry)
    # Don't commit here, let the caller commit with the main transaction


# --- Admin user endpoints ---

@router.get("/users", response_model=list[AdminResponse])
async def list_admins(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(AdminUser).order_by(AdminUser.created_at))
    return result.scalars().all()


@router.post("/users", response_model=AdminResponse, status_code=status.HTTP_201_CREATED)
async def create_admin(
    data: AdminCreate,
    req: Request,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    # Check for duplicates
    existing = await db.execute(select(AdminUser).where(AdminUser.username == data.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Admin username already exists")

    new_admin = AdminUser(
        username=data.username,
        password_hash=bcrypt.hash(data.password),
        role=data.role,
    )
    db.add(new_admin)
    await log_audit(
        db, admin["sub"], "create_admin",
        target_type="admin", target_id=data.username,
        ip_address=req.client.host if req.client else None,
    )
    await db.commit()
    await db.refresh(new_admin)
    return new_admin


@router.delete("/users/{admin_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin(
    admin_id: int,
    req: Request,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    result = await db.execute(select(AdminUser).where(AdminUser.id == admin_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Admin not found")

    # Can't delete yourself
    if target.username == admin["sub"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own admin account")

    await log_audit(
        db, admin["sub"], "delete_admin",
        target_type="admin", target_id=target.username,
        ip_address=req.client.host if req.client else None,
    )
    await db.delete(target)
    await db.commit()


# --- Audit log endpoint ---

@router.get("/audit", response_model=list[AuditLogResponse])
async def get_audit_log(
    limit: int = 100,
    action: str = None,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
    if action:
        query = query.where(AuditLog.action == action)
    result = await db.execute(query)
    return [
        AuditLogResponse(
            id=e.id, timestamp=e.timestamp, admin_username=e.admin_username,
            action=e.action, target_type=e.target_type, target_id=e.target_id,
            details=e.details,
        )
        for e in result.scalars().all()
    ]

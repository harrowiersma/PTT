"""Bulk device enrollment: CSV upload to create multiple users at once."""

import csv
import io
import logging
import secrets
import zipfile

import qrcode
from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.config import settings
from server.database import get_db
from server.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bulk", tags=["bulk"])


@router.get("/template")
async def download_csv_template(_admin: dict = Depends(get_current_admin)):
    """Download a CSV template for bulk enrollment."""
    content = "username,display_name,password\nharro,Harro Wiersma,\nyuliia,Yuliia,\n"
    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=enrollment_template.csv"},
    )


@router.post("/enroll")
async def bulk_enroll(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """Upload a CSV to create multiple users. Returns a ZIP of QR codes.

    CSV format: username,display_name,password
    If password is empty, a random one is generated.
    """
    content = await file.read()
    text = content.decode("utf-8-sig")  # Handle BOM from Excel
    reader = csv.DictReader(io.StringIO(text))

    created = []
    errors = []

    for i, row in enumerate(reader, start=2):  # start=2 because row 1 is header
        username = row.get("username", "").strip()
        display_name = row.get("display_name", "").strip() or None
        password = row.get("password", "").strip() or secrets.token_urlsafe(12)

        if not username:
            errors.append(f"Row {i}: empty username")
            continue

        # Check for existing
        result = await db.execute(select(User).where(User.username == username))
        if result.scalar_one_or_none():
            errors.append(f"Row {i}: '{username}' already exists")
            continue

        user = User(
            username=username,
            display_name=display_name,
            mumble_password=password,
        )
        db.add(user)
        created.append({"username": username, "password": password, "display_name": display_name})

    if created:
        await db.commit()

    # Generate ZIP of QR codes
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add summary CSV
        summary = "username,display_name,password,mumble_url\n"
        for u in created:
            mumble_url = f"mumble://{u['username']}:{u['password']}@{settings.public_host}:{settings.public_port}/"
            summary += f"{u['username']},{u['display_name'] or ''},{u['password']},{mumble_url}\n"

            # Generate QR image
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(mumble_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            img_buf = io.BytesIO()
            img.save(img_buf, format="PNG")
            zf.writestr(f"qr-{u['username']}.png", img_buf.getvalue())

        zf.writestr("enrollment_summary.csv", summary)

        if errors:
            zf.writestr("errors.txt", "\n".join(errors))

    zip_buffer.seek(0)

    logger.info("Bulk enrollment: %d created, %d errors by %s", len(created), len(errors), admin.get("sub"))

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=enrollment.zip",
            "X-Created": str(len(created)),
            "X-Errors": str(len(errors)),
        },
    )

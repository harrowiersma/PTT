"""Device provisioning endpoints — one-click P50 setup.

Workflow:
  1. Admin hits POST /api/provisioning/tokens with a target user_id +
     the plaintext Mumble password. Server generates an 8-char slug,
     stores a 24 h TTL row, returns the short URL.
  2. Field tech opens https://ptt.harro.ch/p/<slug> on their laptop.
     Nginx rewrites to /api/provisioning/script/<slug>; this handler
     sniffs User-Agent, renders the bash or PowerShell template with
     per-device values, and ships it as a download.
  3. The downloaded script drives adb: installs the APK (from
     /apk/openptt-foss-debug.apk), grants runtime perms, seeds the
     Humla mumble.db + SharedPreferences, launches the service.
  4. Final step of the script POSTs to /api/provisioning/tokens/<slug>/
     completed so the token is stamped ``used_at`` and the admin
     dashboard shows it as consumed.

Slug is the auth for the public endpoints — no JWT required on
/script/<slug>, /completed/<slug>, or /apk/*. Admin endpoints (list,
create, revoke) all require admin JWT.
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.schemas import ProvisioningTokenCreate, ProvisioningTokenResponse
from server.auth import get_current_admin
from server.config import settings
from server.database import get_db
from server.models import DeviceProvisioningToken, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/provisioning", tags=["provisioning"])

# Slugs: URL-safe alphanumeric, 8 chars. ~47 bits of entropy, enough for
# a 24 h short-link surface. Ambiguous chars (0/O/I/l) kept in — these
# are emailed/clicked, not typed.
_SLUG_ALPHABET = string.ascii_letters + string.digits
_SLUG_LENGTH = 8
_TOKEN_TTL = timedelta(hours=24)


def _generate_slug() -> str:
    return "".join(secrets.choice(_SLUG_ALPHABET) for _ in range(_SLUG_LENGTH))


def _detect_os(user_agent: Optional[str]) -> str:
    """Return 'macos' | 'windows' | 'linux' from a User-Agent string.

    Unknown UAs return 'linux' (bash script is the safer default for any
    Unix-flavored fallback; Windows has a distinctive signature).
    """
    if not user_agent:
        return "linux"
    ua = user_agent.lower()
    if "windows" in ua:
        return "windows"
    if "mac os x" in ua or "macintosh" in ua or "darwin" in ua:
        return "macos"
    return "linux"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(token: DeviceProvisioningToken) -> bool:
    expires_at = token.expires_at
    # SQLite may hand back naive datetimes — treat them as UTC.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= _now_utc()


async def _load_template(name: str) -> str:
    """Load a provisioning script template from the on-disk provisioning/ dir.

    Templates use ``{{double_brace}}`` placeholders; we substitute with
    plain .replace() to avoid pulling in Jinja for three variables.
    """
    root = Path(__file__).resolve().parents[2] / "provisioning"
    path = root / name
    return path.read_text(encoding="utf-8")


def _render(template: str, ctx: dict[str, str]) -> str:
    out = template
    for key, val in ctx.items():
        out = out.replace("{{" + key + "}}", val)
    return out


# ---------- Admin endpoints ----------

@router.post(
    "/tokens",
    response_model=ProvisioningTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_token(
    data: ProvisioningTokenCreate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    user_row = (await db.execute(select(User).where(User.id == data.user_id))).scalar_one_or_none()
    if user_row is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Retry on the (astronomically unlikely) slug collision.
    for _ in range(5):
        slug = _generate_slug()
        existing = (
            await db.execute(
                select(DeviceProvisioningToken).where(DeviceProvisioningToken.slug == slug)
            )
        ).scalar_one_or_none()
        if existing is None:
            break
    else:
        raise HTTPException(status_code=500, detail="Could not allocate unique slug")

    now = _now_utc()
    token = DeviceProvisioningToken(
        slug=slug,
        user_id=user_row.id,
        mumble_password_plaintext=data.password,
        expires_at=now + _TOKEN_TTL,
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)

    logger.info(
        "provisioning: token issued slug=%s user=%s expires=%s",
        slug, user_row.username, token.expires_at.isoformat(),
    )
    return ProvisioningTokenResponse(
        slug=token.slug,
        url=f"{settings.admin_public_url.rstrip('/')}/p/{token.slug}",
        user_id=token.user_id,
        username=user_row.username,
        created_at=token.created_at,
        expires_at=token.expires_at,
        used_at=token.used_at,
        os_fetched=token.os_fetched,
    )


@router.get("/tokens", response_model=list[ProvisioningTokenResponse])
async def list_tokens(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    """List all tokens with joined usernames. Includes expired rows so
    ops can audit/revoke retroactively; UI filters client-side."""
    rows = (
        await db.execute(
            select(DeviceProvisioningToken, User.username)
            .join(User, User.id == DeviceProvisioningToken.user_id)
            .order_by(DeviceProvisioningToken.created_at.desc())
        )
    ).all()
    base = settings.admin_public_url.rstrip("/")
    out: list[ProvisioningTokenResponse] = []
    for token, username in rows:
        out.append(
            ProvisioningTokenResponse(
                slug=token.slug,
                url=f"{base}/p/{token.slug}",
                user_id=token.user_id,
                username=username,
                created_at=token.created_at,
                expires_at=token.expires_at,
                used_at=token.used_at,
                os_fetched=token.os_fetched,
            )
        )
    return out


@router.delete("/tokens/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    slug: str,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    token = (
        await db.execute(
            select(DeviceProvisioningToken).where(DeviceProvisioningToken.slug == slug)
        )
    ).scalar_one_or_none()
    if token is None:
        raise HTTPException(status_code=404, detail="Token not found")
    await db.delete(token)
    await db.commit()
    logger.info("provisioning: token revoked slug=%s", slug)


# ---------- Public (slug-authed) endpoints ----------

@router.get("/script/{slug}")
async def fetch_script(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_agent: Optional[str] = Header(default=None, alias="User-Agent"),
):
    """Public endpoint — slug is the auth. Returns the rendered setup
    script for the caller's OS. Expired tokens → 410 Gone.
    """
    token = (
        await db.execute(
            select(DeviceProvisioningToken).where(DeviceProvisioningToken.slug == slug)
        )
    ).scalar_one_or_none()
    if token is None:
        raise HTTPException(status_code=404, detail="Not found")
    if _is_expired(token):
        raise HTTPException(status_code=410, detail="Provisioning link expired")

    user_row = (
        await db.execute(select(User).where(User.id == token.user_id))
    ).scalar_one_or_none()
    if user_row is None:
        # User got deleted under the token — treat the slug as dead.
        raise HTTPException(status_code=410, detail="Target user no longer exists")

    os_kind = _detect_os(user_agent)
    if token.os_fetched != os_kind:
        token.os_fetched = os_kind
        await db.commit()

    admin_url = settings.admin_public_url.rstrip("/")
    traccar_url = settings.traccar_osmand_url.rstrip("/")
    ctx = {
        "username": user_row.username,
        "mumble_password": token.mumble_password_plaintext,
        "admin_url": admin_url,
        "traccar_url": traccar_url,
        "mumble_host": settings.public_host,
        "mumble_port": str(settings.public_port),
        "slug": token.slug,
    }

    if os_kind == "windows":
        tmpl = await _load_template("windows.ps1.tmpl")
        body = _render(tmpl, ctx)
        return Response(
            content=body,
            media_type="text/x-powershell; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="openptt-provision.ps1"',
                "Cache-Control": "no-store",
            },
        )

    # macOS + Linux share the bash template.
    tmpl = await _load_template("macos.sh.tmpl")
    body = _render(tmpl, ctx)
    return Response(
        content=body,
        media_type="text/x-shellscript; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="openptt-provision.sh"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/tokens/{slug}/completed", status_code=status.HTTP_200_OK)
async def mark_completed(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """Called by the provisioning script at the end of a successful run.
    No auth beyond the slug — script runs on random laptops, admin JWTs
    aren't available. Only marks the row; idempotent.
    """
    token = (
        await db.execute(
            select(DeviceProvisioningToken).where(DeviceProvisioningToken.slug == slug)
        )
    ).scalar_one_or_none()
    if token is None:
        raise HTTPException(status_code=404, detail="Not found")
    if _is_expired(token):
        raise HTTPException(status_code=410, detail="Provisioning link expired")

    if token.used_at is None:
        token.used_at = _now_utc()
        await db.commit()
        await db.refresh(token)
        logger.info("provisioning: token consumed slug=%s", slug)
    used_at = token.used_at
    # Some backends (SQLite) hand back naive datetimes; normalize to UTC
    # so the response body is stable across calls.
    if used_at.tzinfo is None:
        used_at = used_at.replace(tzinfo=timezone.utc)
    return {"slug": slug, "used_at": used_at.isoformat()}


@router.get("/apk/openptt-foss-debug.apk")
async def fetch_apk():
    """Serve the published APK. Placeholder until the CI release pipeline
    is wired — returns 404 if the configured path is empty.
    """
    apk_path = Path(settings.provisioning_apk_path)
    if not apk_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                "APK not yet published — run the CI release or upload via admin "
                f"(expected at {apk_path})"
            ),
        )
    return FileResponse(
        path=str(apk_path),
        media_type="application/vnd.android.package-archive",
        filename="openptt-foss-debug.apk",
    )


@router.get("/script/{slug}/help", response_class=PlainTextResponse)
async def script_help(slug: str):
    """Human-readable fallback for uncertain User-Agents. Not currently
    auto-served — the OS detector falls back to bash — but wired so a
    support tech can link the user here.
    """
    return (
        "openPTT provisioning\n"
        "\n"
        f"Short link: {settings.admin_public_url.rstrip('/')}/p/{slug}\n"
        "\n"
        "macOS / Linux:  curl -fsSL <link> -o setup.sh && bash setup.sh\n"
        "Windows (PS):  Invoke-WebRequest <link> -OutFile setup.ps1; .\\setup.ps1\n"
    )

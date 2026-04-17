"""SIP gateway configuration API.

Admin CRUD for SipTrunk (provider account) and SipNumber (DID). The
sip-bridge container reads these rows directly to build its Baresip
config — credentials live in the DB, not in environment variables, so
the operator can add/remove DIDs without a redeploy.

Call-time endpoints (webhook, mute, active-calls) land here later when
the bridge container ships.

Also exposes /api/sip/internal/* endpoints for the sip-bridge container
to fetch config (including plaintext passwords). These are gated by a
shared secret in X-Internal-Auth and MUST NOT be forwarded by nginx —
the nginx config returns 404 for /api/sip/internal/*.
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.config import settings
from server.database import get_db
from server.models import SipTrunk, SipNumber, User
from server.api.schemas import (
    SipTrunkCreate, SipTrunkUpdate, SipTrunkResponse,
    SipNumberCreate, SipNumberUpdate, SipNumberResponse,
)


def _require_internal_auth(x_internal_auth: str | None = Header(default=None)) -> None:
    """Gate /api/sip/internal/* endpoints behind a shared secret.

    Disabled entirely if PTT_INTERNAL_API_SECRET is empty so these
    endpoints can't be reached by accident on a misconfigured deployment.
    """
    if not settings.internal_api_secret:
        raise HTTPException(status_code=404, detail="Not found")
    if x_internal_auth != settings.internal_api_secret:
        raise HTTPException(status_code=403, detail="Invalid internal auth")

logger = logging.getLogger(__name__)

from server.dependencies import get_murmur_client

router = APIRouter(prefix="/api/sip", tags=["sip"])


# --- Trunks ---

@router.get("/trunks", response_model=list[SipTrunkResponse])
async def list_trunks(
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipTrunk).order_by(SipTrunk.id))
    return result.scalars().all()


@router.post("/trunks", response_model=SipTrunkResponse, status_code=status.HTTP_201_CREATED)
async def create_trunk(
    data: SipTrunkCreate,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    trunk = SipTrunk(**data.model_dump())
    db.add(trunk)
    await db.commit()
    await db.refresh(trunk)
    logger.info("SIP trunk created: id=%d label=%s host=%s auth=%s",
                trunk.id, trunk.label, trunk.sip_host,
                "user" if trunk.sip_user else "ip")
    return trunk


@router.get("/trunks/{trunk_id}", response_model=SipTrunkResponse)
async def get_trunk(
    trunk_id: int,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipTrunk).where(SipTrunk.id == trunk_id))
    trunk = result.scalar_one_or_none()
    if not trunk:
        raise HTTPException(status_code=404, detail="Trunk not found")
    return trunk


@router.patch("/trunks/{trunk_id}", response_model=SipTrunkResponse)
async def update_trunk(
    trunk_id: int,
    data: SipTrunkUpdate,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipTrunk).where(SipTrunk.id == trunk_id))
    trunk = result.scalar_one_or_none()
    if not trunk:
        raise HTTPException(status_code=404, detail="Trunk not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(trunk, field, value)
    await db.commit()
    await db.refresh(trunk)
    logger.info("SIP trunk updated: id=%d", trunk.id)
    return trunk


@router.delete("/trunks/{trunk_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trunk(
    trunk_id: int,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipTrunk).where(SipTrunk.id == trunk_id))
    trunk = result.scalar_one_or_none()
    if not trunk:
        raise HTTPException(status_code=404, detail="Trunk not found")
    # CASCADE handles sip_numbers.
    await db.delete(trunk)
    await db.commit()
    logger.info("SIP trunk deleted: id=%d", trunk_id)


# --- Numbers (DIDs) ---

@router.get("/numbers", response_model=list[SipNumberResponse])
async def list_numbers(
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipNumber).order_by(SipNumber.trunk_id, SipNumber.id))
    return result.scalars().all()


@router.post("/numbers", response_model=SipNumberResponse, status_code=status.HTTP_201_CREATED)
async def create_number(
    data: SipNumberCreate,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    # Validate the referenced trunk exists; the CASCADE only helps on delete.
    result = await db.execute(select(SipTrunk).where(SipTrunk.id == data.trunk_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="trunk_id does not exist")
    number = SipNumber(**data.model_dump())
    db.add(number)
    await db.commit()
    await db.refresh(number)
    logger.info("SIP number created: id=%d did=%s trunk_id=%d",
                number.id, number.did, number.trunk_id)
    return number


@router.patch("/numbers/{number_id}", response_model=SipNumberResponse)
async def update_number(
    number_id: int,
    data: SipNumberUpdate,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipNumber).where(SipNumber.id == number_id))
    number = result.scalar_one_or_none()
    if not number:
        raise HTTPException(status_code=404, detail="Number not found")
    if data.trunk_id is not None and data.trunk_id != number.trunk_id:
        trunk_chk = await db.execute(select(SipTrunk).where(SipTrunk.id == data.trunk_id))
        if not trunk_chk.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="trunk_id does not exist")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(number, field, value)
    await db.commit()
    await db.refresh(number)
    logger.info("SIP number updated: id=%d", number.id)
    return number


@router.delete("/numbers/{number_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_number(
    number_id: int,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipNumber).where(SipNumber.id == number_id))
    number = result.scalar_one_or_none()
    if not number:
        raise HTTPException(status_code=404, detail="Number not found")
    await db.delete(number)
    await db.commit()
    logger.info("SIP number deleted: id=%d", number_id)


# --- Internal endpoints (sip-bridge container only) ---

@router.get("/internal/config/trunks")
async def internal_list_trunks(
    _auth: None = Depends(_require_internal_auth),
    db: AsyncSession = Depends(get_db),
):
    """Full trunk config INCLUDING sip_password. For the bridge only."""
    result = await db.execute(select(SipTrunk).order_by(SipTrunk.id))
    rows = result.scalars().all()
    return [
        {
            "id": t.id,
            "label": t.label,
            "sip_host": t.sip_host,
            "sip_port": t.sip_port,
            "sip_user": t.sip_user,
            "sip_password": t.sip_password,
            "from_uri": t.from_uri,
            "transport": t.transport,
            "registration_interval_s": t.registration_interval_s,
            "enabled": t.enabled,
        }
        for t in rows
    ]


@router.get("/internal/config/numbers")
async def internal_list_numbers(
    _auth: None = Depends(_require_internal_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SipNumber).order_by(SipNumber.id))
    rows = result.scalars().all()
    return [
        {
            "id": n.id,
            "trunk_id": n.trunk_id,
            "did": n.did,
            "label": n.label,
            "enabled": n.enabled,
        }
        for n in rows
    ]


@router.post("/internal/ensure-phone-channel")
async def internal_ensure_phone_channel(
    _auth: None = Depends(_require_internal_auth),
    murmur=Depends(get_murmur_client),
):
    """Ensure a Mumble channel named "Phone" exists; return its id.

    The sip-bridge's PTTPhone user is unregistered (no Mumble account)
    and cannot create channels on a server where the root ACL doesn't
    grant create permission to anonymous users. The admin container's
    PTTAdmin user is implicitly trusted (it's the first registered
    ICE client, so Murmur grants it create privileges), so channel
    creation must happen here and PTTPhone just joins afterwards.
    """
    if not murmur or not murmur.has_mumble:
        raise HTTPException(status_code=503, detail="Murmur not available")

    mm = murmur._mumble
    for cid, chan in mm.channels.items():
        if chan.get("name") == "Phone":
            return {"channel_id": cid, "created": False}

    mumble_id = murmur.create_channel("Phone")
    if mumble_id is None:
        raise HTTPException(status_code=502, detail="Failed to create Phone channel")
    return {"channel_id": mumble_id, "created": True}


# Pre-rendered ding tone (48 kHz 16-bit mono PCM). Generated on first use
# and cached for the life of the process. ~200 ms double-beep, soft.
_ding_pcm_cache: bytes | None = None


def _get_ding_pcm() -> bytes:
    global _ding_pcm_cache
    if _ding_pcm_cache is not None:
        return _ding_pcm_cache
    import numpy as np
    sr = 48000
    def beep(freq: float, ms: int, amp: float = 0.10) -> np.ndarray:
        n = int(sr * ms / 1000)
        t = np.arange(n, dtype=np.float32) / sr
        wave = np.sin(2 * np.pi * freq * t) * amp * 32767
        # Quick attack + decay so it doesn't click.
        env_n = int(sr * 0.008)
        env = np.ones(n, dtype=np.float32)
        env[:env_n] = np.linspace(0, 1, env_n)
        env[-env_n:] = np.linspace(1, 0, env_n)
        return (wave * env).astype(np.int16)
    def silence(ms: int) -> np.ndarray:
        return np.zeros(int(sr * ms / 1000), dtype=np.int16)
    data = np.concatenate([beep(880, 90), silence(60), beep(1175, 90)])
    _ding_pcm_cache = data.tobytes()
    return _ding_pcm_cache


@router.post("/internal/call-started")
async def internal_call_started(
    payload: dict | None = None,
    _auth: None = Depends(_require_internal_auth),
    murmur=Depends(get_murmur_client),
    db: AsyncSession = Depends(get_db),
):
    """Subtle audio notification when an inbound SIP call lands.

    The sip-bridge's dialplan hits this endpoint (Asterisk CURL()) right
    after Answer(), so the notification fires while the Piper greeting
    plays — by the time the caller is ready to talk, eligible users
    have already heard the ding.

    Target: each Mumble-connected user whose DB row has
    can_answer_calls=true AND is_active=true, except if they're already
    in Phone or Emergency. Whispered per-user (no channel-hop), so
    everyone else in their channel hears nothing.
    """
    if not murmur or not murmur.has_mumble:
        raise HTTPException(status_code=503, detail="Murmur not available")

    caller_id = (payload or {}).get("caller_id", "unknown")

    result = await db.execute(
        select(User.username).where(
            User.can_answer_calls.is_(True),
            User.is_active.is_(True),
        )
    )
    eligible_usernames = {row[0] for row in result.all()}
    if not eligible_usernames:
        logger.info("call-started: no users have can_answer_calls=true; skipping")
        return {"notified": [], "reason": "no eligible users"}

    mm = murmur._mumble
    ding_pcm = _get_ding_pcm()
    notified: list[str] = []

    for sid, user in mm.users.items():
        name = user.get("name")
        if name not in eligible_usernames:
            continue
        chan = mm.channels.get(user.get("channel_id"), {})
        if chan.get("name") in ("Phone", "Emergency"):
            continue  # already in position to answer
        if murmur.whisper_audio(sid, ding_pcm, with_preamble=False):
            notified.append(name)

    logger.info("call-started: notified %s (caller=%s)", notified, caller_id)
    return {"notified": notified, "caller_id": caller_id}


@router.post("/internal/tts")
async def internal_tts(
    payload: dict,
    _auth: None = Depends(_require_internal_auth),
):
    """Return a 48kHz 16-bit mono WAV of the given text, synthesized by Piper.

    The sip-bridge calls this at startup to cache a greeting played to
    inbound callers. Keeping Piper in the admin container means the
    bridge image stays slim and the operator can change the greeting
    text by editing one env var on the bridge, no rebuild needed.
    """
    import io
    import wave
    from server.weather_bot import text_to_audio_pcm

    text = (payload or {}).get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    pcm = text_to_audio_pcm(text)
    if not pcm:
        raise HTTPException(status_code=502, detail="TTS synthesis failed")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(pcm)
    from fastapi.responses import Response
    return Response(content=buf.getvalue(), media_type="audio/wav")

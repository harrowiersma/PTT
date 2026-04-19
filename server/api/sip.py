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
import os

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.config import settings
from server.database import get_db
from server.models import SipTrunk, SipNumber, User
from server.api.schemas import (
    SipTrunkCreate, SipTrunkUpdate, SipTrunkResponse,
    SipNumberCreate, SipNumberUpdate, SipNumberResponse,
    SipGreetingUpdate,
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


# ---------------------------------------------------------------------------
# Greeting text — per-trunk, editable from the dashboard. Saving also
# regenerates the 8 kHz WAV via Piper and pushes it into the sip-bridge
# container's asterisk sounds dir so the next call uses the new audio
# without waiting for a sip-bridge restart.
# ---------------------------------------------------------------------------

SIP_BRIDGE_CONTAINER_NAME = os.environ.get("SIP_BRIDGE_CONTAINER_NAME", "ptt-sip-bridge-1")
SIP_BRIDGE_GREETING_PATH = "/usr/share/asterisk/sounds/en/openptt-greeting.wav"


def _build_greeting_wav_8k(text: str) -> bytes:
    """Render `text` through Piper (48 kHz mono int16 PCM) and wrap as an
    8 kHz WAV — matches what sip_bridge/render_entry produces so the
    asterisk Playback() pipeline consumes it unchanged.
    """
    import io
    import wave

    import numpy as np
    from server.weather_bot import text_to_audio_pcm

    pcm_48k = text_to_audio_pcm(text)
    if not pcm_48k:
        raise RuntimeError("Piper returned no audio")

    samples_48k = np.frombuffer(pcm_48k, dtype=np.int16).astype(np.float32)
    target_n = int(len(samples_48k) * 8000 / 48000)
    src_idx = np.arange(len(samples_48k), dtype=np.float32)
    tgt_idx = np.linspace(0, len(samples_48k) - 1, target_n, dtype=np.float32)
    samples_8k = np.interp(tgt_idx, src_idx, samples_48k)
    samples_8k = np.clip(samples_8k, -32768, 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(samples_8k.tobytes())
    return buf.getvalue()


def _push_greeting_to_sip_bridge(wav_bytes: bytes) -> None:
    """Tar the WAV and put_archive() it into the sip-bridge container's
    asterisk sounds dir. Same Docker socket we already mount for the
    Murmur admin path — no new infrastructure.
    """
    import io
    import tarfile
    import time

    try:
        import docker
    except ImportError as e:
        raise RuntimeError(f"docker SDK not available: {e}")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="openptt-greeting.wav")
        info.size = len(wav_bytes)
        info.mtime = int(time.time())
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(wav_bytes))
    buf.seek(0)

    client = docker.from_env()
    container = client.containers.get(SIP_BRIDGE_CONTAINER_NAME)
    target_dir = os.path.dirname(SIP_BRIDGE_GREETING_PATH)
    ok = container.put_archive(path=target_dir, data=buf.getvalue())
    if not ok:
        raise RuntimeError("put_archive returned False")
    logger.info("pushed new greeting WAV (%d bytes) to %s:%s",
                len(wav_bytes), SIP_BRIDGE_CONTAINER_NAME, SIP_BRIDGE_GREETING_PATH)


@router.get("/greeting")
async def get_sip_greeting(
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return the current greeting text. Falls back to the default string
    from sip_bridge's GREETING_TEXT env if no trunk has a value set."""
    result = await db.execute(select(SipTrunk).order_by(SipTrunk.id))
    trunks = result.scalars().all()
    for t in trunks:
        if t.greeting_text:
            return {"text": t.greeting_text, "source": "db", "trunk_id": t.id}
    return {
        "text": (
            "You are now being connected to the openPTT radio trunk system. "
            "Please note that there may be small delays between transmissions."
        ),
        "source": "default",
        "trunk_id": None,
    }


@router.put("/greeting")
async def put_sip_greeting(
    payload: SipGreetingUpdate,
    _admin: dict = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Save a new greeting text across all trunks, regenerate the WAV via
    Piper, and push the new file into sip-bridge's sounds dir. The next
    inbound call picks it up automatically.
    """
    result = await db.execute(select(SipTrunk).order_by(SipTrunk.id))
    trunks = result.scalars().all()
    if not trunks:
        raise HTTPException(status_code=400, detail="No SIP trunks configured")

    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Greeting text is required")

    for t in trunks:
        t.greeting_text = text
    await db.commit()
    logger.info("greeting_text saved to %d trunk(s)", len(trunks))

    # Regenerate + push. Both are blocking, so run in a thread so the
    # asyncio loop stays responsive for other admin requests.
    import asyncio
    def _regen_and_push() -> tuple[int, str | None]:
        try:
            wav = _build_greeting_wav_8k(text)
            _push_greeting_to_sip_bridge(wav)
            return len(wav), None
        except Exception as e:
            return 0, str(e)

    wav_bytes, err = await asyncio.to_thread(_regen_and_push)
    if err:
        logger.error("greeting regen/push failed: %s", err)
        # DB is already saved; surface the error but don't rollback — next
        # sip-bridge restart will render from DB text on its own.
        raise HTTPException(
            status_code=502,
            detail=f"Greeting saved to DB but push to sip-bridge failed: {err}",
        )
    return {"saved": True, "wav_bytes": wav_bytes, "pushed": True}


# ---------------------------------------------------------------------------
# Radio-initiated call control — hangup (KEYCODE_MENU) + mute-toggle
# (KEYCODE_CALL green-button). App POSTs to these un-authed endpoints with
# a username; we signal sip-bridge's Python process via `docker exec pkill`.
# Signals are the cleanest cross-container control path here: sip-bridge
# is host-networked while admin is bridge-networked, so a direct HTTP port
# between them needs extra plumbing. Using the docker socket admin already
# has mounted.
# ---------------------------------------------------------------------------

def _signal_sip_bridge(signame: str) -> None:
    """Deliver `signame` (e.g. SIGUSR1) to the audiosocket_bridge process
    inside the sip-bridge container. `pkill -<signame> -f audiosocket_bridge`
    exits 0 if at least one process matched, 1 if none — we treat both as
    success because "no bridge running" is a legitimate state (no call).
    """
    try:
        import docker
    except ImportError as e:
        raise RuntimeError(f"docker SDK not available: {e}")
    client = docker.from_env()
    container = client.containers.get(SIP_BRIDGE_CONTAINER_NAME)
    short = signame.replace("SIG", "")  # pkill wants -USR1 not -SIGUSR1
    result = container.exec_run(["pkill", f"-{short}", "-f", "audiosocket_bridge"])
    if result.exit_code not in (0, 1):
        raise RuntimeError(
            f"pkill exit={result.exit_code}: {result.output.decode('utf-8', 'replace').strip()}"
        )


class RadioCallControlRequest(BaseModel):
    """App POSTs its Mumble username so we can log who triggered the action."""
    username: str = Field(min_length=1, max_length=64)


@router.post("/hangup-current")
async def hangup_current_call(req: RadioCallControlRequest):
    """End the in-flight phone call from the radio side. Called by the
    P50 app when the MENU key is pressed while the user is in the Phone
    channel. No auth — app and admin share the internal network only.
    """
    logger.info("radio hangup requested by user=%r", req.username)
    try:
        _signal_sip_bridge("SIGUSR1")
    except Exception as e:
        logger.error("hangup signal failed: %s", e)
        raise HTTPException(status_code=503, detail=f"signal failed: {e}")
    return {"ok": True, "action": "hangup", "username": req.username}


@router.post("/mute-toggle")
async def mute_toggle(req: RadioCallControlRequest):
    """Toggle the caller-inaudible state. Called by the P50 app when the
    green (KEYCODE_CALL) key is pressed in the Phone channel. Radio user
    can still hear the caller; caller hears silence until the next toggle.
    """
    logger.info("radio mute toggle requested by user=%r", req.username)
    try:
        _signal_sip_bridge("SIGUSR2")
    except Exception as e:
        logger.error("mute signal failed: %s", e)
        raise HTTPException(status_code=503, detail=f"signal failed: {e}")
    return {"ok": True, "action": "mute-toggle", "username": req.username}


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
            "greeting_text": t.greeting_text,
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


import asyncio as _asyncio

# Pre-rendered ding tone (48 kHz 16-bit mono PCM). Generated on first use
# and cached for the life of the process. ~200 ms double-beep, soft.
_ding_pcm_cache: bytes | None = None

# Call-notification state. Only one active call at a time (single-caller
# design), so a single-task slot is enough. Guarded by an asyncio lock.
_notify_state: dict = {"active": False, "caller_id": None, "task": None}
_notify_lock = _asyncio.Lock()
_NOTIFY_INTERVAL_S = 3.0


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


async def _fetch_eligible_usernames(db: AsyncSession) -> set[str]:
    result = await db.execute(
        select(User.username).where(
            User.can_answer_calls.is_(True),
            User.is_active.is_(True),
        )
    )
    return {row[0] for row in result.all()}


def _ding_eligible_users(murmur, eligible: set[str]) -> list[str]:
    """Whisper the ding to every eligible user not already in Phone/Emergency.

    Returns the list of usernames notified on this tick. Also returns an
    empty list if at least one eligible user is already in Phone, which
    the caller can use as a signal to stop pinging.
    """
    mm = murmur._mumble
    ding_pcm = _get_ding_pcm()
    in_phone_already = False
    notified: list[str] = []

    for sid, user in mm.users.items():
        name = user.get("name")
        if name not in eligible:
            continue
        chan = mm.channels.get(user.get("channel_id"), {})
        chan_name = chan.get("name")
        if chan_name == "Phone":
            in_phone_already = True
            continue
        if chan_name == "Emergency":
            continue
        if murmur.whisper_audio(sid, ding_pcm, with_preamble=False):
            notified.append(name)

    # Signal "stop pinging" by stashing on the state dict; the poller reads it.
    _notify_state["someone_in_phone"] = in_phone_already
    return notified


async def _notify_loop(caller_id: str, murmur, db_session_factory):
    """Re-ping eligible users every _NOTIFY_INTERVAL_S until either a user
    with can_answer_calls joins the Phone channel, or /internal/call-ended
    clears the active flag.
    """
    from server.database import async_session as _session_factory
    try:
        while _notify_state["active"]:
            async with _session_factory() as db:
                eligible = await _fetch_eligible_usernames(db)
            if not eligible:
                logger.info("notify-loop: no eligible users; stopping")
                break
            notified = _ding_eligible_users(murmur, eligible)
            if _notify_state.get("someone_in_phone"):
                logger.info("notify-loop: a can_answer_calls user reached Phone; stopping")
                break
            logger.info("notify-loop: re-pinged %s (caller=%s)", notified, caller_id)
            await _asyncio.sleep(_NOTIFY_INTERVAL_S)
    except _asyncio.CancelledError:
        logger.info("notify-loop cancelled for caller=%s", caller_id)
    finally:
        _notify_state["active"] = False
        _notify_state["task"] = None


@router.api_route("/internal/call-started", methods=["GET", "POST"])
async def internal_call_started(
    caller_id: str = "unknown",
    _auth: None = Depends(_require_internal_auth),
    murmur=Depends(get_murmur_client),
    db: AsyncSession = Depends(get_db),
):
    """Kick off the ding-notification loop for an inbound call.

    The sip-bridge's dialplan hits this right after Answer(), so the
    first ding fires while the Piper greeting plays. The loop re-dings
    every 3 s until either: (a) a user with can_answer_calls joins the
    Phone channel, or (b) /internal/call-ended is hit on hangup.
    """
    if not murmur or not murmur.has_mumble:
        raise HTTPException(status_code=503, detail="Murmur not available")

    eligible = await _fetch_eligible_usernames(db)
    if not eligible:
        logger.info("call-started: no users have can_answer_calls=true; skipping")
        return {"notified": [], "reason": "no eligible users"}

    notified = _ding_eligible_users(murmur, eligible)
    logger.info("call-started: first ping notified %s (caller=%s)", notified, caller_id)

    async with _notify_lock:
        existing = _notify_state.get("task")
        if existing and not existing.done():
            existing.cancel()
        _notify_state["active"] = True
        _notify_state["caller_id"] = caller_id
        _notify_state["task"] = _asyncio.create_task(_notify_loop(caller_id, murmur, None))

    return {"notified": notified, "caller_id": caller_id, "looping": True}


@router.api_route("/internal/call-ended", methods=["GET", "POST"])
async def internal_call_ended(
    _auth: None = Depends(_require_internal_auth),
):
    """Stop the ding loop. Called by the dialplan right before Hangup()."""
    async with _notify_lock:
        task = _notify_state.get("task")
        _notify_state["active"] = False
        if task and not task.done():
            task.cancel()
    logger.info("call-ended: ding loop stopped")
    return {"stopped": True}


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

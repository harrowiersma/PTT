import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.dependencies import get_murmur_client
from server.features_gate import requires_feature
from server.models import DispatchEvent, User
from server.murmur.client import MurmurClient
from server.traccar_client import TraccarClient

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/dispatch",
    tags=["dispatch"],
    dependencies=[requires_feature("dispatch")],
)


class DispatchRequest(BaseModel):
    target_username: str
    message: str
    latitude: float = 0
    longitude: float = 0


@router.get("/recent")
async def list_recent(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    """Return the N most recent dispatch events for the operational page log."""
    limit = max(1, min(limit, 200))
    result = await db.execute(
        select(DispatchEvent)
        .order_by(desc(DispatchEvent.created_at), desc(DispatchEvent.id))
        .limit(limit)
    )
    return [
        {
            "id": e.id,
            "target_username": e.target_username,
            "message": e.message,
            "latitude": e.latitude,
            "longitude": e.longitude,
            "created_at": e.created_at,
        }
        for e in result.scalars().all()
    ]


def _connected_usernames(murmur) -> set[str]:
    """Lowercased set of Mumble-connected human usernames.
    Patchable in tests via patch('server.api.dispatch._connected_usernames').
    """
    if not murmur or not getattr(murmur, "has_mumble", False):
        return set()
    try:
        from server.murmur.client import BOT_USERNAMES
        return {
            u["name"].lower()
            for u in murmur._mumble.users.values()
            if u["name"] not in BOT_USERNAMES
        }
    except Exception:
        return set()


@router.get("/nearest")
async def find_nearest(
    lat: float,
    lng: float,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
    murmur: MurmurClient | None = Depends(get_murmur_client),
):
    """Find nearest workers to a given GPS location.
    Uses explicit user-device links where available, falls back to name matching.
    Honours dispatch_settings.max_workers and search_radius_m.
    Requires worker to have status_label='online' AND be Mumble-connected.
    """
    from server.api.dispatch_settings import get_cached as _settings

    settings = await _settings(db)
    client = TraccarClient()
    positions = await client.get_positions()

    # Pull usernames + status_label; both paths feed the same filter below.
    result = await db.execute(select(User))
    db_users = result.scalars().all()
    device_to_username = {u.traccar_device_id: u.username for u in db_users if u.traccar_device_id}
    status_by_username = {u.username.lower(): u.status_label for u in db_users}

    connected = _connected_usernames(murmur)
    radius = settings["search_radius_m"]
    results = []
    for p in positions:
        if p.latitude == 0 and p.longitude == 0:
            continue
        username = device_to_username.get(p.device_id, p.device_name)
        # Online + connected predicate (design §Dispatch filter).
        if username.lower() not in connected:
            continue
        if status_by_username.get(username.lower()) != "online":
            continue
        distance = TraccarClient.haversine_distance(lat, lng, p.latitude, p.longitude)
        if radius is not None and distance > radius:
            continue
        results.append({
            "username": username,
            "distance_m": round(distance),
            "latitude": p.latitude,
            "longitude": p.longitude,
            "timestamp": p.timestamp,
        })
    results.sort(key=lambda x: x["distance_m"])
    return results[: settings["max_workers"]]


@router.post("")
async def dispatch_worker(
    req: DispatchRequest,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
    murmur: MurmurClient | None = Depends(get_murmur_client),
):
    """Dispatch a worker: log the event and send them a message."""
    event = DispatchEvent(
        target_username=req.target_username,
        message=req.message,
        latitude=req.latitude,
        longitude=req.longitude,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    # Try TTS whisper first (only the target user hears it); fall back to a
    # text message in their channel if TTS generation or whisper fails.
    delivery = "none"
    if murmur and murmur.has_mumble:
        session_id = murmur.find_session_by_username(req.target_username)
        target_channel = 0
        mm = murmur._mumble
        if mm is not None:
            for sid, user in mm.users.items():
                if user["name"].lower() == req.target_username.lower():
                    target_channel = user.get("channel_id", 0)
                    break

        if session_id is not None:
            try:
                from server.weather_bot import text_to_audio_pcm
                # The first spoken word gets clipped while the receiver's
                # Opus decoder wakes up; lead with throwaway words so the
                # message itself arrives after the path is fully ramped.
                tts_text = (
                    f"Attention. Dispatch for {req.target_username}. "
                    f"{req.message}"
                )
                pcm = text_to_audio_pcm(tts_text)
                if pcm and murmur.whisper_audio(session_id, pcm):
                    delivery = "tts_whisper"
                    logger.info("Dispatch %d delivered via TTS whisper to %s (session %d)",
                                event.id, req.target_username, session_id)
            except Exception as e:
                logger.warning("Dispatch TTS failed, falling back to text: %s", e)

        if delivery == "none":
            murmur.send_message(target_channel, f"DISPATCH: {req.target_username}, {req.message}")
            delivery = "text"
            logger.info("Dispatch %d delivered via text to channel %d",
                        event.id, target_channel)

    return {
        "status": "dispatched",
        "id": event.id,
        "target": req.target_username,
        "message": req.message,
        "delivery": delivery,
    }

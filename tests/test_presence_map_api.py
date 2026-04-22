import pytest
from httpx import AsyncClient
from sqlalchemy import select
from server.models import User


async def _seed(db_session, *triples):
    """triples: (username, status_label, is_audible)."""
    for username, label, audible in triples:
        db_session.add(User(
            username=username, mumble_password="x",
            status_label=label, is_audible=audible,
        ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_presence_map_returns_keyed_dict(client: AsyncClient, db_session):
    await _seed(db_session,
        ("harro", "online", True),
        ("yuliia", "busy", False),
        ("alex", "offline", None),
    )
    r = await client.get("/api/users/presence-map")
    assert r.status_code == 200
    body = r.json()
    assert body["harro"] == {"status_label": "online", "is_audible": True}
    assert body["yuliia"] == {"status_label": "busy", "is_audible": False}
    assert body["alex"] == {"status_label": "offline", "is_audible": None}


@pytest.mark.asyncio
async def test_presence_map_excludes_bots(client: AsyncClient, db_session):
    await _seed(db_session,
        ("PTTAdmin", "online", True),
        ("PTTWeather", "online", True),
        ("PTTPhone-1", "online", True),
        ("PTTPhone-2", "online", True),
        ("real_user", "online", True),
    )
    r = await client.get("/api/users/presence-map")
    assert r.status_code == 200
    body = r.json()
    assert "real_user" in body
    for bot in ("PTTAdmin", "PTTWeather", "PTTPhone-1", "PTTPhone-2"):
        assert bot not in body, f"bot {bot} leaked into presence map"


@pytest.mark.asyncio
async def test_presence_map_no_auth_required(client: AsyncClient, db_session):
    """Anonymous request returns 200 — matches /api/users/status convention."""
    await _seed(db_session, ("solo", "online", True))
    r = await client.get("/api/users/presence-map")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_presence_map_handles_null_status(client: AsyncClient, db_session):
    """A user that never set status surfaces as status_label=null."""
    await _seed(db_session, ("fresh", None, None))
    r = await client.get("/api/users/presence-map")
    body = r.json()
    assert body["fresh"] == {"status_label": None, "is_audible": None}


@pytest.mark.asyncio
async def test_presence_map_lowercases_keys(client: AsyncClient, db_session):
    """Keys are lowercased so the app's case-insensitive lookup works."""
    await _seed(db_session, ("MixedCase", "busy", True))
    r = await client.get("/api/users/presence-map")
    body = r.json()
    assert "mixedcase" in body
    assert "MixedCase" not in body

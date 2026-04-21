import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from httpx import AsyncClient

from server.models import User


class _FakePosition:
    def __init__(self, device_id, name, lat, lng):
        self.device_id = device_id
        self.device_name = name
        self.latitude = lat
        self.longitude = lng
        self.timestamp = "2026-04-21T12:00:00Z"
        self.accuracy = 5
        self.battery = 90


def _fake_positions():
    # Centre = (38.72, -9.14). Build 12 positions at increasing distance.
    base_lat, base_lng = 38.72, -9.14
    return [
        _FakePosition(i, f"w{i}", base_lat + 0.001 * i, base_lng)
        for i in range(1, 13)
    ]


def _all_connected_usernames():
    """Lowercased set matching every fake-position device_name."""
    return {f"w{i}" for i in range(1, 13)}


async def _seed_all_online(db_session):
    """Seed User rows with status_label='online' for every fake-position name."""
    for i in range(1, 13):
        db_session.add(User(username=f"w{i}", mumble_password="x", status_label="online"))
    await db_session.commit()


def _patch_traccar():
    """Return a context manager that replaces server.api.dispatch.TraccarClient
    with a MagicMock whose instance.get_positions is awaitable + whose
    static haversine_distance is the real one."""
    from server.traccar_client import TraccarClient as RealTC

    mock_class = MagicMock()
    mock_instance = MagicMock()
    mock_instance.get_positions = AsyncMock(return_value=_fake_positions())
    mock_class.return_value = mock_instance
    mock_class.haversine_distance = staticmethod(RealTC.haversine_distance)
    return patch("server.api.dispatch.TraccarClient", mock_class)


def _patch_connected():
    return patch(
        "server.api.dispatch._connected_usernames",
        return_value=_all_connected_usernames(),
    )


@pytest.mark.asyncio
async def test_nearest_respects_max_workers(admin_client: AsyncClient, db_session):
    # Default seed = 10. Verify the cap.
    from server.api.dispatch_settings import invalidate_cache
    invalidate_cache()
    await _seed_all_online(db_session)
    with _patch_traccar(), _patch_connected():
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    assert r.status_code == 200
    assert len(r.json()) == 10


@pytest.mark.asyncio
async def test_nearest_respects_lower_max_workers(admin_client: AsyncClient, db_session):
    from server.api.dispatch_settings import invalidate_cache
    await admin_client.put("/api/dispatch/settings", json={"max_workers": 3})
    invalidate_cache()
    await _seed_all_online(db_session)
    with _patch_traccar(), _patch_connected():
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_nearest_respects_radius(admin_client: AsyncClient, db_session):
    # 500 m radius. At lat 38.72, 0.001 deg lat ~111 m, so positions
    # 1..4 are within 500 m, 5..12 are not.
    from server.api.dispatch_settings import invalidate_cache
    await admin_client.put("/api/dispatch/settings", json={"search_radius_m": 500})
    invalidate_cache()
    await _seed_all_online(db_session)
    with _patch_traccar(), _patch_connected():
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    body = r.json()
    assert all(w["distance_m"] <= 500 for w in body)
    assert len(body) <= 4

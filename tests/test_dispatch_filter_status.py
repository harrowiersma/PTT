import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient
from server.models import User


class _FakePos:
    def __init__(self, device_id, name, lat, lng):
        self.device_id = device_id
        self.device_name = name
        self.latitude = lat
        self.longitude = lng
        self.timestamp = "2026-04-21T12:00:00Z"
        self.accuracy = 5
        self.battery = 90


def _positions():
    base = (38.72, -9.14)
    return [
        _FakePos(1, "alice", base[0] + 0.001, base[1]),
        _FakePos(2, "bob", base[0] + 0.002, base[1]),
        _FakePos(3, "carol", base[0] + 0.003, base[1]),
    ]


async def _seed(db_session, label_by_name):
    for name, label in label_by_name.items():
        u = User(username=name, mumble_password="x", status_label=label)
        db_session.add(u)
    await db_session.commit()


@pytest.mark.asyncio
async def test_nearest_excludes_busy_and_offline(admin_client: AsyncClient, db_session):
    await _seed(db_session, {"alice": "online", "bob": "busy", "carol": "offline"})
    with patch("server.api.dispatch.TraccarClient") as MockTC, \
         patch("server.api.dispatch._connected_usernames",
               return_value={"alice", "bob", "carol"}):
        MockTC.return_value.get_positions = AsyncMock(return_value=_positions())
        from server.traccar_client import TraccarClient as RealTC
        MockTC.haversine_distance = staticmethod(RealTC.haversine_distance)
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    assert r.status_code == 200
    names = [w["username"] for w in r.json()]
    assert names == ["alice"]


@pytest.mark.asyncio
async def test_nearest_excludes_online_but_disconnected(admin_client: AsyncClient, db_session):
    await _seed(db_session, {"alice": "online", "bob": "online"})
    with patch("server.api.dispatch.TraccarClient") as MockTC, \
         patch("server.api.dispatch._connected_usernames", return_value={"bob"}):
        MockTC.return_value.get_positions = AsyncMock(return_value=_positions()[:2])
        from server.traccar_client import TraccarClient as RealTC
        MockTC.haversine_distance = staticmethod(RealTC.haversine_distance)
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    names = [w["username"] for w in r.json()]
    assert names == ["bob"]


@pytest.mark.asyncio
async def test_nearest_excludes_null_status(admin_client: AsyncClient, db_session):
    await _seed(db_session, {"alice": None, "bob": "online"})
    with patch("server.api.dispatch.TraccarClient") as MockTC, \
         patch("server.api.dispatch._connected_usernames",
               return_value={"alice", "bob"}):
        MockTC.return_value.get_positions = AsyncMock(return_value=_positions()[:2])
        from server.traccar_client import TraccarClient as RealTC
        MockTC.haversine_distance = staticmethod(RealTC.haversine_distance)
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    names = [w["username"] for w in r.json()]
    assert names == ["bob"]

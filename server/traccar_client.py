"""Traccar REST API client for GPS device management and position queries."""

import logging
import time
from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt

import httpx

from server.config import settings

logger = logging.getLogger(__name__)

# Traccar's default session timeout is 30 minutes; refresh 5 minutes early.
_SESSION_TTL_SECONDS = 25 * 60


@dataclass
class DevicePosition:
    device_id: int
    device_name: str
    latitude: float
    longitude: float
    speed: float  # knots
    course: float  # degrees
    accuracy: float  # meters
    battery_level: float  # 0-100
    timestamp: str  # ISO 8601


class TraccarClient:
    """Client for Traccar REST API.

    The session cookie is cached at the class level so short-lived instances
    created per-request (e.g. `TraccarClient()` inside a route handler) don't
    each re-authenticate. Previous behavior was a POST /api/session on every
    API call, observed as a flood of session requests in the Traccar logs.
    """

    # Shared across instances — cookie + absolute expiry (monotonic time).
    _session_cookie: str | None = None
    _session_expires: float = 0.0

    def __init__(self):
        self.base_url = settings.traccar_api_url
        self.auth = (settings.traccar_admin_email, settings.traccar_admin_password)

    @classmethod
    def _invalidate_session(cls):
        cls._session_cookie = None
        cls._session_expires = 0.0

    async def _get_session(self) -> dict[str, str]:
        """Authenticate and get session cookie, reusing a cached one until TTL."""
        now = time.monotonic()
        if TraccarClient._session_cookie and now < TraccarClient._session_expires:
            return {"Cookie": TraccarClient._session_cookie}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/session",
                data={"email": self.auth[0], "password": self.auth[1]},
            )
            if resp.status_code == 200:
                TraccarClient._session_cookie = resp.headers.get("set-cookie", "")
                TraccarClient._session_expires = now + _SESSION_TTL_SECONDS
                return {"Cookie": TraccarClient._session_cookie}
            logger.warning("Traccar auth failed: %s", resp.status_code)
            return {}

    async def get_positions(self) -> list[DevicePosition]:
        """Get latest position for all devices."""
        try:
            headers = await self._get_session()
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.base_url}/api/positions", headers=headers
                )
                if resp.status_code == 401:
                    # Session expired, clear and retry once
                    TraccarClient._invalidate_session()
                    headers = await self._get_session()
                    resp = await client.get(
                        f"{self.base_url}/api/positions", headers=headers
                    )
                if resp.status_code != 200:
                    logger.warning("Traccar positions failed: %s", resp.status_code)
                    return []

                positions = resp.json()
                devices = await self._get_devices(headers)
                device_names = {d["id"]: d["name"] for d in devices}

                return [
                    DevicePosition(
                        device_id=p["deviceId"],
                        device_name=device_names.get(p["deviceId"], f"Device {p['deviceId']}"),
                        latitude=p.get("latitude", 0),
                        longitude=p.get("longitude", 0),
                        speed=p.get("speed", 0),
                        course=p.get("course", 0),
                        accuracy=p.get("accuracy", 0),
                        battery_level=p.get("attributes", {}).get("batteryLevel", -1),
                        timestamp=p.get("fixTime", ""),
                    )
                    for p in positions
                ]
        except Exception as e:
            logger.error("Error fetching Traccar positions: %s", e)
            return []

    async def _get_devices(self, headers: dict) -> list[dict]:
        """Get all registered devices (internal, with pre-fetched headers)."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/api/devices?all=true", headers=headers)
            return resp.json() if resp.status_code == 200 else []

    async def get_devices(self) -> list[dict]:
        """Get all registered devices (public API)."""
        try:
            headers = await self._get_session()
            return await self._get_devices(headers)
        except Exception as e:
            logger.error("Error fetching Traccar devices: %s", e)
            return []

    async def create_device(self, name: str, unique_id: str) -> int | None:
        """Create a device in Traccar. Returns device ID."""
        try:
            headers = await self._get_session()
            headers["Content-Type"] = "application/json"
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/api/devices",
                    headers=headers,
                    json={"name": name, "uniqueId": unique_id},
                )
                if resp.status_code == 200:
                    device = resp.json()
                    logger.info("Created Traccar device '%s' with ID %d", name, device["id"])
                    return device["id"]
                else:
                    logger.warning("Traccar create device failed: %s %s", resp.status_code, resp.text)
                    return None
        except Exception as e:
            logger.error("Error creating Traccar device: %s", e)
            return None

    async def delete_device(self, device_id: int) -> bool:
        """Delete a device from Traccar."""
        try:
            headers = await self._get_session()
            async with httpx.AsyncClient() as client:
                resp = await client.delete(
                    f"{self.base_url}/api/devices/{device_id}", headers=headers
                )
                return resp.status_code == 204
        except Exception as e:
            logger.error("Error deleting Traccar device: %s", e)
            return False

    async def update_device_unique_id(self, device_id: int, new_unique_id: str) -> bool:
        """Update a device's uniqueId (the value clients send in OsmAnd GET).

        Traccar's PUT /api/devices/{id} requires the full device object, so we
        GET the existing device first, patch the uniqueId, and PUT it back.
        This preserves GPS history linked to the device row.
        """
        try:
            headers = await self._get_session()
            headers["Content-Type"] = "application/json"
            async with httpx.AsyncClient() as client:
                # Fetch current device
                get_resp = await client.get(
                    f"{self.base_url}/api/devices/{device_id}", headers=headers
                )
                if get_resp.status_code != 200:
                    logger.warning(
                        "Traccar get device %s failed: %s",
                        device_id, get_resp.status_code,
                    )
                    return False
                device = get_resp.json()
                device["uniqueId"] = new_unique_id

                # PUT the updated object back
                put_resp = await client.put(
                    f"{self.base_url}/api/devices/{device_id}",
                    headers=headers,
                    json=device,
                )
                if put_resp.status_code == 200:
                    logger.info(
                        "Updated Traccar device id=%d uniqueId='%s'",
                        device_id, new_unique_id,
                    )
                    return True
                logger.warning(
                    "Traccar update device failed: %s %s",
                    put_resp.status_code, put_resp.text,
                )
                return False
        except Exception as e:
            logger.error("Error updating Traccar device: %s", e)
            return False

    @staticmethod
    def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two GPS points in meters."""
        R = 6371000  # Earth radius in meters
        phi1, phi2 = radians(lat1), radians(lat2)
        dphi = radians(lat2 - lat1)
        dlambda = radians(lon2 - lon1)
        a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
        return R * 2 * atan2(sqrt(a), sqrt(1 - a))

    async def find_nearest(self, lat: float, lng: float) -> list[dict]:
        """Find nearest devices to a given location. Returns sorted list."""
        positions = await self.get_positions()
        results = []
        for p in positions:
            if p.latitude == 0 and p.longitude == 0:
                continue
            distance = self.haversine_distance(lat, lng, p.latitude, p.longitude)
            results.append({
                "username": p.device_name,
                "distance_m": round(distance),
                "latitude": p.latitude,
                "longitude": p.longitude,
                "timestamp": p.timestamp,
            })
        results.sort(key=lambda x: x["distance_m"])
        return results

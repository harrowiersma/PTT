"""Weather API endpoint. Fetches weather for a location and optionally plays TTS audio."""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server.auth import get_current_admin
from server.features_gate import requires_feature
from server.weather_bot import fetch_weather, format_weather_report, geocode_location

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/weather",
    tags=["weather"],
    dependencies=[requires_feature("weather")],
)


class WeatherRequest(BaseModel):
    location: str


@router.post("")
async def get_weather(
    req: WeatherRequest,
    _admin: dict = Depends(get_current_admin),
):
    """Fetch weather for a location name. Returns formatted text report."""
    result = await geocode_location(req.location)
    if not result:
        return {"error": f"Could not find location: {req.location}"}

    lat, lon, display_name = result
    weather = await fetch_weather(lat, lon)
    if not weather:
        return {"error": "Weather service temporarily unavailable"}

    report = format_weather_report("admin", weather, location_name=display_name)
    current = weather.get("current", {})

    return {
        "location": display_name,
        "latitude": lat,
        "longitude": lon,
        "report_text": report,
        "temperature": current.get("temperature_2m"),
        "wind_speed": current.get("wind_speed_10m"),
        "wind_direction": current.get("wind_direction_10m"),
        "cloud_cover": current.get("cloud_cover"),
        "precipitation": current.get("precipitation"),
        "weather_code": current.get("weather_code"),
    }

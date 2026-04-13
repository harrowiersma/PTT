"""Weather ATIS channel bot.

Two trigger modes:
1. Double-PTT in "Weather" channel: looks up GPS from Traccar, fetches weather, speaks it.
2. Text message "status {location}": geocodes the location via Open-Meteo, fetches weather,
   speaks it. Works in any channel. Example: "status Paris, France"

Like airport ATIS, but personalized to your location.
"""

import asyncio
import io
import logging
import struct
import time
from datetime import datetime, timezone

import httpx
import numpy as np

logger = logging.getLogger(__name__)

# WMO Weather Codes -> plain English
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}

# Wind direction degrees -> compass
WIND_DIRECTIONS = [
    (0, "north"), (45, "northeast"), (90, "east"), (135, "southeast"),
    (180, "south"), (225, "southwest"), (270, "west"), (315, "northwest"), (360, "north"),
]


def degrees_to_compass(degrees: float) -> str:
    """Convert wind direction in degrees to compass direction."""
    closest = min(WIND_DIRECTIONS, key=lambda x: abs(x[0] - (degrees % 360)))
    return closest[1]


async def fetch_weather(lat: float, lon: float) -> dict | None:
    """Fetch current weather from Open-Meteo API."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,precipitation,wind_speed_10m,"
            f"wind_direction_10m,cloud_cover,weather_code"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.error("Open-Meteo returned %d", resp.status_code)
                return None
            return resp.json()
    except httpx.TimeoutException:
        logger.error("Open-Meteo API timed out")
        return None
    except Exception as e:
        logger.error("Open-Meteo fetch failed: %s", e)
        return None


async def geocode_location(query: str) -> tuple[float, float, str] | None:
    """Geocode a location name to (lat, lon, display_name) via Open-Meteo Geocoding API."""
    try:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={query}&count=1&language=en"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()
            results = data.get("results", [])
            if not results:
                return None
            r = results[0]
            name = r.get("name", query)
            country = r.get("country", "")
            display = f"{name}, {country}" if country else name
            return (r["latitude"], r["longitude"], display)
    except Exception as e:
        logger.error("Geocoding failed for '%s': %s", query, e)
        return None


def format_weather_report(username: str, weather_data: dict, location_name: str | None = None) -> str:
    """Format weather data into ATIS-style spoken text."""
    current = weather_data.get("current", {})
    temp = current.get("temperature_2m", "unknown")
    wind_speed = current.get("wind_speed_10m", 0)
    wind_dir = current.get("wind_direction_10m", 0)
    cloud = current.get("cloud_cover", 0)
    precip = current.get("precipitation", 0)
    code = current.get("weather_code", 0)

    now = datetime.now(timezone.utc)
    time_str = now.strftime("%H %M UTC")

    compass = degrees_to_compass(wind_dir)
    condition = WMO_CODES.get(code, "Unknown conditions")

    if location_name:
        header = f"Weather report for {location_name}."
    else:
        header = f"Weather report for {username}."

    lines = [
        header,
        f"Time {time_str}.",
        f"Temperature {int(round(temp))} degrees celsius.",
        f"Wind from {compass} at {int(round(wind_speed))} kilometers per hour.",
        f"Cloud cover {int(round(cloud))} percent.",
    ]

    if precip > 0:
        lines.append(f"Precipitation {precip} millimeters per hour.")

    lines.append(f"Conditions: {condition}.")
    lines.append("Report ends.")

    return " ".join(lines)


def text_to_audio_pcm(text: str) -> bytes | None:
    """Convert text to 48kHz 16-bit mono PCM audio using TinyTTS."""
    try:
        from tinytts import TinyTTS

        tts = TinyTTS()
        # TinyTTS returns audio at 44100 Hz
        audio_np = tts.synthesize(text, speed=0.9)

        if audio_np is None or len(audio_np) == 0:
            logger.error("TinyTTS returned empty audio")
            return None

        # Resample from 44100 Hz to 48000 Hz (Mumble's sample rate)
        source_rate = 44100
        target_rate = 48000
        duration = len(audio_np) / source_rate
        target_length = int(duration * target_rate)
        indices = np.linspace(0, len(audio_np) - 1, target_length).astype(int)
        resampled = audio_np[indices]

        # Normalize to 16-bit PCM
        if resampled.dtype == np.float32 or resampled.dtype == np.float64:
            resampled = np.clip(resampled, -1.0, 1.0)
            pcm = (resampled * 32767).astype(np.int16)
        else:
            pcm = resampled.astype(np.int16)

        return pcm.tobytes()

    except ImportError:
        logger.error("TinyTTS not installed. Run: pip install tiny-tts")
        return None
    except Exception as e:
        logger.error("TTS generation failed: %s", e)
        return None


class WeatherBot:
    """Weather bot with two trigger modes:
    1. Double-PTT in Weather channel -> GPS-based weather report
    2. Text message "status {location}" in any channel -> location-based weather report
    """

    def __init__(self, murmur_client, traccar_client_class):
        self.murmur = murmur_client
        self.TraccarClient = traccar_client_class
        self._user_state: dict[int, dict] = {}  # session_id -> {burst_count, first_burst_time}
        self._rate_limit: dict[str, float] = {}  # username -> last_request_time
        self._weather_channel_id: int | None = None
        self._running = False

    def start(self):
        """Start the weather bot. Creates Weather channel and registers callbacks."""
        if not self.murmur or not self.murmur.has_mumble:
            logger.warning("Weather bot: pymumble not available, skipping")
            return

        mm = self.murmur._mumble
        if not mm:
            return

        # Create Weather channel if it doesn't exist
        for chan_id, chan in mm.channels.items():
            if chan["name"] == "Weather":
                self._weather_channel_id = chan_id
                break

        if self._weather_channel_id is None:
            mm.channels.new_channel(0, "Weather", temporary=False)
            time.sleep(0.5)
            for chan_id, chan in mm.channels.items():
                if chan["name"] == "Weather":
                    self._weather_channel_id = chan_id
                    break

        if self._weather_channel_id is None:
            logger.error("Weather bot: could not create Weather channel")
            return

        # Also ensure it exists in the database (for dashboard visibility)
        self._ensure_db_channel("Weather", "ATIS-style weather reports. Double-PTT or type 'status <location>'.", self._weather_channel_id)
        # Also ensure Emergency channel exists for SOS
        for chan_id, chan in mm.channels.items():
            if chan["name"] == "Emergency":
                self._ensure_db_channel("Emergency", "Emergency SOS channel. All users moved here during SOS.", chan_id)
                break

        # Register audio callback to detect PTT bursts
        import pymumble_py3.constants as const
        mm.callbacks.set_callback(const.PYMUMBLE_CLBK_SOUNDRECEIVED, self._on_sound)

        self._running = True
        logger.info("Weather bot started. Channel ID: %d. Double-PTT to request weather.", self._weather_channel_id)

    def _ensure_db_channel(self, name: str, description: str, mumble_id: int):
        """Ensure a channel exists in PostgreSQL for dashboard visibility."""
        try:
            import asyncio
            from sqlalchemy import select
            from server.database import async_session
            from server.models import Channel

            async def _create():
                async with async_session() as db:
                    result = await db.execute(select(Channel).where(Channel.name == name))
                    if result.scalar_one_or_none():
                        return  # Already exists
                    ch = Channel(name=name, description=description, mumble_id=mumble_id, max_users=0)
                    db.add(ch)
                    await db.commit()
                    logger.info("Created '%s' channel in database (mumble_id=%d)", name, mumble_id)

            loop = asyncio.new_event_loop()
            loop.run_until_complete(_create())
            loop.close()
        except Exception as e:
            logger.debug("Could not ensure DB channel '%s': %s", name, e)

    def _on_sound(self, user, sound_chunk):
        """Called when audio is received. Track PTT bursts per user."""
        if not self._running:
            return

        try:
            session_id = None
            username = None
            user_channel = None

            # Find the user who sent this audio
            mm = self.murmur._mumble
            for sid, u in mm.users.items():
                if u["name"] == user["name"]:
                    session_id = sid
                    username = u["name"]
                    user_channel = u.get("channel_id", -1)
                    break

            if session_id is None or username == "PTTAdmin":
                return

            # Only react to audio in the Weather channel
            if user_channel != self._weather_channel_id:
                return

            now = time.time()
            state = self._user_state.get(session_id)

            if state is None or (now - state["first_burst_time"]) > 10:
                # Start new tracking window
                self._user_state[session_id] = {
                    "burst_count": 1,
                    "first_burst_time": now,
                    "last_audio_time": now,
                    "username": username,
                }
            else:
                # Check if this is a new burst (gap of >1 second since last audio)
                if now - state["last_audio_time"] > 1.0:
                    state["burst_count"] += 1
                state["last_audio_time"] = now

                # Two bursts detected
                if state["burst_count"] >= 2:
                    self._user_state.pop(session_id, None)
                    self._handle_weather_request(username)

        except Exception as e:
            logger.error("Weather bot audio callback error: %s", e)

    def _handle_weather_request(self, username: str):
        """Process a weather request for the given user."""
        # Rate limit: 1 request per 60 seconds per user
        now = time.time()
        last = self._rate_limit.get(username, 0)
        if now - last < 60:
            logger.info("Weather request from %s rate-limited", username)
            return
        self._rate_limit[username] = now

        logger.info("Weather request triggered by %s", username)

        # Run the async weather fetch in a new thread to avoid blocking pymumble
        import threading
        thread = threading.Thread(target=self._fetch_and_speak, args=(username,), daemon=True)
        thread.start()

    def _fetch_and_speak(self, username: str):
        """Fetch weather and play audio. Runs in a separate thread."""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Get GPS from Traccar
            traccar = self.TraccarClient()
            positions = loop.run_until_complete(traccar.get_positions())

            user_pos = None
            for p in positions:
                if p.device_name.lower() == username.lower():
                    user_pos = p
                    break

            if not user_pos or (user_pos.latitude == 0 and user_pos.longitude == 0):
                logger.warning("No GPS for user %s, cannot fetch weather", username)
                self._speak("No GPS position available for " + username + ". Please ensure Traccar is running on your device.")
                return

            # Fetch weather
            weather = loop.run_until_complete(
                fetch_weather(user_pos.latitude, user_pos.longitude)
            )
            loop.close()

            if not weather:
                self._speak("Weather service temporarily unavailable. Please try again.")
                return

            # Format report
            report_text = format_weather_report(username, weather)
            logger.info("Weather report: %s", report_text)

            # Generate audio
            pcm = text_to_audio_pcm(report_text)
            if not pcm:
                self._speak("Audio generation failed. Weather data is available but cannot be spoken.")
                return

            # Play audio into Weather channel
            self._play_audio(pcm)

        except Exception as e:
            logger.error("Weather fetch-and-speak error: %s", e)

    def _speak(self, text: str):
        """Send a text message to the Weather channel (fallback when audio fails)."""
        if self.murmur and self.murmur.has_mumble and self._weather_channel_id is not None:
            self.murmur.send_message(self._weather_channel_id, text)

    def _play_audio(self, pcm_data: bytes):
        """Play PCM audio into the Weather channel via pymumble."""
        if not self.murmur or not self.murmur.has_mumble:
            return

        mm = self.murmur._mumble

        # Move bot to Weather channel if not already there
        if self._weather_channel_id is not None:
            mm.users.myself.move_in(self._weather_channel_id)
            time.sleep(0.1)

        # Feed PCM audio to pymumble's sound output
        # pymumble expects 48000 Hz, 16-bit, mono PCM in chunks
        CHUNK_SIZE = 48000 * 2 * 20 // 1000  # 20ms of 48kHz 16-bit mono = 1920 bytes

        for i in range(0, len(pcm_data), CHUNK_SIZE):
            chunk = pcm_data[i:i + CHUNK_SIZE]
            if len(chunk) < CHUNK_SIZE:
                # Pad the last chunk with silence
                chunk += b'\x00' * (CHUNK_SIZE - len(chunk))
            mm.sound_output.add_sound(chunk)
            time.sleep(0.018)  # Slightly less than 20ms to keep the buffer fed

        logger.info("Weather audio playback complete")

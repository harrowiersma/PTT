"""Weather ATIS channel bot.

Two trigger modes:
1. Double-PTT in "Weather" channel: looks up GPS from Traccar, fetches weather, speaks it.
2. Text message "status {location}": geocodes the location via Open-Meteo, fetches weather,
   speaks it. Works in any channel. Example: "status Paris, France"

Like airport ATIS, but personalized to your location.
"""

import asyncio
import logging
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


_tts_instance = None


def _get_tts():
    """Get or create the TinyTTS singleton."""
    global _tts_instance
    if _tts_instance is None:
        from tiny_tts import TinyTTS
        _tts_instance = TinyTTS()
        logger.info("TinyTTS model loaded")
    return _tts_instance


def text_to_audio_pcm(text: str) -> bytes | None:
    """Convert text to 48kHz 16-bit mono PCM audio using TinyTTS.

    tiny_tts 0.3.x exposes `speak(text, output_path=...)` instead of
    returning a numpy array, so we synthesize to a temporary WAV and
    read it back with soundfile (already a transitive dependency).
    """
    try:
        import os
        import tempfile
        import soundfile as sf

        tts = _get_tts()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
        try:
            tts.speak(text, output_path=wav_path, speed=0.9)
            audio_np, source_rate = sf.read(wav_path)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

        if audio_np is None or len(audio_np) == 0:
            logger.error("TinyTTS returned empty audio")
            return None

        # soundfile returns float64 mono for this model. If it ever
        # returns stereo (shape (N, 2)), collapse to mono by averaging.
        if audio_np.ndim > 1:
            audio_np = audio_np.mean(axis=1)

        # Resample to 48000 Hz (Mumble's sample rate) via nearest-sample
        # index lookup — same approach as before, just source-rate aware.
        target_rate = 48000
        if source_rate != target_rate:
            target_length = int(len(audio_np) * target_rate / source_rate)
            indices = np.linspace(0, len(audio_np) - 1, target_length).astype(int)
            audio_np = audio_np[indices]

        # Normalize to 16-bit PCM
        if audio_np.dtype in (np.float32, np.float64):
            audio_np = np.clip(audio_np, -1.0, 1.0)
            pcm = (audio_np * 32767).astype(np.int16)
        else:
            pcm = audio_np.astype(np.int16)

        return pcm.tobytes()

    except ImportError as e:
        logger.error("tiny_tts import failed: %s", e)
        return None
    except Exception as e:
        logger.exception("TTS generation failed: %s", e)
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
        # Dedicated pymumble connection as "PTTWeather" — sits in Weather channel
        # so PYMUMBLE_CLBK_SOUNDRECEIVED fires for Weather audio. The main
        # MurmurClient stays in Root for SOS acknowledgements in Emergency.
        self._weather_mumble = None

    async def start(self):
        """Start the weather bot. Creates Weather channel and registers callbacks.

        Async so DB work runs on the caller's event loop — the asyncpg pool is
        bound to the main loop and cannot be driven from a freshly created
        loop (previous bug: RuntimeWarning 'coroutine was never awaited').
        """
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
        await self._ensure_db_channel("Weather", "ATIS-style weather reports. Double-PTT or type 'status <location>'.", self._weather_channel_id)
        # Also ensure Emergency channel exists for SOS
        for chan_id, chan in mm.channels.items():
            if chan["name"] == "Emergency":
                await self._ensure_db_channel("Emergency", "Emergency SOS channel. All users moved here during SOS.", chan_id)
                break

        # Open the dedicated PTTWeather connection and move it to Weather.
        # SOUNDRECEIVED only fires for audio in the bot's own channel; sitting
        # this connection in Weather is what lets double-PTT detection work
        # without yanking the main PTTAdmin bot out of Root (which handles SOS).
        self._start_weather_connection()

        self._running = True
        logger.info("Weather bot started. Channel ID: %d. Double-PTT to request weather.", self._weather_channel_id)

    def _start_weather_connection(self):
        """Open the PTTWeather pymumble connection and move it to Weather channel."""
        try:
            import pymumble_py3 as pymumble
            import pymumble_py3.constants as const

            self._weather_mumble = pymumble.Mumble(
                self.murmur.mumble_host,
                "PTTWeather",
                port=self.murmur.mumble_port,
                reconnect=True,
            )
            self._weather_mumble.set_application_string("openPTT TRX-WeatherBot")
            # pymumble drops incoming audio unless receive_sound is enabled;
            # without this, PYMUMBLE_CLBK_SOUNDRECEIVED never fires, even
            # though the callback is registered.
            self._weather_mumble.set_receive_sound(True)
            self._weather_mumble.start()
            self._weather_mumble.is_ready()
            time.sleep(1)

            # Move into the Weather channel
            if self._weather_channel_id is not None:
                self._weather_mumble.users.myself.move_in(self._weather_channel_id)
                time.sleep(0.2)

            # Register sound callback on the weather connection — only audio
            # from the Weather channel reaches this callback.
            self._weather_mumble.callbacks.set_callback(
                const.PYMUMBLE_CLBK_SOUNDRECEIVED, self._on_sound,
            )
            logger.info("PTTWeather connection ready in Weather channel (receive_sound=on)")
        except Exception as e:
            logger.error("Failed to open PTTWeather connection: %s", e)
            self._weather_mumble = None

    def stop(self):
        """Disconnect the PTTWeather connection. Called from lifespan shutdown."""
        self._running = False
        if self._weather_mumble is not None:
            try:
                # pymumble doesn't expose a clean disconnect; stop the control
                # thread and let the socket close.
                self._weather_mumble.control_socket.close()
            except Exception as e:
                logger.debug("Error closing PTTWeather socket: %s", e)
            self._weather_mumble = None
            logger.info("PTTWeather connection stopped")

    async def _ensure_db_channel(self, name: str, description: str, mumble_id: int):
        """Ensure a channel exists in PostgreSQL for dashboard visibility."""
        try:
            from sqlalchemy import select
            from server.database import async_session
            from server.models import Channel

            async with async_session() as db:
                result = await db.execute(select(Channel).where(Channel.name == name))
                if result.scalar_one_or_none():
                    return  # Already exists
                ch = Channel(name=name, description=description, mumble_id=mumble_id, max_users=0)
                db.add(ch)
                await db.commit()
                logger.info("Created '%s' channel in database (mumble_id=%d)", name, mumble_id)
        except Exception as e:
            logger.debug("Could not ensure DB channel '%s': %s", name, e)

    def _on_sound(self, user, sound_chunk):
        """Called when audio is received. Track PTT bursts per user.

        Fires on the PTTWeather connection, so incoming audio is already
        known to be in the Weather channel.
        """
        if not self._running:
            return

        try:
            session_id = None
            username = None

            mm = self._weather_mumble or self.murmur._mumble
            for sid, u in mm.users.items():
                if u["name"] == user["name"]:
                    session_id = sid
                    username = u["name"]
                    break

            if session_id is None or username in ("PTTAdmin", "PTTWeather"):
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
                logger.info("Weather burst 1/2 from %s (session %d)", username, session_id)
            else:
                # Check if this is a new burst (gap of >1 second since last audio)
                if now - state["last_audio_time"] > 1.0:
                    state["burst_count"] += 1
                    logger.info("Weather burst %d/2 from %s", state["burst_count"], username)
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

    async def _load_device_to_user(self) -> dict[int, str]:
        """Load {traccar_device_id: username} for users with an explicit link."""
        try:
            from sqlalchemy import select
            from server.database import async_session
            from server.models import User
            async with async_session() as db:
                result = await db.execute(
                    select(User.traccar_device_id, User.username)
                    .where(User.traccar_device_id.isnot(None))
                )
                return {row[0]: row[1] for row in result.all()}
        except Exception as e:
            logger.debug("Could not load device-user map: %s", e)
            return {}

    def _fetch_and_speak(self, username: str):
        """Fetch weather and play audio. Runs in a separate thread."""
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)

            # Get GPS from Traccar
            traccar = self.TraccarClient()
            positions = loop.run_until_complete(traccar.get_positions())

            # Resolve positions via explicit User.traccar_device_id link first;
            # fall back to device name match for unlinked rows. Matches the
            # pattern used by server/api/status.py and server/api/dispatch.py.
            device_to_user = loop.run_until_complete(self._load_device_to_user())

            user_pos = None
            target = username.lower()
            for p in positions:
                resolved = device_to_user.get(p.device_id, p.device_name)
                if resolved.lower() == target:
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
        finally:
            loop.close()

    def _speak(self, text: str):
        """Send a text message to the Weather channel (fallback when audio fails)."""
        if self._weather_channel_id is None:
            return
        mm = self._weather_mumble
        if mm is not None and self._weather_channel_id in mm.channels:
            try:
                mm.channels[self._weather_channel_id].send_text_message(text)
                return
            except Exception as e:
                logger.warning("PTTWeather send_text failed, falling back to main: %s", e)
        if self.murmur and self.murmur.has_mumble:
            self.murmur.send_message(self._weather_channel_id, text)

    def _play_audio(self, pcm_data: bytes):
        """Play PCM audio into the Weather channel via the PTTWeather connection."""
        mm = self._weather_mumble
        if mm is None:
            logger.warning("PTTWeather connection unavailable; cannot play audio")
            return

        # PTTWeather is already in the Weather channel (moved on start).
        # Feed PCM audio to pymumble's sound output.
        # pymumble expects 48000 Hz, 16-bit, mono PCM in chunks.
        CHUNK_SIZE = 48000 * 2 * 20 // 1000  # 20ms of 48kHz 16-bit mono = 1920 bytes

        for i in range(0, len(pcm_data), CHUNK_SIZE):
            chunk = pcm_data[i:i + CHUNK_SIZE]
            if len(chunk) < CHUNK_SIZE:
                # Pad the last chunk with silence
                chunk += b'\x00' * (CHUNK_SIZE - len(chunk))
            mm.sound_output.add_sound(chunk)
            time.sleep(0.018)  # Slightly less than 20ms to keep the buffer fed

        logger.info("Weather audio playback complete")

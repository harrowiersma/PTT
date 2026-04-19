"""Murmur client using pymumble for server administration.

Connects as a bot user to Murmur to manage channels, query online users,
and send text messages. Replaces the ICE-based client (zeroc-ice doesn't
compile on python:3.11-slim).
"""

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MumbleUser:
    session: int
    name: str
    channel_id: int
    is_muted: bool = False
    is_deaf: bool = False
    online_secs: int = 0
    address: str = ""


@dataclass
class MumbleChannel:
    id: int
    name: str
    parent_id: int = 0
    description: str = ""
    user_count: int = 0


@dataclass
class ServerStatus:
    is_running: bool = False
    uptime: int = 0
    users_online: int = 0
    max_users: int = 0
    users: list[MumbleUser] = field(default_factory=list)
    channels: list[MumbleChannel] = field(default_factory=list)


class MurmurClient:
    """Client for Murmur using pymumble (connects as a bot user)."""

    def __init__(self, host: str, port: int, secret: str = "",
                 mumble_host: str = "murmur", mumble_port: int = 64738):
        self.host = host  # ICE host (unused with pymumble)
        self.port = port  # ICE port (unused with pymumble)
        self.secret = secret
        self.mumble_host = mumble_host
        self.mumble_port = mumble_port
        self._mumble = None
        self._connected = False
        self._thread = None
        self._on_sos_acknowledge = None  # Callback: fn(username) called when admin types OK in Emergency
        self._text_handlers = []  # List of fn(text) callbacks for text messages

    def connect(self) -> bool:
        """Connect to Murmur as a bot user via pymumble."""
        try:
            import pymumble_py3 as pymumble

            self._mumble = pymumble.Mumble(
                self.mumble_host,
                "PTTAdmin",
                port=self.mumble_port,
                reconnect=True,
            )
            self._mumble.set_application_string("openPTT TRX-Server")
            self._mumble.callbacks.set_callback(
                pymumble.constants.PYMUMBLE_CLBK_TEXTMESSAGERECEIVED,
                self._on_text_message,
            )
            self._mumble.start()
            self._mumble.is_ready()
            time.sleep(1)

            self._connected = True
            logger.info(
                "Connected to Murmur via pymumble at %s:%d",
                self.mumble_host, self.mumble_port,
            )
            return True

        except ImportError:
            logger.warning("pymumble not installed. Trying TCP health check.")
            return self._check_tcp()
        except Exception as e:
            logger.error("pymumble connection failed: %s. Trying TCP health check.", e)
            return self._check_tcp()

    def _check_tcp(self) -> bool:
        """Fallback: simple TCP connect to verify Murmur is running."""
        import socket
        try:
            sock = socket.create_connection((self.mumble_host, self.mumble_port), timeout=5)
            sock.close()
            self._connected = True
            logger.info(
                "Murmur is running (TCP check on %s:%d) but pymumble not available.",
                self.mumble_host, self.mumble_port,
            )
            return True
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            logger.warning("Murmur not reachable at %s:%d: %s", self.mumble_host, self.mumble_port, e)
            return False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def has_mumble(self) -> bool:
        return self._mumble is not None

    def get_status(self) -> ServerStatus:
        """Get current server status including online users."""
        if not self._connected:
            return ServerStatus()

        if not self._mumble:
            # TCP-only mode: just report server is running
            import socket
            try:
                sock = socket.create_connection((self.mumble_host, self.mumble_port), timeout=5)
                sock.close()
                return ServerStatus(is_running=True, users_online=0, max_users=50)
            except Exception:
                return ServerStatus()

        try:
            users = []
            for session_id, user in self._mumble.users.items():
                if user["name"] in ("PTTAdmin", "PTTWeather", "PTTPhone"):
                    continue  # Skip bot users
                users.append(
                    MumbleUser(
                        session=session_id,
                        name=user["name"],
                        channel_id=user.get("channel_id", 0),
                        is_muted=user.get("mute", False),
                        is_deaf=user.get("deaf", False),
                        online_secs=0,
                        address="",
                    )
                )

            channels = []
            for chan_id, chan in self._mumble.channels.items():
                channels.append(
                    MumbleChannel(
                        id=chan_id,
                        name=chan["name"],
                        parent_id=chan.get("parent", 0),
                        description=chan.get("description", ""),
                    )
                )

            return ServerStatus(
                is_running=True,
                users_online=len(users),
                max_users=50,
                users=users,
                channels=channels,
            )
        except Exception as e:
            logger.error("Error querying Murmur status: %s", e)
            return ServerStatus(is_running=True)

    def create_channel(self, name: str, parent_id: int = 0) -> int | None:
        """Create a new channel in Murmur.

        First tries pymumble's new_channel (fast, no restart). On this
        server the anonymous pymumble client doesn't have the MakeChannel
        ACL on Root, so the call silently no-ops — we detect that and
        fall back to editing the sqlite file directly via admin_sqlite,
        then bounce the murmur container. The fallback costs ~3 s of
        Mumble downtime but is the only path that actually works.
        """
        if not self._mumble:
            logger.warning("pymumble not available, cannot create channel in Murmur")
            return None

        try:
            self._mumble.channels.new_channel(parent_id, name, temporary=False)
            time.sleep(0.5)
            for chan_id, chan in self._mumble.channels.items():
                if chan["name"] == name:
                    logger.info("Created channel '%s' with ID %d via pymumble", name, chan_id)
                    return chan_id
        except Exception as e:
            logger.warning("pymumble new_channel('%s') raised %s; falling back to sqlite", name, e)

        # pymumble didn't (or can't) create the channel. Fall back to
        # direct sqlite + restart.
        logger.info("falling back to admin_sqlite for channel '%s'", name)
        try:
            from server.murmur.admin_sqlite import ensure_channel_exists, restart_murmur
            chan_id = ensure_channel_exists(name, parent_id=parent_id)
            restart_murmur()
            logger.info("Created channel '%s' with ID %d via sqlite fallback", name, chan_id)
            return chan_id
        except Exception as e:
            logger.error("sqlite fallback for channel '%s' failed: %s", name, e)
            return None

    def remove_channel(self, channel_id: int) -> bool:
        """Remove a channel from Murmur. Tries pymumble first; falls back
        to sqlite + murmur restart if pymumble's remove has no effect
        (same ACL limitation as create)."""
        if not self._mumble:
            return False

        name: str | None = None
        try:
            if channel_id in self._mumble.channels:
                name = self._mumble.channels[channel_id].get("name")
                self._mumble.channels[channel_id].remove()
                time.sleep(0.5)
                if channel_id not in self._mumble.channels:
                    logger.info("Removed channel ID %d via pymumble", channel_id)
                    return True
                logger.warning("pymumble remove() didn't drop channel %d; falling back to sqlite", channel_id)
            else:
                return False
        except Exception as e:
            logger.warning("pymumble remove_channel(%d) raised %s; falling back to sqlite", channel_id, e)

        if not name:
            logger.error("fallback: channel %d name unknown, cannot delete via sqlite", channel_id)
            return False
        try:
            from server.murmur.admin_sqlite import delete_channel, restart_murmur
            deleted = delete_channel(name)
            if deleted:
                restart_murmur()
            return deleted
        except Exception as e:
            logger.error("sqlite fallback remove_channel(%r) failed: %s", name, e)
            return False

    def send_message(self, channel_id: int, message: str) -> bool:
        """Send a text message to a channel."""
        if not self._mumble:
            return False

        try:
            if channel_id in self._mumble.channels:
                self._mumble.channels[channel_id].send_text_message(message)
                logger.info("Sent message to channel %d", channel_id)
                return True
            return False
        except Exception as e:
            logger.error("Failed to send message to channel %d: %s", channel_id, e)
            return False

    def find_session_by_username(self, username: str) -> int | None:
        """Return the Murmur session ID of a currently-connected user, or None."""
        if not self._mumble:
            return None
        target = username.lower()
        for sid, user in self._mumble.users.items():
            if user["name"].lower() == target:
                return sid
        return None

    def whisper_audio(
        self,
        session_id: int,
        pcm_data: bytes,
        with_preamble: bool = True,
    ) -> bool:
        """Play 48kHz 16-bit mono PCM audio as a whisper to one Murmur session.

        The target user hears the audio regardless of their channel; no one
        else does. Returns False if the connection is unavailable.

        with_preamble prepends a short tone + silence before the payload so
        the receiver's Opus decoder ramps up during the tone and doesn't
        clip the first word of real speech. Default on; turn off only for
        short internal cues where the tone would be noise.
        """
        if not self._mumble or not pcm_data:
            return False
        mm = self._mumble
        try:
            import time as _time
            from server.weather_bot import generate_trailing_silence_pcm
            if with_preamble:
                from server.weather_bot import generate_preamble_pcm
                pcm_data = generate_preamble_pcm() + pcm_data
            # Trailing silence keeps the transmission open past the last word.
            # Without it, P50 receivers clip 200-400 ms of speech as the
            # decoder ramps down.
            pcm_data = pcm_data + generate_trailing_silence_pcm(ms=400)
            mm.sound_output.set_whisper(session_id, channel=False)
            chunk_size = 48000 * 2 * 20 // 1000  # 20ms of 48kHz 16-bit mono
            for i in range(0, len(pcm_data), chunk_size):
                chunk = pcm_data[i:i + chunk_size]
                if len(chunk) < chunk_size:
                    chunk += b'\x00' * (chunk_size - len(chunk))
                mm.sound_output.add_sound(chunk)
                _time.sleep(0.018)
            return True
        except Exception as e:
            logger.error("whisper_audio failed for session %d: %s", session_id, e)
            return False
        finally:
            try:
                mm.sound_output.remove_whisper()
            except Exception:
                pass

    def register_user(self, username: str, password: str) -> int | None:
        """Register a user. pymumble can't register users directly.
        Users auto-register when they connect to Murmur."""
        logger.info(
            "User '%s' will auto-register when they connect to Murmur. "
            "pymumble doesn't support server-side user registration.",
            username,
        )
        return None

    def remove_user(self, user_id: int) -> bool:
        """Remove a registered user. Not supported via pymumble."""
        logger.info("User removal requires ICE or direct DB access.")
        return False

    def set_sos_acknowledge_callback(self, callback):
        """Set callback for when an admin acknowledges SOS via text in Emergency channel.
        callback(username: str) is called when recognized."""
        self._on_sos_acknowledge = callback

    def add_text_handler(self, handler):
        """Register an additional text message handler."""
        self._text_handlers.append(handler)

    def _on_text_message(self, text):
        """Handle incoming text messages. Dispatches to SOS handler + any registered handlers."""
        logger.debug("Text message received: actor=%s handlers=%d",
                     getattr(text, 'actor', '?'), len(self._text_handlers))

        # Dispatch to all registered text handlers first
        for handler in self._text_handlers:
            try:
                handler(text)
            except Exception as e:
                logger.error("Text handler error: %s", e)

        # Then handle SOS acknowledgement
        try:
            message = text.message.strip().lower()
            # Strip HTML tags that Mumble might wrap around the message
            import re
            message = re.sub(r'<[^>]+>', '', message).strip().lower()

            actor = text.actor
            if actor not in self._mumble.users:
                return

            username = self._mumble.users[actor]["name"]
            if username in ("PTTAdmin", "PTTWeather", "PTTPhone"):
                return

            # Check if this is an SOS acknowledgement keyword
            ack_keywords = {"ok", "acknowledge", "ack", "all clear", "allclear", "roger"}
            if message not in ack_keywords:
                return

            # Check if the user is in the Emergency channel
            user_channel = self._mumble.users[actor].get("channel_id", -1)
            emergency_id = None
            for chan_id, chan in self._mumble.channels.items():
                if chan["name"] == "Emergency":
                    emergency_id = chan_id
                    break

            if emergency_id is None or user_channel != emergency_id:
                return

            logger.info("SOS acknowledgement received from '%s' in Emergency channel", username)

            if self._on_sos_acknowledge:
                self._on_sos_acknowledge(username)
            else:
                logger.warning("SOS acknowledge callback not set")

        except Exception as e:
            logger.error("Error handling text message: %s", e)

    def disconnect(self):
        """Clean up pymumble connection."""
        if self._mumble:
            try:
                self._mumble.stop()
            except Exception:
                pass
            self._mumble = None
            self._connected = False

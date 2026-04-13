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
            self._mumble.set_application_string("PTT Admin Service")
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
                if user["name"] == "PTTAdmin":
                    continue  # Skip our own bot user
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
        """Create a new channel in Murmur."""
        if not self._mumble:
            logger.warning("pymumble not available, cannot create channel in Murmur")
            return None

        try:
            self._mumble.channels.new_channel(parent_id, name, temporary=False)
            time.sleep(0.5)
            # Find the channel we just created
            for chan_id, chan in self._mumble.channels.items():
                if chan["name"] == name:
                    logger.info("Created channel '%s' with ID %d in Murmur", name, chan_id)
                    return chan_id
            logger.warning("Channel '%s' created but not found in channel list", name)
            return None
        except Exception as e:
            logger.error("Failed to create channel '%s': %s", name, e)
            return None

    def remove_channel(self, channel_id: int) -> bool:
        """Remove a channel from Murmur."""
        if not self._mumble:
            return False

        try:
            if channel_id in self._mumble.channels:
                self._mumble.channels[channel_id].remove()
                logger.info("Removed channel ID %d from Murmur", channel_id)
                return True
            return False
        except Exception as e:
            logger.error("Failed to remove channel %d: %s", channel_id, e)
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

    def _on_text_message(self, text):
        """Handle incoming text messages. Check for SOS acknowledgement keywords."""
        try:
            message = text.message.strip().lower()
            # Strip HTML tags that Mumble might wrap around the message
            import re
            message = re.sub(r'<[^>]+>', '', message).strip().lower()

            actor = text.actor
            if actor not in self._mumble.users:
                return

            username = self._mumble.users[actor]["name"]
            if username == "PTTAdmin":
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

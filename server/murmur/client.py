"""Murmur ICE client for server administration.

Connects to Murmur's ICE interface to manage users, channels, and query
server status. Requires zeroc-ice and the Murmur.ice slice definitions.

If ICE is unavailable (e.g., during development without Murmur running),
falls back to a stub implementation that returns empty/mock data.
"""

import logging
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
    """Client for Murmur's ICE administration interface."""

    def __init__(self, host: str, port: int, secret: str = ""):
        self.host = host
        self.port = port
        self.secret = secret
        self._ice = None
        self._meta = None
        self._server = None
        self._connected = False

    def connect(self) -> bool:
        """Connect to Murmur's ICE interface."""
        try:
            import Ice

            props = Ice.createProperties()
            props.setProperty("Ice.ImplicitContext", "Shared")
            props.setProperty("Ice.Default.EncodingVersion", "1.0")
            init_data = Ice.InitializationData()
            init_data.properties = props
            self._ice = Ice.initialize(init_data)

            if self.secret:
                self._ice.getImplicitContext().put("secret", self.secret)

            proxy_str = f"Meta:tcp -h {self.host} -p {self.port}"
            proxy = self._ice.stringToProxy(proxy_str)

            # Import generated Murmur module
            import Murmur

            self._meta = Murmur.MetaPrx.checkedCast(proxy)
            if not self._meta:
                logger.error("Failed to cast ICE proxy to Murmur.Meta")
                return False

            servers = self._meta.getBootedServers()
            if servers:
                self._server = servers[0]
                self._connected = True
                logger.info("Connected to Murmur ICE at %s:%d", self.host, self.port)
                return True
            else:
                logger.warning("No booted Murmur servers found")
                return False

        except ImportError:
            logger.warning(
                "ICE or Murmur module not available. "
                "Run slice2py on Murmur.ice to generate stubs. "
                "Using stub implementation."
            )
            return False
        except Exception as e:
            logger.error("Failed to connect to Murmur ICE: %s", e)
            return False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_status(self) -> ServerStatus:
        """Get current server status including online users."""
        if not self._connected or not self._server:
            return ServerStatus()

        try:
            users_dict = self._server.getUsers()
            channels_dict = self._server.getChannels()

            users = []
            for session_id, state in users_dict.items():
                users.append(
                    MumbleUser(
                        session=session_id,
                        name=state.name,
                        channel_id=state.channel,
                        is_muted=state.mute,
                        is_deaf=state.deaf,
                        online_secs=state.onlinesecs,
                        address=".".join(str(b) for b in state.address[:4])
                        if state.address
                        else "",
                    )
                )

            channels = []
            for chan_id, chan_state in channels_dict.items():
                channels.append(
                    MumbleChannel(
                        id=chan_id,
                        name=chan_state.name,
                        parent_id=chan_state.parent,
                        description=chan_state.description,
                    )
                )

            conf = self._server.getAllConf()
            max_users = int(conf.get("users", "100"))

            return ServerStatus(
                is_running=True,
                users_online=len(users),
                max_users=max_users,
                users=users,
                channels=channels,
            )
        except Exception as e:
            logger.error("Error querying Murmur status: %s", e)
            return ServerStatus()

    def register_user(self, username: str, password: str) -> int | None:
        """Register a new user in Murmur. Returns the registered user ID."""
        if not self._connected or not self._server:
            logger.warning("Not connected to Murmur, cannot register user")
            return None

        try:
            import Murmur

            info = {Murmur.UserInfo.UserName: username, Murmur.UserInfo.UserPassword: password}
            user_id = self._server.registerUser(info)
            logger.info("Registered user '%s' with ID %d", username, user_id)
            return user_id
        except Exception as e:
            logger.error("Failed to register user '%s': %s", username, e)
            return None

    def remove_user(self, user_id: int) -> bool:
        """Unregister a user from Murmur."""
        if not self._connected or not self._server:
            return False

        try:
            self._server.unregisterUser(user_id)
            logger.info("Unregistered user ID %d", user_id)
            return True
        except Exception as e:
            logger.error("Failed to unregister user %d: %s", user_id, e)
            return False

    def create_channel(self, name: str, parent_id: int = 0) -> int | None:
        """Create a new channel. Returns the channel ID."""
        if not self._connected or not self._server:
            return None

        try:
            chan_id = self._server.addChannel(name, parent_id)
            logger.info("Created channel '%s' with ID %d", name, chan_id)
            return chan_id
        except Exception as e:
            logger.error("Failed to create channel '%s': %s", name, e)
            return None

    def remove_channel(self, channel_id: int) -> bool:
        """Remove a channel."""
        if not self._connected or not self._server:
            return False

        try:
            self._server.removeChannel(channel_id)
            logger.info("Removed channel ID %d", channel_id)
            return True
        except Exception as e:
            logger.error("Failed to remove channel %d: %s", channel_id, e)
            return False

    def disconnect(self):
        """Clean up ICE connection."""
        if self._ice:
            try:
                self._ice.destroy()
            except Exception:
                pass
            self._ice = None
            self._connected = False

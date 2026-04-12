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
    """Client for Murmur server administration.

    Supports two modes:
    - Full ICE mode: requires zeroc-ice + Murmur slice stubs. Provides user/channel management.
    - Health-check mode: just pings the Mumble TCP port. Shows server as "running" in dashboard.
    """

    def __init__(self, host: str, port: int, secret: str = "",
                 mumble_host: str = "murmur", mumble_port: int = 64738):
        self.host = host
        self.port = port
        self.secret = secret
        self.mumble_host = mumble_host
        self.mumble_port = mumble_port
        self._ice = None
        self._meta = None
        self._server = None
        self._ice_connected = False
        self._connected = False

    def connect(self) -> bool:
        """Connect to Murmur. Tries ICE first, falls back to TCP health check."""
        # Try ICE connection first (full admin capabilities)
        if self._connect_ice():
            self._connected = True
            return True

        # Fall back to TCP health check (server status only)
        if self._check_tcp():
            self._connected = True
            logger.info(
                "Murmur is running (TCP check on %s:%d) but ICE is not available. "
                "Dashboard will show server status. User/channel sync with Murmur is disabled.",
                self.mumble_host, self.mumble_port,
            )
            return True

        logger.warning("Murmur is not reachable at %s:%d", self.mumble_host, self.mumble_port)
        return False

    def _connect_ice(self) -> bool:
        """Try to connect via ICE for full admin capabilities."""
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

            import Murmur

            self._meta = Murmur.MetaPrx.checkedCast(proxy)
            if not self._meta:
                return False

            servers = self._meta.getBootedServers()
            if servers:
                self._server = servers[0]
                self._ice_connected = True
                logger.info("Connected to Murmur ICE at %s:%d", self.host, self.port)
                return True
            return False

        except ImportError:
            logger.info("zeroc-ice not installed. ICE admin features disabled.")
            return False
        except Exception as e:
            logger.info("ICE connection failed (%s). Falling back to TCP health check.", e)
            return False

    def _check_tcp(self) -> bool:
        """Simple TCP connect to verify Murmur is running."""
        import socket
        try:
            sock = socket.create_connection((self.mumble_host, self.mumble_port), timeout=5)
            sock.close()
            return True
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            logger.debug("TCP check to %s:%d failed: %s", self.mumble_host, self.mumble_port, e)
            return False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_status(self) -> ServerStatus:
        """Get current server status including online users."""
        if not self._connected:
            return ServerStatus()

        # Without ICE, just report server is running (from TCP check)
        if not self._ice_connected or not self._server:
            alive = self._check_tcp()
            return ServerStatus(is_running=alive, users_online=0, max_users=50)

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

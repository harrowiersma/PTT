"""Murmur client using pymumble for server administration.

Connects as a bot user to Murmur to manage channels, query online users,
and send text messages. Replaces the ICE-based client (zeroc-ice doesn't
compile on python:3.11-slim).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Bots that should never be subject to channel ACL enforcement or
# mirrored in the dashboard user list. PTTPhone-N (N=1..PHONE_MAX_CALLS)
# are per-call sub-channel bots spawned by the sip-bridge — matched via
# `_is_bot_username()` below, since the suffix is variable.
BOT_USERNAMES = ("PTTAdmin", "PTTWeather", "PTTPhone")


def _is_bot_username(name: str | None) -> bool:
    """True for any bot — including the PTTPhone-N per-call bots."""
    if not name:
        return False
    if name in BOT_USERNAMES:
        return True
    if name.startswith("PTTPhone-"):
        return True
    return False

# Text whispered to users who are bounced out of Phone for lacking the
# can_answer_calls flag. Cached as PCM after the first render.
PHONE_ACL_DENY_TEXT = (
    "Phone channel requires call-answer permission; "
    "please contact an administrator."
)


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
        # Phone channel ACL enforcement state.
        # _user_last_channel[session_id] = previous channel_id. Warms up as
        # USERUPDATED events fire; we only bounce on a real channel change,
        # so users who are already in Phone at startup are not touched.
        self._user_last_channel: dict[int, int] = {}
        # Usernames (lowercase-sensitive match to Mumble's 'name' field)
        # allowed in Phone. Polled from the DB by the lifespan task every
        # 30 s and dropped here by update_phone_eligible().
        self._phone_eligible: set[str] = set()
        # Cached "permission denied" TTS payload. Rendered on first bounce.
        self._phone_deny_pcm: bytes | None = None
        # Call-group ACL state, refreshed every 30 s by the lifespan task.
        # _user_call_groups maps username (lowercase) → set of call-group ids.
        # _channel_call_group maps mumble channel id → call-group id or None.
        # _user_is_admin lets admins bypass the check.
        self._user_call_groups: dict[str, set[int]] = {}
        self._channel_call_group: dict[int, int | None] = {}
        self._user_is_admin: dict[str, bool] = {}
        self._call_group_deny_pcm: bytes | None = None

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
            self._mumble.callbacks.set_callback(
                pymumble.constants.PYMUMBLE_CLBK_USERUPDATED,
                self._on_user_updated,
            )
            # USERCREATED fires on every connect (incl. reconnect). We run
            # _on_user_created_sync (status→online) and _capture_cert_hash_sync
            # (persist the cert hash for later ACL registration) back-to-back.
            # Both are idempotent + cheap; failure in one must not swallow
            # the other.
            def _on_created(user):
                try:
                    _on_user_created_sync(user)
                except Exception as e:
                    logger.error("auto-online hook error: %s", e)
                try:
                    _capture_cert_hash_sync(user)
                except Exception as e:
                    logger.error("cert-hash capture error: %s", e)
            self._mumble.callbacks.set_callback(
                pymumble.constants.PYMUMBLE_CLBK_USERCREATED,
                _on_created,
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
                if user["name"] in BOT_USERNAMES:
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

    def whisper_text(self, session_id: int, message: str) -> bool:
        """Whisper a text message to one Murmur session.

        Pymumble's User.send_text_message delivers the message to that
        specific user only — no one else sees it. Used by Phase 5 to
        push a structured "INCOMING_CALL|<caller>|<sub>" payload to
        P50 radios alongside the audible ding so the app can raise a
        full-screen overlay without polluting any channel chat log.
        """
        if not self._mumble:
            return False
        try:
            user = self._mumble.users.get(session_id)
            if user is None:
                logger.warning("whisper_text: session %d not found", session_id)
                return False
            user.send_text_message(message)
            return True
        except Exception as e:
            logger.error("whisper_text failed for session %d: %s", session_id, e)
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
            if username in BOT_USERNAMES:
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

    # --- Phone channel ACL enforcement --------------------------------

    def update_phone_eligible(self, usernames: set[str]) -> None:
        """Replace the cached set of users allowed in Phone.

        Called by the lifespan poller every 30 s from the main asyncio
        loop. Assignment is atomic under the GIL so no lock needed for
        the reader (the USERUPDATED callback) running on pymumble's
        thread.
        """
        self._phone_eligible = set(usernames)

    def update_call_group_state(
        self,
        user_groups: dict[str, set[int]],
        channel_groups: dict[int, int | None],
        user_admin: dict[str, bool],
    ) -> None:
        """Refresh the in-memory call-group state. Atomic swap.

        Also triggers a one-shot sweep so any user currently sitting in a
        channel they shouldn't be in (because they were already there when
        the bridge started, or because the channel was just tagged with a
        group) gets bounced out immediately — not only on their next move.
        """
        self._user_call_groups = user_groups
        self._channel_call_group = channel_groups
        self._user_is_admin = user_admin
        self._sweep_call_group_violators()

    def _sweep_call_group_violators(self) -> None:
        """Evict users who are currently in a channel their groups don't
        permit. Called after every state refresh so grandfathered
        occupants (there before the bridge bot connected, or before the
        channel was tagged) are caught within one refresh cycle.

        Bounces to Root (0) because we don't have a 'previous channel'
        for these sightings.
        """
        mm = self._mumble
        if not mm:
            return
        try:
            # Snapshot the user dict — pymumble mutates it from its own
            # thread as events arrive.
            snapshot = list(mm.users.items())
        except Exception as e:
            logger.error("call-group sweep: users snapshot failed: %s", e)
            return
        for session_id, user in snapshot:
            try:
                name = user.get("name") if isinstance(user, dict) else getattr(user, "name", None)
                chan = user.get("channel_id") if isinstance(user, dict) else getattr(user, "channel_id", None)
                if not name or chan is None:
                    continue
                if _is_bot_username(name):
                    continue
                if self._call_group_check(name, chan):
                    continue
                logger.info(
                    "call-group: sweep bouncing '%s' (session=%s) out of channel %d",
                    name, session_id, chan,
                )
                self._bounce_from_channel(
                    session_id, 0, self._get_call_group_deny_pcm(),
                )
            except Exception as e:
                logger.error("call-group sweep: per-user bounce failed for session %s: %s",
                             session_id, e)

    def _call_group_check(self, name: str, new_channel_id: int) -> bool:
        """True if `name` may join `new_channel_id` per call-group rules."""
        lc = name.lower() if name else ""
        if self._user_is_admin.get(lc, False):
            return True
        chan_group = self._channel_call_group.get(new_channel_id)
        if chan_group is None:
            return True
        return chan_group in self._user_call_groups.get(lc, set())

    def _get_call_group_deny_pcm(self) -> bytes | None:
        """Lazily render and cache the call-group-deny TTS."""
        if self._call_group_deny_pcm is not None:
            return self._call_group_deny_pcm
        try:
            from server.weather_bot import text_to_audio_pcm
            pcm = text_to_audio_pcm(
                "This channel requires call group membership"
            )
            if pcm:
                self._call_group_deny_pcm = pcm
            return pcm
        except Exception as e:
            logger.error("failed to render call-group-deny TTS: %s", e)
            return None

    def _resolve_phone_channel_id(self) -> int | None:
        """Find Murmur's Phone channel by name; None if not yet seen."""
        if not self._mumble:
            return None
        try:
            for cid, chan in self._mumble.channels.items():
                if chan.get("name") == "Phone":
                    return cid
        except Exception:
            return None
        return None

    def _resolve_phone_and_children(self) -> set[int]:
        """Phone + every Call-N sub-channel directly under it.

        Used by the ACL so entering any Phone/Call-N is gated identically
        to entering Phone itself. Only immediate children — deeper nesting
        isn't a case we ship.
        """
        if not self._mumble:
            return set()
        try:
            phone_id = self._resolve_phone_channel_id()
            if phone_id is None:
                return set()
            gated: set[int] = {phone_id}
            for cid, chan in self._mumble.channels.items():
                if chan.get("parent") == phone_id:
                    gated.add(cid)
            return gated
        except Exception:
            return set()

    def _get_phone_deny_pcm(self) -> bytes | None:
        """Lazily render and cache the permission-denied TTS."""
        if self._phone_deny_pcm is not None:
            return self._phone_deny_pcm
        try:
            from server.weather_bot import text_to_audio_pcm
            pcm = text_to_audio_pcm(PHONE_ACL_DENY_TEXT)
            if pcm:
                self._phone_deny_pcm = pcm
            return pcm
        except Exception as e:
            logger.error("failed to render phone-deny TTS: %s", e)
            return None

    def _on_user_updated(self, user, actions) -> None:
        """pymumble USERUPDATED callback. Fires on pymumble's thread.

        `user` is the current user dict (session, name, channel_id, ...).
        `actions` is a dict of the fields that changed in this update;
        for our purposes we only act when the user's channel_id changed
        AND the new channel is `Phone` AND they are not in the eligible
        set.

        Kept lightweight — this fires for every user-state tick (mute
        toggles, self-deaf, etc.).
        """
        try:
            if not user:
                return
            name = user.get("name") if isinstance(user, dict) else getattr(user, "name", None)
            session_id = user.get("session") if isinstance(user, dict) else getattr(user, "session", None)
            new_channel_id = user.get("channel_id") if isinstance(user, dict) else getattr(user, "channel_id", None)
            if name is None or session_id is None or new_channel_id is None:
                return
            if _is_bot_username(name):
                return

            # Pick up cert-hash changes mid-session (cert rotation). The
            # helper is a no-op when the hash matches the stored one, so
            # calling it unconditionally here is cheap.
            if isinstance(actions, dict) and "hash" in actions:
                try:
                    _capture_cert_hash_sync(user)
                except Exception as e:
                    logger.error("cert-hash capture (updated) error: %s", e)

            prev_channel_id = self._user_last_channel.get(session_id)
            # Always update the record first so subsequent ticks compare
            # against the most recent observation.
            self._user_last_channel[session_id] = new_channel_id

            # Only care about real moves.
            if prev_channel_id is None or prev_channel_id == new_channel_id:
                return

            # Phone ACL: only applies when entering a Phone-family channel.
            gated = self._resolve_phone_and_children()
            if gated and new_channel_id in gated:
                if name not in self._phone_eligible:
                    logger.info(
                        "phone-acl: bouncing '%s' (session=%s) back to channel %d",
                        name, session_id, prev_channel_id,
                    )
                    self._bounce_from_channel(
                        session_id, prev_channel_id, self._get_phone_deny_pcm(),
                    )
                    return

            # Call-group ACL: applies to every channel. Runs after phone-acl
            # so phone-eligible users still get call-group-gated.
            if not self._call_group_check(name, new_channel_id):
                logger.info(
                    "call-group: bouncing '%s' (session=%s) from channel %d",
                    name, session_id, new_channel_id,
                )
                self._bounce_from_channel(
                    session_id, prev_channel_id, self._get_call_group_deny_pcm(),
                )
                return
        except Exception as e:
            logger.error("USERUPDATED handler error: %s", e)

    def _bounce_from_channel(
        self, session_id: int, previous_channel_id: int,
        deny_pcm: bytes | None,
    ) -> None:
        """Move `session_id` back to `previous_channel_id` and whisper why."""
        mm = self._mumble
        if not mm:
            return
        try:
            user = mm.users.get(session_id)
            if user is None:
                return
            user.move_in(previous_channel_id)
            # Update cache so the move we just forced doesn't look like a
            # second violation on the next callback tick.
            self._user_last_channel[session_id] = previous_channel_id
        except Exception as e:
            logger.error("bounce: move_in failed for session %s: %s", session_id, e)
            return

        if deny_pcm:
            try:
                self.whisper_audio(session_id, deny_pcm, with_preamble=True)
            except Exception as e:
                logger.error("bounce: whisper failed for session %s: %s", session_id, e)

    # -------------------------------------------------------------------

    def disconnect(self):
        """Clean up pymumble connection."""
        if self._mumble:
            try:
                self._mumble.stop()
            except Exception:
                pass
            self._mumble = None
            self._connected = False


def _capture_cert_hash_sync(event) -> None:
    """Extract cert hash from a pymumble user dict/object and persist it
    to users.mumble_cert_hash. Runs on pymumble's sync thread via a
    short-lived engine (same pattern as _on_user_created_sync).

    Idempotent: no-op when the hash is missing, matches the stored one,
    or the username is a bot / unknown. On hash change, the stored
    mumble_registered_user_id is cleared so the next auto-registration
    tick re-registers with the new cert.
    """
    try:
        name = event.get("name") if isinstance(event, dict) else getattr(event, "name", None)
        hash_ = event.get("hash") if isinstance(event, dict) else getattr(event, "hash", None)
    except Exception:
        return
    if not name or not hash_ or _is_bot_username(name):
        return

    try:
        from sqlalchemy import create_engine, select as _select
        from sqlalchemy.orm import sessionmaker
        from server.config import settings
        from server.models import User

        engine = create_engine(settings.database_url_sync, echo=False)
        Session = sessionmaker(engine, expire_on_commit=False)
        with Session() as db:
            user = db.execute(
                _select(User).where(User.username == name)
            ).scalar_one_or_none()
            if user is None:
                return
            if user.mumble_cert_hash == hash_:
                return  # no change
            was_set = user.mumble_cert_hash is not None
            user.mumble_cert_hash = hash_
            # Hash changed (or first capture) — invalidate any prior
            # registration so the scheduler re-registers with the new
            # hash. When was_set is False this is already NULL.
            user.mumble_registered_user_id = None
            db.commit()
            logger.info(
                "cert-hash: captured for %s (rotated=%s)",
                name, was_set,
            )
        engine.dispose()
    except Exception as e:
        logger.error("cert-hash capture failed for %s: %s", name, e)


def _on_user_created_sync(event) -> None:
    """PYMUMBLE_CLBK_USERCREATED fires on pymumble's sync thread whenever a
    user joins the server (including reconnects). Promote the matching DB
    user to status_label='online'. Bots are skipped; unknown usernames no-op.

    Uses a short-lived sync engine so we don't fight the async main loop
    (same pattern as loneworker._run_shift_cycle).
    """
    try:
        name = event.get("name") if isinstance(event, dict) else getattr(event, "name", None)
    except Exception:
        return
    if not name or _is_bot_username(name):
        return

    try:
        from sqlalchemy import create_engine, select as _select
        from sqlalchemy.orm import sessionmaker
        from server.config import settings
        from server.models import AuditLog, User

        engine = create_engine(settings.database_url_sync, echo=False)
        Session = sessionmaker(engine, expire_on_commit=False)
        with Session() as db:
            user = db.execute(_select(User).where(User.username == name)).scalar_one_or_none()
            if user is None:
                return
            if user.status_label == "online":
                return
            old = user.status_label
            user.status_label = "online"
            user.status_updated_at = datetime.now(timezone.utc)
            db.add(AuditLog(
                admin_username="system",
                action="user.status_change",
                target_type="user", target_id=user.username,
                details=json.dumps({"from": old, "to": "online", "source": "auto_connect"}),
            ))
            db.commit()
            logger.info("status: %s auto-online on connect", user.username)
        engine.dispose()
    except Exception as e:
        logger.error("auto-online hook failed for %s: %s", name, e)

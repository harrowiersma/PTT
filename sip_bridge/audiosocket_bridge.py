"""AudioSocket-based SIP ↔ Mumble bridge.

Replaces the ARI externalMedia path. Asterisk's AudioSocket() dialplan
application connects to our TCP server on loopback and streams slin
8 kHz audio bidirectionally. Simpler than RTP (no headers, no ports
to learn), and community-reported to avoid the one-way-audio quirks
externalMedia has with UnicastRTP bridges.

Wire format per frame: 1-byte type + 2-byte big-endian length + payload.
  type 0x00 = hangup       (no payload)
  type 0x01 = UUID         (16-byte payload, sent once by Asterisk at connect)
  type 0x10 = audio        (slin payload; 320 B = 20 ms @ 8 kHz)
  type 0xff = error        (2-byte error code)
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

LOG = logging.getLogger("audiosocket-bridge")
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

LISTEN_HOST = os.environ.get("AUDIOSOCKET_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("AUDIOSOCKET_PORT", "9092"))
MUMBLE_HOST = os.environ.get("MUMBLE_HOST", "127.0.0.1")
MUMBLE_PORT = int(os.environ.get("MUMBLE_PORT", "64738"))
ADMIN_BASE_URL = os.environ.get("ADMIN_INTERNAL_URL", "http://127.0.0.1:8000")
INTERNAL_SECRET = os.environ.get("PTT_INTERNAL_API_SECRET", "").strip()
# Priority 7 — multi-caller support. One slot = one concurrent call =
# one dedicated PTTPhone-N bot joined to Phone/Call-N. Sized up front;
# Asterisk's dialplan should keep GROUP_COUNT(phone) ≤ PHONE_MAX_CALLS.
PHONE_MAX_CALLS = int(os.environ.get("PHONE_MAX_CALLS", "3"))


def _notify_call_ended() -> None:
    """POST /internal/call-ended so admin stops the ding-notification loop.
    Called from the main serve() loop after a client connection closes —
    the dialplan's CURL(call-ended) runs only on orderly hangup via our
    Hangup() priority, which doesn't execute when the caller hangs up
    first.
    """
    if not INTERNAL_SECRET:
        return
    try:
        import httpx
        with httpx.Client(timeout=5, headers={"X-Internal-Auth": INTERNAL_SECRET}) as c:
            c.get(f"{ADMIN_BASE_URL}/api/sip/internal/call-ended")
    except Exception as e:
        LOG.warning("call-ended POST failed: %s", e)

# AudioSocket default: slin @ 8 kHz mono int16. 20 ms = 160 samples = 320 B.
SLIN8_FRAME_BYTES = 320
# Mumble / pymumble: slin @ 48 kHz mono int16. 20 ms = 960 samples = 1920 B.
MUMBLE_FRAME_BYTES = 1920
# VAD floor: don't transmit to Mumble when the caller is silent.
# Keeps PTTPhone from hogging the half-duplex P50 channel.
VAD_THRESHOLD = 500
# Downlink (Mumble → phone) attenuation. pymumble delivers peak-normalized
# audio that's hot for a phone line; 0.5 ≈ -6 dBFS is comfortable. Tune via
# DOWNLINK_GAIN env var without rebuild.
DOWNLINK_GAIN = float(os.environ.get("DOWNLINK_GAIN", "0.5"))

# Ringback tone. European-style 400 Hz, 1 s on / 4 s off — played to the
# caller while no human user is present in the Phone channel so they hear
# the line is alive and ringing, not dead silence. Pre-rendered at startup
# as N × 320-byte slin 8 kHz frames, looped.
def _build_ringback_frames() -> list[bytes]:
    sr = 8000
    on_n = sr * 1        # 1 s ringing
    off_n = sr * 4       # 4 s silence → full cycle 5 s
    t = np.arange(on_n, dtype=np.float32) / sr
    # Soft envelope so the tone start/stop doesn't click.
    env = np.ones(on_n, dtype=np.float32)
    ramp = int(sr * 0.03)
    env[:ramp] = np.linspace(0, 1, ramp)
    env[-ramp:] = np.linspace(1, 0, ramp)
    tone = (np.sin(2 * np.pi * 400 * t) * env * 0.25 * 32767).astype(np.int16)
    silence = np.zeros(off_n, dtype=np.int16)
    cycle = np.concatenate([tone, silence]).tobytes()
    # Slice into 320-byte (20 ms) frames.
    return [cycle[i : i + SLIN8_FRAME_BYTES]
            for i in range(0, len(cycle), SLIN8_FRAME_BYTES)
            if len(cycle[i : i + SLIN8_FRAME_BYTES]) == SLIN8_FRAME_BYTES]


_ringback_frames: list[bytes] | None = None

def _get_ringback_frames() -> list[bytes]:
    global _ringback_frames
    if _ringback_frames is None:
        _ringback_frames = _build_ringback_frames()
    return _ringback_frames

# AudioSocket frame-type constants
_TYPE_HANGUP = 0x00
_TYPE_UUID = 0x01
_TYPE_AUDIO = 0x10
_TYPE_ERROR = 0xFF


def resample_8k_to_48k(pcm_8k: bytes) -> bytes:
    if not pcm_8k:
        return b""
    src = np.frombuffer(pcm_8k, dtype=np.int16).astype(np.float32)
    target_n = len(src) * 6
    tgt = np.interp(
        np.linspace(0, len(src) - 1, target_n, dtype=np.float32),
        np.arange(len(src), dtype=np.float32),
        src,
    )
    return np.clip(tgt, -32768, 32767).astype(np.int16).tobytes()


def resample_48k_to_8k(pcm_48k: bytes) -> bytes:
    if not pcm_48k:
        return b""
    src = np.frombuffer(pcm_48k, dtype=np.int16).astype(np.float32)
    target_n = len(src) // 6
    tgt = np.interp(
        np.linspace(0, len(src) - 1, target_n, dtype=np.float32),
        np.arange(len(src), dtype=np.float32),
        src,
    )
    return np.clip(tgt, -32768, 32767).astype(np.int16).tobytes()


def _patch_pymumble_ssl_once() -> None:
    """Pymumble 1.6.1 uses ssl.wrap_socket() removed in Python 3.12."""
    import ssl as _ssl
    if hasattr(_ssl, "wrap_socket"):
        return
    def _compat_wrap_socket(sock, certfile=None, keyfile=None, ssl_version=None, **_kw):
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        if certfile:
            ctx.load_cert_chain(certfile, keyfile)
        return ctx.wrap_socket(sock)
    _ssl.wrap_socket = _compat_wrap_socket


def ensure_phone_slots_via_admin(count: int) -> None:
    """Ask admin to provision Phone + Phone/Call-1..Call-N sub-channels.

    Called once at startup. Admin does the sqlite edits + murmur restart
    if needed (idempotent on subsequent boots). Fire-and-forget: if the
    admin isn't up yet we fall back to best-effort — the per-call bot
    will fail to find its Call-N channel and drop the call, which is
    less confusing than crashing the whole bridge.
    """
    if not INTERNAL_SECRET:
        LOG.warning("no PTT_INTERNAL_API_SECRET set; skipping slot provisioning")
        return
    try:
        import httpx
        with httpx.Client(timeout=30, headers={"X-Internal-Auth": INTERNAL_SECRET}) as c:
            r = c.post(f"{ADMIN_BASE_URL}/api/sip/internal/ensure-phone-slots",
                       params={"count": count})
            r.raise_for_status()
            LOG.info("phone slots provisioned: %s", r.json())
    except Exception as e:
        LOG.warning("ensure-phone-slots failed (continuing): %s", e)


def open_call_bot(slot: int, client: "Client"):
    """Connect as PTTPhone-{slot}, join Phone/Call-{slot}, wire sound callback.

    Each call gets its own dedicated Mumble connection so audio streams
    between concurrent calls stay isolated (same bot in two calls would
    bleed audio between callers). Returns a live Mumble instance; raises
    RuntimeError if the Call-{slot} channel never appears within 6 s
    (admin not up, slot count too low, sqlite edit didn't land yet).
    """
    _patch_pymumble_ssl_once()
    import pymumble_py3 as pymumble
    import pymumble_py3.constants as const

    username = f"PTTPhone-{slot}"
    mm = pymumble.Mumble(MUMBLE_HOST, username, port=MUMBLE_PORT, reconnect=False)
    mm.set_application_string("openPTT SIP Bridge (AudioSocket)")
    mm.set_receive_sound(True)

    def _on_sound(user, sound_chunk):
        client.enqueue_mumble(user.get("name", "?"), sound_chunk.pcm)

    mm.callbacks.set_callback(const.PYMUMBLE_CLBK_SOUNDRECEIVED, _on_sound)
    mm.start()
    mm.is_ready()
    time.sleep(0.5)

    call_channel_name = f"Call-{slot}"

    def _find_call_channel() -> Optional[int]:
        for cid, chan in mm.channels.items():
            if chan.get("name") == call_channel_name:
                return cid
        return None

    cid = _find_call_channel()
    for _ in range(20):
        if cid is not None:
            break
        time.sleep(0.3)
        cid = _find_call_channel()
    if cid is None:
        try:
            mm.stop()
        except Exception:
            pass
        raise RuntimeError(f"sub-channel {call_channel_name} not available")

    mm.users.myself.move_in(cid)
    time.sleep(0.2)
    LOG.info("%s joined sub-channel %s (id=%d)", username, call_channel_name, cid)
    return mm


class _SlotPool:
    """Fixed-size slot pool with per-slot acquire/release.

    Slot numbers are 1-indexed (1..PHONE_MAX_CALLS) so they line up with
    the Phone/Call-N channel naming — no off-by-one surprises on-screen.
    """

    def __init__(self, size: int):
        self._size = size
        self._lock = threading.Lock()
        self._in_use: set[int] = set()

    def acquire(self) -> Optional[int]:
        with self._lock:
            for n in range(1, self._size + 1):
                if n not in self._in_use:
                    self._in_use.add(n)
                    return n
        return None

    def release(self, slot: int) -> None:
        with self._lock:
            self._in_use.discard(slot)

    def active_count(self) -> int:
        with self._lock:
            return len(self._in_use)


# Global pointer to the most recently connected active call. Control
# signals (SIGUSR1 hangup, SIGUSR2 mute-toggle) target this client. This
# is a pragmatic choice: radio operators typically handle one call at a
# time, and "last call wins" matches the pick-up behaviour — the most
# recently ringing call is what the operator is actively engaging.
# Per-slot targeting is follow-up work (needs app-side slot selection).
_most_recent_client: "Optional[Client]" = None
_most_recent_lock = threading.Lock()


def _set_most_recent(client: "Optional[Client]") -> None:
    global _most_recent_client
    with _most_recent_lock:
        _most_recent_client = client


def _get_most_recent() -> "Optional[Client]":
    with _most_recent_lock:
        return _most_recent_client


class Client:
    """One AudioSocket TCP connection. Handles frame parsing + audio pump."""

    def __init__(self, sock: socket.socket, slot: int):
        self._sock = sock
        # Mumble connection is opened after construction (open_call_bot
        # needs `self` for the sound callback), so start as None.
        self._mumble = None
        self.slot = slot
        self._rx_queue: deque = deque(maxlen=50)
        self._stop = False
        self._uuid: Optional[str] = None
        self._ul_count = 0
        self._dl_count = 0
        self._last_log = time.monotonic()
        # Green-button mute: when True, the downlink sends silence to
        # Asterisk regardless of Mumble activity. Radio users can step
        # aside, talk in another channel, and come back without the
        # caller overhearing. Toggled by SIGUSR2 from the admin.
        self.mute_caller = False

    def attach_mumble(self, mumble) -> None:
        self._mumble = mumble

    def enqueue_mumble(self, user_name: str, pcm48k: bytes) -> None:
        """Called from pymumble callback thread. Split to 20 ms pieces."""
        if not pcm48k:
            return
        for i in range(0, len(pcm48k) - MUMBLE_FRAME_BYTES + 1, MUMBLE_FRAME_BYTES):
            self._rx_queue.append(pcm48k[i : i + MUMBLE_FRAME_BYTES])

    def hangup_from_radio(self) -> None:
        """Radio-initiated hangup. Send an AudioSocket hangup frame so
        Asterisk tears down the SIP call; then close the TCP socket so
        serve()'s wrapper runs call-ended. Safe to call repeatedly.
        """
        if self._stop:
            return
        try:
            self._send_frame(_TYPE_HANGUP)
        except OSError as e:
            LOG.debug("hangup send: %s", e)
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _send_frame(self, type_byte: int, payload: bytes = b"") -> None:
        header = struct.pack("!BH", type_byte & 0xFF, len(payload))
        self._sock.sendall(header + payload)

    def _recv_exact(self, n: int) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _log_stats(self) -> None:
        now = time.monotonic()
        if now - self._last_log > 1.0:
            LOG.info("audio pump: uplink=%d dl=%d frames/s",
                     self._ul_count, self._dl_count)
            self._ul_count = 0
            self._dl_count = 0
            self._last_log = now

    def _human_in_phone(self) -> bool:
        """True iff a non-PTTPhone user sits in this call's sub-channel.

        "In this call" = same channel as the bot (Phone/Call-N). Callers
        on other concurrent sub-channels don't count — they're a
        different conversation and shouldn't silence this one's ringback.
        """
        mm = self._mumble
        if mm is None:
            return False
        try:
            my_sid = mm.users.myself.get("session")
            my_cid = mm.users.myself.get("channel_id")
            if my_cid is None:
                return False
            for sid, user in mm.users.items():
                if sid == my_sid:
                    continue
                name = user.get("name", "")
                # Fellow bot on another slot? Ignore — only real humans.
                if name.startswith("PTTPhone"):
                    continue
                if user.get("channel_id") == my_cid:
                    return True
            return False
        except Exception:
            return False

    def _downlink_loop(self) -> None:
        """Runs in a thread. Sends one 20 ms slin8 frame per tick to Asterisk.

        Priority:
          1. Mumble audio in rx_queue (user in Phone talking)
          2. Ringback tone (no human in Phone — caller hears line ringing)
          3. Nothing (human in Phone but silent — Asterisk fills with silence)
        """
        ringback_frames = _get_ringback_frames()
        ringback_idx = 0
        next_tick = time.monotonic()
        while not self._stop:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(min(0.005, next_tick - now))
                continue
            next_tick += 0.020

            slin8: Optional[bytes] = None

            # Highest priority: muted → ship silence regardless of source.
            # Radio users can hop to another channel and chat without the
            # caller overhearing.
            if self.mute_caller:
                slin8 = b"\x00" * SLIN8_FRAME_BYTES
                # Still drain the rx queue so we don't accumulate lag on unmute.
                try:
                    self._rx_queue.popleft()
                except IndexError:
                    pass
                ringback_idx = 0
            else:
                # Priority 1: Mumble audio, if available.
                try:
                    frame_pcm = self._rx_queue.popleft()
                    slin8 = resample_48k_to_8k(frame_pcm)
                    if DOWNLINK_GAIN != 1.0:
                        samples = np.frombuffer(slin8, dtype=np.int16).astype(np.float32)
                        samples *= DOWNLINK_GAIN
                        slin8 = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()
                    ringback_idx = 0  # restart ringback from silent part next time
                except IndexError:
                    # Priority 2: ringback if no human in Phone.
                    if not self._human_in_phone() and ringback_frames:
                        slin8 = ringback_frames[ringback_idx % len(ringback_frames)]
                        ringback_idx += 1

            if slin8 is None:
                continue

            try:
                self._send_frame(_TYPE_AUDIO, slin8)
                self._dl_count += 1
            except OSError as e:
                LOG.warning("AudioSocket send failed: %s", e)
                self._stop = True
                return

    def run(self) -> None:
        """Main loop: parse inbound frames, push audio to Mumble."""
        LOG.info("AudioSocket client connected from %s", self._sock.getpeername())
        threading.Thread(target=self._downlink_loop, daemon=True,
                         name="audiosocket-downlink").start()

        try:
            while not self._stop:
                header = self._recv_exact(3)
                if header is None:
                    LOG.info("AudioSocket connection closed by peer")
                    break
                type_b, length = struct.unpack("!BH", header)
                payload = self._recv_exact(length) if length > 0 else b""
                if length > 0 and payload is None:
                    LOG.warning("short read on %d-byte payload", length)
                    break

                if type_b == _TYPE_HANGUP:
                    LOG.info("AudioSocket hangup frame")
                    break
                elif type_b == _TYPE_UUID:
                    self._uuid = payload.hex()
                    LOG.info("AudioSocket session UUID: %s", self._uuid)
                elif type_b == _TYPE_AUDIO:
                    if len(payload) < 2:
                        continue
                    # slin is native-endian int16 in AudioSocket (not RTP L16 BE).
                    samples = np.frombuffer(payload, dtype=np.int16)
                    if int(np.abs(samples).max()) < VAD_THRESHOLD:
                        continue
                    pcm48k = resample_8k_to_48k(payload)
                    try:
                        self._mumble.sound_output.add_sound(pcm48k)
                        self._ul_count += 1
                    except Exception as e:
                        LOG.warning("mumble add_sound failed: %s", e)
                elif type_b == _TYPE_ERROR:
                    LOG.warning("AudioSocket error frame: %s", payload.hex())
                    break
                else:
                    LOG.debug("AudioSocket ignoring unknown type 0x%02x len=%d",
                              type_b, length)

                self._log_stats()
        finally:
            self._stop = True
            try:
                self._sock.close()
            except OSError:
                pass


def _install_control_signals() -> None:
    """Radio-initiated call control via POSIX signals.

    Admin's `/api/sip/hangup-current` and `/api/sip/mute-toggle`
    endpoints call `docker exec pkill -USR1/-USR2 -f audiosocket_bridge`,
    which delivers these here. Signals are the cleanest cross-container
    control path on this setup — sip-bridge is host-networked and admin
    is bridge-networked, so a loopback HTTP port between them would
    require extra plumbing.

    With multi-caller (Priority 7), signals target the most recent active
    call — typically what the operator is engaging. Per-call targeting is
    a future enhancement that needs an app-side slot selector.
    """
    def _on_hangup(signum, frame):
        client = _get_most_recent()
        if client is None:
            LOG.info("SIGUSR1 received, no active call — ignoring")
            return
        LOG.info("SIGUSR1 received — radio-initiated hangup (slot=%d)", client.slot)
        client.hangup_from_radio()

    def _on_mute(signum, frame):
        client = _get_most_recent()
        if client is None:
            LOG.info("SIGUSR2 received, no active call — ignoring")
            return
        client.mute_caller = not client.mute_caller
        LOG.info("SIGUSR2 received — slot=%d mute_caller=%s",
                 client.slot, client.mute_caller)

    signal.signal(signal.SIGUSR1, _on_hangup)
    signal.signal(signal.SIGUSR2, _on_mute)
    LOG.info("radio control signals installed (SIGUSR1=hangup, SIGUSR2=mute-toggle)")


def serve() -> None:
    LOG.info("multi-caller slots: %d", PHONE_MAX_CALLS)
    ensure_phone_slots_via_admin(PHONE_MAX_CALLS)
    _install_control_signals()

    pool = _SlotPool(PHONE_MAX_CALLS)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(4)
    LOG.info("AudioSocket server listening on %s:%d", LISTEN_HOST, LISTEN_PORT)

    while True:
        conn, addr = srv.accept()
        conn.settimeout(30)

        slot = pool.acquire()
        if slot is None:
            # All slots in use. Asterisk's GROUP_COUNT should have already
            # 486-Busy'd this caller; if we got here, something raced —
            # hangup the TCP stream immediately so Asterisk tears down.
            LOG.warning("no free slot; rejecting AudioSocket connection")
            try:
                conn.sendall(struct.pack("!BH", _TYPE_HANGUP, 0))
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            continue

        client = Client(conn, slot)
        _set_most_recent(client)

        def _run_and_cleanup(_client=client, _slot=slot, _addr=addr):
            mumble = None
            try:
                mumble = open_call_bot(_slot, _client)
                _client.attach_mumble(mumble)
                _client.run()
            except Exception as e:
                LOG.exception("call on slot %d failed: %s", _slot, e)
            finally:
                # Tear down Mumble connection for this slot, release slot,
                # and tell admin the ding-notification loop can stop.
                if mumble is not None:
                    try:
                        mumble.stop()
                    except Exception:
                        pass
                pool.release(_slot)
                # If this was the most-recent client, clear the pointer
                # so stale signals don't fire against a dead Client.
                if _get_most_recent() is _client:
                    _set_most_recent(None)
                _notify_call_ended()

        threading.Thread(target=_run_and_cleanup, daemon=True,
                         name=f"audiosocket-slot{slot}-{addr}").start()


def main() -> None:
    try:
        serve()
    except KeyboardInterrupt:
        LOG.info("shutdown requested")
    except Exception as e:
        LOG.exception("fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()

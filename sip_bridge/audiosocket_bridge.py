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


def open_mumble(host: str, port: int):
    """Connect as PTTPhone, join Phone channel, wire sound callback."""
    # Pymumble 1.6.1 uses ssl.wrap_socket() removed in Python 3.12. Shim it.
    import ssl as _ssl
    if not hasattr(_ssl, "wrap_socket"):
        def _compat_wrap_socket(sock, certfile=None, keyfile=None, ssl_version=None, **_kw):
            ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            if certfile:
                ctx.load_cert_chain(certfile, keyfile)
            return ctx.wrap_socket(sock)
        _ssl.wrap_socket = _compat_wrap_socket

    import pymumble_py3 as pymumble
    import pymumble_py3.constants as const

    mm = pymumble.Mumble(host, "PTTPhone", port=port, reconnect=True)
    mm.set_application_string("openPTT SIP Bridge (AudioSocket)")
    mm.set_receive_sound(True)
    mm.current_client = None  # set by serve() on each accepted connection

    def _on_sound(user, sound_chunk):
        client = getattr(mm, "current_client", None)
        if client is not None:
            client.enqueue_mumble(user.get("name", "?"), sound_chunk.pcm)

    mm.callbacks.set_callback(const.PYMUMBLE_CLBK_SOUNDRECEIVED, _on_sound)
    mm.start()
    mm.is_ready()
    time.sleep(1)

    def _find_phone():
        for cid, chan in mm.channels.items():
            if chan.get("name") == "Phone":
                return cid
        return None

    phone_id = _find_phone()
    for _ in range(20):
        if phone_id is not None:
            break
        time.sleep(0.3)
        phone_id = _find_phone()
    if phone_id is None:
        LOG.error("Phone channel never appeared; admin must create it")
        raise RuntimeError("Phone channel unavailable")

    mm.users.myself.move_in(phone_id)
    time.sleep(0.2)
    LOG.info("PTTPhone joined Phone channel id=%d", phone_id)
    return mm


class Client:
    """One AudioSocket TCP connection. Handles frame parsing + audio pump."""

    def __init__(self, sock: socket.socket, mumble):
        self._sock = sock
        self._mumble = mumble
        self._rx_queue: deque = deque(maxlen=50)
        self._stop = False
        self._uuid: Optional[str] = None
        self._ul_count = 0
        self._dl_count = 0
        self._last_log = time.monotonic()

    def enqueue_mumble(self, user_name: str, pcm48k: bytes) -> None:
        """Called from pymumble callback thread. Split to 20 ms pieces."""
        if not pcm48k:
            return
        for i in range(0, len(pcm48k) - MUMBLE_FRAME_BYTES + 1, MUMBLE_FRAME_BYTES):
            self._rx_queue.append(pcm48k[i : i + MUMBLE_FRAME_BYTES])

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
        """True iff a non-PTTPhone user is currently in the Phone channel."""
        mm = self._mumble
        try:
            phone_cid = None
            for cid, chan in mm.channels.items():
                if chan.get("name") == "Phone":
                    phone_cid = cid
                    break
            if phone_cid is None:
                return False
            my_sid = mm.users.myself.get("session")
            for sid, user in mm.users.items():
                if sid == my_sid:
                    continue
                if user.get("channel_id") == phone_cid:
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


def serve() -> None:
    LOG.info("connecting to Mumble at %s:%d as PTTPhone", MUMBLE_HOST, MUMBLE_PORT)
    mumble = open_mumble(MUMBLE_HOST, MUMBLE_PORT)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(4)
    LOG.info("AudioSocket server listening on %s:%d", LISTEN_HOST, LISTEN_PORT)

    while True:
        conn, addr = srv.accept()
        conn.settimeout(30)
        client = Client(conn, mumble)
        # Phase 2b-audio policy: one caller at a time. If a previous client
        # is still attached, the new one replaces it — the old client's
        # socket will EOF on its next recv and its thread exits.
        mumble.current_client = client

        def _run_and_notify(_client=client, _addr=addr):
            try:
                _client.run()
            finally:
                # Always tell admin the call ended so the ding loop stops,
                # regardless of whether the caller hung up, the radio side
                # hung up, or our client errored.
                _notify_call_ended()

        threading.Thread(target=_run_and_notify, daemon=True,
                         name=f"audiosocket-{addr}").start()


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

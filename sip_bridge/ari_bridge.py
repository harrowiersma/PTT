"""ARI bridge — Asterisk external app that handles inbound calls in
the Stasis(openptt-bridge) context.

Phase 2b-audio scope: single concurrent call, bidirectional audio into
the Mumble Phone channel. No sub-channels, no mute, no notifications.

Architecture is documented in docs/plans/2026-04-17-sip-bridge-phase-2b-audio-design.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
import numpy as np

from sip_bridge.audio import downsample_48_to_16, upsample_16_to_48

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("ari-bridge")

ARI_HOST = os.environ.get("ARI_HOST", "127.0.0.1")
ARI_PORT = int(os.environ.get("ARI_PORT", "8088"))
ARI_USER = os.environ.get("ARI_USER", "openptt")
ARI_APP = os.environ.get("ARI_APP", "openptt-bridge")
ARI_PASSWORD_PATH = Path("/run/sip-bridge/ari-password")


@dataclass
class StasisEvent:
    kind: str  # "start" | "end"
    channel_id: str
    channel_name: str = ""


def parse_stasis_event(event: dict) -> Optional[StasisEvent]:
    """Turn a raw ARI WS event dict into a StasisEvent, or None if irrelevant."""
    etype = event.get("type")
    if etype not in ("StasisStart", "StasisEnd"):
        return None
    channel = event.get("channel") or {}
    return StasisEvent(
        kind="start" if etype == "StasisStart" else "end",
        channel_id=channel.get("id", ""),
        channel_name=channel.get("name", ""),
    )


class AudioPump:
    """Bidirectional audio bridge between Asterisk externalMedia UDP and pymumble.

    Asterisk's externalMedia transport=udp wraps each audio frame in an RTP
    packet: 12-byte RTP header + slin16 payload. On uplink we strip the
    header; on downlink we prepend one so Asterisk accepts the frame.

    Uplink (caller → Mumble): recv RTP → strip header → slin16 → upsample
    → pymumble.sound_output.add_sound.
    Downlink (Mumble → caller): pymumble received sound → downsample →
    prepend RTP header → sendto UDP.

    Called on a 20 ms tick loop from a thread. pymumble's API is blocking
    so we don't fight it; no asyncio here.
    """
    SLIN16_FRAME_BYTES = 640  # 20 ms @ 16 kHz mono int16
    RTP_HEADER_BYTES = 12
    RTP_PAYLOAD_TYPE = 118  # dynamic PT used by Asterisk for slin16
    # VAD: if the caller's 20 ms frame has max |sample| below this,
    # treat it as silence and skip pushing to Mumble. Stops PTTPhone
    # from holding the Mumble channel during pauses between words —
    # without this the half-duplex P50 UX can't transmit back.
    VAD_THRESHOLD = 500  # out of 32768 (≈ -36 dBFS)

    def __init__(self, udp_sock, udp_peer, mumble):
        self._sock = udp_sock
        self._peer = udp_peer
        self._mumble = mumble
        self._sock.setblocking(False)
        # RTP send state (downlink). _pt is learned from the first
        # inbound packet so we send with the same payload type Asterisk
        # uses on this session (the dynamic PT can vary per session).
        self._seq = 0
        self._ts = 0
        self._ssrc = int.from_bytes(os.urandom(4), "big")
        self._pt = self.RTP_PAYLOAD_TYPE  # overridden on first inbound RTP
        # Downlink callback queue: pymumble calls enqueue_mumble_frame
        # from its own thread whenever a user in our channel speaks;
        # step_downlink drains one chunk per 20 ms tick. Avoids the
        # user.sound.is_sound() polling path which never sees audio
        # even though the SOUNDRECEIVED callback fires (confirmed on
        # first deploy of Task 8 downlink).
        from collections import deque
        self._rx_queue: deque = deque(maxlen=50)  # ~1 s of 20 ms chunks

    MUMBLE_CHUNK_BYTES = 1920  # 20 ms @ 48 kHz mono int16

    def enqueue_mumble_frame(self, user_name: str, pcm48k: bytes) -> None:
        """Called from pymumble's callback thread. Splits the incoming
        PCM into 20 ms sub-chunks so step_downlink can send exactly one
        20 ms RTP packet per tick. Mumble commonly hands us 40 ms frames
        (3840 bytes); sending those as one packet would give a 25 fps
        downlink while the RTP timestamp math pretends 50 fps, which the
        far-end jitter buffer discards as malformed.
        """
        if not pcm48k:
            return
        for i in range(0, len(pcm48k) - self.MUMBLE_CHUNK_BYTES + 1,
                       self.MUMBLE_CHUNK_BYTES):
            self._rx_queue.append((user_name, pcm48k[i : i + self.MUMBLE_CHUNK_BYTES]))

    def _build_rtp(self, payload: bytes) -> bytes:
        """Wrap a slin16 payload in a minimal RTP header."""
        import struct
        header = struct.pack(
            "!BBHII",
            0x80,                       # V=2, P=0, X=0, CC=0
            self._pt & 0x7F,            # M=0, PT=learned from inbound
            self._seq & 0xFFFF,
            self._ts & 0xFFFFFFFF,
            self._ssrc,
        )
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + 320) & 0xFFFFFFFF  # 320 samples per 20 ms @ 16 kHz
        return header + payload

    def step_uplink(self) -> None:
        """Pull one RTP frame from Asterisk if available, push slin16 → Mumble.

        Latches the UDP peer address on the first received frame so
        step_downlink knows where to send back.
        """
        try:
            data, addr = self._sock.recvfrom(4096)
        except (BlockingIOError, socket.timeout):
            return
        if self._peer == ("127.0.0.1", 0):
            self._peer = addr
            # Learn Asterisk's payload type from byte 1 of the RTP header
            # (top bit is the marker, bottom 7 bits are PT). Mirroring it
            # on our outbound packets avoids Asterisk dropping them as
            # "unexpected PT".
            if len(data) >= 2:
                self._pt = data[1] & 0x7F
            LOG.info("latched UDP peer to %s, learned PT=%d", addr, self._pt)
        # Expect 12B RTP header + 640B slin16 payload = 652B.
        if len(data) < self.RTP_HEADER_BYTES + self.SLIN16_FRAME_BYTES:
            return  # partial frame (seen at call start/end)
        payload = data[self.RTP_HEADER_BYTES : self.RTP_HEADER_BYTES + self.SLIN16_FRAME_BYTES]
        # RTP L16/slin16 is big-endian (RFC 3551). numpy default is
        # native (little-endian on x86), so we explicitly read as '>i2'.
        samples = np.frombuffer(payload, dtype=">i2")
        if int(np.abs(samples).max()) < self.VAD_THRESHOLD:
            return  # silent frame — don't transmit into Mumble
        payload_le = samples.astype("<i2").tobytes()
        pcm48k = upsample_16_to_48(payload_le)
        try:
            self._mumble.sound_output.add_sound(pcm48k)
        except Exception as e:
            LOG.warning("mumble add_sound failed: %s", e)

    def step_downlink(self) -> None:
        """Drain one 20 ms chunk of received Mumble audio, send to Asterisk.

        Phase 2b-audio scope: one caller, one shared Phone channel. If
        multiple Mumble users speak at once we take the first user's frame
        — naive "last chunk wins" is acceptable for v1 (Phase 2c revisits
        mixing + mute).

        Requires self._peer to be latched (step_uplink sets it on first
        received frame). If the peer hasn't been discovered yet, we skip —
        Asterisk isn't sending to us, so we have nothing to reply to.
        """
        if self._peer == ("127.0.0.1", 0):
            return

        try:
            speaker_name, frame_pcm = self._rx_queue.popleft()
        except IndexError:
            return

        # Throttled log: once per second of downlink activity
        if not hasattr(self, "_dl_count"):
            self._dl_count = 0
            self._dl_last_log = time.monotonic()
        self._dl_count += 1
        now = time.monotonic()
        if now - self._dl_last_log > 1.0:
            LOG.info("downlink: %d frames from Mumble (speaker=%s) in last %.1fs",
                     self._dl_count, speaker_name, now - self._dl_last_log)
            self._dl_count = 0
            self._dl_last_log = now

        slin16_le = downsample_48_to_16(frame_pcm)
        # Asterisk expects RTP L16 in big-endian (RFC 3551).
        slin16_be = np.frombuffer(slin16_le, dtype="<i2").astype(">i2").tobytes()
        packet = self._build_rtp(slin16_be)
        try:
            self._sock.sendto(packet, self._peer)
        except OSError as e:
            LOG.warning("UDP send failed: %s", e)


def _load_ari_password() -> str:
    pw = os.environ.get("ARI_PASSWORD")
    if pw:
        return pw
    if ARI_PASSWORD_PATH.exists():
        return ARI_PASSWORD_PATH.read_text().strip()
    LOG.error("no ARI_PASSWORD env or %s", ARI_PASSWORD_PATH)
    sys.exit(1)


async def spawn_externalmedia(sess: aiohttp.ClientSession, channel_id: str, mumble) -> Optional[AudioPump]:
    """On StasisStart, bind a UDP socket and ask Asterisk to send call
    audio to it as slin16. Returns an AudioPump bound to that socket,
    plus a background thread driving step_uplink + step_downlink on a
    20 ms tick.
    """
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp_port = udp.getsockname()[1]
    LOG.info("bound externalMedia UDP port %d for channel %s", udp_port, channel_id)

    auth = aiohttp.BasicAuth(ARI_USER, _load_ari_password())
    r = await sess.post(
        f"http://{ARI_HOST}:{ARI_PORT}/ari/channels/externalMedia",
        params={
            "app": ARI_APP,
            "external_host": f"127.0.0.1:{udp_port}",
            "format": "slin16",
            "transport": "udp",
        },
        auth=auth,
    )
    if r.status >= 400:
        body = await r.text()
        LOG.error("externalMedia POST failed: %d %s", r.status, body)
        udp.close()
        return None
    body = await r.json()
    external_channel_id = body.get("id")
    LOG.info("externalMedia created: id=%s", external_channel_id)

    # Asterisk's externalMedia channel is isolated — it does not auto-bridge
    # with the caller channel. Create a mixing bridge and put both channels
    # in it so RTP flows from caller → externalMedia → our UDP socket.
    bridge_r = await sess.post(
        f"http://{ARI_HOST}:{ARI_PORT}/ari/bridges",
        params={"type": "mixing", "name": f"bridge-{channel_id}"},
        auth=auth,
    )
    if bridge_r.status >= 400:
        LOG.error("bridge create failed: %d %s", bridge_r.status, await bridge_r.text())
        udp.close()
        return None
    bridge_id = (await bridge_r.json()).get("id")
    LOG.info("mixing bridge created: id=%s", bridge_id)

    for chan_to_add in (channel_id, external_channel_id):
        add_r = await sess.post(
            f"http://{ARI_HOST}:{ARI_PORT}/ari/bridges/{bridge_id}/addChannel",
            params={"channel": chan_to_add},
            auth=auth,
        )
        if add_r.status >= 400:
            LOG.error("addChannel failed for %s: %d %s",
                      chan_to_add, add_r.status, await add_r.text())
            udp.close()
            return None
    LOG.info("bridge %s wired: SIP=%s externalMedia=%s", bridge_id, channel_id, external_channel_id)

    pump = AudioPump(udp_sock=udp, udp_peer=("127.0.0.1", 0), mumble=mumble)
    # Wire pymumble's SOUNDRECEIVED callback to this pump's rx_queue.
    # The callback in open_mumble reads mm.current_pump on every frame.
    mumble.current_pump = pump

    def _pump_loop():
        frame_count = 0
        last_log = time.monotonic()
        while True:
            pump.step_uplink()
            pump.step_downlink()
            frame_count += 1
            now = time.monotonic()
            if now - last_log > 1.0:
                LOG.info("pump: %d ticks in last %.1fs", frame_count, now - last_log)
                frame_count = 0
                last_log = now
            time.sleep(0.02)

    threading.Thread(target=_pump_loop, daemon=True, name=f"audio-pump-{channel_id}").start()
    return pump


def open_mumble(host: str, port: int) -> "object":
    """Open a pymumble connection as 'PTTPhone', ensure Phone channel
    exists, and move into it. Returns the live Mumble client.

    Imported lazily so local pytest runs (where pymumble isn't installed)
    still work — pymumble only runs inside the sip-bridge container.
    """
    # Compat shim: pymumble 1.6.1 calls ssl.wrap_socket(), which was
    # removed in Python 3.12. Ubuntu 24.04 (our base image) ships 3.12.
    # Re-implement with SSLContext before importing pymumble so its
    # module-top `import ssl; ssl.wrap_socket(...)` call resolves.
    import ssl as _ssl
    if not hasattr(_ssl, "wrap_socket"):
        def _compat_wrap_socket(sock, certfile=None, keyfile=None,
                                ssl_version=None, **_kw):
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
    mm.set_application_string("openPTT SIP Bridge")
    mm.set_receive_sound(True)

    # Downlink feed: pymumble fires SOUNDRECEIVED from its own thread for
    # every decoded user audio chunk. Poll-based drain via user.sound.is_sound
    # never saw audio (confirmed empirically), so we push to the active
    # AudioPump's rx_queue directly. spawn_externalmedia attaches the pump
    # as mm.current_pump; if no call is active, frames are dropped on the
    # floor (no consumer, no accumulation).
    mm.current_pump = None
    _rx_state = {"count": 0, "last_log": time.monotonic(), "speaker": None}
    def _on_sound(user, sound_chunk):
        _rx_state["count"] += 1
        _rx_state["speaker"] = user.get("name", "?")
        pump = getattr(mm, "current_pump", None)
        if pump is not None:
            pump.enqueue_mumble_frame(user.get("name", "?"), sound_chunk.pcm)
        now = time.monotonic()
        if now - _rx_state["last_log"] > 1.0:
            LOG.info("mumble rx: %d frames from %s in last %.1fs (pump=%s)",
                     _rx_state["count"], _rx_state["speaker"],
                     now - _rx_state["last_log"],
                     "active" if pump else "idle")
            _rx_state["count"] = 0
            _rx_state["last_log"] = now
    mm.callbacks.set_callback(const.PYMUMBLE_CLBK_SOUNDRECEIVED, _on_sound)

    mm.start()
    mm.is_ready()
    time.sleep(1)  # pymumble needs a moment for the channel list to populate

    LOG.info("mumble connected; visible channels: %s",
             {cid: c.get("name") for cid, c in mm.channels.items()})

    def _find_phone():
        for cid, chan in mm.channels.items():
            if chan.get("name") == "Phone":
                return cid
        return None

    # The admin container's render_entry.ensure_phone_channel() creates
    # Phone before sip-bridge starts up, but on a cold boot it may still
    # be propagating when we arrive. PTTPhone itself cannot create
    # channels (unregistered user, no ACL grant), so just wait for it.
    phone_id = _find_phone()
    for attempt in range(20):
        if phone_id is not None:
            break
        time.sleep(0.3)
        phone_id = _find_phone()
    if phone_id is None:
        LOG.error("Phone channel never appeared; channels seen: %s",
                  {cid: c.get("name") for cid, c in mm.channels.items()})
        raise RuntimeError("Phone channel unavailable — admin must create it")

    mm.users.myself.move_in(phone_id)
    time.sleep(0.2)
    LOG.info("PTTPhone joined Phone channel (id=%d)", phone_id)
    return mm


async def run() -> None:
    password = _load_ari_password()
    ws_url = f"ws://{ARI_HOST}:{ARI_PORT}/ari/events?app={ARI_APP}&api_key={ARI_USER}:{password}"
    LOG.info("connecting to ARI: ws://%s:%d (app=%s)", ARI_HOST, ARI_PORT, ARI_APP)

    # Open pymumble PTTPhone connection. This stays up across calls.
    mumble_host = os.environ.get("MUMBLE_HOST", "127.0.0.1")
    mumble_port = int(os.environ.get("MUMBLE_PORT", "64738"))
    LOG.info("connecting to Mumble at %s:%d as PTTPhone", mumble_host, mumble_port)
    # open_mumble is blocking (pymumble.start + is_ready + move_in) — run it
    # in a thread so the asyncio event loop isn't blocked. We won't touch it
    # from async code directly; the pump thread is the only other consumer.
    mumble = await asyncio.to_thread(open_mumble, mumble_host, mumble_port)

    async with aiohttp.ClientSession() as sess:
        # Retry until Asterisk's HTTP server is up (it starts a few seconds after supervisord).
        for attempt in range(30):
            try:
                ws = await sess.ws_connect(ws_url)
                break
            except aiohttp.ClientError as e:
                LOG.warning("ARI not ready (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(2)
        else:
            LOG.error("gave up connecting to ARI")
            return

        LOG.info("ARI WebSocket connected")
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                event = json.loads(msg.data)
                parsed = parse_stasis_event(event)
                if parsed is None:
                    continue
                LOG.info("Stasis %s: channel=%s (%s)", parsed.kind, parsed.channel_id, parsed.channel_name)
                if parsed.kind == "start":
                    # Asterisk's externalMedia creates a UnicastRTP helper
                    # channel that ALSO enters our Stasis app. Spawning
                    # externalMedia for that helper recursively creates
                    # more helpers — infinite loop. Only act on real SIP
                    # channels (PJSIP/*), ignore everything else.
                    if not parsed.channel_name.startswith("PJSIP/"):
                        LOG.debug("ignoring non-PJSIP StasisStart: %s", parsed.channel_name)
                        continue
                    pump = await spawn_externalmedia(sess, parsed.channel_id, mumble)
                    if pump is None:
                        LOG.error("externalMedia spawn failed for %s; channel will hear silence", parsed.channel_id)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                LOG.warning("ARI WebSocket closed/errored: %s", msg)
                break


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        LOG.info("shutdown requested")


if __name__ == "__main__":
    main()

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

from sip_bridge.audio import upsample_16_to_48

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

    Uplink (caller → Mumble): recv UDP slin16 → upsample → pymumble.sound_output.add_sound
    Downlink (Mumble → caller): wired in Task 8 (step_downlink will land then).

    This class holds no asyncio — it's called on a 20 ms tick loop from
    a thread. pymumble's API is blocking so we don't fight it.
    """
    SLIN16_FRAME_BYTES = 640  # 20 ms @ 16 kHz mono int16

    def __init__(self, udp_sock, udp_peer, mumble):
        self._sock = udp_sock
        self._peer = udp_peer
        self._mumble = mumble
        self._sock.setblocking(False)

    def step_uplink(self) -> None:
        """Pull one frame from Asterisk if available, push to Mumble.

        Latches the UDP peer address on the first received frame so a
        future step_downlink (Task 8) knows where to send back.
        """
        try:
            data, addr = self._sock.recvfrom(4096)
        except (BlockingIOError, socket.timeout):
            return
        if self._peer == ("127.0.0.1", 0):
            self._peer = addr
            LOG.info("latched UDP peer to %s", addr)
        if len(data) != self.SLIN16_FRAME_BYTES:
            return  # skip malformed / partial frames (common at call start/end)
        pcm48k = upsample_16_to_48(data)
        try:
            self._mumble.sound_output.add_sound(pcm48k)
        except Exception as e:
            LOG.warning("mumble add_sound failed: %s", e)


def _load_ari_password() -> str:
    pw = os.environ.get("ARI_PASSWORD")
    if pw:
        return pw
    if ARI_PASSWORD_PATH.exists():
        return ARI_PASSWORD_PATH.read_text().strip()
    LOG.error("no ARI_PASSWORD env or %s", ARI_PASSWORD_PATH)
    sys.exit(1)


async def spawn_externalmedia(sess: aiohttp.ClientSession, channel_id: str) -> Optional[AudioPump]:
    """On StasisStart, bind a UDP socket and ask Asterisk to send call
    audio to it as slin16. Returns an AudioPump bound to that socket,
    plus a background thread driving step_uplink on a 20 ms tick.

    Phase 2b-audio scope: uplink only, pymumble is mocked.
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
    LOG.info("externalMedia created: id=%s", body.get("id"))

    # Stub pymumble — a mock object that just logs. Real pymumble in Task 8.
    class _MockMumble:
        class _SoundOutput:
            def add_sound(self, pcm: bytes) -> None:
                LOG.debug("mock mumble: would forward %d bytes (48 kHz)", len(pcm))
        sound_output = _SoundOutput()

    pump = AudioPump(udp_sock=udp, udp_peer=("127.0.0.1", 0), mumble=_MockMumble())

    def _pump_loop():
        frame_count = 0
        last_log = time.monotonic()
        while True:
            pump.step_uplink()
            frame_count += 1
            now = time.monotonic()
            if now - last_log > 1.0:
                LOG.info("uplink pump: %d ticks in last %.1fs", frame_count, now - last_log)
                frame_count = 0
                last_log = now
            time.sleep(0.02)

    threading.Thread(target=_pump_loop, daemon=True, name=f"audio-pump-{channel_id}").start()
    return pump


async def run() -> None:
    password = _load_ari_password()
    ws_url = f"ws://{ARI_HOST}:{ARI_PORT}/ari/events?app={ARI_APP}&api_key={ARI_USER}:{password}"
    LOG.info("connecting to ARI: ws://%s:%d (app=%s)", ARI_HOST, ARI_PORT, ARI_APP)

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
                    pump = await spawn_externalmedia(sess, parsed.channel_id)
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

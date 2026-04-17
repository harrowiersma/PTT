"""ARI bridge — Asterisk external app that handles inbound calls in
the Stasis(openptt-bridge) context.

Phase 2b-audio scope: single concurrent call, bidirectional audio into
the Mumble Phone channel. No sub-channels, no mute, no notifications.

Architecture is documented in docs/plans/2026-04-17-sip-bridge-phase-2b-audio-design.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp

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


def _load_ari_password() -> str:
    pw = os.environ.get("ARI_PASSWORD")
    if pw:
        return pw
    if ARI_PASSWORD_PATH.exists():
        return ARI_PASSWORD_PATH.read_text().strip()
    LOG.error("no ARI_PASSWORD env or %s", ARI_PASSWORD_PATH)
    sys.exit(1)


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
                import json
                event = json.loads(msg.data)
                parsed = parse_stasis_event(event)
                if parsed is None:
                    continue
                LOG.info("Stasis %s: channel=%s (%s)", parsed.kind, parsed.channel_id, parsed.channel_name)
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

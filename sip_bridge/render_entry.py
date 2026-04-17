# sip_bridge/render_entry.py
"""Entrypoint-time orchestrator.

Pulls trunk config from the admin API, renders pjsip.conf and ari.conf,
drops them in /etc/asterisk/, fetches the greeting WAV, and exits. The
shell entrypoint then hands off to supervisord.

Kept as a separate script (not inlined in bash) so the template rendering
lives next to its unit tests.
"""
from __future__ import annotations

import logging
import os
import secrets
import sys
import time
from pathlib import Path

import httpx

from sip_bridge.render_config import render_pjsip_conf

LOG = logging.getLogger("render-entry")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ADMIN = os.environ.get("ADMIN_INTERNAL_URL", "http://127.0.0.1:8000")
SECRET = os.environ.get("PTT_INTERNAL_API_SECRET", "").strip()
GREETING_TEXT = os.environ.get(
    "GREETING_TEXT",
    "You are now being connected to the openPTT radio trunk system. "
    "Please note that there may be small delays between transmissions.",
)

CONFIG_DIR = Path("/etc/asterisk")
# Asterisk on Ubuntu 24.04 reports its Data directory as
# /usr/share/asterisk (the compile-time astdatadir), not the
# astvarlibdir in asterisk.conf. Playback() searches under the data
# directory's sounds/<language>/ tree, so the greeting must live there.
SOUNDS_DIR = Path("/usr/share/asterisk/sounds/en")
GREETING_PATH = SOUNDS_DIR / "openptt-greeting.wav"
ARI_PASSWORD_PATH = Path("/run/sip-bridge/ari-password")


def fetch_trunk() -> dict:
    if not SECRET:
        LOG.error("PTT_INTERNAL_API_SECRET not set")
        sys.exit(1)
    headers = {"X-Internal-Auth": SECRET}
    for attempt in range(30):
        try:
            with httpx.Client(timeout=5, headers=headers) as c:
                r = c.get(f"{ADMIN}/api/sip/internal/config/trunks")
                r.raise_for_status()
                trunks = [t for t in r.json() if t.get("enabled")]
                if not trunks:
                    LOG.error("No enabled trunks in admin")
                    sys.exit(1)
                return trunks[0]  # Phase 2b: single trunk
        except Exception as e:
            LOG.warning("admin not ready (attempt %d): %s", attempt + 1, e)
            time.sleep(2)
    LOG.error("gave up waiting for admin")
    sys.exit(1)


def render_and_write(trunk: dict) -> None:
    pjsip = render_pjsip_conf(trunk)
    (CONFIG_DIR / "pjsip.conf").write_text(pjsip)
    LOG.info("wrote pjsip.conf (%d bytes)", len(pjsip))

    # ari.conf with a random password if the env var wasn't set.
    pw = os.environ.get("ARI_PASSWORD") or secrets.token_urlsafe(24)
    ARI_PASSWORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARI_PASSWORD_PATH.write_text(pw)
    tmpl = (Path(__file__).parent / "configs" / "ari.conf.tmpl").read_text()
    (CONFIG_DIR / "ari.conf").write_text(tmpl.format(ari_password=pw))
    LOG.info("wrote ari.conf (password in %s)", ARI_PASSWORD_PATH)


def _resample_wav_to_8k(wav_bytes: bytes) -> bytes:
    """Downsample a WAV of any rate to 8 kHz 16-bit mono WAV.

    Asterisk's format_wav only reliably plays 8 kHz (and 16 kHz via .wav16)
    via Playback(). The admin's TTS endpoint returns 48 kHz — resample here
    so Asterisk can read the file without format errors.
    """
    import io
    import wave

    import numpy as np

    with wave.open(io.BytesIO(wav_bytes), "rb") as r:
        src_rate = r.getframerate()
        n_channels = r.getnchannels()
        sampwidth = r.getsampwidth()
        frames = r.readframes(r.getnframes())

    if n_channels != 1 or sampwidth != 2:
        LOG.warning("unexpected WAV format (channels=%d sampwidth=%d); using as-is",
                    n_channels, sampwidth)
        return wav_bytes

    if src_rate == 8000:
        return wav_bytes

    src = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
    tgt_n = int(len(src) * 8000 / src_rate)
    src_idx = np.arange(len(src), dtype=np.float32)
    tgt_idx = np.linspace(0, len(src) - 1, tgt_n, dtype=np.float32)
    tgt = np.interp(tgt_idx, src_idx, src)
    tgt = np.clip(tgt, -32768, 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(tgt.tobytes())
    LOG.info("greeting resampled %d Hz -> 8000 Hz (%d -> %d samples)",
             src_rate, len(src), tgt_n)
    return buf.getvalue()


def fetch_greeting() -> None:
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    if GREETING_PATH.exists():
        LOG.info("greeting already cached at %s", GREETING_PATH)
        return
    try:
        with httpx.Client(timeout=60) as c:
            r = c.post(
                f"{ADMIN}/api/sip/internal/tts",
                headers={"X-Internal-Auth": SECRET},
                json={"text": GREETING_TEXT},
            )
            if r.status_code == 200 and r.content:
                resampled = _resample_wav_to_8k(r.content)
                GREETING_PATH.write_bytes(resampled)
                LOG.info("greeting cached (%d bytes)", len(resampled))
                return
            LOG.warning("admin TTS returned %d; using fallback tones", r.status_code)
    except Exception as e:
        LOG.warning("admin TTS unreachable (%s); using fallback tones", e)

    # Three-tone fallback (same pattern as old bridge.py)
    import wave

    import numpy as np

    sr = 8000
    def tone(f: float, ms: int, amp: float = 0.25) -> np.ndarray:
        n = int(sr * ms / 1000)
        t = np.arange(n, dtype=np.float32) / sr
        return (np.sin(2 * np.pi * f * t) * amp * 32767).astype(np.int16)
    def silence(ms: int) -> np.ndarray:
        return np.zeros(int(sr * ms / 1000), dtype=np.int16)

    data = np.concatenate([
        tone(800, 300), silence(150),
        tone(600, 300), silence(150),
        tone(400, 400), silence(500),
    ])
    with wave.open(str(GREETING_PATH), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())
    LOG.info("fallback tone WAV written (%d frames @ %d Hz)", len(data), sr)


def ensure_phone_channel() -> None:
    """Ask admin to create the Mumble 'Phone' channel if it doesn't exist.

    PTTPhone (the sip-bridge's Mumble identity) is unregistered and cannot
    create channels itself on Murmur's default ACL — PTTAdmin can, so we
    delegate creation to the admin container's internal endpoint. Safe to
    call repeatedly; the endpoint returns the existing id if present.
    """
    if not SECRET:
        LOG.warning("no internal secret; cannot ensure Phone channel")
        return
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(
                f"{ADMIN}/api/sip/internal/ensure-phone-channel",
                headers={"X-Internal-Auth": SECRET},
            )
            if r.status_code == 200:
                body = r.json()
                LOG.info("Phone channel ready: id=%s created=%s",
                         body.get("channel_id"), body.get("created"))
                return
            LOG.warning("ensure-phone-channel returned %d: %s", r.status_code, r.text)
    except Exception as e:
        LOG.warning("ensure-phone-channel failed (%s); PTTPhone will retry in open_mumble", e)


def main() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    trunk = fetch_trunk()
    render_and_write(trunk)
    fetch_greeting()
    ensure_phone_channel()
    LOG.info("entrypoint rendering complete")


if __name__ == "__main__":
    main()

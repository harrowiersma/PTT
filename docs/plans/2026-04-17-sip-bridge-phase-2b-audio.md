# SIP Bridge Phase 2b-audio Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the pjsua2-based SIP bridge with Asterisk + ARI externalMedia + Python/pymumble, delivering bidirectional audio between a single DIDWW caller and the shared Mumble `Phone` channel.

**Architecture:** One container running Asterisk 20 (apt-installed on Debian bookworm) and a Python ARI client under `supervisord`. Asterisk handles SIP registration, answers the INVITE, plays the Piper greeting via `Playback`, then hands the call to the Stasis app. The Python app bridges Asterisk's externalMedia UDP socket (slin16 @ 16 kHz) to a pymumble `PTTPhone` connection (48 kHz), resampling with `np.interp` on a 20 ms tick.

**Tech Stack:** Asterisk 20 (apt), asterisk-modules (`chan_pjsip`, `res_ari`, `res_ari_channels`, `app_playback`, `app_stasis`), Python 3.11, `aiohttp`, `numpy`, `pymumble_py3`, `supervisord`, Debian bookworm-slim.

**Design doc:** [docs/plans/2026-04-17-sip-bridge-phase-2b-audio-design.md](2026-04-17-sip-bridge-phase-2b-audio-design.md)

---

## Pre-flight

**Working directory:** `/Users/harrowiersma/Documents/CLAUDE/PTT`
**Server:** `ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch`, Docker Compose at `/opt/ptt`
**Test DID:** +351300500404 (DIDWW Amsterdam, trunk already registered and live)
**Current state on `main`:** Phase 2b-initial shipped (commit 8c9c6f4 is the design doc on top).

The whole rewrite happens inside `sip_bridge/`; the admin container, `server/api/sip.py`, `docker-compose.yml`, and Mumble stay untouched until the final deploy task.

---

## Task 1: Pure-Python audio resample helpers (TDD)

The resample + pump math is the one piece of Phase 2b-audio we can fully unit-test without Asterisk or Mumble. Ship it first.

**Files:**
- Create: `sip_bridge/audio.py`
- Create: `tests/sip_bridge/__init__.py` (empty)
- Test: `tests/sip_bridge/test_audio.py`

**Step 1: Write the failing tests**

```python
# tests/sip_bridge/test_audio.py
"""Unit tests for sip-bridge resample helpers.

Asterisk externalMedia delivers slin16 @ 16 kHz in 20 ms frames (640 bytes
= 320 int16 samples). Mumble / pymumble wants 48 kHz in 20 ms frames
(1920 bytes = 960 int16 samples). These helpers convert between the two.

Pure-Python, no network or audio hardware required — pins the contract
the audio pump depends on.
"""
from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from sip_bridge.audio import downsample_48_to_16, upsample_16_to_48


def _sine_pcm(freq_hz: float, sample_rate: int, duration_ms: int, amp: float = 0.5) -> bytes:
    """Build an int16 mono PCM sine wave of given length and frequency."""
    n = int(sample_rate * duration_ms / 1000)
    t = np.arange(n, dtype=np.float32) / sample_rate
    wave = (np.sin(2 * np.pi * freq_hz * t) * amp * 32767).astype(np.int16)
    return wave.tobytes()


def _dominant_freq(pcm: bytes, sample_rate: int) -> float:
    """Estimate dominant frequency via FFT. Used to verify resample preserved pitch."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(samples) < 2:
        return 0.0
    spectrum = np.fft.rfft(samples * np.hanning(len(samples)))
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / sample_rate)
    return float(freqs[int(np.argmax(np.abs(spectrum)))])


class TestUpsample16to48:
    def test_frame_length_20ms(self):
        """640-byte 16 kHz frame → 1920-byte 48 kHz frame (exactly one pymumble chunk)."""
        pcm_16k = _sine_pcm(440, 16000, 20)
        assert len(pcm_16k) == 640

        pcm_48k = upsample_16_to_48(pcm_16k)

        assert len(pcm_48k) == 1920

    def test_preserves_tone_frequency(self):
        """A 440 Hz tone at 16 kHz should remain 440 Hz after upsample to 48 kHz."""
        pcm_16k = _sine_pcm(440, 16000, 200)

        pcm_48k = upsample_16_to_48(pcm_16k)

        detected = _dominant_freq(pcm_48k, 48000)
        assert abs(detected - 440) < 10, f"Expected ~440 Hz, got {detected}"

    def test_silence_in_silence_out(self):
        pcm_16k = b"\x00" * 640

        pcm_48k = upsample_16_to_48(pcm_16k)

        assert pcm_48k == b"\x00" * 1920


class TestDownsample48to16:
    def test_frame_length_20ms(self):
        """1920-byte 48 kHz frame → 640-byte 16 kHz frame (exactly one externalMedia frame)."""
        pcm_48k = _sine_pcm(440, 48000, 20)
        assert len(pcm_48k) == 1920

        pcm_16k = downsample_48_to_16(pcm_48k)

        assert len(pcm_16k) == 640

    def test_preserves_tone_frequency(self):
        pcm_48k = _sine_pcm(440, 48000, 200)

        pcm_16k = downsample_48_to_16(pcm_48k)

        detected = _dominant_freq(pcm_16k, 16000)
        assert abs(detected - 440) < 10, f"Expected ~440 Hz, got {detected}"

    def test_silence_in_silence_out(self):
        pcm_48k = b"\x00" * 1920

        pcm_16k = downsample_48_to_16(pcm_48k)

        assert pcm_16k == b"\x00" * 640


class TestRoundTrip:
    def test_16_to_48_to_16_recovers_tone(self):
        """Round-trip: 16kHz sine → 48kHz → back to 16kHz should still sound like 440 Hz."""
        original = _sine_pcm(440, 16000, 200)

        up = upsample_16_to_48(original)
        down = downsample_48_to_16(up)

        assert len(down) == len(original)
        detected = _dominant_freq(down, 16000)
        assert abs(detected - 440) < 10
```

**Step 2: Verify the tests fail**

Run: `cd /Users/harrowiersma/Documents/CLAUDE/PTT && pip install -e sip_bridge/ 2>/dev/null; PYTHONPATH=. pytest tests/sip_bridge/test_audio.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sip_bridge.audio'`

**Step 3: Minimal implementation**

```python
# sip_bridge/audio.py
"""Audio helpers for the SIP↔Mumble bridge.

Pure functions, no state. Linear-interpolation resample between the two
fixed sample rates the bridge cares about — 16 kHz (Asterisk externalMedia
slin16) and 48 kHz (Mumble/pymumble). Frame sizes are assumed to be 20 ms
aligned; passing something else returns a proportionally-sized buffer.

The same np.interp trick is used by server/weather_bot.py:212 for
Piper output — pick it up there if you want to understand the shape.
"""
from __future__ import annotations

import numpy as np


def upsample_16_to_48(pcm_16k: bytes) -> bytes:
    """Linear-interp upsample int16 mono PCM from 16 kHz to 48 kHz."""
    if not pcm_16k:
        return b""
    src = np.frombuffer(pcm_16k, dtype=np.int16).astype(np.float32)
    target_n = len(src) * 3
    src_idx = np.arange(len(src), dtype=np.float32)
    tgt_idx = np.linspace(0, len(src) - 1, target_n, dtype=np.float32)
    tgt = np.interp(tgt_idx, src_idx, src)
    return np.clip(tgt, -32768, 32767).astype(np.int16).tobytes()


def downsample_48_to_16(pcm_48k: bytes) -> bytes:
    """Linear-interp downsample int16 mono PCM from 48 kHz to 16 kHz.

    Warning: this is an anti-aliasing-free resample. For the 300-3400 Hz
    band that G.711 telephony cares about it's fine (well below Nyquist
    at 16 kHz), but do not reuse for wideband audio without a low-pass.
    """
    if not pcm_48k:
        return b""
    src = np.frombuffer(pcm_48k, dtype=np.int16).astype(np.float32)
    target_n = len(src) // 3
    src_idx = np.arange(len(src), dtype=np.float32)
    tgt_idx = np.linspace(0, len(src) - 1, target_n, dtype=np.float32)
    tgt = np.interp(tgt_idx, src_idx, src)
    return np.clip(tgt, -32768, 32767).astype(np.int16).tobytes()
```

Also create `sip_bridge/__init__.py` (empty) so the tests can import `sip_bridge.audio`.

**Step 4: Verify the tests pass**

Run: `PYTHONPATH=. pytest tests/sip_bridge/test_audio.py -v`
Expected: all 7 tests PASS.

**Step 5: Commit**

```bash
git add sip_bridge/__init__.py sip_bridge/audio.py tests/sip_bridge/__init__.py tests/sip_bridge/test_audio.py
git commit -m "sip-bridge: add audio resample helpers (TDD)"
```

---

## Task 2: Config renderer (TDD — pure function, mocked admin response)

Asterisk gets its PJSIP credentials from the same `/api/sip/internal/config/trunks` endpoint the current bridge uses. A template renderer converts the JSON into `pjsip.conf`. Pure function, easy to test.

**Files:**
- Create: `sip_bridge/render_config.py`
- Create: `sip_bridge/templates/pjsip.conf.tmpl`
- Test: `tests/sip_bridge/test_render_config.py`

**Step 1: Write the failing test**

```python
# tests/sip_bridge/test_render_config.py
"""Template-rendering unit tests. No network, no file I/O — feed in
a trunk dict, get back the pjsip.conf string."""
from __future__ import annotations

import pytest

from sip_bridge.render_config import render_pjsip_conf


TRUNK_WITH_AUTH = {
    "id": 1,
    "label": "DIDWW Amsterdam",
    "sip_host": "ams.sip.didww.com",
    "sip_port": 5060,
    "sip_user": "userid123",
    "sip_password": "secretpw",
    "from_uri": "sip:userid123@ams.sip.didww.com",
    "transport": "udp",
    "registration_interval_s": 3600,
    "enabled": True,
}


def test_renders_required_sections():
    conf = render_pjsip_conf(TRUNK_WITH_AUTH)

    assert "[transport-udp]" in conf
    assert "type=transport" in conf
    assert "protocol=udp" in conf
    assert "[didww]" in conf
    assert "type=endpoint" in conf
    assert "[didww-auth]" in conf
    assert "type=auth" in conf
    assert "[didww-aor]" in conf
    assert "type=aor" in conf
    assert "[didww-identify]" in conf
    assert "type=identify" in conf


def test_credentials_interpolated():
    conf = render_pjsip_conf(TRUNK_WITH_AUTH)

    assert "username=userid123" in conf
    assert "password=secretpw" in conf
    assert "ams.sip.didww.com:5060" in conf


def test_context_routes_to_didww_inbound():
    """Inbound calls must land in the extensions.conf [didww-inbound] context."""
    conf = render_pjsip_conf(TRUNK_WITH_AUTH)
    assert "context=didww-inbound" in conf


def test_disabled_trunk_returns_empty():
    """render_pjsip_conf on a disabled trunk returns an empty string —
    the entrypoint uses this to decide whether to start Asterisk at all."""
    trunk = dict(TRUNK_WITH_AUTH, enabled=False)
    assert render_pjsip_conf(trunk) == ""


def test_allows_only_ulaw_alaw_opus():
    """Codec list must match what Asterisk externalMedia supports cleanly."""
    conf = render_pjsip_conf(TRUNK_WITH_AUTH)
    assert "allow=ulaw" in conf
    assert "allow=alaw" in conf
    assert "disallow=all" in conf
```

**Step 2: Verify the tests fail**

Run: `PYTHONPATH=. pytest tests/sip_bridge/test_render_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sip_bridge.render_config'`

**Step 3: Minimal implementation**

```python
# sip_bridge/render_config.py
"""Render Asterisk configs from admin-API trunk data.

The sip-bridge container runs a one-shot renderer at entrypoint time
that pulls /api/sip/internal/config/trunks and writes pjsip.conf. Keeps
the DB as source of truth while letting Asterisk consume a static file.

Only the trunk → pjsip.conf mapping lives here. extensions.conf, ari.conf,
http.conf, and modules.conf are static and shipped as-is; no rendering
needed for those.
"""
from __future__ import annotations

from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def render_pjsip_conf(trunk: dict) -> str:
    """Render pjsip.conf from a single trunk row. Returns '' if disabled."""
    if not trunk.get("enabled"):
        return ""

    tmpl = (_TEMPLATE_DIR / "pjsip.conf.tmpl").read_text()
    return tmpl.format(
        sip_host=trunk["sip_host"],
        sip_port=trunk.get("sip_port") or 5060,
        sip_user=trunk["sip_user"],
        sip_password=trunk["sip_password"],
        transport=trunk.get("transport", "udp"),
    )
```

```
# sip_bridge/templates/pjsip.conf.tmpl
[transport-{transport}]
type=transport
protocol={transport}
bind=0.0.0.0:5060
external_media_address=
external_signaling_address=

[didww]
type=endpoint
context=didww-inbound
disallow=all
allow=ulaw
allow=alaw
outbound_auth=didww-auth
aors=didww-aor
from_user={sip_user}
from_domain={sip_host}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes

[didww-auth]
type=auth
auth_type=userpass
username={sip_user}
password={sip_password}

[didww-aor]
type=aor
contact=sip:{sip_user}@{sip_host}:{sip_port}
qualify_frequency=60

[didww-identify]
type=identify
endpoint=didww
match={sip_host}

[didww-registration]
type=registration
outbound_auth=didww-auth
server_uri=sip:{sip_host}:{sip_port}
client_uri=sip:{sip_user}@{sip_host}
contact_user={sip_user}
retry_interval=60
forbidden_retry_interval=600
expiration=3600
transport=transport-{transport}
```

**Step 4: Verify the tests pass**

Run: `PYTHONPATH=. pytest tests/sip_bridge/test_render_config.py -v`
Expected: all 5 tests PASS.

**Step 5: Commit**

```bash
git add sip_bridge/render_config.py sip_bridge/templates/pjsip.conf.tmpl tests/sip_bridge/test_render_config.py
git commit -m "sip-bridge: add pjsip.conf renderer from admin trunk data"
```

---

## Task 3: Static Asterisk configs

No tests — these are static files Asterisk loads. We verify them in Task 5 by booting the container and checking `asterisk -rx`.

**Files:**
- Create: `sip_bridge/configs/extensions.conf`
- Create: `sip_bridge/configs/ari.conf.tmpl`
- Create: `sip_bridge/configs/http.conf`
- Create: `sip_bridge/configs/modules.conf`
- Create: `sip_bridge/configs/asterisk.conf`
- Create: `sip_bridge/configs/logger.conf`

**Step 1: Write `extensions.conf`**

```
; sip_bridge/configs/extensions.conf
; Inbound call dialplan. One concurrent call — second INVITE gets 486 Busy.
;
; Flow: Answer → Playback(greeting) → Stasis(openptt-bridge) → Hangup
; The Python ARI client takes over in Stasis and runs the audio pump.

[globals]

[didww-inbound]
exten => _X.,1,NoOp(Incoming from ${CALLERID(num)} to ${EXTEN})
 same => n,GotoIf($[${GROUP_COUNT(phone)} > 0]?busy)
 same => n,Set(GROUP()=phone)
 same => n,Answer()
 same => n,Wait(0.3)
 same => n,Playback(openptt-greeting)
 same => n,Stasis(openptt-bridge)
 same => n,Hangup()
 same => n(busy),NoOp(Busy — one call already active)
 same => n,Busy(10)
```

**Step 2: Write `ari.conf.tmpl`**

```
; sip_bridge/configs/ari.conf.tmpl
; Rendered at entrypoint time — the password comes from env var
; ARI_PASSWORD (generated if not set).

[general]
enabled = yes
pretty = yes
allowed_origins = *

[openptt]
type = user
read_only = no
password = {ari_password}
```

**Step 3: Write `http.conf`**

```
; sip_bridge/configs/http.conf
; ARI + external API transport. Loopback only.

[general]
enabled=yes
bindaddr=127.0.0.1
bindport=8088
```

**Step 4: Write `modules.conf`**

```
; sip_bridge/configs/modules.conf
; Explicit load list — keep Asterisk lean. Anything not listed is noload.

[modules]
autoload=no

; Core
load => pbx_config.so
load => func_groupcount.so
load => func_callerid.so
load => func_channel.so
load => func_logic.so
load => func_strings.so

; PJSIP stack
load => res_pjproject.so
load => res_pjsip.so
load => res_pjsip_session.so
load => res_pjsip_authenticator_digest.so
load => res_pjsip_endpoint_identifier_user.so
load => res_pjsip_endpoint_identifier_ip.so
load => res_pjsip_endpoint_identifier_anonymous.so
load => res_pjsip_outbound_registration.so
load => res_pjsip_outbound_authenticator_digest.so
load => res_pjsip_registrar.so
load => res_pjsip_acl.so
load => res_pjsip_sdp_rtp.so
load => res_pjsip_notify.so
load => res_pjsip_caller_id.so
load => res_pjsip_dtmf_info.so
load => res_pjsip_rfc3326.so
load => res_pjsip_dialog_info_body_generator.so
load => chan_pjsip.so

; RTP
load => res_rtp_asterisk.so

; Codecs (ulaw/alaw only; Opus is Mumble-side)
load => codec_ulaw.so
load => codec_alaw.so

; Applications
load => app_stack.so
load => app_playback.so
load => app_dial.so
load => app_verbose.so
load => app_userevent.so
load => app_stasis.so

; File formats (needed by Playback)
load => format_wav.so
load => format_wav_gsm.so
load => format_pcm.so

; Stasis / ARI
load => res_stasis.so
load => res_stasis_answer.so
load => res_stasis_device_state.so
load => res_stasis_playback.so
load => res_stasis_recording.so
load => res_stasis_snoop.so

; HTTP + ARI REST + WebSocket
load => res_http_websocket.so
load => res_ari.so
load => res_ari_asterisk.so
load => res_ari_channels.so
load => res_ari_bridges.so
load => res_ari_events.so
load => res_ari_playbacks.so
```

**Step 5: Write `asterisk.conf` and `logger.conf`**

```
; sip_bridge/configs/asterisk.conf
; Minimal — Debian package provides sensible paths.

[directories](!)
astetcdir => /etc/asterisk
astmoddir => /usr/lib/x86_64-linux-gnu/asterisk/modules
astvarlibdir => /var/lib/asterisk
astdbdir => /var/lib/asterisk
astkeydir => /var/lib/asterisk
astdatadir => /var/lib/asterisk
astagidir => /var/lib/asterisk/agi-bin
astspooldir => /var/spool/asterisk
astrundir => /var/run/asterisk
astlogdir => /var/log/asterisk
astsbindir => /usr/sbin

[options]
verbose = 3
debug = 0
runuser = root
rungroup = root
```

```
; sip_bridge/configs/logger.conf
[general]

[logfiles]
console => notice,warning,error
```

**Step 6: Commit**

```bash
git add sip_bridge/configs/
git commit -m "sip-bridge: static Asterisk configs (extensions, ari, http, modules, logger)"
```

---

## Task 4: Entrypoint + supervisord config

Boots Asterisk and the Python bridge in the same container. Supervisor restarts either if it dies.

**Files:**
- Modify: `sip_bridge/entrypoint.sh` (full rewrite)
- Create: `sip_bridge/supervisord.conf`
- Create: `sip_bridge/render_entry.py` (orchestrates config fetch + render)

**Step 1: Write `render_entry.py`**

```python
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
SOUNDS_DIR = Path("/var/lib/asterisk/sounds/en")
GREETING_PATH = SOUNDS_DIR / "openptt-greeting.wav"
ARI_PASSWORD_PATH = Path("/run/sip_bridge/ari-password")


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
                GREETING_PATH.write_bytes(r.content)
                LOG.info("greeting cached (%d bytes)", len(r.content))
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


def main() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    trunk = fetch_trunk()
    render_and_write(trunk)
    fetch_greeting()
    LOG.info("entrypoint rendering complete")


if __name__ == "__main__":
    main()
```

**Step 2: Write `supervisord.conf`**

```
; sip_bridge/supervisord.conf
[supervisord]
nodaemon=true
user=root
logfile=/var/log/supervisord.log
pidfile=/run/supervisord.pid

[program:asterisk]
command=/usr/sbin/asterisk -f -U root -G root
autostart=true
autorestart=true
startsecs=5
stopwaitsecs=10
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:ari_bridge]
command=/usr/bin/python3 -m sip_bridge.ari_bridge
autostart=true
autorestart=true
startsecs=3
stopwaitsecs=5
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
environment=PYTHONUNBUFFERED="1"
```

**Step 3: Rewrite `entrypoint.sh`**

```bash
#!/bin/bash
# sip_bridge/entrypoint.sh
#
# One-shot config rendering, then hand off to supervisord which runs
# Asterisk + the Python ARI bridge.

set -euo pipefail

echo "[entrypoint] rendering config from admin API"
python3 -m sip_bridge.render_entry

echo "[entrypoint] starting supervisord"
exec /usr/bin/supervisord -c /app/supervisord.conf
```

**Step 4: Commit**

```bash
chmod +x sip_bridge/entrypoint.sh
git add sip_bridge/entrypoint.sh sip_bridge/supervisord.conf sip_bridge/render_entry.py
git commit -m "sip-bridge: entrypoint renders configs, supervisord manages asterisk+python"
```

---

## Task 5: Dockerfile rewrite

Replace the pjsip-from-source Debian image with an asterisk-apt image. Drop ~200 MB of build chain.

**Files:**
- Modify: `sip_bridge/Dockerfile` (full rewrite)
- Modify: `sip_bridge/requirements.txt`

**Step 1: Rewrite `Dockerfile`**

```dockerfile
# sip_bridge/Dockerfile
#
# Phase 2b-audio: Asterisk 20 (apt-installed) + Python ARI bridge.
# network_mode: host + apparmor:unconfined in docker-compose — host
# networking is required for SIP/RTP to reach DIDWW with correct SDP
# addresses; AppArmor default profile blocks UDP sendto in host mode.

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        asterisk \
        asterisk-modules \
        asterisk-core-sounds-en-wav \
        python3 \
        python3-pip \
        python3-venv \
        supervisor \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Python deps. Use --break-system-packages because Debian bookworm
# has PEP 668 enabled; we're inside a container so it's fine.
COPY sip_bridge/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Layout: the sip_bridge Python package + configs live in /app.
WORKDIR /app
COPY sip_bridge/__init__.py /app/sip_bridge/__init__.py
COPY sip_bridge/audio.py /app/sip_bridge/audio.py
COPY sip_bridge/render_config.py /app/sip_bridge/render_config.py
COPY sip_bridge/render_entry.py /app/sip_bridge/render_entry.py
COPY sip_bridge/ari_bridge.py /app/sip_bridge/ari_bridge.py
COPY sip_bridge/templates /app/sip_bridge/templates
COPY sip_bridge/configs /app/sip_bridge/configs
COPY sip_bridge/supervisord.conf /app/supervisord.conf
COPY sip_bridge/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Ship the static Asterisk configs that do NOT need per-deployment
# interpolation — extensions.conf, http.conf, modules.conf,
# asterisk.conf, logger.conf. pjsip.conf + ari.conf are written by the
# entrypoint renderer.
RUN cp /app/sip_bridge/configs/extensions.conf /etc/asterisk/extensions.conf && \
    cp /app/sip_bridge/configs/http.conf       /etc/asterisk/http.conf && \
    cp /app/sip_bridge/configs/modules.conf    /etc/asterisk/modules.conf && \
    cp /app/sip_bridge/configs/asterisk.conf   /etc/asterisk/asterisk.conf && \
    cp /app/sip_bridge/configs/logger.conf     /etc/asterisk/logger.conf

ENV PYTHONPATH=/app

EXPOSE 5060/udp
EXPOSE 10000-10200/udp

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
```

Note: the Dockerfile build context in `docker-compose.yml` is `./sip-bridge`, so `COPY sip_bridge/...` above is wrong. We need to change the compose context to `.` OR use paths relative to `sip_bridge/`. Going with the latter — it's less invasive. Adjust all `COPY` lines to drop the `sip_bridge/` prefix:

```dockerfile
COPY requirements.txt /tmp/requirements.txt
# ...
COPY __init__.py /app/sip_bridge/__init__.py
COPY audio.py /app/sip_bridge/audio.py
# etc.
```

**Step 2: Update `requirements.txt`**

```
# sip_bridge/requirements.txt
pymumble==1.6.1
httpx==0.28.1
numpy>=1.24.0
aiohttp>=3.9.0
```

**Step 3: Local build test**

Run: `docker build -t ptt-sip-bridge:dev ./sip-bridge`
Expected: Build succeeds. Image size `docker images ptt-sip-bridge:dev` should be under 500 MB (for comparison, the current pjsip-source image is ~700 MB).

**Step 4: Commit**

```bash
git add sip_bridge/Dockerfile sip_bridge/requirements.txt
git commit -m "sip-bridge: Dockerfile rewrite — Asterisk 20 apt, drop pjsip source build"
```

---

## Task 6: Python ARI bridge skeleton (connect WS, log Stasis events)

This task is scoped to "prove the ARI plumbing works": connect the WebSocket, receive StasisStart/StasisEnd, log them. No audio pumping yet — that's Task 7.

**Files:**
- Create: `sip_bridge/ari_bridge.py`
- Test: `tests/sip_bridge/test_ari_bridge.py`

**Step 1: Write the failing test**

ARI WebSocket interactions are hard to unit-test without a mock server. We test the one piece that IS pure-Python: the StasisStart event parser.

```python
# tests/sip_bridge/test_ari_bridge.py
"""ARI event-parsing unit tests. Live WebSocket is covered by integration."""
from __future__ import annotations

from sip_bridge.ari_bridge import parse_stasis_event


def test_parses_stasis_start():
    event = {
        "type": "StasisStart",
        "application": "openptt-bridge",
        "channel": {"id": "1234567890.1", "name": "PJSIP/didww-00000001"},
        "args": [],
    }
    parsed = parse_stasis_event(event)
    assert parsed is not None
    assert parsed.kind == "start"
    assert parsed.channel_id == "1234567890.1"
    assert parsed.channel_name == "PJSIP/didww-00000001"


def test_parses_stasis_end():
    event = {
        "type": "StasisEnd",
        "application": "openptt-bridge",
        "channel": {"id": "1234567890.1", "name": "PJSIP/didww-00000001"},
    }
    parsed = parse_stasis_event(event)
    assert parsed is not None
    assert parsed.kind == "end"
    assert parsed.channel_id == "1234567890.1"


def test_ignores_unrelated_events():
    assert parse_stasis_event({"type": "ChannelDtmfReceived"}) is None
    assert parse_stasis_event({}) is None
```

**Step 2: Verify the tests fail**

Run: `PYTHONPATH=. pytest tests/sip_bridge/test_ari_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError`.

**Step 3: Minimal implementation**

```python
# sip_bridge/ari_bridge.py
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
ARI_PASSWORD_PATH = Path("/run/sip_bridge/ari-password")


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
```

**Step 4: Verify unit tests pass**

Run: `PYTHONPATH=. pytest tests/sip_bridge/test_ari_bridge.py -v`
Expected: 3 tests PASS.

**Step 5: Deploy to the server and test StasisStart fires on a real call**

```bash
git add sip_bridge/ari_bridge.py tests/sip_bridge/test_ari_bridge.py
git commit -m "sip-bridge: ARI bridge skeleton — WS connect + Stasis event parser (TDD)"

# Push + deploy
git push origin main
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch 'cd /opt/ptt && git pull && docker compose build sip-bridge && docker compose up -d sip-bridge'

# Watch logs during a live test call
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch 'cd /opt/ptt && docker compose logs -f sip-bridge'
```

Expected on a test call:
- Asterisk logs: `Executing [<did>@didww-inbound:...] Playback("PJSIP/...", "openptt-greeting")`
- ARI bridge logs: `ARI WebSocket connected`, then `Stasis start: channel=...`, then `Stasis end: channel=...`
- Caller hears Piper greeting, then silence, then Asterisk hangs up at end of dialplan.

If this smoke passes, the plumbing is correct and we're ready to wire audio.

---

## Task 7: externalMedia + audio pump (caller → Mumble direction only)

Add the externalMedia channel spawn and uplink (caller → Mumble) first. Downlink comes in Task 8. Scoping this way lets us test "caller's voice appears in Mumble" before dealing with the reverse path.

**Files:**
- Modify: `sip_bridge/ari_bridge.py`
- Test: `tests/sip_bridge/test_audio_pump.py`

**Step 1: Write the failing test**

```python
# tests/sip_bridge/test_audio_pump.py
"""Audio pump unit tests. The pump itself is a tight loop — we test
the pieces that can be tested without network: frame-size validation,
the no-op-when-idle guarantee, and pymumble-queue interaction via mock."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sip_bridge.ari_bridge import AudioPump


class FakeSock:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    def recv(self, bufsize):
        if not self._frames:
            raise BlockingIOError
        return self._frames.pop(0)

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def setblocking(self, flag):
        pass


def test_pump_forwards_uplink_frame_to_pymumble():
    mumble = MagicMock()
    sock = FakeSock([b"\x00\x10" * 320])  # 640-byte 16 kHz slin16 frame

    pump = AudioPump(udp_sock=sock, udp_peer=("127.0.0.1", 12345), mumble=mumble)
    pump.step_uplink()

    mumble.sound_output.add_sound.assert_called_once()
    pushed = mumble.sound_output.add_sound.call_args.args[0]
    assert len(pushed) == 1920  # upsampled to 48 kHz


def test_pump_idle_no_mumble_writes():
    mumble = MagicMock()
    sock = FakeSock([])

    pump = AudioPump(udp_sock=sock, udp_peer=("127.0.0.1", 12345), mumble=mumble)
    pump.step_uplink()

    mumble.sound_output.add_sound.assert_not_called()


def test_pump_skips_wrong_size_frame():
    """Defensive: externalMedia occasionally sends short frames on call setup/teardown."""
    mumble = MagicMock()
    sock = FakeSock([b"\x00" * 100])

    pump = AudioPump(udp_sock=sock, udp_peer=("127.0.0.1", 12345), mumble=mumble)
    pump.step_uplink()

    mumble.sound_output.add_sound.assert_not_called()
```

**Step 2: Extend `ari_bridge.py` with externalMedia + AudioPump**

Add these pieces to `ari_bridge.py`:

```python
# At the top of ari_bridge.py (new imports)
import socket
import threading
from sip_bridge.audio import upsample_16_to_48, downsample_48_to_16

# New class — AudioPump
class AudioPump:
    """Bidirectional audio bridge between Asterisk externalMedia UDP and pymumble.

    Uplink (caller → Mumble): recv UDP slin16 → upsample → pymumble.sound_output.add_sound
    Downlink (Mumble → caller): drain pymumble.sound_received → downsample → sendto UDP

    This class holds no asyncio — it's called on a 20 ms tick loop from
    a thread. pymumble's API is blocking, so we don't fight it.
    """
    SLIN16_FRAME_BYTES = 640  # 20 ms @ 16 kHz mono int16

    def __init__(self, udp_sock, udp_peer, mumble):
        self._sock = udp_sock
        self._peer = udp_peer
        self._mumble = mumble
        self._sock.setblocking(False)

    def step_uplink(self) -> None:
        """Pull one frame from Asterisk if available, push to Mumble."""
        try:
            data = self._sock.recv(4096)
        except (BlockingIOError, socket.timeout):
            return
        if len(data) != self.SLIN16_FRAME_BYTES:
            return  # skip malformed
        pcm48k = upsample_16_to_48(data)
        try:
            self._mumble.sound_output.add_sound(pcm48k)
        except Exception as e:
            LOG.warning("mumble add_sound failed: %s", e)


# Modify the event loop in run() so StasisStart spawns externalMedia and starts the pump
```

For this task, only wire `step_uplink`. The StasisStart handler uses aiohttp to POST externalMedia, binds a UDP socket, and starts a thread running `step_uplink` in a 20 ms loop. Mumble connection is not yet wired — pass a MagicMock-style object that just logs.

Full expected addition to `run()`:

```python
async def on_stasis_start(parsed: StasisEvent, sess, mumble) -> Optional[AudioPump]:
    LOG.info("spawning externalMedia for channel %s", parsed.channel_id)
    # Bind a UDP socket we'll tell Asterisk to send to.
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp_port = udp.getsockname()[1]
    LOG.info("bound UDP port %d", udp_port)

    # Ask Asterisk to bridge the call audio to us.
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
    r.raise_for_status()
    body = await r.json()
    LOG.info("externalMedia created: %s", body.get("id"))

    # Asterisk sends RTP to us at the same port it received from; we
    # just discover the peer on first frame. For now, use (127.0.0.1, 0)
    # and let the kernel route by wildcard.
    pump = AudioPump(udp_sock=udp, udp_peer=("127.0.0.1", 0), mumble=mumble)

    # Start the uplink thread. Downlink is wired in Task 8.
    def _pump_loop():
        import time
        while True:
            pump.step_uplink()
            time.sleep(0.02)
    t = threading.Thread(target=_pump_loop, daemon=True, name="audio-pump")
    t.start()
    return pump
```

**Step 3: Verify unit tests pass**

Run: `PYTHONPATH=. pytest tests/sip_bridge/test_audio_pump.py -v`
Expected: 3 tests PASS.

**Step 4: Deploy + test with a placeholder pymumble stub**

Use a MagicMock-style no-op pymumble for this deploy (so we can verify externalMedia frames are arriving without needing a real Mumble bot). Log the first 10 bytes of each received frame to confirm audio is flowing from Asterisk.

Deploy + live test:
```bash
git add sip_bridge/ari_bridge.py tests/sip_bridge/test_audio_pump.py
git commit -m "sip-bridge: wire externalMedia + uplink audio pump (caller→mock Mumble)"
git push origin main
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch 'cd /opt/ptt && git pull && docker compose build sip-bridge && docker compose up -d sip-bridge'
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch 'cd /opt/ptt && docker compose logs -f sip-bridge'
```

On a test call:
- Expected: after greeting, `uplink frame 640 bytes` log lines appear every ~20 ms.
- If you speak into the phone, frame contents should vary (non-silent). Easy check: log `sum(abs(frame))` per 500 frames.

---

## Task 8: Pymumble connection + downlink audio pump

Wire the real `PTTPhone` pymumble connection and the downlink (Mumble → caller) direction.

**Files:**
- Modify: `sip_bridge/ari_bridge.py`

**Step 1: Add pymumble connection setup**

```python
# Add to ari_bridge.py imports
import time

# New function
def open_mumble(host: str, port: int) -> "pymumble_py3.Mumble":
    """Connect PTTPhone, move into Phone channel (create if missing)."""
    import pymumble_py3 as pymumble
    import pymumble_py3.constants as const

    mm = pymumble.Mumble(host, "PTTPhone", port=port, reconnect=True)
    mm.set_application_string("openPTT SIP Bridge")
    mm.set_receive_sound(True)
    mm.start()
    mm.is_ready()
    time.sleep(1)

    # Ensure Phone channel exists
    phone_id = None
    for cid, chan in mm.channels.items():
        if chan["name"] == "Phone":
            phone_id = cid
            break
    if phone_id is None:
        mm.channels.new_channel(0, "Phone", temporary=False)
        time.sleep(0.5)
        for cid, chan in mm.channels.items():
            if chan["name"] == "Phone":
                phone_id = cid
                break
    if phone_id is None:
        LOG.error("could not create or find Phone channel")
        raise RuntimeError("Phone channel unavailable")

    mm.users.myself.move_in(phone_id)
    time.sleep(0.2)
    LOG.info("PTTPhone joined Phone channel (id=%d)", phone_id)
    return mm
```

**Step 2: Add `step_downlink` to `AudioPump`**

```python
# Add to AudioPump class
def step_downlink(self) -> None:
    """Drain one 20 ms chunk of received Mumble audio, send to Asterisk."""
    # pymumble buffers received sound per-user; we mix all users into one stream.
    # For Phase 2b-audio with one caller, we expect 0-1 Mumble users speaking
    # at a time — naive "last chunk wins" is acceptable.
    import pymumble_py3.constants as const

    frame = None
    for user in self._mumble.users.values():
        if user.get("session") == self._mumble.users.myself["session"]:
            continue
        sound = user.sound
        if sound.is_sound():
            chunk = sound.get_sound(0.02)  # 20 ms
            if chunk is not None and len(chunk.pcm) == 1920:
                frame = chunk.pcm
                break
    if frame is None:
        return
    slin16 = downsample_48_to_16(frame)
    try:
        self._sock.sendto(slin16, self._peer)
    except OSError as e:
        LOG.warning("UDP send failed: %s", e)
```

**Step 3: Wire peer discovery for UDP send**

Asterisk externalMedia doesn't announce its source port via the ARI response directly — we discover it from the first received frame. Adjust `step_uplink`:

```python
def step_uplink(self) -> None:
    try:
        data, addr = self._sock.recvfrom(4096)
    except (BlockingIOError, socket.timeout):
        return
    # Latch the peer on first valid frame so step_downlink knows where to send.
    if self._peer == ("127.0.0.1", 0):
        self._peer = addr
        LOG.info("latched UDP peer to %s", addr)
    if len(data) != self.SLIN16_FRAME_BYTES:
        return
    pcm48k = upsample_16_to_48(data)
    try:
        self._mumble.sound_output.add_sound(pcm48k)
    except Exception as e:
        LOG.warning("mumble add_sound failed: %s", e)
```

**Step 4: Update pump loop to run both directions**

```python
def _pump_loop():
    while True:
        pump.step_uplink()
        pump.step_downlink()
        time.sleep(0.02)
```

**Step 5: Wire `open_mumble` into `run()`**

In `run()`, before the WS connect:

```python
mumble_host = os.environ.get("MUMBLE_HOST", "127.0.0.1")
mumble_port = int(os.environ.get("MUMBLE_PORT", "64738"))
mumble = open_mumble(mumble_host, mumble_port)
```

Pass `mumble` into `on_stasis_start`.

**Step 6: Add MUMBLE_HOST/MUMBLE_PORT to docker-compose**

```yaml
# docker-compose.yml sip-bridge service, environment section — add:
MUMBLE_HOST: "127.0.0.1"
MUMBLE_PORT: "64738"
```

**Step 7: Deploy + full bidirectional smoke test**

```bash
git add sip_bridge/ari_bridge.py docker-compose.yml
git commit -m "sip-bridge: wire pymumble + downlink — full bidirectional bridge"
git push origin main
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch 'cd /opt/ptt && git pull && docker compose up -d --build sip-bridge'
```

**Manual smoke test** (verification checklist item #2-5):
1. Connect a Mumble client as a regular user, join `Phone` channel.
2. Call +351300500404 from a mobile phone.
3. Expect: ring → answer → Piper greeting plays (~10 s) → silence on caller's end.
4. Speak into the phone. Mumble user hears you within ~200 ms.
5. Transmit PTT in Mumble. Caller hears Mumble user's voice.
6. Caller hangs up. Check Asterisk logs: clean `StasisEnd`, no orphan channels.
7. Second call while first is still live → phone B hears `486 Busy`.

If any step fails, **do not proceed**. Debug using `docker compose logs sip-bridge` and `asterisk -rx "core show channels"` via `docker compose exec sip-bridge`.

---

## Task 9: Documentation + handoff

**Files:**
- Modify: `docs/open_issues.md` (if the item exists, mark Phase 2b-audio done)
- Modify: `docs/roadmap.md`

**Step 1: Update roadmap**

Add an entry under `## Completed`:

```
- **Phase 2b-audio** (shipped 2026-04-17): Asterisk-based SIP↔Mumble bridge.
  One caller at a time, bidirectional audio into shared Phone channel,
  Piper greeting on answer. Replaces Phase 2b-initial pjsua2 bridge.
  Next: Phase 2c (incoming-call notifications, mute, can_answer_calls ACL).
```

**Step 2: Final commit**

```bash
git add docs/roadmap.md docs/open_issues.md
git commit -m "docs: mark Phase 2b-audio shipped"
git push origin main
```

---

## Rollback plan

If anything is catastrophic on deploy:

```bash
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch 'cd /opt/ptt && git reset --hard 8e1cbc6 && docker compose up -d --build sip-bridge'
```

`8e1cbc6` is the last-known-good Phase 2b-initial commit. Deploys will restore pjsua2 + greeting behavior.

---

## Verification checklist (final ship gate)

1. `docker compose ps sip-bridge` → Up, healthy.
2. `docker compose exec sip-bridge asterisk -rx "pjsip show registrations"` → Registered.
3. `docker compose exec sip-bridge asterisk -rx "ari show apps"` → `openptt-bridge` listed.
4. Real call → Piper greeting plays to completion.
5. Bidirectional audio confirmed from both sides.
6. Second concurrent call → 486 Busy. First call undisturbed.
7. Caller hangs up → clean StasisEnd in logs, no orphan channels.
8. Container restart → DIDWW re-registers + ready within 60 s.
9. All unit tests pass: `PYTHONPATH=. pytest tests/sip_bridge/ -v` (expect ~16 tests).

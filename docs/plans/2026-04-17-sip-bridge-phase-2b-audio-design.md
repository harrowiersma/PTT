# Phase 2b-audio — SIP ↔ Mumble bidirectional audio bridge

Generated: 2026-04-17 via `/brainstorming`
Branch: `main`
Predecessor: Phase 2b-initial (pjsua2 answer + Piper greeting, shipped 8e1cbc6)
Successor: Phase 2c (mute + ACL + incoming-call notifications), Phase 2d (app integration)

---

## Context

Phase 2b-initial ships a working SIP leg: the `sip-bridge` container registers to
DIDWW (Amsterdam), answers inbound INVITEs on +351300500404, plays a Piper-rendered
TTS greeting to the caller, then hangs up after 12 seconds.

What it cannot do: get the caller's audio into a Mumble channel, or play Mumble audio
back to the caller. The blocker is that pjsua2's Python SWIG binding does not expose
raw audio-frame callbacks — `AudioMediaPort` subclassing is C++ only.

Phase 2b-audio replaces the pjsua2-based bridge with Asterisk 22 + a Python ARI client
that uses Asterisk's `externalMedia` channel to receive and send PCM frames over a
loopback UDP socket. The ARI client bridges those frames into a `pymumble` connection
that sits in the shared `Phone` channel.

## Product scope

One caller at a time, bidirectional audio into one shared `Phone` Mumble channel.
This is by design: the phone gateway is an emergency-line use case, not a call center.

**In scope**
- Single concurrent call. A second INVITE while one is live → `486 Busy Here` (Asterisk
  dialplan, no Python involvement).
- Piper TTS greeting plays to caller on answer (same WAV bytes as 2b-initial, rendered
  by the admin API, played by Asterisk `Playback`).
- Bidirectional 8 kHz G.711 ↔ 48 kHz Opus audio, 20 ms framing.
- Pymumble connection as user `PTTPhone`, joins `Phone` channel (creates it if missing),
  stays connected across calls.

**Deferred to Phase 2c** (explicit)
- Sub-channels (`Phone/Call-N`). Not needed with one-caller concurrency.
- Green-button mute. Not needed for the audio bridge itself.
- `User.can_answer_calls` ACL.
- Incoming-call soft-ding tones + text messages in non-empty channels.
- Auto-move-user-back-to-previous-channel on hangup.

**Deferred to Phase 2d** — app integration on the P50.

**Explicitly out of scope forever** (per CEO plan):
- Outbound calls, call recording, transcription, TTS caller-ID announcements.

## Architecture

One container, two processes (supervised):

```
┌──────── sip-bridge container (network_mode: host, apparmor:unconfined) ────────┐
│                                                                                 │
│  ┌─────────────────┐    ARI WebSocket     ┌───────────────────────────────┐   │
│  │   Asterisk 22   │ ◄── events+cmds ──►  │  Python ARI bridge            │   │
│  │                 │                      │                                │   │
│  │ • PJSIP reg to  │    externalMedia UDP │ • aiohttp ARI WS client       │   │
│  │   DIDWW         │ ◄── slin16 20ms ──►  │ • pymumble "PTTPhone" thread  │   │
│  │ • Answers INVITE│    (loopback)        │ • Audio pump with np.interp   │   │
│  │ • Plays greeting│                      │   resample 16↔48 kHz          │   │
│  │ • Stasis handoff│                      └────────────────┬──────────────┘   │
│  └─────────────────┘                                       │                   │
│         ▲                                                  │                   │
│         │ REGISTER / INVITE / RTP  (host net)              │ pymumble TCP 64738 │
└─────────┼──────────────────────────────────────────────────┼──────────────────┘
          │                                                  │
          ▼                                                  ▼
    DIDWW Amsterdam                                   murmur container
```

**Why one container, not two:**
- Loopback-only communication between Asterisk and Python (ARI HTTP + externalMedia UDP).
- Both need host networking anyway — splitting doubles the network-config surface.
- Supervisor restarts either process independently.

**Why host networking + AppArmor unconfined:** documented in memory — docker bridge NAT
breaks SDP; AppArmor default policy blocks UDP sendto in host-network mode.

## Components

### A. Asterisk config (rendered at startup)

- `pjsip.conf` — one `transport/0`, one `endpoint/didww`, one `aor`, one `auth`, one
  `identify` block. Rendered from `/api/sip/internal/config/trunks` at entrypoint time
  via a ~50-line Python template renderer. DB remains source of truth for credentials.
- `extensions.conf` — single context `[didww-inbound]`:
  ```
  exten => _X.,1,NoOp(Incoming from ${CALLERID(num)})
   same => n,GotoIf($[${GROUP_COUNT(phone)} > 0]?busy)
   same => n,Set(GROUP()=phone)
   same => n,Answer()
   same => n,Playback(openptt-greeting)
   same => n,Stasis(openptt-bridge)
   same => n,Hangup()
   same => n(busy),Busy(10)
  ```
- `ari.conf` — app user `openptt` with a random secret (from env), bound to 127.0.0.1:8088.
- `http.conf` — enable HTTP on 127.0.0.1:8088 (ARI transport).
- `modules.conf` — explicit load list; `noload` everything else to keep the process lean.

### B. Python ARI bridge (new `sip-bridge/bridge.py`, ~250 LOC)

```
bridge.py
├── startup:
│   ├── fetch WAV from admin /api/sip/internal/tts (existing endpoint)
│   ├── write to /var/lib/asterisk/sounds/en/openptt-greeting.wav
│   ├── open pymumble "PTTPhone" → ensure Phone channel → join
│   └── connect ARI WebSocket → subscribe to Stasis(openptt-bridge)
├── on StasisStart(channel):
│   ├── POST /ari/channels/externalMedia (format=slin16, host=127.0.0.1:<port>)
│   ├── bind UDP socket on <port>
│   └── start audio pump
├── audio pump (tick = 20 ms):
│   ├── uplink:   UDP recv 640B → np.interp 16→48 kHz → pymumble.sound_output.add_sound()
│   └── downlink: drain pymumble.sound_received → np.interp 48→16 kHz → UDP send 640B
├── on StasisEnd(channel): close UDP socket, stop pump
└── shutdown: close pymumble, close WS
```

Pymumble stays connected between calls — no connect/disconnect churn. Audio pump sleeps
when no call is active.

### C. Dockerfile

Rewrite. Base `debian:bookworm-slim`, install `asterisk` + `asterisk-modules` via apt
(no source build). Python 3.11 deps: `aiohttp`, `numpy`, `pymumble`. Drop the pjsip-from-source
build chain entirely → image shrinks ~200 MB.

### D. What stays the same
- `docker-compose.yml` sip-bridge service: `network_mode: host`, `security_opt:
  apparmor:unconfined`, same env vars (`ADMIN_INTERNAL_URL`, `PTT_INTERNAL_API_SECRET`,
  `GREETING_TEXT`, `LOG_LEVEL`).
- `server/api/sip.py` `/api/sip/internal/config/*` + `/api/sip/internal/tts` — unchanged.
- Admin dashboard SIP Gateway tab — unchanged (still CRUDs `SipTrunk`/`SipNumber`).

## Data flow (one call)

| t (s) | Event |
|-------|-------|
| 0 | DIDWW → INVITE → Asterisk (UDP 5060, host net) |
| 0 | Dialplan: `GROUP_COUNT(phone)=0` → `Answer()` → 200 OK → RTP start |
| 0–10 | `Playback(openptt-greeting)` streams to caller |
| ~10 | `Stasis(openptt-bridge)` → StasisStart over ARI WS |
| ~10 | Python: POST externalMedia → UDP socket bound → audio pump starts |
| ~10+ | Bidirectional: caller ↔ Asterisk ↔ UDP ↔ resample ↔ pymumble ↔ Mumble |
| hangup | Caller disconnects → StasisEnd → UDP closes → dialplan `Hangup()` |

**Resampling math** (confirms 20 ms alignment):
- Uplink: 640 B slin16 @ 16 kHz = 320 samples = 20 ms → interp to 960 samples @ 48 kHz =
  1920 B = one pymumble chunk.
- Downlink: 1920 B @ 48 kHz = 20 ms → interp to 320 samples @ 16 kHz = 640 B = one UDP frame.

No clock drift to manage — Asterisk's externalMedia RTP clock paces uplink; downlink
ticks against pymumble's outgoing frame boundary (20 ms).

## Error handling

| Failure | Behavior |
|---------|----------|
| DIDWW registration fails | Asterisk auto-retries per `aor`. Healthcheck fails after 5 min → Docker restarts container. |
| Python bridge crashes mid-call | Supervisor restarts it. Asterisk dialplan falls through to `Hangup()`. Caller hears ≤1 s silence then hangs up. |
| Asterisk crashes | Supervisor restarts. Any live call is lost. Caller hears dead air → auto-hangup. |
| Mumble disconnect while call active | `pymumble(reconnect=True)` (pattern from weather_bot). Audio pump skips failed pushes until reconnect. |
| Concurrent call attempt | Dialplan `GROUP_COUNT` → `Busy(10)` → 486. Existing call undisturbed. |
| Greeting fetch fails at startup | Fall back to existing three-tone pattern helper (keep `ensure_greeting_wav` tone-fallback logic). |
| Admin not ready at bridge startup | Existing retry loop (30 × 2 s) stays. Asterisk waits for rendered `pjsip.conf` before accepting calls. |

## Testing

**Unit (new `tests/sip_bridge_audio_test.py`)**
- `test_resample_uplink`: 640 B 16 kHz sine → 1920 B 48 kHz sine, frequency preserved.
- `test_resample_downlink`: reverse.
- `test_pump_skips_when_idle`: mocked UDP socket, assert no pymumble writes.
- `test_pump_one_frame_per_tick`: mocked frame in → one pymumble chunk out, timing correct.

**Integration (manual smoke)**
- Real call from mobile → +351300500404:
  1. Ring → connect within <2 s.
  2. Hear Piper greeting ~10 s.
  3. Speak; tester in Mumble `Phone` channel hears it.
  4. Tester transmits in Mumble; caller hears it.
  5. Caller hangs up → Asterisk logs clean StasisEnd.
- Second call while first is live → hears busy tone (486).
- Container restart → DIDWW re-registers within 60 s.

**Regression**
- Piper greeting bytes identical to 2b-initial (still rendered by admin).
- `/api/sip/internal/*` endpoints unchanged (same bridge-side consumers).

## File impact

### New
- `sip-bridge/ari_bridge.py` — Python ARI client + audio pump (~250 LOC).
- `sip-bridge/config/` — pjsip.conf.tmpl, extensions.conf, ari.conf.tmpl, http.conf, modules.conf.
- `sip-bridge/render_config.py` — entrypoint-time template renderer (~50 LOC).
- `sip-bridge/supervisord.conf` (or shell-based supervisor) — manages asterisk + python.
- `tests/sip_bridge_audio_test.py` — unit tests for the resample + pump.

### Rewritten
- `sip-bridge/Dockerfile` — Debian + asterisk apt package, drop pjsip source build.
- `sip-bridge/entrypoint.sh` — render configs, start supervisor.
- `sip-bridge/requirements.txt` — add `aiohttp`, drop pjsua2 build artifacts.

### Deleted
- `sip-bridge/bridge.py` — replaced by `ari_bridge.py`. Greeting-tone fallback helper
  moves into `render_config.py` since the WAV now lives in Asterisk sounds dir.

### Unchanged
- `server/api/sip.py`
- `server/weather_bot.py` (source of the TTS pattern)
- `docker-compose.yml` sip-bridge service definition (same env, same network mode)

## Risks + open questions

- **Asterisk apt package version on Debian bookworm is 20.x, not 22.** ARI externalMedia
  stabilized in Asterisk 18+, so 20.x is sufficient. Mentioning 22 above is aspirational —
  20.x is what we will actually install.
- **PulseAudio/ALSA warnings in Asterisk logs** — harmless in containers with no sound
  device; suppress via `modules.conf` `noload = chan_alsa.so` etc.
- **pymumble thread-safety with aiohttp asyncio loop** — pymumble's callbacks fire on its
  own thread. Audio pump must use `asyncio.Queue` or `loop.call_soon_threadsafe` to cross
  threads. The weather_bot pattern shows the working recipe.
- **First-frame latency**: ARI externalMedia has ~40–60 ms startup latency on the first
  frame. Acceptable for phone audio. Tested in production by Deepgram, Assembly, etc.

## Verification checklist (ship gate)

1. `asterisk -rx "pjsip show registrations"` shows `Registered` for the DIDWW endpoint.
2. Real call to DID → answer → Piper greeting plays to completion.
3. Bidirectional audio confirmed both directions with Mumble client in `Phone`.
4. Second concurrent call → 486 Busy, first call undisturbed.
5. Caller hangs up → Asterisk logs clean StasisEnd, no orphan channels.
6. Container restart → re-registers + ready to accept calls within 60 s.
7. Unit tests pass in CI.

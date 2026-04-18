# openPTT TRX Product Roadmap

## Current State (Shipped)

| Feature | Status |
|---------|--------|
| Two-way PTT voice | Live |
| Multi-channel with admin management | Live |
| GPS tracking (Traccar) | Live |
| Live map with Leaflet | Live |
| Emergency SOS (auto-move to Emergency channel) | Live |
| SOS acknowledgement via voice (admin types OK) | Live |
| Weather ATIS channel (double-PTT, TinyTTS) | Live |
| Dispatch nearest worker | Live |
| Admin dashboard (7 tabs, dark/light theme) | Live |
| Multi-admin accounts (bcrypt) | Live |
| Audit logging | Live |
| Bulk device enrollment (CSV) | Live |
| Prometheus metrics | Live |
| Automated daily backups | Live |
| One-command VPS install | Live |
| HTTPS with SNI routing | Live |
| CI/CD pipeline (GitHub Actions) | Live |
| Lone-worker shifts (triple-tap PTT toggle, shift-aware checker) | Live |
| SIP gateway — Phase 2b-audio (inbound calls, bidirectional audio) | Live |
| SIP notifications (per-user whisper ding every 3 s until answered) | Live |
| SIP ringback to caller while no user in Phone channel | Live |

## Phase 2b-audio notes (shipped 2026-04-18)

End-to-end inbound phone → Mumble bridge on DID +351300500404 (DIDWW Amsterdam).

**Architecture:** Asterisk 20 (Ubuntu 24.04 apt) in the `sip-bridge` container
registers to the DIDWW trunk, answers inbound INVITEs, plays a Piper TTS
greeting, then hands the call to Asterisk's `AudioSocket` dialplan app. A
Python TCP server inside the same container bridges AudioSocket's slin 8 kHz
stream to a `pymumble` connection as user `PTTPhone`, which sits in the
shared `Phone` Mumble channel.

**Why AudioSocket, not ARI externalMedia:** externalMedia produced working
uplink but stubborn one-way-audio on the downlink, on this specific
Asterisk/Ubuntu build. Six hypotheses failed (endianness, PT learning,
40→20 ms split, echo test, `direction=both`, `UNICASTRTP_LOCAL_PORT`).
AudioSocket's TCP framing sidesteps RTP bridge-routing entirely and worked
on the second commit. Detail in `memory/project_sip_use_audiosocket.md`.

**Key UX choices:**
- One caller at a time (`GROUP_COUNT` in dialplan → 486 Busy Here otherwise).
- Per-user whisper ding to users with `can_answer_calls=true`, re-ping every
  3 s until someone joins Phone or the call ends. Ding stops on call-ended.
- European ringback (400 Hz, 1 s on / 4 s off) to the caller while no human
  user is present in Phone.
- Channel-switching acts as implicit hold (caller hears ringback, bridge
  stays open, audio resumes when the user returns).
- Downlink gain -6 dBFS default, tunable via `DOWNLINK_GAIN` env.
- `PTTPhone` bot hidden from the admin dashboard user list.

**Deferred to later phases:** radio-initiated hangup gesture (needs app-side
key-handler work first — see `docs/open_issues.md`), admin-editable greeting
text, per-call sub-channels, green-button mute, `can_answer_calls` ACL
enforcement on channel entry.

## Phase 3: Safety Features (Next Priority)

### 3.1 Man-Down Detection
**What:** P50's accelerometer detects a fall or prolonged no-movement. Auto-triggers SOS with GPS.
**How (server-side):** Traccar Client already supports alarm events including "powerOff", "sos", "fall", and "vibration". Configure Traccar Client on P50 to send fall detection alarms. Our existing `/api/sos/traccar/event` webhook already handles Traccar alarm events and triggers the SOS flow.
**What's needed:** Configure Traccar Client alarm settings on P50 devices. The server-side infrastructure is already built.
**Effort:** S (configuration only, no new code)

### 3.2 Lone Worker Timer (Check-In System)
**What:** Worker must check in every X minutes by pressing PTT or tapping a button. If they miss a check-in, the system alerts the admin and triggers SOS.
**How:** New `check_in` table in PostgreSQL. Background task monitors check-in timestamps. If a user's last check-in exceeds the threshold (configurable per user/channel), trigger SOS automatically.
**Dashboard:** New "Lone Worker" section showing check-in status per user, with green/yellow/red indicators.
**Effort:** M (human: ~1 week / CC: ~2-3 hours)

### 3.3 Priority Calling (Emergency Override)
**What:** When SOS is triggered, the emergency audio overrides all other channels. Currently we move users to Emergency channel. Enhancement: play an alert tone on all devices before moving.
**How:** PTTAdmin bot sends a distinctive alert sound (siren/tone) to all channels before moving users. The sound plays for 3 seconds, then users are moved.
**Effort:** S (CC: ~1 hour)

### 3.4 Periodic Device Health Check
**What:** Dashboard shows device health: battery level, signal strength, GPS accuracy, last heartbeat. Alert when a device goes silent (no GPS update for > X minutes).
**How:** Extend the Traccar polling to check for stale positions. New alert in dashboard when a device hasn't reported in >15 minutes.
**Effort:** S (CC: ~1 hour)

## Phase 4: Operations Features

### 4.1 Shift Management
**What:** Define shifts (Morning, Afternoon, Night). Assign users to shifts. Dashboard shows current shift's active users.
**How:** New `shifts` table. Assign users to shifts. Dashboard filters by active shift.
**Effort:** M (CC: ~2 hours)

### 4.2 Roll Call / Check-In
**What:** Admin triggers "roll call" from dashboard. All online users get a text message. They must respond within 60 seconds. Dashboard shows who responded and who didn't.
**How:** PTTAdmin bot sends roll call message to all channels. Track responses via text message callback.
**Effort:** M (CC: ~2 hours)

### 4.3 Task Assignment
**What:** Admin creates a task in the dashboard, assigns it to a user. User receives text message on their radio with task details. Task can be acknowledged/completed from the radio.
**How:** New `tasks` table. Send task via Mumble text message. Track acknowledgement.
**Effort:** M (CC: ~3 hours)

### 4.4 Automated Status Reports
**What:** Daily/weekly email report to admin: total PTT transmissions, hours online per user, SOS events, GPS distance traveled, devices with low battery.
**How:** Background task generates report, sends via email (SMTP already configured for SOS webhooks).
**Effort:** M (CC: ~2 hours)

## Phase 5: Multimedia Features

### 5.1 Voice Message Recording/Playback
**What:** Record PTT transmissions per channel. Playback from dashboard timeline.
**How:** pymumble can capture audio. Store as WAV/Opus files. Dashboard shows timeline with playback.
**Effort:** L (CC: ~4 hours, storage planning needed)

### 5.2 Photo Sharing
**What:** Users send photos from P50 camera to a channel. Photos appear in dashboard.
**How:** Requires custom Android app or a separate photo upload endpoint. HamMumble doesn't support image sharing natively.
**Effort:** L (requires custom app or separate mechanism)

### 5.3 Offline Voice Messages
**What:** Send voice message to offline user. Delivered when they come back online.
**How:** Record and store message in PostgreSQL. PTTAdmin bot plays it when user reconnects.
**Effort:** M (CC: ~3 hours)

## Phase 6: Platform Features

### 6.1 Multi-Tenant Support
**What:** Multiple organizations on one server, each with isolated users/channels/data.
**How:** Add `organization_id` to all models. Scope all queries by organization.
**Effort:** XL (human: ~4 weeks / CC: ~1 week)

### 6.2 Custom Android App (openPTT TRX Client)
**What:** Fork HamMumble into branded "openPTT TRX" app with built-in GPS, SOS button, DTMF, auto-config via QR.
**How:** Kotlin Android development. Maintain as separate repo.
**Effort:** XL (human: ~8 weeks / CC: ~2 weeks) + ongoing maintenance

### 6.3 API Gateway / Webhooks
**What:** Expose all events (SOS, check-in, dispatch) as webhooks. Third-party integration.
**How:** Event bus + webhook dispatcher. Configure webhook URLs per event type.
**Effort:** M (CC: ~3 hours)

### 6.4 Real-Time Translation
**What:** Translate text messages between users in different languages using AI.
**How:** Integrate a translation API (DeepL, Google Translate). Translate text messages on the server before delivery.
**Effort:** M (CC: ~2 hours)

## Competitive Positioning

**openPTT TRX is unique because:**
1. Fully self-hosted (no cloud dependency, no per-device licensing)
2. Weather ATIS channel (no competitor has this)
3. Open source server (EVO PTT is open source but our admin dashboard is richer)
4. One-command install (no competitor offers this simplicity)
5. GPS + voice + dispatch in one platform (Zello has no GPS, EVO PTT has no dispatch)

**Where competitors beat us:**
1. HyTalk: video calling, multi-tenant, carrier-grade reliability
2. Weavix: real-time translation, Walt Smart Radio hardware
3. Zello: massive user base, offline voice messages, 500-person channels

**Our strategy:** Don't compete on everything. Win on: self-hosted control, safety features for small teams, and zero licensing cost. The IT admin who's tired of HyTalk pricing is our customer.

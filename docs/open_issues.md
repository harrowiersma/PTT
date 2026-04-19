# openPTT TRX — Open Issues

Last updated: 2026-04-17

This document is the rolling ledger of outstanding work. Items move from
"Open" to "Resolved" with their resolution commit so history is auditable.

---

## Resolved (2026-04-17 session)

### App (openPTT-app commit `46e2dc5`)
- **#1 Custom sounds** — SoundPool-backed connect + channel-change chimes.
- **#3 Remove donate** — Footer block in nav drawer deleted.
- **#4 Small screen (240x320 P50)** — PTT button default 60dp; Root channel
  hidden; tab strip hidden on small screens; `values-small/dimens.xml` +
  `layout-small/activity_main.xml`; brighter dark palette for 120dpi.
- **#5 Channel knob** — `KEYCODE_F5`/`F6` intercepted in
  `dispatchKeyEvent()`, cycles Root's direct subchannels.
- **Bonus: Hytera hardware PTT** — `MeigPttReceiver` catches keycode 142
  system-wide (no foreground required) and forwards PTT up/down to
  `MumlaService`.
- **Bonus: Built-in GPS reporting** — `LocationReporter` sends OsmAnd
  payloads to `voice.harro.ch:5055` using the Mumble username as
  `uniqueId`. Replaces the separate Traccar Client app.
- **Bonus: Dispatch-radio behaviour** — `PREF_AUTO_CONNECT` (default true),
  `BootReceiver` launches the service on device boot.

### Shift system + Alembic (2026-04-17 afternoon)
- **#17 Alembic migrations** — baseline migration `274f344764e1_initial_schema`
  covers every existing table; `server/entrypoint.sh` detects pre-Alembic
  databases and stamps them to head on first boot, then runs
  `alembic upgrade head` on every startup. Live on prod; DB is at
  `1f38074872ac` (shifts + user columns). Fresh installs now build the
  schema entirely through migrations.
- **#14 Lone-worker shift system** — new `lone_worker_shifts` table plus
  `users.shift_duration_hours` and `users.can_answer_calls` columns.
  Server endpoints `/api/loneworker/shift/{start,stop,active}`;
  checker loop is now shift-aware (only pings users with an active
  shift, auto-closes on expiry). Dashboard user-edit form gained a
  shift duration field and a "can answer SIP calls" toggle.
- **Shift trigger — triple-tap PTT (not long-press F3)** — the P50's
  ROM owns F3 long-press and doesn't forward the key event to apps or
  even to the system-level `LoneWorkerManagerService`. Pivoted to
  three quick PTT presses within 1.2s as the shift-toggle gesture,
  detected in `MumlaService.detectTripleTap()`. Reliable through the
  existing `MeigPttReceiver` path, works regardless of foreground.
  Confirmation via Android's built-in TextToSpeech. App commit
  `e12e3f3` in openPTT-app.

### Server (PTT commits `4cc3752`, `4201c8e`)
- **#9 Weather bot GPS matching** — resolves Traccar position via
  `User.traccar_device_id` link first, falls back to device name. Matches
  the pattern already in `server/api/status.py` and `server/api/dispatch.py`.
  (`server/weather_bot.py`)
- **#10 Weather bot channel fix** — dedicated `PTTWeather` pymumble
  connection sits permanently in Weather so
  `PYMUMBLE_CLBK_SOUNDRECEIVED` fires for Weather audio. The main
  `PTTAdmin` connection stays in Root for SOS acknowledgements in
  Emergency. Bot-name exclusion extended to `PTTWeather` in user listings,
  SOS moves, and text-ack handling.
- **#12 `_ensure_db_channel` coroutine warning** — `WeatherBot.start()` is
  now async and awaits DB work on the main event loop, eliminating the
  detached-loop path that left the coroutine unscheduled.
- **#13 Dispatch TTS whisper** — dispatches first generate TTS audio via
  `text_to_audio_pcm` and `MurmurClient.whisper_audio(session_id, pcm)`
  (using `pymumble sound_output.set_whisper`), falling back to the
  existing channel text message on failure. Response now includes a
  `delivery` field (`tts_whisper` | `text` | `none`).
- **#15 Traccar event webhook 405** — `traccar.xml` forward URL corrected
  from `/api/traccar/event` to `/api/sos/traccar/event` (matching the
  actual router prefix); `event.forward.enable` restored to `true` after
  the earlier mute workaround.
- **#8 Traccar session caching** — session cookie cached at the class
  level with a 25-minute TTL (instead of per-instance). Observed 0
  `POST /api/session` calls in a 30-second window that sees 12 Traccar
  reads. Was ~1 session POST per request beforehand.

### Already done before this session
- **#7 Traccar device auto-assignment** — resolved by commit `2308f17`
  (`server/api/users.py::create_user` auto-creates the Traccar device with
  `uniqueId=<username>`; admin-created devices are auto-owned).
- **#18 AAAA DNS record** — verified removed; only A record
  `178.104.188.248` for `voice.harro.ch`.

### Reclassified
- **#19 Traccar Client on P50s** — **obsolete.** The openPTT app ships
  its own `LocationReporter` (commit `46e2dc5`). Verification now is
  "openPTT app has Location permission + Traccar URL set". Live state:
  `yuliia` device online with recent fix; `harro` device exists and
  received GPS earlier today.

---

## Dropped

### #2 Transmitted roger beep
Explicitly removed in openPTT-app commit `46e2dc5`: a locally-played beep
provides no value (the other users don't hear it), and injecting PCM into
Humla's Opus encoder pipeline to transmit it adds complexity without a
clean upstream hook point. Re-open only if someone produces a working
transmit path.

---

## Still open

### Physical / operational
- ~~**#6 Install openPTT TRX on harro's P50**~~ — **Resolved 2026-04-18**:
  APK installed on device `R259060623` via `adb install -r` (same build
  that includes the triple-tap lone-worker gate). No `harro` Murmur
  registration existed (only `SuperUser`), so no certificate cleanup
  was needed. In-app configuration (Mumble creds + admin URL + optional
  lone-worker-mode toggle) is the manual remaining step until the
  one-click provisioning script ships.

### P50 provisioning script (one-click deploy for field devices)
Today, flashing a P50 is manual: build APK, `adb install`, open the app,
type Mumble creds, type admin URL, toggle lone-worker mode, set Traccar
URL. Every step is a chance for drift between devices.

**Wanted:** a downloadable provisioning script, reachable from a short
link like `ptt.harro.ch/1234`, that picks the user's OS (macOS or
Windows) and runs an ADB-driven zero-touch setup:

- Detects the connected P50 via `adb devices`.
- Downloads the latest `openptt-foss-debug.apk` (signed release once we
  set that up) and `adb install -r`.
- Pre-populates SharedPreferences via `adb shell am start` with intent
  extras, or pushes a seeded `shared_prefs` XML — username, Mumble
  server, admin URL, Traccar URL, lone-worker-mode, rotary behavior,
  whatever else ships with defaults.
- Grants the `android.permission.POST_NOTIFICATIONS`,
  `ACCESS_FINE_LOCATION`, and `RECORD_AUDIO` runtime perms via
  `adb shell pm grant`.
- Launches the service so `BootReceiver` + `LocationReporter` are live
  before the user touches the handset.

**Packaging:** bash script for macOS/Linux + PowerShell script for
Windows. Both gated by OS detection in the nginx shortlink handler
(User-Agent sniff → redirect to the right file). Short URL slug pattern
`ptt.harro.ch/<short>` decoded to specific device config (per-user
shortlinks so each device gets its own credentials).

**Effort:** M. Needs Android SDK platform-tools bundled (or installer
hints), persona-specific config baking, a short-URL service on nginx,
and careful ADB error handling (unauthorized device, USB debugging
off, multiple devices connected). No new server-side surface.

### Architecture discussion — PTT gesture detection: app vs server
Triple-tap-PTT shift toggle currently lives in `MumlaService.detectTripleTap()`
in the openPTT-app (commit `91adebe` gated it on lone-worker mode + not-in-Phone).
Question raised: should gesture detection be server-side instead, so gesture
changes don't require an APK build + flash?

Trade-offs to discuss in a focused session:

- **App-side (current):** precise hardware-key timing, no audio round-trip,
  instant response, but every gesture change needs an APK rebuild + per-device
  flash.
- **Server-side (alternative):** hear PTT as audio bursts via pymumble's
  SOUNDRECEIVED callback on a dedicated bot, detect gaps → count taps. Updates
  via `docker compose up -d`. Fuzzier detection (depends on VAD + decode timing),
  one frame of latency, couples gesture to "audio is actually reaching Mumble".
- **Hybrid:** app emits raw gesture events to server (`/api/gesture/tap?count=3`);
  server owns the semantics. Best of both, biggest scope.

Not urgent — only blocking if we expect to add several new PTT gestures and
the APK-flash cycle becomes the bottleneck.

### Server — deferred (low risk, separate focused session)

- ~~**#16 Murmur registered-user reset**~~ — **Resolved 2026-04-19** via
  `server/murmur/admin_sqlite.py` (commits `75fd700`, `d602efe`). Admin
  container mounts the host Docker socket and runs `docker exec sqlite3`
  inside `ptt-murmur-1` to edit `mumble-server.sqlite` as murmur's own
  user, then bounces the container. Exposed as
  `POST /api/users/{user_id}/reset-murmur-registration`; dashboard
  user-edit form has a "Reset Murmur registration" button.

- ~~**Dashboard "Create Channel" doesn't actually create in Murmur"**~~
  — **Resolved 2026-04-19** (same infrastructure). `MurmurClient.create_channel`
  and `remove_channel` now fall back to the sqlite helper when pymumble's
  call silently no-ops. Verified end-to-end: create → murmur sees it →
  delete → gone.

### SIP gateway — partially shipped (Phase 2b-audio live 2026-04-18)

**Shipped:**
- Asterisk 20 + AudioSocket bridge in `sip-bridge` container (not Baresip).
- DIDWW Amsterdam trunk live on DID +351300500404.
- Inbound calls → shared `Phone` Mumble channel, bidirectional audio.
- Piper greeting on answer; European ringback to caller while empty Phone.
- Per-user whisper ding every 3 s until answered (`can_answer_calls=true` gate).
- Single concurrent call (486 Busy on second INVITE).
- Channel-switching acts as implicit hold.

**Deferred (future phases):**
- Radio-initiated hangup gesture — every PTT pattern collides with an
  existing app shortcut; needs dedicated keycode handler (likely
  `KEYCODE_MENU` or `KEYCODE_CALL`) in openPTT-app first.
- ~~Admin-editable greeting text~~ — **Resolved 2026-04-19** via
  `sip_trunks.greeting_text` + `PUT /api/sip/greeting` + live WAV
  push to the sip-bridge container (commits `85dee5e`, `d9343aa`).
  Dashboard SIP tab has a textarea with Save & regenerate.
- Per-call sub-channels (`Phone/Call-N`) for concurrent-call support.
- Green-button (`KEYCODE_CALL`) mute toggle per the CEO plan.
- ~~ACL enforcement on `Phone` channel entry~~ — **Resolved 2026-04-19**.
  `MurmurClient` watches `PYMUMBLE_CLBK_USERUPDATED`; non-eligible
  users who walk into Phone are bounced back to their previous channel
  and whispered "Phone channel requires call-answer permission". 30-s
  eligible-set cache refreshed from `users.can_answer_calls`.
  14 unit tests (`tests/test_phone_acl.py`). Commits `01ee04c`, `094855e`.

### Dashboard IA + visual overhaul — shipped 2026-04-19
Consolidated 8 flat tabs into 4 grouped modes (Live Ops · Directory ·
SIP Gateway · System) with secondary segmented controls under each.
Rebuilt the aesthetic as "Broadcast Station" — espresso-on-parchment
palette, amber primary, JetBrains Mono for data, Bricolage Grotesque
for display, dot-grid radio-faceplate backdrop. `094855e`.

---

## Reference

- App source: `/Users/harrowiersma/Documents/CLAUDE/openPTT-app`
- Build: `JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home ANDROID_HOME=~/Library/Android/sdk ./gradlew assembleFossDebug`
- APK output: `app/build/outputs/apk/foss/debug/openptt-foss-debug.apk`
- Server SSH: `ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch`
- Server repo: `/opt/ptt`
- Traccar admin (internal): `admin@ptt.local` / `admin` (see
  `server/config.py`)

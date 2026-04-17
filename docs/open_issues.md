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
- **#6 Install openPTT TRX on harro's P50** — yuliia's P50 has the app and
  is connecting; harro's needs the APK installed via
  `adb -s <serial> install -r openptt-foss-debug.apk`, then Mumble creds
  configured in-app. The `harro` Murmur registration may need to be
  cleared first if the cert from HamMumble is still on file:
  `docker exec ptt-murmur-1 sqlite3 /data/mumble-server.sqlite "DELETE FROM users WHERE name='harro';" && docker restart ptt-murmur-1`.

### Server — deferred (low risk, separate focused session)
- **#11 TinyTTS verification** — needs a live double-PTT from a P50 in the
  Weather channel to exercise the model load and audio output. Code path
  is wired through the new `PTTWeather` connection; no known blockers,
  just untested end-to-end.
- **#14 Lone worker shift system** — shift-based on/off instead of 24/7
  reminders. Blocked on #17 (new `LoneWorkerShift` table wants a
  migration).
- **#16 Murmur registered-user reset** — pymumble can't delete server-side
  registrations. Implementation needs to share the `murmur-data` volume
  into the admin container and modify `mumble-server.sqlite` directly, or
  introduce an alternate channel. Current manual workaround (SSH +
  `docker exec` + sqlite3) is documented and works.
- **#17 Alembic migrations** — scaffold exists (`server/alembic/env.py`,
  `alembic.ini`), no baseline migration yet. Plan: `alembic revision
  --autogenerate` against an empty SQLite, then `alembic stamp head` in
  prod to mark the existing schema as migrated, then bake `alembic
  upgrade head` into the admin entrypoint.

---

## Reference

- App source: `/Users/harrowiersma/Documents/CLAUDE/openPTT-app`
- Build: `JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home ANDROID_HOME=~/Library/Android/sdk ./gradlew assembleFossDebug`
- APK output: `app/build/outputs/apk/foss/debug/openptt-foss-debug.apk`
- Server SSH: `ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch`
- Server repo: `/opt/ptt`
- Traccar admin (internal): `admin@ptt.local` / `admin` (see
  `server/config.py`)

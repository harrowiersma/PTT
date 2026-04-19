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

### Commercial-peer feature research (2026-04-19)
Background research on what Hytera HyTalk Pro, Motorola WAVE PTX, Zello
Work, Tait AXIOM, and ESChat ship that openPTT doesn't. Full landscape +
selection reasoning lives in
`~/.gstack/projects/PTT/harrowiersma-main-design-20260419-152016.md`.
The five items below were picked as the next iteration's theme
("day-to-day ops upgrade"). A parallel track — safety & situational
awareness (man-down, geofencing automation, dispatch video-on-demand,
voice recording + transcription) — is captured in the design doc and
intentionally deferred until this theme lands.

### Photo + file messaging in channels (implementation TBD)
Zello / Motorola WAVE / ESChat all ship multimedia messaging as a core
feature. Our current text-chat path carries strings only. For
field-to-dispatch clarity ("here's the thing I'm looking at"), this is
the single highest-daily-value add from the research.

**Shape (sketched):**
- New `message_attachments` table keyed by a Mumble text-message id.
- P50 app: add an attach-image button in the chat UI that uploads to
  the admin API, receives an attachment_id, sends a Mumble text message
  with a `[img:N]` sentinel that other clients render as a thumbnail.
- Dashboard: chat tab per channel with inline image previews + full-size
  modal on click.
- Storage: `/var/openptt/attachments/` mounted into admin, similar to
  the `/var/openptt/apk/` pattern from provisioning. Age-out policy TBD
  (30 days default?).

Effort: S-M. Dependencies: existing dashboard chat surface; Humla app
text-message plumbing; admin container volume mount + retention job.

### User status / presence (implementation TBD)
Zello and Motorola WAVE surface a per-user status (available / busy /
off-shift). Today the dashboard shows channel membership but no intent.
A status field lets dispatchers see "Sarah is at lunch" before
whispering a dispatch to her.

**Shape:**
- New `users.status_label` + `users.status_updated_at` columns.
- P50 app: status picker in the drawer (3-4 fixed options + custom).
- Dashboard: status badge next to the username in Live Ops + user list.
- Interplay with lone-worker shift: "off-shift" status auto-stops any
  active shift; starting a shift auto-sets "on-duty."

Effort: S. Smallest item of the five — good warm-up.

### Private 1:1 PTT (not channel-based) (implementation TBD)
Zello's signature feature. User picks a target from their contact list
and presses PTT; only the target hears it. We have a server-side whisper
path already (used by dispatch TTS); this extends it into a radio-side
picker flow.

**Shape:**
- P50 app: "call user" UI — a contact picker list (filtered to fleet
  members) + a picked-target-for-next-PTT state. While armed, PTT
  transmits as a Mumble whisper to the target session instead of a
  channel broadcast. Target hears only that user.
- Admin API: GET `/api/users?online=true` already exists; app polls it
  for the picker list.
- Hardware-key question: which P50 key enters "call user" mode? Every
  pattern collides with something. Likely app-side UI + soft button,
  not a keycode.
- Timeout: after N seconds of no transmission, drop the target and
  revert to channel PTT.

Effort: M. Depends on answering the hardware-key UX question first.

### Dynamic user-created channels (implementation TBD)
Zello's other big idea — any user with the flag can create a channel on
the fly ("hey let's talk in `job-site-7`"), and it auto-cleans when
empty for N minutes. Reduces admin toil when field teams self-organize.

**Shape:**
- New `users.can_create_channels` boolean (admin-gated, off by default).
- P50 app: long-press the channel switcher to prompt for a channel name.
  Calls admin's `POST /api/channels` (which already uses admin_sqlite +
  murmur restart from Priority 1).
- Admin: add a "dynamic" flag on channels; a reaper runs every 5 min
  and deletes dynamic channels empty for 15 min.
- Interplay with call groups (the other open item): dynamic channels
  inherit the creator's call-group membership by default.

Effort: M. Depends on call groups being specced (so the membership
inheritance rule is clear) and on accepting the ~3 s Murmur-restart
hit on every channel create (acceptable at the hoped-for cadence of
a few per week).

### Web / desktop client for office users (implementation TBD)
Today, joining Mumble requires the P50 app or a desktop Mumble client
(separate install). Zello and Motorola WAVE ship web and Windows
clients so office staff can participate without dedicated hardware. A
browser PWA that connects to Mumble via a websocket proxy lets
dispatchers and managers listen and text-chat from any browser.

**Shape (two variants, decision needed):**
- **Slim path:** admin API already exposes channels + chat; build a
  React/Preact page that renders channel tree + text chat + user list.
  No voice. Talks only to admin, not Murmur directly.
- **Full path:** WebRTC or websocket proxy in front of Murmur, full
  Opus decode in browser (via mumble-web or similar), voice + text.

Effort: L (slim) to XL (full). Defer to after the first four items
land; pick variant based on actual demand from office users.

### Call groups — per-user channel-access scoping (implementation TBD)
Today every user can see and join every channel the Mumble server
knows about. As the fleet grows (multiple teams, sites, or customers
sharing a server) we need a way to scope a user's visible channel set
to a subset — a "call group."

**Shape (sketched, not committed):**
- New `call_groups` table + `user_call_groups` join table.
- Each channel gets an optional `call_group_id` (null = visible to all,
  the current default).
- A user sees and can join only channels whose `call_group_id` is in
  their group membership set, or is null.
- Enforcement lives in `MurmurClient`: on `CHANNEL_CHANGE`, bounce a
  user out of any channel whose `call_group_id` isn't in their set
  (same mechanism as the Phone ACL in commit `01ee04c`).
- Dashboard: a Call Groups tab under Directory. On the user edit form,
  a multi-select of groups. On a channel edit form, a single-select.

**Open questions:**
- Do we also hide non-visible channels from the user's client channel
  tree (requires Murmur-side ACL work, harder) or just bounce on entry
  (simpler, uses the pattern we already have)?
- How does this interact with the Phone / Call-N sub-channels? Probably
  orthogonal — `can_answer_calls` gates Phone, call group gates the
  broader channel tree.
- Should there be a default "all users" group, or do new users start
  with zero group memberships?
- Super-admin escape hatch: admins ignore call-group restrictions so
  they can moderate any channel.

Effort estimate: M. Schema + migration + ACL callback + two dashboard
tabs. No Murmur protocol changes required if we do bounce-on-entry;
much more if we go client-tree-hiding.

### Physical / operational
- ~~**#6 Install openPTT TRX on harro's P50**~~ — **Resolved 2026-04-18**:
  APK installed on device `R259060623` via `adb install -r` (same build
  that includes the triple-tap lone-worker gate). No `harro` Murmur
  registration existed (only `SuperUser`), so no certificate cleanup
  was needed. In-app configuration (Mumble creds + admin URL + optional
  lone-worker-mode toggle) is the manual remaining step until the
  one-click provisioning script ships.

### ~~P50 provisioning script~~ — **Resolved 2026-04-19** (commit `5d95ba2`)
Admin generates a per-device short-link `ptt.harro.ch/p/<slug>` from the
user-edit modal. Token is single-use, 24 h TTL, single-view password
reveal. The script (bash on macOS/Linux, PowerShell on Windows) picks
up the connected P50 via ADB, installs the APK from `/apk/openptt-foss-debug.apk`,
pushes seeded SharedPreferences + Humla `mumble.db`, grants the
`RECORD_AUDIO`/`ACCESS_FINE_LOCATION`/`POST_NOTIFICATIONS` perms, and
launches MumlaActivity. Short-link + QR code shown in the dashboard.

**Known follow-up:** the APK served at `/apk/openptt-foss-debug.apk`
is debug-signed. Wiring the CI signed-release flow to overwrite
`/var/openptt/apk/openptt-foss-debug.apk` is a separate task.

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
- ~~Radio-initiated hangup gesture~~ — **Resolved 2026-04-19** (commit `127132f`).
  `KEYCODE_MENU` in MumlaActivity → admin `/api/sip/hangup-current` →
  `docker exec pkill -USR1` into sip-bridge → `Client.hangup_from_radio()`
  sends an AudioSocket HANGUP frame so Asterisk tears down the SIP leg.
  Only active while the user is in the `Phone` channel; TTS confirms.
- ~~Admin-editable greeting text~~ — **Resolved 2026-04-19** via
  `sip_trunks.greeting_text` + `PUT /api/sip/greeting` + live WAV
  push to the sip-bridge container (commits `85dee5e`, `d9343aa`).
  Dashboard SIP tab has a textarea with Save & regenerate.
- ~~Per-call sub-channels (`Phone/Call-N`)~~ — **Resolved 2026-04-19**
  (commit `a1d7281`). `PHONE_MAX_CALLS` (default 3) slots provisioned
  at sip-bridge startup via a new
  `POST /api/sip/internal/ensure-phone-slots` admin endpoint
  (`admin_sqlite.ensure_phone_slots_and_restart`). Each concurrent call
  gets its own `PTTPhone-N` Mumble bot joined to `Phone/Call-N` so audio
  streams stay isolated between callers. Dialplan cap reads
  `ENV(PHONE_MAX_CALLS)`; Phone ACL and the ding-notification loop both
  consider any `Phone/Call-N` membership equivalent to `Phone`. Control
  signals target the most-recent client; per-slot targeting is a
  follow-up once the app exposes a slot picker.
- ~~Green-button (`KEYCODE_CALL`) mute toggle~~ — **Resolved 2026-04-19**
  (commit `127132f`). Only active in `Phone` channel; toggles
  `Client.mute_caller` in the bridge, which ships 320 B silence frames
  on the downlink while still draining the Mumble rx queue so no
  backlog builds up during the mute window. TTS confirms each toggle.
- ~~ACL enforcement on `Phone` channel entry~~ — **Resolved 2026-04-19**.
  `MurmurClient` watches `PYMUMBLE_CLBK_USERUPDATED`; non-eligible
  users who walk into Phone are bounced back to their previous channel
  and whispered "Phone channel requires call-answer permission". 30-s
  eligible-set cache refreshed from `users.can_answer_calls`.
  14 unit tests (`tests/test_phone_acl.py`). Commits `01ee04c`, `094855e`.
  Extended to `Phone/Call-N` sub-channels in commit `a1d7281`.

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

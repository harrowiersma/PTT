# openPTT TRX — Open Issues

Last updated: 2026-04-22

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

### ~~Photo + file messaging in channels~~ — **Dropped 2026-04-21**
Operator decision: not needed for the current iteration. Re-open if
field workers start asking for it.

### ~~User status / presence~~ — **Resolved 2026-04-21**
Shipped as 11 tasks across server + dashboard + openPTT-app. Three-state
presence (`online | busy | offline`) set from the P50's orange top
button (`KEYCODE_F4`) with TTS confirmation; auto-Online on every Mumble
connect (`PYMUMBLE_CLBK_USERCREATED` hook); shift coupling gated on
`features.lone_worker + is_lone_worker` (shift start force-sets Online,
Offline ends active shift); dispatch `find_nearest` filters to
online-AND-connected; dashboard pills in Live Ops + Directory with a
🔇 icon when device is not audible (ringer silent or voice-stream
volume 0); user-edit modal has a Status dropdown with an audit-sourced
"Last changed" line; audit log renders each change as
`[pill] → [pill] via <source>`.

Commits (server): `54a04162` (schema) → `50422f8a` (endpoints) →
`c389a36a` (shift start Online) → `2c8a00d6` (Offline ends shift) →
`b754511d` (auto-Online hook) → `0684aeb` (dispatch filter) →
`771517b` (dashboard pills) → `636dbd2` (edit modal) → `76fab7d`
(audit polish + directory pill fix).
Commits (app): `0210ef5` (orange-button cycle) → `5a26fff` (carousel
pill + hydration). Both P50s flashed with `3.7.3-44-g5a26fff-debug`.
Design: `docs/plans/2026-04-21-user-status-presence-design.md`.
Plan: `docs/plans/2026-04-21-user-status-presence.md`.

### ~~Hide offline-status users from P50 channel user list~~ — **Resolved 2026-04-22**
Shipped as 8 tasks across server + app (debug-build APK installed on
both P50s). New `GET /api/users/presence-map` endpoint returns the
whole `{lc-username: {status_label, is_audible}}` picture in one call
(no auth, bots excluded). App-side `PresenceCache` polls every 20 s
while Mumble-connected (immediate refresh on connect + after every
`postStatus`); cache notifies `PresenceCache.Listener`s on map-content
change so channel-list adapters re-submit without a Mumble event.
`PresenceFilter.isHidden` (mirrors `BotUsers.isBot`) wired into
`UserRowAdapter.submit` + `ChannelCardFragment` + `ChannelCarouselFragment`
member counts. Self always visible; Busy users render with an amber
**BUSY** badge in the row; only Offline hides.

Decisions locked: hide Offline only (not Busy), polling at 20 s, no
"show everyone" toggle (YAGNI).

Commits (server): `19709ce` (presence-map endpoint).
Commits (app): `9081ef0` (PresenceCache + lifecycle) → `f831d93`
(PresenceFilter + 9 JUnit) → `a12b45b` (UserRowAdapter wiring) →
`b5d994e` (member counts) → `1ecf50d` (Busy badge) → `18e0b83`
(repaint listener).
Design: `docs/plans/2026-04-22-hide-offline-from-channel-list-design.md`.
Plan: `docs/plans/2026-04-22-hide-offline-from-channel-list.md`.

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

### Dynamic user-created channels — **Deferred 2026-04-21**
Operator decision: defer. Worth re-evaluating after call groups land
(membership inheritance rule needs to be defined first) and after
investigating whether the voice-server runtime channel-create path
can avoid the ~3 s restart hit.

### Office-user access — Microsoft Teams bridge via Graph API (implementation TBD)
Re-scoped 2026-04-21 from "build our own web/desktop client" to
"reuse Microsoft Teams." Office staff already live in Teams; standing
up our own browser/desktop client duplicates a tool they already pay
for and have to learn. Instead, investigate bridging Teams calls
into a radio channel via the Microsoft Graph API + Teams calling
APIs so an office user can dial a channel from Teams and be heard
on the radios.

**To investigate (in this order):**
- Which Teams API surface fits: Cloud Communications (Graph
  `/communications/calls`), Teams Phone (PSTN), or a bot
  identity that joins as a participant.
- Auth model: app-only with admin consent vs. delegated user
  consent. Tenant-level configuration cost.
- Audio path: real-time audio access requires registering as a
  Communications-API-enabled bot. Latency target: comparable to
  the SIP gateway today (sub-second).
- Bidirectional vs. listen-only: do we want office users to also
  hear the radio channel back into Teams, or is one-way
  (Teams→radio) enough for v1?
- Licensing: which Microsoft 365 / Teams Phone SKUs the operator's
  tenant already has; whether we need additional ones.
- Compare effort + dependency footprint against the original
  "build our own client" path before committing.

**Shape (sketched, pending the investigation above):**
- Admin: a "Teams bridge" config card with the tenant ID, app
  registration credentials, and per-channel mapping (which Teams
  meeting / call queue routes to which radio channel).
- Bridge service: small container that authenticates via Graph,
  joins Teams calls as a bot, and pipes audio to/from a per-bridge
  Mumble user (same pattern as the existing SIP bridge).
- Dashboard: a "Bridges" sub-tab under System listing live bridges.

Effort: M-L (depending on investigation outcome). Lower than
maintaining a custom voice client if Graph supports the audio path
cleanly.

### ~~Call groups — per-user channel-access scoping~~ — **Resolved 2026-04-22**

Shipped in two layers, staged so the second can be rolled back via a
feature flag without regressing the first.

**Layer 1 (bounce-on-entry) — commits `237dec3`, `6c7e231`, `f590f3e`,
`6136cc5`:**
- `call_groups` + `user_call_groups` tables, optional
  `channels.call_group_id` (NULL = visible-to-all).
- Lifespan poller (30 s) mirrors DB state into the bridge's
  `MurmurClient`, which bounces any non-member who enters a tagged
  channel (same pattern as the Phone ACL in commit `01ee04c`).
- Sweep on every state refresh so users already sitting in a tagged
  channel when the bridge (re)connects are evicted within one cycle.
- Admins (`users.is_admin`) bypass the check so they can moderate.
- Dashboard: Call Groups sub-tab under Directory, group modal with
  member and channel checkboxes, delete-cascade-compatible join-row
  cleanup.

**Layer 2 (true Murmur ACL hiding) — commits `a605678`, `a3b12b1`,
`d47ba5a`, `b44f498`, `086b431`, `40130c4`, `0b5fbdd`:**
- `users.mumble_cert_hash` + `users.mumble_registered_user_id` columns
  (migration `h4c9e5a7f3b2`).
- Bridge captures each user's Mumble cert SHA-1 on USERCREATED /
  USERUPDATED and writes it back to the admin DB.
- Background auto-registration scheduler (60 s, batch size 10)
  registers captured users in Murmur's sqlite so the ACL table can
  reference them by `user_id`.
- `admin_sqlite.set_channel_acl` / `clear_channel_acl` /
  `batched_acl_apply` write a deny-@all + per-member allow pair to
  Murmur's `acl` table. One murmur restart per batch.
- Endpoint wiring in `server/api/call_groups.py`: PUT /members, PUT
  /channels, DELETE /{id} recompute the ACL after the DB commit.
- Dashboard: per-user registration pill in the group modal, "Force all
  reconnect" escape hatch on the Call Groups tab.
- Feature flag `call_groups_hiding` (seeded `false` by migration
  `i5d8a2f4c6b7`). Flag off → falls back to layer 1 (bounce-only).
  Operator flips it after observing that every connected user has a
  captured cert hash.
- Password-authenticated registration (fix commit `3ed7b3e`): first
  pass registered users with pw=NULL + only a cert hash, intending
  cert-based auth, which broke password logins. Now `register_user`
  also hashes `User.mumble_password` with PBKDF2-HMAC-SHA384 (Murmur's
  exact recipe from `src/murmur/PBKDF2.cpp` — UTF-8 password, 8-byte
  random salt, 48-byte dk, 8000 iters) so Mumble's own auth path
  validates the row unchanged. Cert hash is additive when provided.

**Companion app commit — openPTT-app `91fd796`:**
- `HumanChannels.isVisible(IChannel)` now also checks the channel's
  Mumble Traverse permission; carousel and F5/F6 knob rotation hide
  channels the server says the user can't Traverse.
- `ChannelCarouselFragment` fires `requestPermissions()` for each
  Root child on rebuild and re-runs rebuild when
  `onChannelPermissionsUpdated` arrives, so the hide step completes
  within one round-trip of a fresh connect.
- Smoked on yuliia's P50: Sales vanishes from the carousel and F6
  rotates Internal → Weather → business, skipping Sales entirely.

Original open questions (2026-04-21) and their resolution:
- Tree-hiding vs bounce-only → **both.** Layer 1 always on, layer 2
  on when the operator opts in.
- Interaction with Phone / Call-N → orthogonal (as predicted).
  `can_answer_calls` still gates Phone; call groups scope the rest of
  the tree.
- Default group → none. New users start with zero memberships; all
  their channels stay visible-to-all until a group is assigned.
- Super-admin escape hatch → `users.is_admin == true` bypasses the
  bounce check in layer 1. Layer 2's ACL references admins like any
  other user but they can always use /force-reconnect.

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

### ~~True call hold — channel switching while on hold + caller hold tone/music~~ — **Resolved 2026-04-22**
Green button now does real consultation hold: caller hears music loop,
operator can navigate freely (carousel knob unlocks while held), green
from anywhere resumes + auto-navigates back to `Phone/Call-N` and
re-raises `ActiveCallActivity` with the original caller id. Putting a
call on hold also pre-navigates the operator to their pre-call channel
so they can PTT immediately. Module-level `_HELD_CLIENT` enforces one
held call at a time (second-call HOLD logged as refused); 180 s
auto-hangup timeout via background thread; `_HELD_CLIENT` +
`/tmp/openptt-hold-state.json` reset instantly when the caller hangs
up so the "CALL HOLD" banner clears on the app's next 5 s poll. Also
handles the inverse: when the `PTTPhone-<slot>` bot leaves Mumble
(caller-side hangup or bridge teardown) AND the operator's session is
still in a `Call-*` channel, restore the pre-call channel so the
radio isn't stranded. `phoneHangup()` restores pre-call itself so the
MENU key works even when the bridge SIGUSR1 is a no-op.

Asset: `Opus1.mp3` pre-transcoded to `sip_bridge/assets/hold-music.slin8`
(8 kHz mono slin8, 338 s, 16912 frames). `render-hold-music.sh` script
committed for future re-rendering. Admin endpoints:
`POST /api/sip/hold-toggle` + `GET /api/sip/hold-state`;
`/mute-toggle` kept as a deprecated alias for one release.

Carousel-empty bug fix piggybacks: `HumlaObserver.onConnected` now
triggers a rebuild, closing a cold-start race where `onChannelAdded`
fired before the fragment's observer was registered. And
`docker-compose.yml` pins `MUMBLE_CONFIG_DATABASE` so an image refresh
can't silently switch the sqlite path on the running murmur again.

Commits (server): `b1be154`, `6ca2d73`, `6d29f0a`, `c755f56`, `e469160`.
Commits (app): `29e9ff3`, `c1da0ae`, `736a16a`, `6f315a9`, `e7ad4a6`,
`04928a2`, `68f35b4`, `258a2d3`, `547d387`, `0776b63`. Design:
[`docs/plans/2026-04-22-true-call-hold-design.md`](plans/2026-04-22-true-call-hold-design.md).
Plan: [`docs/plans/2026-04-22-true-call-hold.md`](plans/2026-04-22-true-call-hold.md).
Smoke-tested on R259060623 + R259060618 (`3.7.3-63-g0776b63-debug`):
hold/resume audio + banner, concurrent-call refusal, 180 s auto-hangup,
caller-hangup-while-held channel restore, and the mute-toggle alias
for un-upgraded radios all green.

### ~~Red button (POWER) as reject/hangup~~ — **Resolved 2026-04-22**
Ad-hoc ask from the evening of the same-day ship train. P50's red button
is physically the same key as POWER (raw kernel `KEY_POWER`, Android
`KEYCODE_POWER` 26 — confirmed via adb `getevent` probe). Before this
ship, pressing red during a call did nothing useful (OS default: screen
off). Intercepted `KEYCODE_POWER` in `IncomingCallActivity.dispatchKeyEvent`
(→ Decline) and `ActiveCallActivity.dispatchKeyEvent` (→ Hangup), scoped
so the main carousel retains default OS power-off behaviour. The OS-level
`PhoneWindowManager` still turns the screen off in parallel — we can't
suppress that from `dispatchKeyEvent`, but the call action fires first.
Accepted UX tradeoff: mirrors the "hang up handset → phone at rest"
metaphor. For held calls from a non-phone channel, operator presses
green to resume (auto-navigates back into `ActiveCallActivity`), then red
to end — two presses from the held state, keeps `MumlaActivity`'s POWER
semantics clean.

Verified via live logcat during a real inbound call: `hardware POWER →
Decline` fires cleanly; `/api/status/server` confirms the Mumble session
stays in the operator's original channel (no phantom move). Commits
(app): `39a873d`. Smoke-tested on R259060623 + R259060618
(`3.7.3-65-g39a873d-debug`).

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

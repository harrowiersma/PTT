# True Call Hold — Design

**Date:** 2026-04-22
**Scope:** SIP gateway behaviour change (sip-bridge + admin + openPTT-app).
**Branch target:** `main` on both repos.

---

## Goal

Make the green button on the P50 do consultation hold instead of one-way mute:

1. Caller hears a recognisable hold-music loop (not silence).
2. Operator can navigate to a different Mumble channel to confer with colleagues.
3. Operator presses green again from any non-Phone channel to resume — auto-navigates the radio back to the held `Phone/Call-N`.

Closes the two gaps the operator reported on 2026-04-22.

---

## Decisions (locked)

1. **In-bridge mixer, not Asterisk MusicOnHold.** The downlink loop in `audiosocket_bridge.py::_downlink_loop` already runs every 20 ms; we add a `hold_caller=True` branch alongside the existing `mute_caller` one and ship pre-rendered hold-music frames. Asterisk MoH would require breaking the AudioSocket pin (re-INVITE + dialplan re-entry) — significant complexity for no architectural gain. We already own the audio path; use it.
2. **Replace `mute_caller` semantics with `hold_caller`.** The operator already calls today's mute "hold." Single state machine, no dual-concept confusion. The existing `/api/sip/mute-toggle` HTTP route stays for one release as an alias to ease device-update lag, then drops.
3. **One held call at a time in v1.** Multi-hold semantics get hairy (which one resumes? what about a third call?). Trying to HOLD a second call while one is held → server 409, radio TTS *"Resume held call first."* Honest error beats surprise behaviour.
4. **Knob lock relaxes only when a call is held.** `MumlaActivity.dispatchKeyEvent` continues to lock F5/F6/DPAD inside `Phone/Call-*` for ACTIVE calls. When `hold_caller=true` for the operator's slot, the lock releases. App polls `/api/sip/hold-state` to know.
5. **Resume = green button from any non-Phone channel.** When green is pressed and the operator is NOT in a `Phone/Call-*` channel, app POSTs `/api/sip/hold-toggle` → server flips `hold_caller=false` → app auto-navigates the radio back to the held `Phone/Call-N`. TTS: *"Resumed."*
6. **Hold timeout: 180 s default.** Configurable via `PHONE_HOLD_TIMEOUT_SECONDS` env on the bridge container. After 3 min on hold without resume, the bridge auto-hangs-up the caller (caller hears music throughout, then disconnect). Prevents indefinite hold if operator forgets.
7. **#7 architecture answer (folded in): app-side detection stays.** Single-key intent (green button) belongs on the device — immediate response, no Mumble round-trip. The original #7 motivator was timing-precision gestures (triple-tap), where server-side detection would be fuzzier; for single-key controls, app-side is correct on every dimension. This design is the precedent for future hardware-key features.

---

## Backend — `sip_bridge`

### `audiosocket_bridge.py`

Add module-level state:

```python
_HELD_CLIENT: Optional["Client"] = None
_HELD_LOCK = threading.Lock()
PHONE_HOLD_TIMEOUT_SECONDS = int(os.environ.get("PHONE_HOLD_TIMEOUT_SECONDS", "180"))
```

Replace the `Client.mute_caller` field with `hold_caller` (same default `False`) and add `hold_started_at: Optional[float] = None`.

Add `_get_hold_frames()` mirroring `_get_ringback_frames()`. Soft 4-note loop (C-E-G-E, sinewave amplitude 0.15, ~10 s total) synthesized at startup, returned as a list of slin8 frames (320-byte each = 20 ms at 8 kHz). No external WAV file.

In `_downlink_loop`, replace the existing `if self.mute_caller:` branch with:

```python
if self.hold_caller:
    slin8 = hold_frames[hold_idx % len(hold_frames)]
    hold_idx += 1
    # Drain rx so unhold doesn't dump backlog
    try: self._rx_queue.popleft()
    except IndexError: pass
    ringback_idx = 0
```

(rx-drain logic identical to today's mute path.)

Replace `_on_mute()` SIGUSR2 handler with `_on_hold()`:

```python
def _on_hold(signum, frame):
    target = _get_most_recent()
    if target is None:
        LOG.info("SIGUSR2 received, no active call — ignoring")
        return
    global _HELD_CLIENT
    with _HELD_LOCK:
        if target.hold_caller:
            # Resuming THIS call.
            target.hold_caller = False
            target.hold_started_at = None
            if _HELD_CLIENT is target:
                _HELD_CLIENT = None
            LOG.info("SIGUSR2 — resume slot=%d", target.slot)
        elif _HELD_CLIENT is not None and _HELD_CLIENT is not target:
            # Another call already held. Refuse.
            LOG.warning("SIGUSR2 — refused, slot=%d already held",
                        _HELD_CLIENT.slot)
        else:
            # Putting THIS call on hold.
            target.hold_caller = True
            target.hold_started_at = time.monotonic()
            _HELD_CLIENT = target
            LOG.info("SIGUSR2 — hold slot=%d", target.slot)
```

Add `_hold_timeout_loop()` running in a daemon thread (started in `serve()`):

```python
while not _stop:
    time.sleep(10)
    with _HELD_LOCK:
        if _HELD_CLIENT and _HELD_CLIENT.hold_started_at:
            if time.monotonic() - _HELD_CLIENT.hold_started_at \
                    > PHONE_HOLD_TIMEOUT_SECONDS:
                LOG.info("hold timeout, hanging up slot=%d",
                         _HELD_CLIENT.slot)
                _HELD_CLIENT.hangup_from_radio()
                _HELD_CLIENT = None
```

### Read endpoint for app polling

Add a tiny TCP/HTTP-style state surface. Two options — pick at implementation time:

- (a) Bridge writes `/tmp/openptt-hold-state.json` every state change; admin reads it via `docker exec cat`.
- (b) Bridge exposes a small Unix socket the admin queries.

Most pragmatic: (a) — file-based, low ceremony, admin already does `docker exec` for `pkill`. Will land that way unless something blocks it.

---

## Backend — `server/api/sip.py`

### `POST /api/sip/hold-toggle`

Replaces the semantic of `mute-toggle`. Same body (`{"username": str}`), same un-authed convention, same `_signal_sip_bridge("SIGUSR2")`. Logs `hold` instead of `mute`.

`POST /api/sip/mute-toggle` remains as a thin alias for one release — same code path, deprecation comment in the docstring.

### `GET /api/sip/hold-state`

Device-trusted (no auth — same convention as `/api/users/status`). Reads the bridge's state file via `docker exec cat /tmp/openptt-hold-state.json`. Returns:

```json
{
  "holding": true,
  "slot": 1,
  "held_for_seconds": 47,
  "operator_username": "harro"
}
```

When no call is held: `{"holding": false}`.

App polls every 5 s while connected (cheap, like the presence cache pattern). The handler also writes a freshness timestamp into the state file so a stale file (bridge crashed) returns `{"holding": false}` after a short TTL.

---

## App — `openPTT-app`

### `MumlaService` additions

- `volatile boolean mHoldingCall` + `int mHoldingSlot`.
- `HoldStateClient` — parallels `PresenceCache` (5 s polling, on-demand refresh after `phoneHoldToggle()`), populates the two fields.
- Rename `phoneMuteToggle()` → `phoneHoldToggle()`. Change endpoint to `/api/sip/hold-toggle`. After a successful POST, immediately refresh hold-state. If the response indicates a resume, call `switchChannelByName("Phone/Call-" + slot)` to auto-navigate back.

### `MumlaActivity.dispatchKeyEvent`

Two changes:

1. The knob-lock check (`if (isDir && isInActivePhoneCall())`) gains a held-state escape: `if (isDir && isInActivePhoneCall() && !mService.isHoldingCall()) { ... block }`.
2. `KEYCODE_CALL` handler: instead of only firing inside the Phone tree, fire when EITHER (a) inside Phone tree (start hold) OR (b) `mService.isHoldingCall()` is true (resume from anywhere).

### Carousel hold-banner

New `R.id.hold_banner` in `fragment_channel_carousel.xml` — a small chip at the top showing *"Call on hold (Slot N) — press green to resume"* when `mHoldingCall=true`. Hides otherwise. Matches the existing presence-pill aesthetic but distinct enough to not be confused with a status badge.

### Strings

- Drop `phone_mute_toggled_tts`.
- Add `phone_held_tts` = `"On hold."`
- Add `phone_resumed_tts` = `"Resumed."`
- Add `phone_already_held_tts` = `"Resume held call first."` (TTS for the 409 case from the server.)
- Add `phone_hold_banner` = `"Call on hold (Slot %d) — press green to resume"`.

Update `phone_call_knob_blocked` text? Today's *"Press MENU to hang up"* still applies for active (non-held) calls. Leave as is.

---

## Hold-music asset

Operator provided `Opus1.mp3` (5.4 MB, 5 min 38 s, 128 kbps stereo @ 44.1 kHz). Pre-transcoded once locally to **`sip_bridge/assets/hold-music.slin8`** — 8 kHz mono int16 PCM, ~5.16 MB, 16,912 slin8 frames (20 ms each = 338 s of audio). Committed binary; no runtime decode, no ffmpeg dependency in the container.

A small `sip_bridge/assets/render-hold-music.sh` script regenerates the .slin8 from any source MP3 path:

```bash
ffmpeg -y -i "$1" -ac 1 -ar 8000 -f s16le sip_bridge/assets/hold-music.slin8
```

Bridge load path:

```python
HOLD_FRAMES_PATH = Path(__file__).parent / "assets" / "hold-music.slin8"
SLIN8_FRAME_BYTES = 320  # 20 ms at 8 kHz mono int16

def _get_hold_frames() -> list[bytes]:
    """Load the pre-transcoded hold-music asset and slice into 20 ms slin8
    frames. Same shape as _get_ringback_frames; cached at startup."""
    raw = HOLD_FRAMES_PATH.read_bytes()
    return [raw[i:i + SLIN8_FRAME_BYTES]
            for i in range(0, len(raw), SLIN8_FRAME_BYTES)
            if len(raw[i:i + SLIN8_FRAME_BYTES]) == SLIN8_FRAME_BYTES]
```

The hold timeout (180 s default) means a single hold session plays the first ~9,000 frames. The loop only wraps if the operator pushes past timeout (which auto-hangs-up the call anyway). Fine for v1.

To rotate the music: replace the source MP3 anywhere on disk, run `bash sip_bridge/assets/render-hold-music.sh /path/to/new.mp3`, commit the regenerated `.slin8` + push.

---

## Testing

### Bridge unit (`tests/sip_bridge/test_hold_state.py`)

Mostly state-machine tests:

- `test_hold_sets_flag_and_drains_rx` — call `_on_hold()` on a fake client → `hold_caller=True`, `_HELD_CLIENT` is set; subsequent `_downlink_loop` tick ships hold-frame bytes (not silence, not Mumble rx).
- `test_resume_clears_flag` — second `_on_hold()` on same client → `hold_caller=False`, `_HELD_CLIENT` is None.
- `test_second_hold_refused` — held client A, signal SIGUSR2 with target client B (mocked) → B's hold_caller stays False, log warning emitted.
- `test_timeout_hangs_up` — fast-forward `PHONE_HOLD_TIMEOUT_SECONDS` → timeout loop calls `hangup_from_radio()` on the held client.

### Server API (`tests/test_sip_hold_api.py`)

- `GET /api/sip/hold-state` returns `{"holding": false}` when no file exists.
- `POST /api/sip/hold-toggle` returns 200 (no auth needed). Doesn't actually verify the bridge state — that's e2e.
- `POST /api/sip/mute-toggle` (deprecated alias) still returns 200 (regression — for one release).

### Manual smoke (post-deploy)

1. Call the DID from a phone. Answer on a P50.
2. Press green on the answering radio. Caller hears music. Radio TTS: *"On hold."*
3. Turn the channel knob away from `Phone/Call-1` to e.g. `Root`. No knob block.
4. Press green again. Radio TTS: *"Resumed."* Radio auto-navigates back to `Phone/Call-1`. Caller hears the operator again.
5. Repeat 1-2, then leave the call on hold for 3+ minutes. Caller is auto-hung-up. Asterisk log shows the hangup.
6. With a held call active, dial in a SECOND call. Answer it (operator picks up via the new slot). Press green on the second call → TTS *"Resume held call first."*; second call stays active without going on hold.

---

## Out of scope (deferred)

- **Multi-hold UI** — pick from a list of held calls. Defer until anyone asks.
- **Per-slot signaling refactor** — today's "most-recent client" semantics for the initial-hold press; resume targets `_HELD_CLIENT` lookup. Adequate for v1.
- **Admin-customisable hold music** — upload a WAV, choose volume. Defer.
- **Hold-elapsed indicator on the dashboard's Call Log** — nice-to-have, defer.

---

## Migration

- Existing devices on `3.7.3-52-g7dc0b57-debug` keep saying *"Call mute toggled."* until they update — but the server-side action will be hold (not silence-only) once the bridge ships, which is actually fine UX-wise. Once devices upgrade, TTS matches behaviour.
- `/api/sip/mute-toggle` alias stays one release. Drop in next iteration.
- No DB migration. State lives in-memory on the bridge + a temp file.

---

## Dependencies + ordering

- Bridge changes (Tasks 1-3) ship before the app changes — server can land alone since the existing app keeps calling the (now-aliased) `/mute-toggle` and the new behaviour kicks in immediately on the first press.
- App changes (Tasks 4-6) add the knob-lock relaxation, hold banner, and resume auto-navigation. None of this BREAKS without the server changes; they layer cleanly.
- Final task: deploy + manual smoke (caller side + operator side).

Implementation plan: `docs/plans/2026-04-22-true-call-hold.md`.

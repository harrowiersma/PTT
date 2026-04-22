# True Call Hold Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the green button do consultation hold (caller hears music, operator can navigate to confer, green again to resume + auto-navigate back) instead of one-way mute.

**Architecture:** Pre-transcoded `Opus1.mp3` (committed at `sip_bridge/assets/hold-music.slin8`) feeds the existing `_downlink_loop` via a new `hold_caller` branch. SIGUSR2 toggles per-client hold state, with module-level `_HELD_CLIENT` enforcing one-held-at-a-time. A bridge-written `/tmp/openptt-hold-state.json` lets the admin expose `/api/sip/hold-state` to the radio app, which polls every 5 s, relaxes its carousel knob lock when held, and routes the green button to a resume from any non-Phone channel.

**Tech Stack:** Python (sip_bridge audio loop + state machine), FastAPI (admin endpoints), Java/Android (app polling + UI).

**Companion design doc:** `docs/plans/2026-04-22-true-call-hold-design.md`

---

## Pre-flight

```bash
# 1. Sidecars + admin up
docker ps --format "{{.Names}}" | grep -E "ptt-pytest|ptt-admin|ptt-sip-bridge"
# Expected: all three listed

# 2. Pytest baseline
docker exec ptt-pytest sh -c "rm -f test.db && python -m pytest tests/ --ignore=tests/sip_bridge -q 2>&1 | tail -3"
# Expected: 114 passed (yesterday's hide-offline end-state; today's
# deterministic-signing was app-only and didn't touch server tests)

# 3. App build pipeline still good
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
./gradlew :app:testFossDebugUnitTest 2>&1 | tail -3
# Expected: 9 PresenceFilter tests pass

# 4. Source MP3 + transcoded asset already in place
ls -la sip_bridge/assets/hold-music.slin8
# Expected: ~5.16 MB file (committed by design-doc commit on the source MP3)
```

If any fails, fix before Task 1.

---

## Phases

1. **Task 1** — render script + slin8 asset + `_get_hold_frames()` + `Client.hold_caller` field + `_downlink_loop` branch.
2. **Task 2** — `_on_hold()` SIGUSR2 handler + `_HELD_CLIENT` state + hold-timeout loop + state-file writer.
3. **Task 3** — admin endpoints: `POST /api/sip/hold-toggle` + `GET /api/sip/hold-state` + tests.
4. **Task 4** — app `HoldStateClient` (5 s polling, mirrors `PresenceCache`) + `MumlaService` fields + interface method.
5. **Task 5** — `MumlaActivity.dispatchKeyEvent` knob-unlock-while-held + green-from-anywhere routing + service `phoneHoldToggle()` rename + auto-navigate-back on resume.
6. **Task 6** — carousel hold-banner UI + strings refresh.
7. **Task 7** — deploy server + bridge + push + APK install + manual smoke.

Each task is independently committable. Commit-per-task. Hard-stop after Task 7 to confirm prod and both P50s pass the manual smoke.

---

## Task 1: hold-music asset + bridge frame loader + `Client.hold_caller`

**Files:**
- Create: `sip_bridge/assets/render-hold-music.sh` (executable)
- Modify: `sip_bridge/Dockerfile` (COPY the new assets dir)
- Modify: `sip_bridge/audiosocket_bridge.py` (add `_get_hold_frames`, rename `mute_caller` → `hold_caller`, swap `_downlink_loop` branch)
- Test: `tests/sip_bridge/test_hold_state.py` (new)

The committed `sip_bridge/assets/hold-music.slin8` already exists (rendered when the design doc was finalised).

**Step 1: Create the render script**

```bash
mkdir -p /Users/harrowiersma/Documents/CLAUDE/PTT/sip_bridge/assets
```

Write `sip_bridge/assets/render-hold-music.sh` with this content:

```bash
#!/usr/bin/env bash
# Re-render the hold-music slin8 asset from a source MP3.
#
# Usage: bash sip_bridge/assets/render-hold-music.sh /path/to/source.mp3
#
# Output: sip_bridge/assets/hold-music.slin8 — 8 kHz mono signed 16-bit
# little-endian PCM. Read directly by audiosocket_bridge._get_hold_frames
# at startup. Commit the regenerated .slin8 alongside this script.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 /path/to/source.mp3" >&2
    exit 1
fi

SRC="$1"
OUT_DIR=$(cd "$(dirname "$0")" && pwd)
OUT="${OUT_DIR}/hold-music.slin8"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg not installed (brew install ffmpeg / apt install ffmpeg)" >&2
    exit 1
fi

if [[ ! -f "$SRC" ]]; then
    echo "source not found: $SRC" >&2
    exit 1
fi

ffmpeg -nostdin -loglevel error -y \
    -i "$SRC" \
    -ac 1 -ar 8000 -f s16le \
    "$OUT"

BYTES=$(wc -c <"$OUT")
SECONDS=$((BYTES / 16000))
FRAMES=$((BYTES / 320))
printf "rendered: %s\n  bytes:  %d\n  seconds: %d\n  frames:  %d (20ms each)\n" \
    "$OUT" "$BYTES" "$SECONDS" "$FRAMES"
```

```bash
chmod +x sip_bridge/assets/render-hold-music.sh
```

**Step 2: Update Dockerfile to ship the assets dir**

In `sip_bridge/Dockerfile`, find the cluster of `COPY <python file> /app/sip_bridge/<file>` lines (around line 37-40). Add a single line below them:

```dockerfile
COPY assets /app/sip_bridge/assets
```

Verify by listing the cluster after the change:

```bash
grep -nE "^COPY" sip_bridge/Dockerfile | head -20
# Expected: a new "COPY assets /app/sip_bridge/assets" line.
```

**Step 3: Write the failing test**

Create `tests/sip_bridge/test_hold_state.py`:

```python
"""State-machine tests for sip_bridge hold/resume.

These tests don't spin up real Mumble or Asterisk — they instantiate
Client objects with mocked sockets and exercise the hold path directly.
The downlink-loop branch is checked by inspecting which buffer the
loop selects when hold_caller is True.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add repo root so `import sip_bridge.audiosocket_bridge` works without
# the package needing to be pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sip_bridge import audiosocket_bridge as ab  # noqa: E402


def test_get_hold_frames_loads_committed_asset():
    """The committed sip_bridge/assets/hold-music.slin8 loads cleanly
    and yields a non-empty list of fixed-size 20 ms slin8 frames."""
    frames = ab._get_hold_frames()
    assert len(frames) > 100  # ≈ at least 2 seconds of audio
    for frame in frames[:10]:
        assert len(frame) == ab.SLIN8_FRAME_BYTES


def test_client_starts_with_hold_caller_false():
    """A freshly-constructed Client is not on hold."""
    sock = MagicMock()
    client = ab.Client(conn=sock, slot=1, mumble=None)
    assert client.hold_caller is False
    assert client.hold_started_at is None
```

**Step 4: Run the test, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/sip_bridge/test_hold_state.py -v"
```

Expected: both tests fail. The first fails with `AttributeError: module 'sip_bridge.audiosocket_bridge' has no attribute '_get_hold_frames'`. The second fails with `AttributeError: 'Client' object has no attribute 'hold_caller'`.

**Step 5: Add `_get_hold_frames` and the constants**

In `sip_bridge/audiosocket_bridge.py`, near the top with the other module-level constants (above the existing `_get_ringback_frames`), add:

```python
HOLD_FRAMES_PATH = Path(__file__).parent / "assets" / "hold-music.slin8"


def _get_hold_frames() -> list[bytes]:
    """Load the pre-transcoded hold-music asset and slice into 20 ms
    slin8 frames. Cached implicitly: callers store the returned list."""
    raw = HOLD_FRAMES_PATH.read_bytes()
    return [raw[i:i + SLIN8_FRAME_BYTES]
            for i in range(0, len(raw), SLIN8_FRAME_BYTES)
            if len(raw[i:i + SLIN8_FRAME_BYTES]) == SLIN8_FRAME_BYTES]
```

Make sure `from pathlib import Path` is already imported at the top of the module — if not, add it.

**Step 6: Rename `Client.mute_caller` → `Client.hold_caller`, add `hold_started_at`**

In `Client.__init__` (currently around line 312-323), replace:

```python
        # Green-button mute: when True, the downlink sends silence to
        # the caller while still draining the rx queue to avoid the
        # caller overhearing. Toggled by SIGUSR2 from the admin.
        self.mute_caller = False
```

with:

```python
        # Green-button hold: when True, the downlink ships hold-music
        # frames to the caller while still draining the rx queue (so
        # unhold doesn't dump backlog). Toggled by SIGUSR2 from the
        # admin. See _on_hold for the per-call state machine.
        self.hold_caller: bool = False
        # monotonic() timestamp when this client entered hold; used by
        # the timeout loop (PHONE_HOLD_TIMEOUT_SECONDS).
        self.hold_started_at: float | None = None
```

**Step 7: Swap the `_downlink_loop` branch**

In `_downlink_loop` (currently around lines 402-458), find:

```python
            # Highest priority: muted → ship silence regardless of source.
            # Radio users can hop to another channel and chat without the
            # caller overhearing.
            if self.mute_caller:
                slin8 = b"\x00" * SLIN8_FRAME_BYTES
                # Still drain the rx queue so we don't accumulate lag on unmute.
                try:
                    self._rx_queue.popleft()
                except IndexError:
                    pass
                ringback_idx = 0
            else:
```

Replace with:

```python
            # Highest priority: held → ship hold-music regardless of source.
            # Radio user can hop to another channel and chat without the
            # caller overhearing. rx_queue is drained so unhold doesn't
            # dump backlog.
            if self.hold_caller:
                slin8 = hold_frames[hold_idx % len(hold_frames)]
                hold_idx += 1
                try:
                    self._rx_queue.popleft()
                except IndexError:
                    pass
                ringback_idx = 0
            else:
```

And at the top of `_downlink_loop`, alongside `ringback_frames = _get_ringback_frames()` and `ringback_idx = 0`, add:

```python
        hold_frames = _get_hold_frames()
        hold_idx = 0
```

**Step 8: Run test, expect pass**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/sip_bridge/test_hold_state.py -v"
```

Expected: both tests pass.

Full sip_bridge suite for regressions:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/sip_bridge/ -v"
```

Expected: all existing sip_bridge tests still pass, plus the 2 new ones. (Take note of the pre-existing test count to compare.)

Server-side full suite:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 114 passed (no change — server tests untouched).

**Step 9: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add sip_bridge/assets/hold-music.slin8 sip_bridge/assets/render-hold-music.sh \
        sip_bridge/Dockerfile sip_bridge/audiosocket_bridge.py \
        tests/sip_bridge/test_hold_state.py
git commit -m "$(cat <<'EOF'
sip-bridge: hold-music asset + Client.hold_caller branch in downlink loop

Pre-transcoded Opus1.mp3 → 8 kHz mono slin8 (5.16 MB, 338 s, 16912
frames) committed at sip_bridge/assets/hold-music.slin8. Bridge reads
it at startup via _get_hold_frames(). Client.mute_caller renamed to
hold_caller; the downlink loop's silence branch now ships hold-music
frames. State-machine glue (SIGUSR2 handler, _HELD_CLIENT, timeout)
lands in Task 2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Self-review:**
- [ ] `assets/hold-music.slin8` and `assets/render-hold-music.sh` are in the commit (the .slin8 is staged as binary).
- [ ] `Dockerfile` adds the COPY for the assets dir.
- [ ] `audiosocket_bridge.py` has `_get_hold_frames` and `HOLD_FRAMES_PATH`.
- [ ] `Client.mute_caller` is gone; `Client.hold_caller` and `hold_started_at` are present.
- [ ] `_downlink_loop` references `hold_frames` and `hold_idx`.
- [ ] 2 new tests pass; existing sip_bridge tests still pass.
- [ ] Commit has Co-Authored-By trailer.

---

## Task 2: SIGUSR2 handler + `_HELD_CLIENT` + timeout + state-file writer

**Files:**
- Modify: `sip_bridge/audiosocket_bridge.py` (replace `_on_mute`, add timeout loop, add state-file writer)
- Test: `tests/sip_bridge/test_hold_state.py` (extend with state-machine tests)

**Step 1: Extend tests**

Append to `tests/sip_bridge/test_hold_state.py`:

```python
import json
import threading
import time
from unittest.mock import patch


@pytest.fixture(autouse=True)
def _reset_held_client():
    """Each state-machine test starts with no held client."""
    ab._HELD_CLIENT = None
    yield
    ab._HELD_CLIENT = None


def _make_client(slot: int = 1) -> ab.Client:
    sock = MagicMock()
    client = ab.Client(conn=sock, slot=slot, mumble=None)
    # Make _get_most_recent return this client by default.
    return client


def test_signal_puts_call_on_hold(monkeypatch):
    """Pressing green on an active call sets hold_caller and updates _HELD_CLIENT."""
    client = _make_client(slot=1)
    monkeypatch.setattr(ab, "_get_most_recent", lambda: client)

    ab._on_hold(signum=10, frame=None)

    assert client.hold_caller is True
    assert ab._HELD_CLIENT is client
    assert client.hold_started_at is not None


def test_second_signal_resumes(monkeypatch):
    """Pressing green again on the same call clears hold_caller and _HELD_CLIENT."""
    client = _make_client(slot=1)
    monkeypatch.setattr(ab, "_get_most_recent", lambda: client)

    ab._on_hold(signum=10, frame=None)
    assert client.hold_caller is True

    ab._on_hold(signum=10, frame=None)
    assert client.hold_caller is False
    assert ab._HELD_CLIENT is None
    assert client.hold_started_at is None


def test_second_call_hold_refused_when_already_held(monkeypatch, caplog):
    """If a different call is already held, the new HOLD attempt is refused."""
    held = _make_client(slot=1)
    held.hold_caller = True
    held.hold_started_at = time.monotonic()
    ab._HELD_CLIENT = held

    other = _make_client(slot=2)
    monkeypatch.setattr(ab, "_get_most_recent", lambda: other)

    with caplog.at_level("WARNING"):
        ab._on_hold(signum=10, frame=None)

    assert other.hold_caller is False
    assert ab._HELD_CLIENT is held
    assert any("refused" in r.message.lower() for r in caplog.records)


def test_state_file_written_on_hold(monkeypatch, tmp_path):
    """The state file is updated when a call enters hold and when it resumes."""
    state_path = tmp_path / "hold-state.json"
    monkeypatch.setattr(ab, "HOLD_STATE_FILE", state_path)

    client = _make_client(slot=2)
    monkeypatch.setattr(ab, "_get_most_recent", lambda: client)

    ab._on_hold(signum=10, frame=None)

    state = json.loads(state_path.read_text())
    assert state["holding"] is True
    assert state["slot"] == 2
    assert state["held_for_seconds"] >= 0

    ab._on_hold(signum=10, frame=None)
    state = json.loads(state_path.read_text())
    assert state["holding"] is False


def test_timeout_force_hangs_up(monkeypatch):
    """Past PHONE_HOLD_TIMEOUT_SECONDS, the timeout loop hangs up the held call."""
    client = _make_client(slot=1)
    client.hold_caller = True
    client.hold_started_at = time.monotonic() - 1000  # well past any sane timeout
    ab._HELD_CLIENT = client

    # Fast path through one iteration of the loop body.
    ab._hold_timeout_check()

    client.conn.sendall.assert_called()  # _send_frame was invoked
    assert ab._HELD_CLIENT is None
```

**Step 2: Run tests, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/sip_bridge/test_hold_state.py -v"
```

Expected: the new tests fail with `AttributeError`s on `_on_hold`, `HOLD_STATE_FILE`, and `_hold_timeout_check`.

**Step 3: Implement the state machine**

Near the existing `_get_most_recent` definition (around line 299), add module-level state:

```python
_HELD_CLIENT: "Optional[Client]" = None
_HELD_LOCK = threading.Lock()
HOLD_STATE_FILE = Path("/tmp/openptt-hold-state.json")
PHONE_HOLD_TIMEOUT_SECONDS = int(os.environ.get("PHONE_HOLD_TIMEOUT_SECONDS", "180"))
```

(`threading`, `json`, `os`, and `Path` are likely already imported — add any that aren't.)

Add the state-file writer:

```python
def _write_hold_state() -> None:
    """Snapshot the held-call state to disk so the admin can read it
    without entering this Python process."""
    global _HELD_CLIENT
    if _HELD_CLIENT is None or not _HELD_CLIENT.hold_caller:
        payload = {"holding": False}
    else:
        elapsed = 0
        if _HELD_CLIENT.hold_started_at is not None:
            elapsed = int(time.monotonic() - _HELD_CLIENT.hold_started_at)
        payload = {
            "holding": True,
            "slot": _HELD_CLIENT.slot,
            "held_for_seconds": elapsed,
        }
    try:
        HOLD_STATE_FILE.write_text(json.dumps(payload))
    except OSError as e:
        LOG.warning("hold state file write failed: %s", e)
```

Replace `_on_mute` with `_on_hold`:

```python
def _on_hold(signum, frame) -> None:
    """SIGUSR2 handler — toggle per-call hold state.

    Three branches:
      1. No call held + target on a call → put target ON hold.
      2. Target IS the held call → resume.
      3. Some OTHER call is held → refuse (log warning + leave state).
    """
    global _HELD_CLIENT
    target = _get_most_recent()
    if target is None:
        LOG.info("SIGUSR2 received, no active call — ignoring")
        return

    with _HELD_LOCK:
        if target.hold_caller:
            target.hold_caller = False
            target.hold_started_at = None
            if _HELD_CLIENT is target:
                _HELD_CLIENT = None
            LOG.info("SIGUSR2 — resume slot=%d", target.slot)
        elif _HELD_CLIENT is not None and _HELD_CLIENT is not target:
            LOG.warning(
                "SIGUSR2 — refused, slot=%d already held",
                _HELD_CLIENT.slot,
            )
            return
        else:
            target.hold_caller = True
            target.hold_started_at = time.monotonic()
            _HELD_CLIENT = target
            LOG.info("SIGUSR2 — hold slot=%d", target.slot)

    _write_hold_state()
```

Add the timeout helper + loop:

```python
def _hold_timeout_check() -> None:
    """One pass of the hold-timeout loop. Hangs up the held call if it's
    been on hold longer than PHONE_HOLD_TIMEOUT_SECONDS."""
    global _HELD_CLIENT
    with _HELD_LOCK:
        if _HELD_CLIENT is None or _HELD_CLIENT.hold_started_at is None:
            return
        if time.monotonic() - _HELD_CLIENT.hold_started_at \
                <= PHONE_HOLD_TIMEOUT_SECONDS:
            return
        slot = _HELD_CLIENT.slot
        target = _HELD_CLIENT
        _HELD_CLIENT = None
    LOG.info("hold timeout, hanging up slot=%d", slot)
    try:
        target.hangup_from_radio()
    except Exception as e:
        LOG.warning("hold-timeout hangup failed slot=%d: %s", slot, e)
    _write_hold_state()


def _hold_timeout_loop(stop_event: threading.Event) -> None:
    """Daemon background thread; ticks every 10 s."""
    while not stop_event.is_set():
        stop_event.wait(10)
        try:
            _hold_timeout_check()
        except Exception as e:
            LOG.warning("hold timeout loop error: %s", e)
```

In `_install_control_signals`, swap the binding:

```python
    signal.signal(signal.SIGUSR1, _on_hangup)
    signal.signal(signal.SIGUSR2, _on_hold)  # was _on_mute
    LOG.info("radio control signals installed (SIGUSR1=hangup, SIGUSR2=hold-toggle)")
```

In `serve()` (currently around line 549-560), after `_install_control_signals()`, spawn the timeout thread:

```python
    _hold_timeout_stop = threading.Event()
    threading.Thread(
        target=_hold_timeout_loop,
        args=(_hold_timeout_stop,),
        name="hold-timeout",
        daemon=True,
    ).start()
```

(The stop event is fine to leave dangling at the module scope — the daemon dies with the process.)

Initialise the state file at startup (write `{"holding": false}` so the admin's first read isn't a 404):

```python
    _write_hold_state()
```

**Step 4: Run tests, expect pass**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/sip_bridge/test_hold_state.py -v"
```

Expected: 7 tests pass (2 from Task 1 + 5 new).

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/sip_bridge/ -v"
```

Expected: all existing sip_bridge tests still pass.

**Step 5: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add sip_bridge/audiosocket_bridge.py tests/sip_bridge/test_hold_state.py
git commit -m "$(cat <<'EOF'
sip-bridge: hold/resume state machine + timeout + state file

SIGUSR2 now toggles per-client hold; module-level _HELD_CLIENT
enforces one held call at a time (second-call HOLD attempts logged
as refused). Background timeout thread hangs up held calls after
PHONE_HOLD_TIMEOUT_SECONDS (default 180 s). Bridge writes
/tmp/openptt-hold-state.json on each transition for the admin to read.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Self-review:**
- [ ] 7 tests pass in `test_hold_state.py`.
- [ ] Existing sip_bridge tests still pass.
- [ ] `_on_mute` is gone from the file (`grep -n _on_mute sip_bridge/audiosocket_bridge.py` → nothing).
- [ ] `_install_control_signals` binds SIGUSR2 to `_on_hold`.
- [ ] `_hold_timeout_loop` is started in `serve()`.
- [ ] `_write_hold_state()` is called at the end of `_on_hold` AND after timeout hangup AND in `serve()` startup.
- [ ] Commit has Co-Authored-By trailer.

---

## Task 3: Admin endpoints — `POST /api/sip/hold-toggle` + `GET /api/sip/hold-state`

**Files:**
- Modify: `server/api/sip.py` (rename `mute_toggle` → `hold_toggle`, keep `mute-toggle` route as alias, add `hold-state`)
- Test: `tests/test_sip_hold_api.py` (new)

**Step 1: Write the failing tests**

Create `tests/test_sip_hold_api.py`:

```python
"""HTTP-layer tests for /api/sip/hold-toggle and /api/sip/hold-state.

These don't talk to the real sip-bridge container — they verify the
request shape, response shape, and that mute-toggle still routes for
backwards-compat with un-upgraded radios.
"""
import json
import pytest
from unittest.mock import patch
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_hold_toggle_no_auth_required(client: AsyncClient):
    """POST /api/sip/hold-toggle is device-trusted (no auth)."""
    with patch("server.api.sip._signal_sip_bridge") as mock_signal:
        r = await client.post(
            "/api/sip/hold-toggle",
            json={"username": "harro"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "hold-toggle"
    assert body["username"] == "harro"
    mock_signal.assert_called_once_with("SIGUSR2")


@pytest.mark.asyncio
async def test_mute_toggle_alias_still_works(client: AsyncClient):
    """POST /api/sip/mute-toggle remains for one release as an alias."""
    with patch("server.api.sip._signal_sip_bridge") as mock_signal:
        r = await client.post(
            "/api/sip/mute-toggle",
            json={"username": "harro"},
        )
    assert r.status_code == 200
    mock_signal.assert_called_once_with("SIGUSR2")


@pytest.mark.asyncio
async def test_hold_state_returns_false_when_no_file(client: AsyncClient):
    """No state file exists → response is {'holding': false}."""
    with patch("server.api.sip._read_hold_state_from_bridge") as mock_read:
        mock_read.return_value = None
        r = await client.get("/api/sip/hold-state")
    assert r.status_code == 200
    assert r.json() == {"holding": False}


@pytest.mark.asyncio
async def test_hold_state_returns_full_payload_when_held(client: AsyncClient):
    """Bridge state file says holding → endpoint surfaces it verbatim."""
    payload = {"holding": True, "slot": 2, "held_for_seconds": 47}
    with patch("server.api.sip._read_hold_state_from_bridge") as mock_read:
        mock_read.return_value = payload
        r = await client.get("/api/sip/hold-state")
    assert r.status_code == 200
    assert r.json() == payload
```

**Step 2: Run tests, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_sip_hold_api.py -v"
```

Expected: 4 tests fail (404 on `hold-toggle` and `hold-state`; the existing `mute-toggle` returns wrong action label).

**Step 3: Implement**

In `server/api/sip.py`, find the existing `mute_toggle` handler (around line 341-353). Replace it with:

```python
@router.post("/hold-toggle")
async def hold_toggle(req: RadioCallControlRequest):
    """Toggle the call-hold state on the most-recent active call. Called
    by the P50 app when the green (KEYCODE_CALL) key is pressed in the
    Phone channel — and also from any non-Phone channel to RESUME a
    held call.

    Caller hears the hold-music loop while held; operator's carousel
    knob lock relaxes so they can navigate away to confer.
    """
    logger.info("radio hold toggle requested by user=%r", req.username)
    try:
        _signal_sip_bridge("SIGUSR2")
    except Exception as e:
        logger.error("hold signal failed: %s", e)
        raise HTTPException(status_code=503, detail=f"signal failed: {e}")
    return {"ok": True, "action": "hold-toggle", "username": req.username}


@router.post("/mute-toggle", deprecated=True)
async def mute_toggle_alias(req: RadioCallControlRequest):
    """DEPRECATED — alias for /hold-toggle. Will be removed once all
    field devices are on the hold-aware app build (≥ 3.7.3-XX). Same
    code path, kept under the old route name so radios that haven't
    upgraded keep working."""
    logger.info("radio mute-toggle (deprecated alias) requested by user=%r", req.username)
    try:
        _signal_sip_bridge("SIGUSR2")
    except Exception as e:
        logger.error("hold signal failed: %s", e)
        raise HTTPException(status_code=503, detail=f"signal failed: {e}")
    return {"ok": True, "action": "hold-toggle", "username": req.username}
```

Add the state-read helper + endpoint near the bottom of the file (or alongside the other read-style endpoints; keep co-located with hold-toggle for cohesion):

```python
def _read_hold_state_from_bridge() -> dict | None:
    """Read /tmp/openptt-hold-state.json from inside the sip-bridge
    container via docker exec. Returns the parsed JSON, or None if the
    file doesn't exist / can't be read."""
    try:
        import docker
    except ImportError as e:
        logger.warning("docker SDK unavailable, hold-state unread: %s", e)
        return None
    try:
        client = docker.from_env()
        container = client.containers.get(SIP_BRIDGE_CONTAINER_NAME)
        result = container.exec_run(
            ["cat", "/tmp/openptt-hold-state.json"],
        )
        if result.exit_code != 0:
            return None
        return json.loads(result.output.decode("utf-8", "replace"))
    except Exception as e:
        logger.warning("hold-state read failed: %s", e)
        return None


@router.get("/hold-state")
async def hold_state():
    """Read the bridge's current hold state. Device-trusted (no auth);
    same convention as /api/users/status. App polls every 5 s while
    connected to know whether to relax the carousel knob lock and
    show the hold-banner UI."""
    state = _read_hold_state_from_bridge()
    if state is None:
        return {"holding": False}
    return state
```

Make sure `import json` is at the top of `server/api/sip.py` (it likely already is).

**Step 4: Run tests**

```bash
docker compose up -d --build admin
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_sip_hold_api.py -v"
```

Expected: 4 tests pass.

Full server suite for regressions:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 118 passed (114 baseline + 4 new).

**Step 5: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add server/api/sip.py tests/test_sip_hold_api.py
git commit -m "$(cat <<'EOF'
sip: /hold-toggle + /hold-state admin endpoints; /mute-toggle aliased

POST /api/sip/hold-toggle replaces the semantic of /mute-toggle (kept
as a deprecated alias for one release so devices without the new app
keep working). GET /api/sip/hold-state reads the bridge's
/tmp/openptt-hold-state.json via docker exec; returns {holding: false}
when the file doesn't exist or the bridge is offline. Both endpoints
device-trusted (no auth), matching the existing convention.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Self-review:**
- [ ] 4 tests pass in `tests/test_sip_hold_api.py`.
- [ ] Full server suite: 118 passed.
- [ ] `POST /api/sip/hold-toggle` returns `{"ok": True, "action": "hold-toggle"}`.
- [ ] `POST /api/sip/mute-toggle` still works (returns same shape).
- [ ] `GET /api/sip/hold-state` returns `{"holding": false}` when no file.
- [ ] Commit has Co-Authored-By trailer.

---

## Task 4: App `HoldStateClient` + `MumlaService` plumbing

**Files:**
- Create: `openPTT-app/app/src/main/java/se/lublin/mumla/sip/HoldStateClient.java`
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/service/MumlaService.java`
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/service/IMumlaService.java`

**Step 1: Create `HoldStateClient.java`**

(Lives in a new `se.lublin.mumla.sip` package — the carousel-list filter and the SIP-call state are conceptually distinct. Or just drop it under `se.lublin.mumla.service` if you'd rather not create a new package — controller's call.)

```java
/*
 * Polls /api/sip/hold-state every 5 s while Mumble is connected. Tracks
 * whether THIS device is currently holding a SIP call (operator's slot
 * matches the held-state's slot). Notifies listeners on transitions so
 * the carousel knob-lock and the hold-banner UI repaint immediately.
 *
 * Mirrors PresenceCache in shape. Not coalesced with PresenceCache
 * because the underlying endpoint, polling cadence, and consumer set
 * are different — keep them as siblings, not a god-object.
 */
package se.lublin.mumla.sip;

import android.util.Log;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class HoldStateClient {

    private static final String TAG = "HoldStateClient";
    private static final int CONNECT_TIMEOUT_MS = 4000;
    private static final int READ_TIMEOUT_MS = 6000;

    public interface Listener {
        /** Called on a worker thread whenever the holding flag flips. */
        void onHoldStateChanged(boolean holding, int slot);
    }

    private final String mAdminUrl;
    private volatile boolean mHolding = false;
    private volatile int mSlot = 0;
    private final Set<Listener> mListeners =
            Collections.synchronizedSet(new LinkedHashSet<>());
    private final ExecutorService mWorker = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "hold-state-refresh");
        t.setDaemon(true);
        return t;
    });

    public HoldStateClient(String adminUrl) {
        mAdminUrl = adminUrl;
    }

    public boolean isHolding() { return mHolding; }
    public int getSlot()       { return mSlot; }

    public void addListener(Listener l) { if (l != null) mListeners.add(l); }
    public void removeListener(Listener l) { mListeners.remove(l); }

    public void refresh() {
        if (mAdminUrl == null || mAdminUrl.isEmpty()) return;
        mWorker.execute(this::doRefresh);
    }

    public void close() {
        mWorker.shutdownNow();
        mListeners.clear();
    }

    private void doRefresh() {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(
                    mAdminUrl + "/api/sip/hold-state").openConnection();
            conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
            conn.setReadTimeout(READ_TIMEOUT_MS);
            if (conn.getResponseCode() != 200) return;

            InputStream is = conn.getInputStream();
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] tmp = new byte[256]; int n;
            while ((n = is.read(tmp)) > 0) buf.write(tmp, 0, n);
            JSONObject root = new JSONObject(buf.toString("UTF-8"));

            boolean nextHolding = root.optBoolean("holding", false);
            int nextSlot = root.optInt("slot", 0);

            if (nextHolding != mHolding || nextSlot != mSlot) {
                mHolding = nextHolding;
                mSlot = nextSlot;
                notifyListeners();
            }
        } catch (Exception e) {
            Log.w(TAG, "hold-state refresh failed: " + e);
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private void notifyListeners() {
        Listener[] snap;
        synchronized (mListeners) {
            snap = mListeners.toArray(new Listener[0]);
        }
        for (Listener l : snap) {
            try { l.onHoldStateChanged(mHolding, mSlot); }
            catch (Exception e) { Log.w(TAG, "listener threw: " + e); }
        }
    }
}
```

**Step 2: Wire into `MumlaService`**

Add field declarations near `mPresenceCache` (around line 115ish — the field cluster you added in the hide-offline work):

```java
private HoldStateClient mHoldStateClient;
private ScheduledExecutorService mHoldStateScheduler;
private static final int HOLD_STATE_POLL_SECONDS = 5;
```

Add the import at the top:
```java
import se.lublin.mumla.sip.HoldStateClient;
```

In `onCreate()` (after `mPresenceCache = new PresenceCache(...)` from the hide-offline work):

```java
mHoldStateClient = new HoldStateClient(mSettings.getAdminUrl());
```

In `HumlaService.Observer.onConnected()` (line ~160), after `startPresencePolling();`:

```java
            startHoldStatePolling();
```

In `onDisconnected(...)`, after `stopPresencePolling();`:

```java
            stopHoldStatePolling();
```

In `onDestroy()`, before `super.onDestroy()` (where you already do `stopPresencePolling()` + `mPresenceCache.close()`):

```java
stopHoldStatePolling();
if (mHoldStateClient != null) {
    mHoldStateClient.close();
    mHoldStateClient = null;
}
```

Add the lifecycle helpers near `startPresencePolling`:

```java
private synchronized void startHoldStatePolling() {
    if (mHoldStateClient == null) return;
    stopHoldStatePolling();
    mHoldStateClient.refresh();
    mHoldStateScheduler = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "hold-state-poll");
        t.setDaemon(true);
        return t;
    });
    mHoldStateScheduler.scheduleAtFixedRate(
            mHoldStateClient::refresh,
            HOLD_STATE_POLL_SECONDS, HOLD_STATE_POLL_SECONDS, TimeUnit.SECONDS);
}

private synchronized void stopHoldStatePolling() {
    if (mHoldStateScheduler != null) {
        mHoldStateScheduler.shutdownNow();
        mHoldStateScheduler = null;
    }
}

public HoldStateClient getHoldStateClient() {
    return mHoldStateClient;
}

public boolean isHoldingCall() {
    return mHoldStateClient != null && mHoldStateClient.isHolding();
}

public int getHoldingSlot() {
    return mHoldStateClient == null ? 0 : mHoldStateClient.getSlot();
}
```

**Step 3: Add to `IMumlaService`**

In `IMumlaService.java`, add the import and three method declarations:

```java
import se.lublin.mumla.sip.HoldStateClient;

// ... (existing methods)

HoldStateClient getHoldStateClient();
boolean isHoldingCall();
int getHoldingSlot();
```

**Step 4: Compile-check**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew :app:compileFossDebugJavaWithJavac 2>&1 | tail -10
```

Expected: `BUILD SUCCESSFUL`. No new tests in this task; behaviour is verified manually in Task 7.

Existing JUnit (PresenceFilter) regression check:

```bash
./gradlew :app:testFossDebugUnitTest 2>&1 | tail -10
```

Expected: 9/9 still pass.

**Step 5: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/java/se/lublin/mumla/sip/HoldStateClient.java \
        app/src/main/java/se/lublin/mumla/service/MumlaService.java \
        app/src/main/java/se/lublin/mumla/service/IMumlaService.java
git commit -m "$(cat <<'EOF'
app: HoldStateClient + 5s polling lifecycle in MumlaService

Mirrors PresenceCache. Polls GET /api/sip/hold-state every 5 s while
Mumble is connected; tracks whether this device's most-recent SIP call
is currently held + which slot. Listener interface for the carousel
knob-lock and hold-banner UI in Tasks 5-6.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Self-review:**
- [ ] `HoldStateClient.java` created in the new `se.lublin.mumla.sip` package.
- [ ] `MumlaService` instantiates `mHoldStateClient` in `onCreate`, polls in `onConnected`, stops in `onDisconnected` + `onDestroy`.
- [ ] `IMumlaService` declares `getHoldStateClient`, `isHoldingCall`, `getHoldingSlot`.
- [ ] Build clean, 9 PresenceFilter tests still pass.
- [ ] Commit has Co-Authored-By trailer.

---

## Task 5: `dispatchKeyEvent` updates + `phoneHoldToggle` rename + auto-navigate-back

**Files:**
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/app/MumlaActivity.java`
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/service/MumlaService.java`
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/service/IMumlaService.java`

**Step 1: Rename `phoneMuteToggle` → `phoneHoldToggle` + change endpoint**

In `MumlaService.java`, find `phoneMuteToggle()` (around line 534) and replace the entire method with:

```java
/** Green-button toggle: HOLD if in Phone/Call-*, RESUME if elsewhere
 *  AND a held call exists. Server's /hold-toggle handler interprets
 *  the SIGUSR2 the same way regardless. After the POST succeeds we
 *  refresh hold-state immediately and, on resume, auto-navigate the
 *  Mumble client back to the held Phone/Call-N. */
public void phoneHoldToggle() {
    String adminUrl = mSettings.getAdminUrl();
    if (adminUrl == null || adminUrl.isEmpty()) {
        Log.w(TAG, "phoneHoldToggle: admin URL not configured");
        return;
    }
    String username = currentMumbleUsername();
    if (username == null) return;
    final boolean wasHolding = isHoldingCall();
    final int heldSlot = getHoldingSlot();
    speak(getString(wasHolding
            ? R.string.phone_resumed_tts
            : R.string.phone_held_tts));
    new Thread(() -> {
        boolean ok = _postPhoneControl(adminUrl + "/api/sip/hold-toggle", username);
        if (ok && mHoldStateClient != null) {
            mHoldStateClient.refresh();
        }
        if (ok && wasHolding && heldSlot > 0) {
            // Auto-navigate the radio back to the slot that just resumed.
            new android.os.Handler(android.os.Looper.getMainLooper()).post(() ->
                switchChannelByName("Phone/Call-" + heldSlot)
            );
        }
    }).start();
}
```

`switchChannelByName` likely doesn't exist yet on `MumlaService`. Add it near `switchChannel(int direction)` (around line 1427):

```java
/** Navigate the live Mumble client to the channel with the given name.
 *  No-op if no matching channel exists or the service isn't connected. */
public void switchChannelByName(String channelName) {
    try {
        if (!isConnected() || channelName == null) return;
        IHumlaSession session = HumlaService.getSession();
        if (session == null) return;
        IChannel match = findChannelByName(session.getRootChannel(), channelName);
        if (match != null) {
            session.joinChannel(match.getId());
        }
    } catch (Exception e) {
        Log.w(TAG, "switchChannelByName failed: " + e);
    }
}

private static IChannel findChannelByName(IChannel root, String name) {
    if (root == null) return null;
    if (name.equalsIgnoreCase(root.getName())) return root;
    for (IChannel sub : root.getSubchannels()) {
        IChannel hit = findChannelByName(sub, name);
        if (hit != null) return hit;
    }
    return null;
}
```

(`IHumlaSession`, `IChannel`, etc. are likely already imported — add what's missing.)

Update `IMumlaService.java`: add `void phoneHoldToggle();` and remove the old `void phoneMuteToggle();`. Also add `void switchChannelByName(String channelName);` if it'll be called from outside the service (it's only called from inside `phoneHoldToggle` in this task, so it can stay package-private — leave off the interface).

**Step 2: Update `MumlaActivity.dispatchKeyEvent`**

Find the existing knob-lock check (around line 591):

```java
            if (isDir && isInActivePhoneCall()) {
                android.widget.Toast.makeText(this,
                        R.string.phone_call_knob_blocked,
                        android.widget.Toast.LENGTH_SHORT).show();
                return true;
            }
```

Wrap the inner block with the held-state escape:

```java
            if (isDir && isInActivePhoneCall() && !mService.isHoldingCall()) {
                android.widget.Toast.makeText(this,
                        R.string.phone_call_knob_blocked,
                        android.widget.Toast.LENGTH_SHORT).show();
                return true;
            }
```

Find the green-button handler (around line 610-622). Today it's gated by `inPhoneTree`. Replace with:

```java
            if (keyCode == KeyEvent.KEYCODE_CALL || keyCode == KeyEvent.KEYCODE_MENU) {
                String chan = mService.currentChannelName();
                boolean inPhoneTree = chan != null
                        && ("Phone".equals(chan) || chan.startsWith("Call-"));
                if (mService.hasFeature("sip")) {
                    if (keyCode == KeyEvent.KEYCODE_CALL) {
                        // GREEN: hold/resume.
                        // - In Phone tree: toggle hold (start hold).
                        // - Elsewhere AND a call is held: toggle (resume).
                        if (inPhoneTree || mService.isHoldingCall()) {
                            mService.phoneHoldToggle();
                            return true;
                        }
                    } else if (inPhoneTree) {
                        // MENU: hangup, only valid in the Phone tree.
                        mService.phoneHangup();
                        return true;
                    }
                }
            }
```

Replace the existing `phoneMuteToggle` reference with `phoneHoldToggle` (the rename above means any compile-time check will catch leftover usage).

**Step 3: Compile-check**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew :app:compileFossDebugJavaWithJavac 2>&1 | tail -10
```

Expected: `BUILD SUCCESSFUL`.

```bash
./gradlew :app:testFossDebugUnitTest 2>&1 | tail -10
```

Expected: 9/9 PresenceFilter tests still pass.

**Step 4: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/java/se/lublin/mumla/app/MumlaActivity.java \
        app/src/main/java/se/lublin/mumla/service/MumlaService.java \
        app/src/main/java/se/lublin/mumla/service/IMumlaService.java
git commit -m "$(cat <<'EOF'
app: green button = hold/resume + knob unlock while held + auto-navigate back

phoneMuteToggle renamed to phoneHoldToggle, posts to /api/sip/hold-toggle.
dispatchKeyEvent: knob lock relaxes when isHoldingCall() is true; green
fires from the Phone tree (start hold) AND from any non-Phone channel
when a call is held (resume). Resume path auto-navigates the radio
back to the held Phone/Call-N via the new switchChannelByName helper.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Self-review:**
- [ ] `phoneMuteToggle` is gone from `MumlaService` (`grep -n phoneMuteToggle openPTT-app/app/src/main/java/se/lublin/mumla/service/MumlaService.java` returns nothing).
- [ ] `phoneHoldToggle` posts to `/api/sip/hold-toggle`.
- [ ] `IMumlaService` has `phoneHoldToggle`, no `phoneMuteToggle`.
- [ ] `switchChannelByName` exists on `MumlaService`.
- [ ] `dispatchKeyEvent` knob lock has `&& !mService.isHoldingCall()` guard.
- [ ] Green-button handler routes to `phoneHoldToggle` from any channel when `isHoldingCall()`.
- [ ] Build clean + 9 unit tests pass.
- [ ] Commit has Co-Authored-By trailer.

---

## Task 6: Carousel hold-banner UI + strings

**Files:**
- Modify: `openPTT-app/app/src/main/res/values/strings.xml`
- Modify: `openPTT-app/app/src/main/res/layout/fragment_channel_carousel.xml`
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/ChannelCarouselFragment.java`

**Step 1: Strings**

In `openPTT-app/app/src/main/res/values/strings.xml`, find the existing phone-related strings. Replace `phone_mute_toggled_tts` with the new set:

```xml
    <!-- Replaces phone_mute_toggled_tts. Spoken when call enters HOLD. -->
    <string name="phone_held_tts">On hold.</string>
    <!-- Spoken when the operator presses green from a non-Phone channel
         and the held call resumes. -->
    <string name="phone_resumed_tts">Resumed.</string>
    <!-- Spoken when the operator tries to HOLD a second call while one is
         already held. (Bridge refuses; this is feedback to the operator.) -->
    <string name="phone_already_held_tts">Resume held call first.</string>
    <!-- Banner above the channels strip when this device is the operator
         on a call that is currently on hold. %d = slot number. -->
    <string name="phone_hold_banner">Call on hold (Slot %d) — press green to resume</string>
```

Delete (or comment out) the old `phone_mute_toggled_tts` string.

**Step 2: Layout — add the hold banner**

In `openPTT-app/app/src/main/res/layout/fragment_channel_carousel.xml`, find the root `LinearLayout` (line 10) and the existing `channelCarouselHeader` (line 16). Insert this BEFORE the header:

```xml
    <!-- Visible only while this device's most-recent SIP call is on hold. -->
    <TextView
        android:id="@+id/holdBanner"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:gravity="center"
        android:textSize="11sp"
        android:textStyle="bold"
        android:textColor="?attr/callUiBg"
        android:background="?attr/callUiSoftkey"
        android:padding="4dp"
        android:visibility="gone"/>
```

**Step 3: Wire the banner in `ChannelCarouselFragment`**

Add a field for the banner view + a `HoldStateClient.Listener`:

```java
    private TextView mHoldBanner;
    private final HoldStateClient.Listener mHoldListener = (holding, slot) -> {
        android.os.Handler h = new android.os.Handler(android.os.Looper.getMainLooper());
        h.post(this::refreshHoldBanner);
    };
```

In `onCreateView` (or `onViewCreated`, wherever the other `findViewById` calls live), wire it:

```java
        mHoldBanner = root.findViewById(R.id.holdBanner);
        refreshHoldBanner();
```

In `onServiceBound(IHumlaService service)` — after the existing presence listener registration:

```java
        IMumlaService svc = (IMumlaService) service;  // or however the cast works in this fragment today
        if (svc != null && svc.getHoldStateClient() != null) {
            svc.getHoldStateClient().addListener(mHoldListener);
        }
        refreshHoldBanner();
```

In `onServiceUnbound()` — after the existing presence listener unregistration:

```java
        IMumlaService svc = (IMumlaService) ...;
        if (svc != null && svc.getHoldStateClient() != null) {
            svc.getHoldStateClient().removeListener(mHoldListener);
        }
```

Add `refreshHoldBanner` near the other `refresh*` helpers:

```java
    private void refreshHoldBanner() {
        if (mHoldBanner == null) return;
        IMumlaService svc = (IMumlaService) getService();
        if (svc != null && svc.isHoldingCall()) {
            int slot = svc.getHoldingSlot();
            mHoldBanner.setText(getString(R.string.phone_hold_banner, slot));
            mHoldBanner.setVisibility(android.view.View.VISIBLE);
        } else {
            mHoldBanner.setVisibility(android.view.View.GONE);
        }
    }
```

(Adjust the `IMumlaService` import / cast to match the patterns the fragment already uses for presence — same lifecycle + accessor approach.)

**Step 4: Compile-check + unit tests**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew :app:compileFossDebugJavaWithJavac :app:testFossDebugUnitTest 2>&1 | tail -10
```

Expected: `BUILD SUCCESSFUL` + 9 tests pass.

**Step 5: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/res/values/strings.xml \
        app/src/main/res/layout/fragment_channel_carousel.xml \
        app/src/main/java/se/lublin/mumla/channel/ChannelCarouselFragment.java
git commit -m "$(cat <<'EOF'
app: carousel hold-banner + strings refresh for hold/resume TTS

Banner above the channels strip when this device's most-recent SIP
call is on hold ("Call on hold (Slot N) — press green to resume").
Subscribes to HoldStateClient.Listener so transitions repaint
immediately. Strings: phone_held_tts, phone_resumed_tts,
phone_already_held_tts (for the v1 second-call-while-held branch),
phone_hold_banner. Drops phone_mute_toggled_tts.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Self-review:**
- [ ] Strings: `phone_held_tts`, `phone_resumed_tts`, `phone_already_held_tts`, `phone_hold_banner` all defined; `phone_mute_toggled_tts` is gone.
- [ ] Layout has `R.id.holdBanner` above the `channelCarouselHeader`.
- [ ] Carousel fragment subscribes to `HoldStateClient.Listener` on bind, unsubscribes on unbind.
- [ ] `refreshHoldBanner` posts to UI thread, sets banner text + visibility.
- [ ] Build clean + 9 unit tests pass.
- [ ] Commit has Co-Authored-By trailer.

---

## Task 7: Deploy + manual smoke

**Step 1: Push the server commits**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git push origin main
```

**Step 2: Deploy on prod**

```bash
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch "
    cd /opt/ptt && \
    git pull --ff-only && \
    docker compose up -d --build admin sip-bridge
" 2>&1 | tail -10
```

Both `admin` and `sip-bridge` rebuild — the bridge for the new asset + state machine, the admin for the new endpoints.

**Step 3: Smoke the endpoint**

```bash
curl -s "https://ptt.harro.ch/api/sip/hold-state" | python3 -m json.tool
# Expected: {"holding": false}
```

If you get an error, check `ssh root@ptt.harro.ch docker logs ptt-sip-bridge-1 --tail 30` for the asset-load output (`_get_hold_frames` log line) and `ssh ... docker logs ptt-admin-1 --tail 30` for the docker-exec read errors.

**Step 4: Push the app commits + build + install**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git push origin main
./scripts/deploy-apk.sh   # builds + scps to prod's /apk/openptt-foss-debug.apk
```

```bash
adb -s R259060623 install -r app/build/outputs/apk/foss/debug/openptt-foss-debug.apk
adb -s R259060618 install -r app/build/outputs/apk/foss/debug/openptt-foss-debug.apk 2>&1 | tail -3
adb -s R259060623 shell "dumpsys package ch.harro.openptt | grep versionName"
adb -s R259060618 shell "dumpsys package ch.harro.openptt | grep versionName"
```

Both should show the same `3.7.3-N-g<sha>-debug` string.

**Step 5: End-to-end manual smoke**

1. **Place a call** to the DID (`+351300500404` per the briefing) from a phone. Answer it on R259060623 — the operator hears the caller.
2. **Press green** on R259060623. Radio TTS: *"On hold."* The caller hears the music. Carousel banner appears: *"Call on hold (Slot 1) — press green to resume"*.
3. **Turn the channel knob** on R259060623 to e.g. Root. No "Press MENU to hang up" toast — the lock is relaxed.
4. **Press green** while in Root. Radio TTS: *"Resumed."* Radio auto-navigates back to `Phone/Call-1`. Caller hears the operator again.
5. **Repeat 1-2**, then leave the call on hold for 4 minutes (longer than the 180 s timeout). The caller hears music throughout, then the call disconnects (Asterisk-side BYE). Verify in `docker logs ptt-sip-bridge-1` for the "hold timeout, hanging up slot=1" line.
6. **Concurrent-call refusal**: place TWO calls at once. R259060623 answers the first, presses green (hold). Now place a second call — R259060618 answers it on its own slot. R259060618 presses green from its Phone/Call-2: TTS *"Resume held call first."*; bridge log shows "SIGUSR2 — refused, slot=1 already held". The second call stays active (not held).
7. **Backwards-compat check**: confirm an old client (if any are still on the previous build) calling `POST /api/sip/mute-toggle` still works. If both P50s are upgraded, you can simulate with curl:
   ```bash
   curl -s -X POST https://ptt.harro.ch/api/sip/mute-toggle \
       -H 'Content-Type: application/json' -d '{"username":"harro"}'
   ```
   Expected: `{"ok": true, "action": "hold-toggle", "username": "harro"}`.

**Step 6: Update `docs/open_issues.md`**

Move the "True call hold" backlog item from the open list (currently in the SIP gateway "Deferred (future phases)" section) to a Resolved bullet with today's commit hashes:

```markdown
- ~~True call hold — channel switching while on hold + caller hold tone/music~~
  — **Resolved 2026-04-22**. SIP-bridge swap of `mute_caller` for `hold_caller`
  (Opus1.mp3 transcoded to slin8 at sip_bridge/assets/hold-music.slin8;
  in-bridge mixer in _downlink_loop). _HELD_CLIENT enforces one held call
  at a time; second-call HOLD attempts logged as refused. 180 s auto-hangup
  timeout via background thread. Admin endpoints
  POST /api/sip/hold-toggle + GET /api/sip/hold-state (mute-toggle aliased
  for one release). App polls hold-state every 5 s, relaxes the carousel
  knob lock when held, routes green-from-anywhere to a resume that
  auto-navigates back to Phone/Call-N. Carousel banner shows hold state.
  #7 architecture answered: app-side detection stays for single-key
  intents — server-side detection is for timing-precision gestures only.
  Commits (server): <SHAs>. Commits (app): <SHAs>.
  Design: docs/plans/2026-04-22-true-call-hold-design.md.
  Plan: docs/plans/2026-04-22-true-call-hold.md.
```

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
# Edit docs/open_issues.md per above
git add docs/open_issues.md
git commit -m "$(cat <<'EOF'
docs: true-call-hold resolved (2026-04-22)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

---

## Verification checklist (all phases)

After Task 7:

- [ ] `docker exec ptt-pytest sh -c "rm -f test.db && python -m pytest tests/ --ignore=tests/sip_bridge -q"` → 118 passed.
- [ ] `docker exec ptt-pytest sh -c "rm -f test.db && python -m pytest tests/sip_bridge/ -q"` → all green incl. 7 new hold-state tests.
- [ ] `./gradlew :app:testFossDebugUnitTest` → 9 PresenceFilter tests pass.
- [ ] `curl https://ptt.harro.ch/api/sip/hold-state` returns valid JSON.
- [ ] Both P50s on the new `versionName`.
- [ ] Smoke step 2 (hold + caller-hears-music + banner + lock relaxed) passes.
- [ ] Smoke step 4 (resume from non-Phone channel + auto-nav back) passes.
- [ ] Smoke step 5 (180 s auto-hangup) passes.
- [ ] Smoke step 6 (concurrent-call refusal) passes.
- [ ] `docs/open_issues.md` updated.

All ten green = True Call Hold ships.

---

## Open questions / deferred (carried from design)

1. **Multi-hold UI** — pick from a list. Defer until anyone asks.
2. **Per-slot signaling refactor** — today's "most-recent client" semantics for the initial press. Adequate for v1.
3. **Admin-customisable hold music** — upload WAV, choose volume. Defer.
4. **Hold-elapsed indicator on the dashboard's Call Log** — nice-to-have, defer.

---

## Dependencies + parallelization

- Tasks 1 → 2 → 3 (server/bridge sequence) — bridge state machine and admin endpoints depend on Task 1's `Client.hold_caller` field rename.
- Tasks 4 → 5 → 6 (app sequence) — service field plumbing first, then the key-event wiring uses it, then the UI subscribes.
- Task 4-6 don't strictly depend on Task 3 landing first (the app endpoints don't exist on prod yet, so first-press-after-deploy fails fast and the app falls back gracefully) — but landing all server tasks before app tasks is cleaner.
- Task 7 is the final deploy gate.

Solo execution: run in order. Estimated 3-4 hours of focused work end-to-end.

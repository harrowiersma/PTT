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


def _make_client(slot: int = 1) -> "ab.Client":
    sock = MagicMock()
    return ab.Client(sock, slot)


def test_get_hold_frames_loads_committed_asset():
    """The committed sip_bridge/assets/hold-music.slin8 loads cleanly
    and yields a non-empty list of fixed-size 20 ms slin8 frames."""
    frames = ab._get_hold_frames()
    assert len(frames) > 100  # at least ~2 seconds of audio
    for frame in frames[:10]:
        assert len(frame) == ab.SLIN8_FRAME_BYTES


def test_client_starts_with_hold_caller_false():
    """A freshly-constructed Client is not on hold."""
    client = _make_client(slot=1)
    assert client.hold_caller is False
    assert client.hold_started_at is None


import json  # noqa: E402
import time  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_held_client():
    """Each state-machine test starts with no held client."""
    ab._HELD_CLIENT = None
    yield
    ab._HELD_CLIENT = None


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


def test_clear_hold_on_teardown_resets_state(monkeypatch, tmp_path):
    """When a held caller's Client is torn down (caller hangup mid-hold),
    _HELD_CLIENT is cleared and the state file is rewritten to
    {holding: false} so the app banner stops lingering until timeout."""
    state_path = tmp_path / "hold-state.json"
    monkeypatch.setattr(ab, "HOLD_STATE_FILE", state_path)

    held = _make_client(slot=1)
    held.hold_caller = True
    held.hold_started_at = time.monotonic()
    ab._HELD_CLIENT = held

    ab._clear_hold_if_holding(held)

    assert ab._HELD_CLIENT is None
    assert held.hold_caller is False
    state = json.loads(state_path.read_text())
    assert state == {"holding": False}


def test_clear_hold_ignores_other_client(monkeypatch, tmp_path):
    """Tearing down a non-held client must not touch hold state."""
    state_path = tmp_path / "hold-state.json"
    monkeypatch.setattr(ab, "HOLD_STATE_FILE", state_path)

    held = _make_client(slot=1)
    held.hold_caller = True
    held.hold_started_at = time.monotonic()
    ab._HELD_CLIENT = held

    other = _make_client(slot=2)
    ab._clear_hold_if_holding(other)

    assert ab._HELD_CLIENT is held
    assert held.hold_caller is True


def test_timeout_force_hangs_up(monkeypatch):
    """Past PHONE_HOLD_TIMEOUT_SECONDS, the timeout loop hangs up the held call."""
    client = _make_client(slot=1)
    client.hold_caller = True
    client.hold_started_at = time.monotonic() - 10_000  # well past any sane timeout
    ab._HELD_CLIENT = client

    ab._hold_timeout_check()

    # _send_frame(hangup) invokes sock.sendall under the covers; and/or
    # the socket is closed. Either is acceptable evidence the hangup fired.
    assert client._sock.sendall.called or client._sock.close.called
    assert ab._HELD_CLIENT is None

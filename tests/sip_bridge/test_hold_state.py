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

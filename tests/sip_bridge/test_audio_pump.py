"""Audio pump unit tests. The pump itself is a tight loop — we test
the pieces that can be tested without network: frame-size validation,
the no-op-when-idle guarantee, and pymumble-queue interaction via mock."""
from __future__ import annotations

import socket
from unittest.mock import MagicMock

import pytest

from sip_bridge.ari_bridge import AudioPump


class FakeSock:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    def recvfrom(self, bufsize):
        if not self._frames:
            raise BlockingIOError
        frame = self._frames.pop(0)
        return (frame, ("127.0.0.1", 65000))

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def setblocking(self, flag):
        pass


def test_pump_forwards_uplink_frame_to_pymumble():
    mumble = MagicMock()
    sock = FakeSock([b"\x00\x10" * 320])  # 640-byte 16 kHz slin16 frame

    pump = AudioPump(udp_sock=sock, udp_peer=("127.0.0.1", 12345), mumble=mumble)
    pump.step_uplink()

    mumble.sound_output.add_sound.assert_called_once()
    pushed = mumble.sound_output.add_sound.call_args.args[0]
    assert len(pushed) == 1920  # upsampled to 48 kHz


def test_pump_idle_no_mumble_writes():
    mumble = MagicMock()
    sock = FakeSock([])

    pump = AudioPump(udp_sock=sock, udp_peer=("127.0.0.1", 12345), mumble=mumble)
    pump.step_uplink()

    mumble.sound_output.add_sound.assert_not_called()


def test_pump_skips_wrong_size_frame():
    """Defensive: externalMedia occasionally sends short frames on call setup/teardown."""
    mumble = MagicMock()
    sock = FakeSock([b"\x00" * 100])

    pump = AudioPump(udp_sock=sock, udp_peer=("127.0.0.1", 12345), mumble=mumble)
    pump.step_uplink()

    mumble.sound_output.add_sound.assert_not_called()

"""Unit tests for sip-bridge resample helpers.

Asterisk externalMedia delivers slin16 @ 16 kHz in 20 ms frames (640 bytes
= 320 int16 samples). Mumble / pymumble wants 48 kHz in 20 ms frames
(1920 bytes = 960 int16 samples). These helpers convert between the two.

Pure-Python, no network or audio hardware required — pins the contract
the audio pump depends on.
"""
from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from sip_bridge.audio import downsample_48_to_16, upsample_16_to_48


def _sine_pcm(freq_hz: float, sample_rate: int, duration_ms: int, amp: float = 0.5) -> bytes:
    """Build an int16 mono PCM sine wave of given length and frequency."""
    n = int(sample_rate * duration_ms / 1000)
    t = np.arange(n, dtype=np.float32) / sample_rate
    wave = (np.sin(2 * np.pi * freq_hz * t) * amp * 32767).astype(np.int16)
    return wave.tobytes()


def _dominant_freq(pcm: bytes, sample_rate: int) -> float:
    """Estimate dominant frequency via FFT. Used to verify resample preserved pitch."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(samples) < 2:
        return 0.0
    spectrum = np.fft.rfft(samples * np.hanning(len(samples)))
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / sample_rate)
    return float(freqs[int(np.argmax(np.abs(spectrum)))])


class TestUpsample16to48:
    def test_frame_length_20ms(self):
        """640-byte 16 kHz frame → 1920-byte 48 kHz frame (exactly one pymumble chunk)."""
        pcm_16k = _sine_pcm(440, 16000, 20)
        assert len(pcm_16k) == 640

        pcm_48k = upsample_16_to_48(pcm_16k)

        assert len(pcm_48k) == 1920

    def test_preserves_tone_frequency(self):
        """A 440 Hz tone at 16 kHz should remain 440 Hz after upsample to 48 kHz."""
        pcm_16k = _sine_pcm(440, 16000, 200)

        pcm_48k = upsample_16_to_48(pcm_16k)

        detected = _dominant_freq(pcm_48k, 48000)
        assert abs(detected - 440) < 10, f"Expected ~440 Hz, got {detected}"

    def test_silence_in_silence_out(self):
        pcm_16k = b"\x00" * 640

        pcm_48k = upsample_16_to_48(pcm_16k)

        assert pcm_48k == b"\x00" * 1920


class TestDownsample48to16:
    def test_frame_length_20ms(self):
        """1920-byte 48 kHz frame → 640-byte 16 kHz frame (exactly one externalMedia frame)."""
        pcm_48k = _sine_pcm(440, 48000, 20)
        assert len(pcm_48k) == 1920

        pcm_16k = downsample_48_to_16(pcm_48k)

        assert len(pcm_16k) == 640

    def test_preserves_tone_frequency(self):
        pcm_48k = _sine_pcm(440, 48000, 200)

        pcm_16k = downsample_48_to_16(pcm_48k)

        detected = _dominant_freq(pcm_16k, 16000)
        assert abs(detected - 440) < 10, f"Expected ~440 Hz, got {detected}"

    def test_silence_in_silence_out(self):
        pcm_48k = b"\x00" * 1920

        pcm_16k = downsample_48_to_16(pcm_48k)

        assert pcm_16k == b"\x00" * 640


class TestRoundTrip:
    def test_16_to_48_to_16_recovers_tone(self):
        """Round-trip: 16kHz sine → 48kHz → back to 16kHz should still sound like 440 Hz."""
        original = _sine_pcm(440, 16000, 200)

        up = upsample_16_to_48(original)
        down = downsample_48_to_16(up)

        assert len(down) == len(original)
        detected = _dominant_freq(down, 16000)
        assert abs(detected - 440) < 10

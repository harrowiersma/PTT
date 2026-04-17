"""Audio helpers for the SIP↔Mumble bridge.

Pure functions, no state. Linear-interpolation resample between the two
fixed sample rates the bridge cares about — 16 kHz (Asterisk externalMedia
slin16) and 48 kHz (Mumble/pymumble). Frame sizes are assumed to be 20 ms
aligned; passing something else returns a proportionally-sized buffer.

The same np.interp trick is used by server/weather_bot.py:212 for
Piper output — pick it up there if you want to understand the shape.
"""
from __future__ import annotations

import numpy as np


def upsample_16_to_48(pcm_16k: bytes) -> bytes:
    """Linear-interp upsample int16 mono PCM from 16 kHz to 48 kHz."""
    if not pcm_16k:
        return b""
    src = np.frombuffer(pcm_16k, dtype=np.int16).astype(np.float32)
    target_n = len(src) * 3
    src_idx = np.arange(len(src), dtype=np.float32)
    tgt_idx = np.linspace(0, len(src) - 1, target_n, dtype=np.float32)
    tgt = np.interp(tgt_idx, src_idx, src)
    return np.clip(tgt, -32768, 32767).astype(np.int16).tobytes()


def downsample_48_to_16(pcm_48k: bytes) -> bytes:
    """Linear-interp downsample int16 mono PCM from 48 kHz to 16 kHz.

    Warning: this is an anti-aliasing-free resample. For the 300-3400 Hz
    band that G.711 telephony cares about it's fine (well below Nyquist
    at 16 kHz), but do not reuse for wideband audio without a low-pass.
    """
    if not pcm_48k:
        return b""
    src = np.frombuffer(pcm_48k, dtype=np.int16).astype(np.float32)
    target_n = len(src) // 3
    src_idx = np.arange(len(src), dtype=np.float32)
    tgt_idx = np.linspace(0, len(src) - 1, target_n, dtype=np.float32)
    tgt = np.interp(tgt_idx, src_idx, src)
    return np.clip(tgt, -32768, 32767).astype(np.int16).tobytes()

"""Unit tests for Phone-channel ACL enforcement in MurmurClient.

The ACL is implemented as a PYMUMBLE_CLBK_USERUPDATED callback. Tests
drive the callback directly with synthetic user/state payloads that
mimic pymumble's dict-shaped user objects, asserting the resulting
behavior on mocked mumble state.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_phone_acl.py -v --noconftest
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------
# Stub out pymumble so server.murmur.client can be imported without the
# real C-dependency chain. Also stub server.weather_bot.text_to_audio_pcm
# so the lazy TTS render in _bounce_from_phone doesn't try to load Piper.
# ---------------------------------------------------------------------

def _install_stubs() -> None:
    if "pymumble_py3" not in sys.modules:
        pymumble_mod = types.ModuleType("pymumble_py3")
        constants_mod = types.ModuleType("pymumble_py3.constants")
        constants_mod.PYMUMBLE_CLBK_TEXTMESSAGERECEIVED = "text_message_received"
        constants_mod.PYMUMBLE_CLBK_USERUPDATED = "user_updated"
        pymumble_mod.constants = constants_mod
        sys.modules["pymumble_py3"] = pymumble_mod
        sys.modules["pymumble_py3.constants"] = constants_mod

    if "server.weather_bot" not in sys.modules:
        wb = types.ModuleType("server.weather_bot")
        wb.text_to_audio_pcm = lambda text: b"\x00\x01" * 100
        wb.generate_preamble_pcm = lambda: b"\x00" * 48
        wb.generate_trailing_silence_pcm = lambda ms=400: b"\x00" * 48
        sys.modules["server.weather_bot"] = wb


_install_stubs()


from server.murmur.client import MurmurClient  # noqa: E402


# ---------------------------------------------------------------------
# Helpers to build fake pymumble state.
# ---------------------------------------------------------------------

def _make_client() -> MurmurClient:
    c = MurmurClient(host="", port=0, mumble_host="localhost", mumble_port=64738)
    # Build a minimal fake Mumble instance.
    c._mumble = MagicMock()
    c._mumble.channels = {
        0: {"name": "Root"},
        1: {"name": "General"},
        2: {"name": "Phone"},
        3: {"name": "Weather"},
    }
    c._mumble.users = {}
    # Pre-render cache so the bounce path doesn't call into the TTS stub.
    c._phone_deny_pcm = b"\x00" * 100
    return c


def _add_user(c: MurmurClient, session_id: int, name: str, channel_id: int) -> MagicMock:
    """Return a MagicMock whose `.move_in` we can assert against."""
    fake = MagicMock()
    # Support dict-like get/subscript for the handler code which reads
    # user["name"]/user.get("session")/etc. — but we pass a plain dict to
    # the handler. The stored user is the one that mm.users[sid] yields
    # for move_in() to be called against.
    c._mumble.users[session_id] = fake
    return fake


def _payload(session_id: int, name: str, channel_id: int) -> dict:
    return {"session": session_id, "name": name, "channel_id": channel_id}


# ---------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------

def test_eligible_user_moves_into_phone_not_bounced():
    c = _make_client()
    mock_user = _add_user(c, session_id=42, name="alice", channel_id=2)
    c.update_phone_eligible({"alice"})

    # Warm the cache: alice was previously in General.
    c._user_last_channel[42] = 1

    c._on_user_updated(_payload(42, "alice", 2), {"channel_id": 2})

    mock_user.move_in.assert_not_called()
    # Cache updated to new channel.
    assert c._user_last_channel[42] == 2


def test_non_eligible_user_moves_into_phone_bounced():
    c = _make_client()
    mock_user = _add_user(c, session_id=77, name="bob", channel_id=2)
    c.update_phone_eligible({"alice"})  # bob not in the set

    # Warm the cache: bob came from General (id=1).
    c._user_last_channel[77] = 1

    # Spy on whisper_audio to avoid sending actual audio.
    whisper_calls: list[tuple] = []
    c.whisper_audio = lambda sid, pcm, with_preamble=True: whisper_calls.append((sid, len(pcm)))

    c._on_user_updated(_payload(77, "bob", 2), {"channel_id": 2})

    mock_user.move_in.assert_called_once_with(1)
    # Whisper fired with bob's session id.
    assert len(whisper_calls) == 1
    assert whisper_calls[0][0] == 77
    # Cache now reflects the forced move back, so a subsequent tick at
    # channel=1 won't trigger another bounce.
    assert c._user_last_channel[77] == 1


def test_bot_user_moves_into_phone_not_bounced():
    c = _make_client()
    mock_user = _add_user(c, session_id=1, name="PTTWeather", channel_id=2)
    c.update_phone_eligible(set())

    c._user_last_channel[1] = 3  # was in Weather

    c._on_user_updated(_payload(1, "PTTWeather", 2), {"channel_id": 2})

    mock_user.move_in.assert_not_called()
    # Bot handler returns before touching the cache, so the test's
    # preloaded value survives unchanged (no cache churn on bots).
    assert c._user_last_channel.get(1) == 3


def test_eligible_user_moves_between_non_phone_channels_not_bounced():
    c = _make_client()
    mock_user = _add_user(c, session_id=9, name="alice", channel_id=3)
    c.update_phone_eligible({"alice"})

    c._user_last_channel[9] = 1  # General → Weather

    c._on_user_updated(_payload(9, "alice", 3), {"channel_id": 3})

    mock_user.move_in.assert_not_called()
    assert c._user_last_channel[9] == 3


def test_non_eligible_user_first_sighting_in_phone_not_bounced():
    """Users whose first observed channel is Phone (no prior tick) must
    NOT be bounced — we only act on confirmed moves so we never race with
    the initial user-list sync at callback registration."""
    c = _make_client()
    mock_user = _add_user(c, session_id=55, name="charlie", channel_id=2)
    c.update_phone_eligible(set())

    # No entry in _user_last_channel yet.
    c._on_user_updated(_payload(55, "charlie", 2), {"channel_id": 2})

    mock_user.move_in.assert_not_called()
    # Cache warmed for next time.
    assert c._user_last_channel[55] == 2


def test_no_phone_channel_in_tree_is_noop():
    c = _make_client()
    # Remove Phone from the channel tree.
    c._mumble.channels.pop(2)
    mock_user = _add_user(c, session_id=10, name="bob", channel_id=99)
    c.update_phone_eligible(set())

    c._user_last_channel[10] = 1
    # bob moved to a channel id that no longer maps to Phone.
    c._on_user_updated(_payload(10, "bob", 99), {"channel_id": 99})

    mock_user.move_in.assert_not_called()

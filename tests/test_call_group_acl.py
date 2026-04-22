"""Bounce-on-entry tests for the call-group ACL.

Mirrors tests/test_phone_acl.py — same MurmurClient under test, same
USERUPDATED simulation. Copies the stub pattern for pymumble + TTS.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def _install_stubs() -> None:
    if "pymumble_py3" not in sys.modules:
        pymumble_mod = types.ModuleType("pymumble_py3")
        constants_mod = types.ModuleType("pymumble_py3.constants")
        constants_mod.PYMUMBLE_CLBK_TEXTMESSAGERECEIVED = "text_message_received"
        constants_mod.PYMUMBLE_CLBK_USERUPDATED = "user_updated"
        constants_mod.PYMUMBLE_CLBK_USERCREATED = "user_created"
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


def _make_client() -> MurmurClient:
    c = MurmurClient(host="", port=0, mumble_host="localhost", mumble_port=64738)
    c._mumble = MagicMock()
    c._mumble.channels = {
        0: {"name": "Root"},
        1: {"name": "General"},
        5: {"name": "SalesChan"},
    }
    c._mumble.users = {}
    # Pre-render both TTS caches so bounce paths don't call into stubs.
    c._phone_deny_pcm = b"\x00" * 100
    c._call_group_deny_pcm = b"\x00" * 100
    return c


def _payload(session_id: int, name: str, channel_id: int) -> dict:
    return {"session": session_id, "name": name, "channel_id": channel_id}


# ---- _call_group_check pure logic ---------------------------------------

def test_call_group_check_admin_bypasses():
    c = _make_client()
    c._user_call_groups = {"alice": set()}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {"alice": True}
    assert c._call_group_check("alice", 5) is True


def test_call_group_check_unrestricted_channel_allowed():
    c = _make_client()
    c._user_call_groups = {"alice": set()}
    c._channel_call_group = {5: None}
    c._user_is_admin = {"alice": False}
    assert c._call_group_check("alice", 5) is True


def test_call_group_check_member_allowed():
    c = _make_client()
    c._user_call_groups = {"alice": {1, 2}}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {"alice": False}
    assert c._call_group_check("alice", 5) is True


def test_call_group_check_non_member_denied():
    c = _make_client()
    c._user_call_groups = {"alice": {2}}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {"alice": False}
    assert c._call_group_check("alice", 5) is False


def test_call_group_check_unknown_user_denied():
    """User never refreshed → treated as non-member, channel is restricted → denied."""
    c = _make_client()
    c._user_call_groups = {}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {}
    assert c._call_group_check("alice", 5) is False


# ---- update_call_group_state ---------------------------------------------

def test_update_call_group_state_atomic():
    c = _make_client()
    c.update_call_group_state(
        user_groups={"alice": {1}},
        channel_groups={5: 1},
        user_admin={"alice": False},
    )
    assert c._user_call_groups == {"alice": {1}}
    assert c._channel_call_group == {5: 1}
    assert c._user_is_admin == {"alice": False}


# ---- _on_user_updated integration ----------------------------------------

def test_user_updated_bounces_non_member():
    """USERUPDATED with a non-member entering a restricted channel triggers a bounce."""
    c = _make_client()
    c._user_call_groups = {"alice": set()}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {"alice": False}
    c._user_last_channel[42] = 0

    fake = MagicMock()
    c._mumble.users[42] = fake
    c.whisper_audio = lambda sid, pcm, with_preamble=True: None

    c._on_user_updated(_payload(42, "alice", 5), {"channel_id": 5})
    fake.move_in.assert_called_once_with(0)


def test_user_updated_admin_not_bounced():
    """is_admin user is not bounced from a restricted channel."""
    c = _make_client()
    c._user_call_groups = {"alice": set()}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {"alice": True}
    c._user_last_channel[42] = 0

    fake = MagicMock()
    c._mumble.users[42] = fake

    c._on_user_updated(_payload(42, "alice", 5), {"channel_id": 5})
    fake.move_in.assert_not_called()


def test_user_updated_member_not_bounced():
    """Member entering their group's channel is not bounced."""
    c = _make_client()
    c._user_call_groups = {"alice": {1}}
    c._channel_call_group = {5: 1}
    c._user_is_admin = {"alice": False}
    c._user_last_channel[42] = 0

    fake = MagicMock()
    c._mumble.users[42] = fake

    c._on_user_updated(_payload(42, "alice", 5), {"channel_id": 5})
    fake.move_in.assert_not_called()

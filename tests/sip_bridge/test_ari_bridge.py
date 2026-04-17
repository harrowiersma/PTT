"""ARI event-parsing unit tests. Live WebSocket is covered by integration."""
from __future__ import annotations

from sip_bridge.ari_bridge import parse_stasis_event


def test_parses_stasis_start():
    event = {
        "type": "StasisStart",
        "application": "openptt-bridge",
        "channel": {"id": "1234567890.1", "name": "PJSIP/didww-00000001"},
        "args": [],
    }
    parsed = parse_stasis_event(event)
    assert parsed is not None
    assert parsed.kind == "start"
    assert parsed.channel_id == "1234567890.1"
    assert parsed.channel_name == "PJSIP/didww-00000001"


def test_parses_stasis_end():
    event = {
        "type": "StasisEnd",
        "application": "openptt-bridge",
        "channel": {"id": "1234567890.1", "name": "PJSIP/didww-00000001"},
    }
    parsed = parse_stasis_event(event)
    assert parsed is not None
    assert parsed.kind == "end"
    assert parsed.channel_id == "1234567890.1"


def test_ignores_unrelated_events():
    assert parse_stasis_event({"type": "ChannelDtmfReceived"}) is None
    assert parse_stasis_event({}) is None

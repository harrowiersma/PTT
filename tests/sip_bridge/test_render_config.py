"""Template-rendering unit tests. No network, no file I/O — feed in
a trunk dict, get back the pjsip.conf string."""
from __future__ import annotations

import pytest

from sip_bridge.render_config import render_pjsip_conf


TRUNK_WITH_AUTH = {
    "id": 1,
    "label": "DIDWW Amsterdam",
    "sip_host": "ams.sip.didww.com",
    "sip_port": 5060,
    "sip_user": "userid123",
    "sip_password": "secretpw",
    "from_uri": "sip:userid123@ams.sip.didww.com",
    "transport": "udp",
    "registration_interval_s": 3600,
    "enabled": True,
}


def test_renders_required_sections():
    conf = render_pjsip_conf(TRUNK_WITH_AUTH)

    assert "[transport-udp]" in conf
    assert "type=transport" in conf
    assert "protocol=udp" in conf
    assert "[didww]" in conf
    assert "type=endpoint" in conf
    assert "[didww-auth]" in conf
    assert "type=auth" in conf
    assert "[didww-aor]" in conf
    assert "type=aor" in conf
    assert "[didww-identify]" in conf
    assert "type=identify" in conf


def test_credentials_interpolated():
    conf = render_pjsip_conf(TRUNK_WITH_AUTH)

    assert "username=userid123" in conf
    assert "password=secretpw" in conf
    assert "ams.sip.didww.com:5060" in conf


def test_context_routes_to_didww_inbound():
    """Inbound calls must land in the extensions.conf [didww-inbound] context."""
    conf = render_pjsip_conf(TRUNK_WITH_AUTH)
    assert "context=didww-inbound" in conf


def test_disabled_trunk_returns_empty():
    """render_pjsip_conf on a disabled trunk returns an empty string —
    the entrypoint uses this to decide whether to start Asterisk at all."""
    trunk = dict(TRUNK_WITH_AUTH, enabled=False)
    assert render_pjsip_conf(trunk) == ""


def test_allows_exactly_ulaw_and_alaw():
    """Codec list: ulaw + alaw only. DIDWW supports Opus on some trunks
    but externalMedia + Mumble are simpler when we stay in G.711 land."""
    import re
    conf = render_pjsip_conf(TRUNK_WITH_AUTH)
    allowed = re.findall(r"^allow=(\w+)", conf, re.MULTILINE)
    assert allowed == ["ulaw", "alaw"], f"Expected ['ulaw', 'alaw'], got {allowed}"
    assert "disallow=all" in conf

"""Render Asterisk configs from admin-API trunk data.

The sip-bridge container runs a one-shot renderer at entrypoint time
that pulls /api/sip/internal/config/trunks and writes pjsip.conf. Keeps
the DB as source of truth while letting Asterisk consume a static file.

Only the trunk → pjsip.conf mapping lives here. extensions.conf, ari.conf,
http.conf, and modules.conf are static and shipped as-is; no rendering
needed for those.
"""
from __future__ import annotations

from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def render_pjsip_conf(trunk: dict) -> str:
    """Render pjsip.conf from a single trunk row. Returns '' if disabled."""
    if not trunk.get("enabled"):
        return ""

    tmpl = (_TEMPLATE_DIR / "pjsip.conf.tmpl").read_text()
    return tmpl.format(
        sip_host=trunk["sip_host"],
        sip_port=trunk.get("sip_port") or 5060,
        sip_user=trunk["sip_user"],
        sip_password=trunk["sip_password"],
        transport=trunk.get("transport", "udp"),
    )

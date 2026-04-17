#!/bin/bash
# sip-bridge/entrypoint.sh
#
# One-shot config rendering, then hand off to supervisord which runs
# Asterisk + the Python ARI bridge.

set -euo pipefail

echo "[entrypoint] rendering config from admin API"
python3 -m sip_bridge.render_entry

echo "[entrypoint] starting supervisord"
exec /usr/bin/supervisord -c /app/supervisord.conf

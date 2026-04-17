#!/bin/sh
set -e

# PJSIP depends on libraries installed into /usr/local/lib by the Dockerfile;
# make sure ldconfig picked them up (it did at build time, but the cache can
# get pruned if the runtime user differs).
ldconfig 2>/dev/null || true

exec python3 -u /app/bridge.py

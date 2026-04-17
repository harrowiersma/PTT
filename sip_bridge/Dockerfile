# sip_bridge/Dockerfile
#
# Phase 2b-audio: Asterisk 20 (apt-installed) + Python ARI bridge.
# network_mode: host + apparmor:unconfined in docker-compose — host
# networking is required for SIP/RTP to reach DIDWW with correct SDP
# addresses; AppArmor default profile blocks UDP sendto in host mode.
#
# Base image note: Debian bookworm no longer ships `asterisk` in apt
# (package was removed mid-cycle). Ubuntu 24.04 "noble" ships Asterisk
# 20.6.0 in the `universe` component, which is enabled by default.
# This is the nearest apt-installed Asterisk 20 available today.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        asterisk \
        asterisk-modules \
        asterisk-core-sounds-en-wav \
        python3 \
        python3-pip \
        python3-venv \
        supervisor \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Python deps. Use --break-system-packages because Ubuntu 24.04 has
# PEP 668 enabled on its system pip; we're inside a container, so
# using the system site-packages is fine.
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Layout: the sip_bridge Python package + configs live in /app.
WORKDIR /app
COPY __init__.py /app/sip_bridge/__init__.py
COPY audio.py /app/sip_bridge/audio.py
COPY render_config.py /app/sip_bridge/render_config.py
COPY render_entry.py /app/sip_bridge/render_entry.py
COPY ari_bridge.py /app/sip_bridge/ari_bridge.py
COPY templates /app/sip_bridge/templates
COPY configs /app/sip_bridge/configs
COPY supervisord.conf /app/supervisord.conf
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Ship the static Asterisk configs that do NOT need per-deployment
# interpolation — extensions.conf, http.conf, modules.conf,
# asterisk.conf, logger.conf. pjsip.conf + ari.conf are written by the
# entrypoint renderer.
RUN cp /app/sip_bridge/configs/extensions.conf /etc/asterisk/extensions.conf && \
    cp /app/sip_bridge/configs/http.conf       /etc/asterisk/http.conf && \
    cp /app/sip_bridge/configs/modules.conf    /etc/asterisk/modules.conf && \
    cp /app/sip_bridge/configs/asterisk.conf   /etc/asterisk/asterisk.conf && \
    cp /app/sip_bridge/configs/logger.conf     /etc/asterisk/logger.conf

ENV PYTHONPATH=/app

EXPOSE 5060/udp
EXPOSE 10000-10200/udp

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]

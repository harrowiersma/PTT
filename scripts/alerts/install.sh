#!/bin/sh
# Install the openPTT alerting stack on a Debian/Ubuntu host. Idempotent.
#
#   - /usr/local/bin/openptt-alert     - thin wrapper (curls healthchecks)
#   - /etc/default/openptt-alerts      - URL config (edit after install)
#   - /etc/cron.hourly/ptt-heartbeat   - hourly server health probe
#   - /etc/cron.daily/ptt-disk-check   - alert if / usage > threshold
#   - /etc/cron.daily/ptt-cert-check   - alert if any LE cert expires soon
#   - /etc/cron.daily/ptt-backup       - wraps backup.sh, pings on success
#   - /etc/cron.weekly/ptt-sqlite-prune       - Murmur slog trim + VACUUM
#   - /etc/cron.weekly/docker-builder-prune   - free BuildKit cache
#
# After running: edit /etc/default/openptt-alerts and paste the ping URLs
# from your healthchecks.io project. See docs/ops-notes.md.

set -eu
cd "$(dirname "$0")"

install -m 755 openptt-alert          /usr/local/bin/openptt-alert
install -m 755 ptt-heartbeat          /etc/cron.hourly/ptt-heartbeat
install -m 755 ptt-disk-check         /etc/cron.daily/ptt-disk-check
install -m 755 ptt-cert-check         /etc/cron.daily/ptt-cert-check
install -m 755 ptt-backup             /etc/cron.daily/ptt-backup
install -m 755 ptt-sqlite-prune       /etc/cron.weekly/ptt-sqlite-prune
install -m 755 docker-builder-prune   /etc/cron.weekly/docker-builder-prune

# Don't clobber a live config — this is where the operator's URLs are.
if [ ! -f /etc/default/openptt-alerts ]; then
  install -m 644 openptt-alerts.example /etc/default/openptt-alerts
  echo "Installed default /etc/default/openptt-alerts — edit it to add URLs."
else
  echo "Kept existing /etc/default/openptt-alerts (edit it if URLs need updating)."
fi

# anacron makes missed cron runs execute at boot / hourly timer. Without
# it the weekly build-cache prune silently skipped every run for 3 weeks
# on this VM (docs/ops-notes.md, 2026-07-24 outage).
if ! dpkg -l anacron >/dev/null 2>&1; then
  echo "Installing anacron..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y anacron
fi

# Post-install sanity: try one heartbeat cycle to prove the wiring.
if [ -x /etc/cron.hourly/ptt-heartbeat ]; then
  echo "Firing one heartbeat now to smoke-test..."
  /etc/cron.hourly/ptt-heartbeat || true
  echo "See: journalctl -t openptt-alert --since '1 min ago'"
fi

#!/usr/bin/env bash
# =============================================================================
# TLS Setup for PTT Server
# Gets Let's Encrypt certificates for ptt.harro.ch and voice.harro.ch
# =============================================================================
set -euo pipefail

PTT_DIR="${PTT_DIR:-/opt/ptt}"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run as root: sudo bash scripts/setup-tls.sh"
    exit 1
fi

# Install certbot
if ! command -v certbot &>/dev/null; then
    echo "Installing certbot..."
    apt-get update -qq
    apt-get install -y -qq certbot
fi

# Stop nginx to free port 80/443
echo "Stopping Nginx temporarily..."
cd "$PTT_DIR"
docker compose stop nginx

# Get certs
echo "Getting certificate for ptt.harro.ch..."
certbot certonly --standalone -d ptt.harro.ch --non-interactive --agree-tos --email admin@harro.ch

echo "Getting certificate for voice.harro.ch..."
certbot certonly --standalone -d voice.harro.ch --non-interactive --agree-tos --email admin@harro.ch

# Mount certs into Docker volume
echo "Linking certificates..."
docker compose up -d nginx

echo ""
echo "TLS configured!"
echo "  https://ptt.harro.ch   (admin panel)"
echo "  voice.harro.ch:443     (Mumble over TLS)"
echo ""
echo "Auto-renewal is handled by certbot's systemd timer."
echo "Run 'certbot renew --dry-run' to verify."

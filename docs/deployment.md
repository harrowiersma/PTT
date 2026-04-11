# Deployment Guide

## Prerequisites

- Ubuntu 25.04 VPS (1 vCPU, 1 GB RAM minimum; 2 vCPU, 2 GB recommended for 50+ users)
- 10-20 GB SSD storage
- Root access
- DNS A records pointing to the VPS IP:
  ```
  ptt.harro.ch    A  <VPS_IP>
  voice.harro.ch  A  <VPS_IP>
  ```

## One-Command Install

Set up DNS first, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/harrowiersma/PTT/main/scripts/install.sh | sudo bash
```

This single command:
1. Installs Docker and Docker Compose
2. Installs certbot and git
3. Clones the repo to `/opt/ptt`
4. Generates a `.env` with random admin password and JWT secret
5. Obtains Let's Encrypt TLS certificates for both domains
6. Opens firewall ports (443, 80, 64738)
7. Builds and starts all containers

The admin password is printed to the terminal and saved in `/opt/ptt/.env`.

To retrieve it later:
```bash
grep PTT_ADMIN_PASSWORD /opt/ptt/.env
```

## What Gets Deployed

| Service    | Port | Purpose |
|------------|------|---------|
| Nginx      | 443  | SNI routing: ptt.harro.ch -> admin, voice.harro.ch -> Murmur |
| Nginx      | 80   | HTTP redirect to HTTPS |
| Murmur     | 64738| Mumble voice (direct fallback) |
| Admin API  | 8000 | FastAPI (internal, accessed via Nginx) |
| PostgreSQL | 5432 | Database (internal only) |

## After Install

1. Open `https://ptt.harro.ch` and log in
2. Create channels (Warehouse, Office, Security, etc.)
3. Create users with passwords
4. Click **QR** on each user to generate an enrollment code
5. Sideload HamMumble on P50 devices and scan the QR code

## Setting Up Hytera P50 Devices

See [p50-setup.md](p50-setup.md) for device configuration.

## How SNI Routing Works

Both admin dashboard and Mumble voice share port 443. Nginx's stream module inspects the TLS ClientHello SNI field to route:
- `ptt.harro.ch:443` -> admin dashboard (HTTPS)
- `voice.harro.ch:443` -> Murmur (Mumble protocol, TCP passthrough)

This solves the corporate firewall problem: only port 443 needs to be open, which is almost never blocked.

Port 64738 (TCP+UDP) is also open as a fallback for Mumble clients that connect directly.

## TLS Certificate Renewal

Certificates auto-renew via certbot's systemd timer. Verify with:
```bash
certbot renew --dry-run
```

Certificates are stored at:
- `/etc/letsencrypt/live/ptt.harro.ch/`
- `/etc/letsencrypt/live/voice.harro.ch/`

## Monitoring

Health check endpoint: `GET https://ptt.harro.ch/api/status/health`

```json
{"status": "healthy", "murmur": "connected"}
```

Use with UptimeRobot, Healthchecks.io, or similar.

## Backup

```bash
cd /opt/ptt

# Backup PostgreSQL
docker compose exec postgres pg_dump -U ptt ptt > backup.sql

# Backup Murmur data
docker compose cp murmur:/data ./murmur-backup
```

## Management

```bash
cd /opt/ptt
docker compose logs -f        # view logs
docker compose restart         # restart all
docker compose down            # stop all
docker compose up -d           # start all
docker compose pull && docker compose up -d  # update
```

## Troubleshooting

### Admin dashboard shows "Murmur disconnected"
- Check Murmur: `docker compose logs murmur`
- Verify ICE in `murmur.ini`: `ice="tcp -h 0.0.0.0 -p 6502"`
- Test ICE connection: `docker compose exec admin curl -v murmur:6502`

### Devices can't connect
- Check port: `nc -zv voice.harro.ch 443` and `nc -zv voice.harro.ch 64738`
- Check firewall: `ufw status`
- Verify device has correct server address (`voice.harro.ch`, port `443`)

### TLS certificate failed during install
- Verify DNS resolves: `dig ptt.harro.ch` and `dig voice.harro.ch`
- Retry manually:
  ```bash
  docker compose stop nginx
  certbot certonly --standalone -d ptt.harro.ch --email harro@wiersma.info
  certbot certonly --standalone -d voice.harro.ch --email harro@wiersma.info
  docker compose up -d nginx
  ```

### Audio quality issues
- Bandwidth in `murmur.ini`: 24000 = 24kbps (increase to 48000 for better quality)
- Check client network: unstable cellular causes jitter
- Murmur logs: `docker compose logs murmur | grep -i error`

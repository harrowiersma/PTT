# Deployment Guide

## VPS Requirements

- 1 vCPU, 1 GB RAM minimum (2 vCPU, 2 GB recommended for 50+ users)
- 10-20 GB SSD storage
- Ubuntu 22.04 or 24.04 LTS
- Open ports: 443 (TCP) for both admin + voice via SNI routing, 80 for HTTP redirect, 64738 (TCP+UDP) as fallback
- Two DNS A records pointing to the VPS IP: `ptt.harro.ch` + `voice.harro.ch`

## Quick Start

### 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in for group change to take effect
```

### 2. Clone and configure

```bash
git clone <your-repo-url> ptt-server
cd ptt-server
cp .env.example .env
```

Edit `.env`:
- Set `PTT_ADMIN_PASSWORD` to a strong password
- Set `PTT_SECRET_KEY` (generate: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`)
- Set `PTT_PUBLIC_HOST` to your voice subdomain (e.g., `voice.harro.ch`)

### 2b. Set up DNS

Create two A records pointing to your VPS IP:
```
ptt.harro.ch  A  <VPS_IP>
voice.harro.ch  A  <VPS_IP>
```

Then update `nginx.conf`, `nginx-stream.conf`, and `nginx-main.conf`:
replace `harro.ch` with your actual domain.

### 3. Start the server

```bash
docker compose up -d
```

This starts:
- Murmur (Mumble voice server) on port 64738 (direct) and via Nginx SNI on port 443
- PostgreSQL database
- Admin service on port 8000 (direct) and via Nginx on port 80/443
- Nginx with stream module: routes `ptt.harro.ch:443` to admin, `voice.harro.ch:443` to Murmur

### 4. Access admin dashboard

Open `http://ptt.harro.ch` (or `https://ptt.harro.ch` after TLS setup) in a browser. Log in with the credentials from `.env`.

### 5. Create users and channels

1. Go to the **Channels** tab, create your channels
2. Go to the **Users** tab, create users with passwords
3. Click **QR** on a user to generate their enrollment QR code

## Setting Up Hytera P50 Devices

See [p50-setup.md](p50-setup.md) for device configuration.

## TLS/HTTPS Setup

For production, configure TLS for both subdomains:

```bash
# Stop Nginx temporarily so certbot can bind port 80/443
docker compose stop nginx

# Get certificates for both subdomains
apt install certbot
certbot certonly --standalone -d ptt.harro.ch
certbot certonly --standalone -d voice.harro.ch

# Restart Nginx with certs
docker compose up -d nginx
```

The Nginx config is pre-configured to use Let's Encrypt cert paths. Just replace `harro.ch` with your domain in the config files.

## Firewall Configuration

```bash
# Allow HTTPS (SNI routes both admin and voice on port 443)
sudo ufw allow 443/tcp

# Allow HTTP (for redirect / initial setup)
sudo ufw allow 80/tcp

# Allow Mumble direct (fallback for clients not using SNI subdomain)
sudo ufw allow 64738/tcp
sudo ufw allow 64738/udp

sudo ufw enable
```

**How SNI routing works:** Both admin dashboard and Mumble voice share port 443. Nginx's stream module inspects the TLS ClientHello SNI field to route:
- `ptt.harro.ch:443` -> admin dashboard (HTTPS)
- `voice.harro.ch:443` -> Murmur (Mumble protocol, TCP passthrough)

This solves the corporate firewall problem: only port 443 needs to be open, which is almost never blocked.

## Monitoring

Health check endpoint: `GET /api/status/health`

Returns:
```json
{"status": "healthy", "murmur": "connected"}
```

Use this for uptime monitoring (e.g., UptimeRobot, Healthchecks.io).

## Backup

```bash
# Backup PostgreSQL
docker compose exec postgres pg_dump -U ptt ptt > backup.sql

# Backup Murmur data
docker compose cp murmur:/data ./murmur-backup
```

## Troubleshooting

### Admin dashboard shows "Murmur disconnected"
- Check if Murmur is running: `docker compose logs murmur`
- Verify ICE is enabled in `murmur.ini` (line: `ice="tcp -h 0.0.0.0 -p 6502"`)
- Check network: `docker compose exec admin curl -v murmur:6502`

### Devices can't connect
- Check port is open: `nc -zv voice.harro.ch 64738`
- Check firewall rules
- Verify device has the correct server address and credentials

### Audio quality issues
- Check bandwidth setting in `murmur.ini` (24000 = 24kbps, increase to 48000 for better quality)
- Check client-side network: unstable cellular connection causes jitter
- Murmur logs: `docker compose logs murmur | grep -i error`

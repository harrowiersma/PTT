# Operational notes

Server-side hardening added incrementally after two disk-full outages
and one silent-alerting incident. Kept here so the next person who finds
a wedged dashboard knows what to look at.

## Alert delivery (added 2026-07-24)

External heartbeat via [healthchecks.io](https://healthchecks.io). The
admin already exposes an unauthenticated `GET /api/status/health` that
returns `{status,murmur}`; `/etc/cron.hourly/ptt-heartbeat` hits it,
maps to a ping URL, and healthchecks emails the operator if pings stop
for >2h. This is the ONLY layer that pages you when the whole VM is off
— every internal safety net (Docker restart, keepalive, cron) fails
silently otherwise.

Configuration:

- `/etc/default/openptt-alerts` — URL config file. `ALERT_HEARTBEAT_URL`
  is the healthchecks.io ping URL; leaving it blank makes every alert a
  no-op (still logs to journald under tag `openptt-alert`).
- `/usr/local/bin/openptt-alert` — thin wrapper that any script can
  call. Modes: `ok <name> [msg]`, `incident <title> <body>`,
  `dead-mans <name>`.
- Add two more free healthchecks.io checks for `ALERT_DISK_URL` and
  `ALERT_BACKUP_URL` if you want per-check granularity (heartbeat alone
  catches the outage class but not slow-burn issues).

## Docker Compose hardening (added 2026-07-24)

`docker-compose.yml` now uses:

- `x-logging` anchor with `max-size=50m, max-file=3` on every service —
  log rotation baked into the deployment, not just the host.
- `healthcheck:` on murmur, postgres, admin, traccar, nginx (sip-bridge
  is host-networked so we skip it — its telemetry surfaces via admin's
  call-flow logs instead).
- `depends_on: condition: service_healthy` chain: admin waits for
  postgres+murmur healthy; sip-bridge, nginx, traccar wait for admin
  healthy. A crash-looping container no longer masquerades as "Up".

## Server code resilience fixes (added 2026-07-24)

- **admin↔murmur keepalive** — `server/main.py` re-binds pymumble on
  wedge, replacing the manual `docker compose restart admin` that used
  to be needed after every murmur crash.
- **Multi-call state** — `server/api/sip.py` `_active_calls: dict` now
  keyed by CallLog.id instead of the previous single-slot dict that
  corrupted concurrent-call bookkeeping. `sip-bridge` passes `slot=N`
  in the `/internal/call-ended` callback for authoritative closure.
- **SOS channel restore** — new `sos_channel_restore` table persists
  per-user return-to-channel state (was module-global dict; admin
  restart mid-SOS stranded users in Emergency).
- **Lone-worker escalation** — replaced one-shot `_reminded` set with
  time-throttled reminders that auto-fire `/api/sos` after N missed
  check-ins (previously `auto_sos_on_overdue` config existed but was
  never actually checked).
- **Traccar timeout** — every `httpx.AsyncClient()` now has
  `timeout=10.0`. A hung Traccar could otherwise choke every dashboard
  request via `/api/status/server`.

## Android app resilience fixes (added 2026-07-24)

- **TX watchdog on primary PTT path** — the 60s force-release that
  already existed for the BLE ring now covers every keying path
  (`MumlaService.onTalkKeyDown`). A dropped ROM ACTION_UP broadcast can
  no longer stick the mic keyed.
- **ACTION_CANCEL** in `MumlaOverlay` and `MumlaHotCorner` PTT
  listeners — a focus steal (e.g. incoming-call overlay) mid-press no
  longer leaves TX on.
- **ActiveCallActivity** — 60-min max-duration watchdog + hangup.
  Prevents a stuck in-call screen with `FLAG_KEEP_SCREEN_ON` draining
  the battery when the SIP bridge crashes mid-call.
- **IncomingCallActivity.onNewIntent** — second `INCOMING_CALL` whisper
  while the first is still ringing now correctly updates the visible
  caller-id/sub-channel + re-arms the ring timeout (was silently
  dropped by `singleTask` launch mode).

## Container log rotation (added 2026-07-20)

Docker's default `json-file` logging driver has **no rotation** — a
verbose container can fill the disk in weeks. On 2026-07-20 the
`ptt-sip-bridge-1` container log alone reached 31 GB and filled
`/dev/sda1`, taking Murmur down (same `database or disk is full`
crash pattern as the earlier outage).

Two independent controls now in place:

**`/etc/docker/daemon.json`** — sets `max-size=50m, max-file=3` as the
default log-opts for future containers. Takes effect only when a
container is recreated (`docker compose up -d --force-recreate` or after
`docker compose down/up`).

**`/etc/logrotate.d/docker-containers`** — daily rotation with
`copytruncate` for existing running containers. Kicks in when any
`*-json.log` exceeds 50 MB, keeps 7 rotations, compressed. No Docker
restart or container recreation needed.

Combined: worst case ~150 MB per container between rotations, ~450 MB
per container total on disk. Check current sizes with:

```
ssh voice.harro.ch 'du -sh /var/lib/docker/containers/*/*json.log*'
```

## anacron (added 2026-07-20)

`/etc/cron.weekly/docker-builder-prune` (below) was installed 2026-06-25
but had **not run once** by 2026-07-20 — Ubuntu ships without `anacron`
by default, and if the machine is off at exactly Sunday 06:47 the cron
run is silently missed. Installed `anacron` so missed runs execute at
the next boot / next hourly `anacron.timer` fire. Verify with:

```
ssh voice.harro.ch 'systemctl list-timers anacron.timer'
```

## Weekly Docker build cache prune

`/etc/cron.weekly/docker-builder-prune` runs `docker builder prune -af` every
Sunday at 06:47. Output goes to journald under tag `docker-builder-prune` —
inspect with:

```
journalctl -t docker-builder-prune --since "1 month ago"
```

**Why it exists.** On 2026-06-16 `/dev/sda1` filled to 100% (38G) — 12.8 GB
of it was stale BuildKit cache layers. The murmur container couldn't write
to its SQLite log table on boot (`database or disk is full`), crash-looped
a few times, and exited. It stayed down for 8 days because nothing alerted
on disk usage or container exit.

**What the script does NOT prune.** Only the BuildKit build cache. If a
future disk fill comes from images, stopped containers, or volumes instead,
this won't catch it. The script logs `df -h /` before and after so a
regression that doesn't free space is visible.

## Admin → Murmur auto-reconnect

`server/main.py` runs a background task (`Murmur keepalive`) every 30 s that:

1. TCP-probes `murmur:64738`.
2. Checks the pymumble worker thread is still alive.

If either fails it calls `client.disconnect()` (cleans up the zombie pymumble
thread and socket) and then `client.connect()` to rebind cleanly. Silent on
the happy path; emits `Murmur keepalive: tcp=... py_alive=... — rebinding`
when it has to recover.

**Why it exists.** pymumble has `reconnect=True` internally, but it can wedge
if Murmur is down long enough or the container is recreated. Before this
keepalive, the admin's Ice/pymumble state would diverge from reality:
`_connected=True` but the user dict was empty, so `/api/status/server`
returned `users=[]` and the dashboard showed everyone offline even after
Murmur came back. Manual fix was `docker compose restart admin`. Now it
recovers automatically.

## Useful one-liners

Check every safety net is doing its job:

```
# Disk: should be well under 80%
ssh voice.harro.ch 'df -h / | tail -1'

# Largest container logs (any single file > 200 MB means something is off)
ssh voice.harro.ch 'du -sh /var/lib/docker/containers/*/*json.log* | sort -rh | head -10'

# Last logrotate cycle status
ssh voice.harro.ch 'cat /var/lib/logrotate/status | grep docker | head -10'

# Last build-cache prune outcome
ssh voice.harro.ch 'journalctl -t docker-builder-prune --since "1 month ago" --no-pager'

# Anacron: confirm the weekly job will fire (or has fired)
ssh voice.harro.ch 'systemctl list-timers anacron.timer && cat /var/spool/anacron/cron.weekly 2>/dev/null'

# Keepalive activity (silent = healthy; rebind lines = something recovered)
ssh voice.harro.ch 'cd /opt/ptt && docker compose logs --since 24h admin | grep -i keepalive'
```

## Known noise sources

The `ptt-sip-bridge-1` container was the disk-full culprit on 2026-07-20:
~15 MB/hour of logging in steady state. Worth investigating whether that's
per-call SIP traces that can be quieted (the log-rotation caps the damage
either way).

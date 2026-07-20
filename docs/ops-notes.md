# Operational notes

Server-side hardening added incrementally after two disk-full outages.
Kept here so the next person who finds a wedged dashboard knows what to
look at.

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

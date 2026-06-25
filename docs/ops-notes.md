# Operational notes

Server-side hardening added 2026-06-25 after an 8-day outage. Kept here so
the next person who finds a wedged dashboard knows what to look at.

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

Check both safety nets are doing their job:

```
# Disk: should be well under 80%
ssh voice.harro.ch 'df -h / | tail -1'

# Last build-cache prune outcome
ssh voice.harro.ch 'journalctl -t docker-builder-prune --since "1 week ago" --no-pager'

# Keepalive activity (silent = healthy; rebind lines = something recovered)
ssh voice.harro.ch 'cd /opt/ptt && docker compose logs --since 24h admin | grep -i keepalive'
```

# User Status / Presence — Design

**Date:** 2026-04-21
**Scope:** Feature #2 of the "day-to-day ops upgrade" iteration — warm-up item.
**Branch target:** `main` (server + dashboard + openPTT-app).

---

## Goal

Give every radio user an explicit three-state presence signal — **Online**, **Busy**, **Offline** — surfaced everywhere operators look (Live Ops table, user list, dispatch targeting), and settable from the radio via the single orange top button.

Today the dashboard shows whether someone's Mumble-connected, but not whether they're *available for work*. This adds intent on top of connection state and makes `/api/dispatch/nearest` respect it.

---

## Decisions (locked)

1. **Three states, fixed.** `online | busy | offline`. No custom free-text anywhere; the P50 has no keyboard.
2. **Connect auto-sets Online.** When a Mumble client connects, the server sets their `status_label='online'` (system-driven, audited as `auto_connect`). Busy and Offline are only ever set by explicit user action or admin override.
3. **Disconnect does not update the DB.** The stored label persists. The **displayed** badge collapses to "Offline" whenever the user isn't currently connected — a single rule that covers both intent and connection.
4. **Dispatch filter is strict.** `/api/dispatch/nearest` returns only workers whose `status_label='online'` AND who are currently connected. Busy / Offline / never-connected are excluded.
5. **Shift coupling is gated on lone-worker.** If `features.lone_worker` is enabled AND `users.is_lone_worker=true`:
   - Starting a shift force-sets status to Online (audit `shift_start`).
   - Picking Offline auto-ends any active shift with `end_reason='user_offline'` (audit `shift_stop_offline`).
   Otherwise status is a pure intent signal with no shift side-effects.
6. **Who can change whose status.** Self (radio picks its own), admin (dashboard can override any user). All changes write an audit-log entry with the actor and source.
7. **Default on user creation.** `status_label=NULL` (effective Offline). First successful Mumble connect promotes to Online.
8. **P50 trigger is the orange top button.** Empirically confirmed: single press emits `KEYCODE_F4` (and `KEYCODE_F2` as a paired event we ignore) and the ROM's `LoneWorkerManagerService` responds "loneworker is disabled", letting the event reach `MumlaActivity.dispatchKeyEvent` as an unhandled key. Cycle is Online → Busy → Offline → Online.
9. **TTS confirms every state change** — same path already used for shift toggle / SIP hangup / mute.

---

## Backend

### Migration

`server/alembic/versions/f2a9c3b7e4d1_user_status.py`
- `down_revision = "e1c4f8a3b5d6"` (dispatch settings head).

Two columns added to `users`:

| column | type | notes |
|---|---|---|
| `status_label` | varchar(16) nullable | values ∈ {`online`,`busy`,`offline`} or NULL |
| `status_updated_at` | timestamptz nullable | set on every write |

No separate seed. Existing rows get NULL; first Mumble connect promotes.

### Endpoints

Pattern matches `/api/loneworker/shift/*` — device-trusted for self operations, admin-only for cross-user writes.

| method | path | auth | body / query | purpose |
|---|---|---|---|---|
| `GET` | `/api/users/status?username=X` | none | — | Read a user's stored + effective status. |
| `POST` | `/api/users/status` | none (device-trusted) | `{username, label}` | Self set. Radio client uses this. |
| `PATCH` | `/api/users/{id}/status` | admin | `{label}` | Admin override. Dashboard user-edit modal. |

All three:
- Validate `label ∈ {'online','busy','offline'}`.
- Persist `(status_label, status_updated_at=now)`.
- Write an `AuditLog` row — `action='user.status_change'`, `payload={username, from, to, source}` where `source ∈ {'self','admin','auto_connect','shift_start','shift_stop_offline'}`.
- Return `{label, updated_at, effective_label}`.

`effective_label` = `'offline'` if the user is not Mumble-connected, otherwise `status_label` (with NULL mapping to `'offline'`).

### Auto-Online on Mumble connect

`MurmurClient` already subscribes to `PYMUMBLE_CLBK_USERCONNECTED`. Add a handler that:

1. Skips bot usernames (reuse the existing `BotUsers` filter).
2. Resolves the Mumble username → `users` row.
3. If found and `status_label != 'online'`, writes `status_label='online'`, `status_updated_at=now`, plus an audit row with `source='auto_connect'`.
4. No-op on disconnect.

The callback runs on pymumble's sync thread — use the same short-lived sync engine pattern as `loneworker._run_shift_cycle` to avoid fighting the async event loop.

### Shift coupling

In `/api/loneworker/shift/start`, after the new shift row is committed:
- Read `features.lone_worker` (enabled?) and `user.is_lone_worker` (true?). Both must hold.
- If so, call internal `set_status(user, 'online', source='shift_start')`.

In `POST /api/users/status` and `PATCH /api/users/{id}/status`, when the new label is `'offline'`:
- Read the same two gates.
- If both hold and an active `LoneWorkerShift` exists, call internal `_end_shift(active, reason='user_offline')`.
- Audit the shift end exactly like the existing `/shift/stop` path.

### Dispatch filter

`server/api/dispatch.py::find_nearest` currently drops positions with `(0,0)` coords. Extend the predicate:

```python
if username not in connected_usernames:     # from MurmurClient.online_usernames()
    continue
if db_user_by_name.get(username.lower(), {}).get('status_label') != 'online':
    continue
```

Connected-set comes from the existing Mumble user tracker. User-by-name map is a single DB query at the top of the handler (cached nowhere — the handler is already doing a `users` query for `traccar_device_id` mapping; reuse the same result set).

### Audit log

The `AuditLog` table already exists and is surfaced in `System → Audit Log`. No schema change. Status rows render with `action='user.status_change'` and the payload above.

---

## Dashboard

### Live Ops → Overview

The online-users table currently has columns `Username | Channel | Location | Battery | Last Seen | State`. Insert a new **Status** column between Username and Channel. Renders as a pill with a 6px coloured dot + one-word label:

- `online` → `?var(--color-success)` (green)
- `busy`   → amber
- `offline` → grey
- NULL / never-set → em dash

Offline rows (Mumble-disconnected registered users) are already rendered at `opacity:0.6` and always show the pill as Offline regardless of stored label — this is the "effective" rule applied client-side.

### Directory → Users

Same pill column added to the user table — helps admins glance at presence alongside Traccar linkage and role.

### User edit modal

Add a **Status** dropdown (Online / Busy / Offline) just above the "Can answer calls" toggle. Saving hits `PATCH /api/users/{id}/status`. A small line underneath: `Last changed 3m ago by admin` — pulled from the audit log's most recent matching entry for this user.

### Audit Log tab

No UI work beyond what's already there. Status changes will appear automatically once the server writes `action='user.status_change'` entries.

---

## P50 app — openPTT

### Hardware evidence

Capture from `R259060623` on 2026-04-21:

- Raw input (`getevent -lq`): `/dev/input/event11: EV_KEY KEY_F4 DOWN/UP` on single press of the orange top button.
- Logcat on press (filtered): the kernel emits **both** `KEYCODE_F2 (132)` and `KEYCODE_F4 (134)` from the same device (scan codes 60 and 62). `LoneWorkerManagerService.notifyKeyEvent` sees both and logs `loneworker is disabled` — the ROM lets them through. The framework then logs:
  ```
  WindowManager: Unhandled key: title=ch.harro.openptt/.app.MumlaActivity,
                 action=0, keyCode=134, scanCode=62
  ```
  Meaning `MumlaActivity.dispatchKeyEvent` receives both events — we simply aren't consuming them yet.

**Prerequisite:** Hytera's ROM-level lone-worker toggle must remain disabled. If it's ever enabled, the ROM claims F3/F4 for its built-in handler and this mechanism breaks. Default state on both P50s today is disabled; the provisioning flow should never touch it.

### Intercept point

`MumlaActivity.dispatchKeyEvent(KeyEvent event)` — same method that already handles the channel knob (`KEYCODE_F5` / `KEYCODE_F6`). Add:

- On `ACTION_DOWN` of `KEYCODE_F4` (scan code 62): call `cycleStatus()`, return `true` to consume.
- On `ACTION_DOWN` of `KEYCODE_F2` (scan code 60): return `true` to consume **without** cycling — prevents the paired-press from double-firing.
- Both `ACTION_UP` events: return `true` to consume silently.

### Cycle logic

`cycleStatus()`:

1. Read current label from an in-memory field (seeded from the server on connect/resume).
2. Compute next: `online → busy → offline → online`.
3. `POST /api/users/status` with `{username, label: next}`.
4. On success: update the in-memory field, update the carousel status pill, speak the new state via TTS (`"Online"` / `"Busy"` / `"Offline"`).
5. On failure: speak `"Status change failed"` and leave the in-memory field untouched so the pill stays accurate.

### Status pill on the carousel

- Placed directly under the active-channel name in the carousel page, centered.
- 16dp tall, auto-width, 4dp horizontal padding.
- Coloured dot (6dp) on the left using theme attrs: `?attr/colorSuccess` | `?attr/colorAmber` | `?attr/colorMuted`.
- Label: one word, display font, same case as the dot colour suggests (`ONLINE` / `BUSY` / `OFFLINE`).
- Parchment-card backdrop to match the ActiveCall overlay aesthetic.
- Invisible when no status is known yet (initial app launch before first `GET /api/users/status` resolves).

### Hydration

- On `MumlaService` connection established → `GET /api/users/status?username=me` — this catches the server-side auto-Online promote.
- On `MumlaActivity.onResume` → same call. Catches admin overrides and shift-coupling effects that happened while the app was backgrounded.

---

## Testing

Run all pytest via the docker sidecar (Python 3.11):

```bash
docker exec ptt-pytest sh -c "rm -f test.db && python -m pytest tests/ -v"
```

New test files:

- `tests/test_user_status_model.py` — columns exist, nullable, default NULL; migration applies cleanly.
- `tests/test_user_status_api.py` — GET / POST self / PATCH admin; label validation (422 on bad values); admin auth required for PATCH; audit row written with correct source.
- `tests/test_user_status_shift_coupling.py` —
  - Shift start forces status to Online (with lone_worker enabled + is_lone_worker=true).
  - Shift start does nothing to status when lone_worker is disabled.
  - Picking Offline ends an active shift with `end_reason='user_offline'` — gated the same way.
- `tests/test_dispatch_filter_status.py` — `/api/dispatch/nearest` excludes users whose `status_label` is Busy, Offline, or NULL; includes Online+connected users.
- `tests/test_murmur_auto_online.py` — simulating the `USERCONNECTED` callback promotes a user to Online and skips bots.

Dashboard + P50 verified manually on `ptt.harro.ch` and on both P50s (R259060623, R259060618).

---

## Open questions / deferred

1. **Busy-survives-reconnect?** Current design says every connect auto-sets Online, erasing a prior Busy pick. The operator accepted this for simplicity. Reconsider only if users complain about having to re-pick Busy after a flaky connection.
2. **Admin override audit surfacing in the user-edit modal** — we show "Last changed Xm ago by <actor>" but the full history lives in the Audit Log tab. Dedicated per-user timeline deferred until someone asks.
3. **Custom status text** — explicitly rejected for this iteration. P50 can't type; dashboard-driven custom labels would leak a fourth state and complicate dispatch filtering. Revisit only if a specific ops ask emerges.
4. **Status pill visible on ALL app screens (not just carousel)** — deferred. The carousel is the app's home; other screens are transient. Add later if requested.

---

## Dependencies + ordering

- Server-side migration + endpoints + auto-Online hook can ship before the app changes — status will just never be set to Busy/Offline without the radio UI.
- Dashboard and P50 app are independent of each other after the server side lands.
- Dispatch filter should land in the same commit as the server endpoints so it reads the new column from day one.

Full execution plan will be produced by `writing-plans` into `docs/plans/2026-04-21-user-status-presence.md`.

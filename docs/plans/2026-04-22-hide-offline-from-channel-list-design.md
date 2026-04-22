# Hide Offline-Status Users from P50 Channel List — Design

**Date:** 2026-04-22
**Scope:** Follow-up to user status / presence (shipped 2026-04-21).
**Branch target:** `main` (server + openPTT-app).

---

## Goal

When a radio user marks themself **Offline** (orange top button or admin override), hide them from every other P50's channel user list within 20 seconds. The user is still Mumble-connected, but they're signaling "off-duty" — their row should disappear from the list so dispatchers see only people who actually intend to be reachable. The user themself still sees their own row regardless.

---

## Why this isn't just an `/api/users/status` extension

The existing `GET /api/users/status?username=X` answers a single user's state. Polling it once per visible user every 20s is wasteful (N round trips, N×ish bytes). A single map endpoint hands back the whole picture in one shot, scales with channel size, and matches the "humans-only view" surface the dashboard's `_status.users` already exposes.

---

## Decisions (locked)

1. **Hide Offline only.** Busy stays visible with an amber "BUSY" badge in the user row (mirrors today's "ONLINE" green fallback). Operator's framing was specifically *"when a user is in offline mode"* — Busy means "do not dispatch right now," not "I'm gone."
2. **Polling, every 20 seconds, while Mumble-connected.** Simpler than server push via Mumble text messages; bandwidth is trivial (a few hundred bytes per poll for the small fleet today). Refresh also fires immediately on connect and right after each `postStatus(...)` (the user just changed their own state — usually changes their own visibility too).
3. **Self is never hidden.** Even when the local user is Offline, their own row stays in the channel list. Otherwise the carousel page looks broken on the device whose user just toggled to Offline.
4. **No "show everyone" toggle in v1.** YAGNI — the dashboard has the full picture for any "where did Sarah go?" debugging. Reopen if operators ask for it.
5. **Pure client-side filter.** The server endpoint exposes data; the app decides who to hide. No server-side concept of "channel-list visibility."
6. **NULL / unknown status = visible (fail-open).** A user not in the map (network error, race condition with a fresh registration) renders normally. Wrong call would be to hide them and panic the operator; right call is to show them and refresh on the next poll.

---

## Backend

### Endpoint

`server/api/user_status.py` gains one route:

```
GET /api/users/presence-map  →  no auth (matches GET /api/users/status convention)
```

Response shape (lowercased usernames as keys; bots excluded):

```json
{
  "harro":  { "status_label": "online", "is_audible": true },
  "yuliia": { "status_label": "busy",   "is_audible": false },
  "alex":   { "status_label": "offline", "is_audible": null }
}
```

Implementation (~10 LoC): single `SELECT username, status_label, is_audible FROM users` with a Python-side filter against `BOT_USERNAMES + ("PTTPhone-*",)`. No caching — the table has dozens of rows at most and the query is in the millisecond range.

### Why no auth

Same reasoning as `/api/users/status`: the data leaked is "this username has presence label X." Lower sensitivity than channel membership (which is already public-via-Mumble) and the dashboard's per-user list. The radio app needs the data without managing JWTs.

---

## App

### Three new pieces

**`PresenceCache.java`** — `se.lublin.mumla.channel`

Singleton-ish (one instance held by `MumlaService`). Holds:

```java
private volatile Map<String, String> mStatusByLcUsername = Collections.emptyMap();
public  String  getStatus(String username);   // null if unknown
public  void    refresh();                    // background HTTP, notifies on change
public  void    addListener(Listener l);
```

`refresh()` POSTs are off the main thread (same `new Thread(...)` pattern as `MumlaService.postStatus`). Compares the new map to the previous; if different, broadcasts `PRESENCE_MAP_UPDATED` so the channel-list adapters re-submit.

**`PresenceFilter.java`** — `se.lublin.mumla.channel`

Pure-helper static methods, mirrors `BotUsers`:

```java
public static boolean isHidden(IUser user, PresenceCache cache, String selfName);
public static int     countVisible(List<? extends IUser> users, PresenceCache cache, String selfName);
```

`isHidden` returns true iff:
- `user != null`
- `user.getName()` is NOT `selfName` (case-insensitive)
- `cache.getStatus(user.getName())` equals `"offline"`

Otherwise false (visible). Bot users are still filtered separately at every call site (we don't fold the bot check into `PresenceFilter` because the two concerns are independent — a future change might want bots-only or presence-only filtering).

**`MumlaService` integration:**

- New field: `PresenceCache mPresenceCache`.
- Constructor: `mPresenceCache = new PresenceCache(this)`.
- On Mumble `onConnectionEstablished`: spin up a `ScheduledExecutorService` that calls `mPresenceCache.refresh()` every 20s. First call is immediate.
- On disconnect: cancel the scheduler.
- After each successful `postStatus(...)` HTTP 2xx: trigger an immediate `mPresenceCache.refresh()` (the user just changed their own state; the visible-set on every other radio just changed too).
- Public getter `getPresenceCache()` for fragments to subscribe.

### Call-site wirings

| Site | Today | Change |
|---|---|---|
| `UserRowAdapter.submit()` line 33-37 | Skips bots | Also skip `PresenceFilter.isHidden(u, cache, selfName)` |
| `UserRowAdapter.onBindViewHolder` lines 99-107 | "ONLINE" green fallback | When stored status is `"busy"` (and no higher-priority state wins): render "BUSY" in amber `#FFBF00`. Otherwise "ONLINE" stays as today. |
| `ChannelCardFragment.java:104` | `BotUsers.countHumans(...)` | `PresenceFilter.countVisible(channel.getUsers(), cache, self)` |
| `ChannelCarouselFragment.java:353` | `BotUsers.countHumans(...)` | Same as above |

`UserRowAdapter` gains a constructor parameter `PresenceCache cache` (or a setter — TBD by the implementer when they see how it's currently instantiated).

### Repaint trigger

`PresenceCache` exposes a listener interface. `ChannelCarouselFragment` and `ChannelCardFragment` register on view bind, unregister on view destroy. The listener calls back on the main thread (post to `mHandler`) and re-submits the user list to the adapter, which triggers a redraw.

Without this, the list would only redraw on Mumble events (USERUPDATED, channel switches) — a remote user toggling Offline would stay visible until the next Mumble event.

---

## Tests

**Server** (pytest sidecar):

- `tests/test_presence_map_api.py`:
  - `test_presence_map_returns_keyed_dict` — three users seeded, response keyed by lowercased username.
  - `test_presence_map_excludes_bots` — seed `PTTAdmin`, `PTTPhone-1`, real users; bots not in response.
  - `test_presence_map_no_auth_required` — unauthenticated GET returns 200.
  - `test_presence_map_handles_null_status` — user with NULL status_label appears with `status_label: null`.

**App** (JUnit, runs on the build host via `./gradlew :app:testFossDebugUnitTest`):

- `PresenceFilterTest.java`:
  - `online_user_is_visible`
  - `busy_user_is_visible`
  - `offline_user_is_hidden`
  - `null_status_user_is_visible` (fail-open)
  - `missing_from_map_user_is_visible` (fail-open)
  - `self_offline_is_still_visible`
  - `bot_user_not_in_scope` (PresenceFilter doesn't care; that's BotUsers' job)

**Manual smoke on both P50s**:

1. Both connected, both Online → both see each other.
2. harro presses orange → "Busy" → yuliia's P50 sees harro listed with **BUSY** badge (still visible). harro still sees themselves.
3. harro presses orange again → "Offline" → within 20s, yuliia's P50 no longer lists harro. harro still sees themselves.
4. harro presses orange again → "Online" → within 20s, yuliia's P50 lists harro again with the green ONLINE badge.
5. Admin overrides yuliia to "Offline" via dashboard → within 20s, harro's P50 stops listing yuliia.

---

## Open questions / deferred

1. **"Show everyone" toggle** — explicitly deferred. Drawer entry, carousel softkey, or just a debug preference? Decide when an operator asks.
2. **Visually distinguishing "hidden because Offline" vs "left the channel"** — out of scope; both render as "not in the list."
3. **Server push instead of polling** — re-evaluate if the fleet grows past ~50 users or if 20s lag becomes an operator complaint.
4. **Filter on the dashboard's Live Ops list too** — no. The dashboard is a complete-picture surface; the channel list on the radio is a "who's reachable" surface. Different views.

---

## Dependencies + ordering

- Server endpoint can ship before the app — no observable behavior change.
- App-side filter requires the endpoint, so endpoint goes first.
- All three call-site wirings are independent; can land in one commit or three.
- Repaint listener is the second-most-important piece (after the filter itself); without it the cache updates but the UI doesn't.

Full execution plan will be produced by `writing-plans` into `docs/plans/2026-04-22-hide-offline-from-channel-list.md`.

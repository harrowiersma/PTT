# Call Groups — Design

**Date:** 2026-04-22
**Scope:** Server (DB + Murmur ACL + admin endpoints + dashboard).
**Branch target:** `main`.

---

## Goal

Scope each Mumble user's joinable channel set to one or more **call groups**. As fleets grow (multi-team, multi-site, multi-customer on a shared server), operators need to keep teams in their own channels without seeing or hijacking each other's. Today every user can see and join every channel; that's fine for one team but not for shared infrastructure.

---

## Decisions (locked)

1. **Bounce-on-entry, not tree-hide.** Reuses the proven Phone-ACL pattern in `MurmurClient._on_user_updated` (line 524). Tree-hiding non-visible channels would require Murmur-side per-user ACL state in `mumble-server.sqlite` — possible (we have `admin_sqlite.py`) but a second source of truth to keep in sync. Bounce-on-entry achieves the same end-state with code we already trust. App-side carousel hiding can layer on top later as a separate enhancement.

2. **NULL default → visible-to-all; new users start in zero groups.** Schema-level: `channels.call_group_id IS NULL` means "no restriction" (anyone joins). A user with no group memberships can join NULL-tagged channels and nothing else. Migration is zero-cost — every existing channel gets `call_group_id=NULL` so today's "everyone sees everything" behaviour is preserved exactly. As soon as an admin creates a group and assigns it to a channel, that channel becomes restricted to group members. No "Everyone" pseudo-group needed; NULL is simpler and requires zero fixture data.

3. **Super-admin escape hatch via `users.is_admin`.** If `user.is_admin = true`, the bounce check returns early. That's the existing User-row flag (set per-user at create time), distinct from the dashboard JWT admin login. Lets `is_admin` users moderate any channel from the radio side.

4. **30 s refresh cadence via lifespan loop.** The bridge holds `_user_call_groups` + `_channel_call_group` + `_user_is_admin` dicts in memory; a background task in `server/main.py`'s lifespan refreshes them every 30 s from the DB. Same pattern + same cadence as today's phone-eligible refresh. No per-bounce DB hit.

5. **`PUT /api/call-groups/{id}/members` replaces wholesale.** Membership delta APIs (add/remove individual users) are wordier and need conflict handling. The PUT-with-full-list shape is simpler and matches the dashboard's checkbox-list UX (the form sends the current full state on save).

6. **Channel `ON DELETE SET NULL`** — deleting a group converts every channel it gated back to "visible to all" rather than orphaning the channel or cascading-delete-the-channel. Safer default; admins explicitly retag channels if they want different post-delete behaviour.

---

## Schema

New Alembic migration `g3b8d4f6e2a9_call_groups.py`, `down_revision = "f2a9c3b7e4d1"` (user_status head).

### `call_groups`

| column | type | notes |
|---|---|---|
| `id` | int PK autoincrement | |
| `name` | varchar(64) | unique, not null |
| `description` | varchar(256) | nullable |
| `created_at` | timestamptz | server_default now |

### `user_call_groups` (join table)

| column | type | notes |
|---|---|---|
| `user_id` | int FK `users(id)` | ON DELETE CASCADE |
| `call_group_id` | int FK `call_groups(id)` | ON DELETE CASCADE |

Composite primary key `(user_id, call_group_id)`.

### `channels` (modify)

Add column:

| column | type | notes |
|---|---|---|
| `call_group_id` | int FK `call_groups(id)` | ON DELETE SET NULL, nullable |

Existing rows default to NULL → no behaviour change at deploy time.

---

## Bridge — `server/murmur/client.py`

Mirrors the phone-eligible pattern.

### New `MurmurClient` fields

```python
# Lowercased username → set of call_group_ids the user belongs to.
self._user_call_groups: dict[str, set[int]] = {}
# Mumble channel_id → call_group_id (or None for unrestricted).
self._channel_call_group: dict[int, Optional[int]] = {}
# Lowercased username → is_admin flag (escape-hatch lookup).
self._user_is_admin: dict[str, bool] = {}
# Cached deny-whisper TTS, rendered on first bounce.
self._call_group_deny_pcm: Optional[bytes] = None
```

### New public method

```python
def update_call_group_state(
    self,
    user_groups: dict[str, set[int]],
    channel_groups: dict[int, Optional[int]],
    user_admin: dict[str, bool],
) -> None:
    """Refresh the in-memory call-group state.

    Called by a lifespan task every 30 s. Atomic swap; the read paths
    in _on_user_updated grab the dicts at call time so no locking is
    needed for the swap itself."""
    self._user_call_groups = user_groups
    self._channel_call_group = channel_groups
    self._user_is_admin = user_admin
```

### New helper

```python
def _call_group_check(self, name: str, new_channel_id: int) -> bool:
    """Return True if `name` may join `new_channel_id` per call-group rules.

    Allowed when:
      - the user is an admin (escape hatch), OR
      - the channel has no call_group_id (unrestricted), OR
      - the channel's call_group_id is in the user's group set.
    """
    lc = name.lower()
    if self._user_is_admin.get(lc, False):
        return True
    chan_group = self._channel_call_group.get(new_channel_id)
    if chan_group is None:
        return True
    return chan_group in self._user_call_groups.get(lc, set())
```

### Extend `_on_user_updated`

After the existing phone-acl block (line ~558), add:

```python
            # Call-group ACL — same bounce mechanic, different deny TTS.
            if not self._call_group_check(name, new_channel_id):
                logger.info(
                    "call-group: bouncing '%s' (session=%s) from channel %d",
                    name, session_id, new_channel_id,
                )
                self._bounce_from_channel(
                    session_id, prev_channel_id, self._render_call_group_deny(),
                )
                return
```

### Generalise the bounce

Today's `_bounce_from_phone(session_id, previous_channel_id)` always uses the cached phone-deny PCM. Generalise to take the deny PCM as a parameter:

```python
def _bounce_from_channel(
    self, session_id: int, previous_channel_id: int, deny_pcm: Optional[bytes],
) -> None:
```

The phone path becomes `self._bounce_from_channel(sid, prev, self._render_phone_deny())`. New `_render_call_group_deny()` mirrors `_render_phone_deny()` with text *"This channel requires call group membership"*.

### Lifespan refresh loop — `server/main.py`

Existing pattern: a background task polls every 30 s and calls `murmur_client.update_phone_eligible(...)`. Add a parallel task or extend the existing one to also build the three call-group dicts and pass them via `update_call_group_state(...)`.

Single SQL query covers everything:

```python
# users + their groups + admin flag
result = await db.execute(
    select(User.username, User.is_admin, UserCallGroup.call_group_id)
    .outerjoin(UserCallGroup, UserCallGroup.user_id == User.id)
)
# channels + their group_id (mumble_id is what the bridge uses for callbacks)
result2 = await db.execute(
    select(Channel.mumble_id, Channel.call_group_id)
    .where(Channel.mumble_id.isnot(None))
)
```

Bucket into the three dicts client-side, swap atomically.

---

## Server endpoints — new `server/api/call_groups.py`

Standard admin-CRUD:

```
GET    /api/call-groups                    → list with member + channel counts
POST   /api/call-groups                    → {name, description?} → 201
GET    /api/call-groups/{id}               → detail incl. members + channels
PATCH  /api/call-groups/{id}               → {name?, description?}
DELETE /api/call-groups/{id}               → 204 (FK cascades clean up)
PUT    /api/call-groups/{id}/members       → {user_ids: [int]} replaces wholesale
```

All admin-auth (`Depends(get_current_admin)`). Audit-log every write.

---

## Existing endpoint extensions

### `server/api/users.py`

- `UserResponse` gains `call_group_ids: list[int]` (loaded from join table).
- `UserCreate` + `UserUpdate` accept optional `call_group_ids: list[int] | None`. When set, replace the user's join-table rows.

### `server/api/channels.py`

- `ChannelResponse` gains `call_group_id: int | None`.
- `ChannelCreate` + `ChannelUpdate` accept optional `call_group_id: int | None`. `null` clears the assignment.

---

## Dashboard — `server/dashboard/index.html`

- New sub-tab **Call Groups** in the Directory group (alongside Users + Channels), `data-tab="call-groups"`. Insert at line ~838 in the `subTabsDirectory` block.
- New `tab-call-groups` content block: standard table (name, description, member count, channel count, actions) + "Add group" form + edit modal.
- Extend the user-edit modal with a checkbox-list of call groups (multi-select dropdowns are clunky on small screens; for typically-low-N membership lists, checkboxes are clearer).
- Extend the channel-edit modal with a single-select dropdown of call groups, with "— Visible to all —" as the null option.
- No Live Ops badges. Call group membership is admin metadata, not operator-relevant per-row state.

---

## Testing

- `tests/test_call_groups_model.py` — schema correctness, cascade behaviour (deleting a group SET NULLs channels; deleting a user removes join rows).
- `tests/test_call_groups_api.py` — CRUD + member replacement + auth.
- `tests/test_call_group_acl.py` — mirrors `tests/test_phone_acl.py`:
  - User not in group → bounced.
  - User in group → not bounced.
  - User with `is_admin=true` → not bounced regardless.
  - Channel with `call_group_id=NULL` → no restriction.
  - Multi-group user can enter any of their groups' channels.

All run via the existing `ptt-pytest` docker sidecar.

---

## Out of scope (deferred)

- **App-side carousel filter** — hide non-visible channels from the knob navigation. Defer until operators ask for crispier UX; bounce-on-entry is already in the field via Phone-ACL with no complaints.
- **Per-channel ACL beyond group membership** (read-only vs talk, etc.). Defer.
- **Group nesting / hierarchy.** Defer.
- **Bulk membership ops beyond `PUT /members`** (CSV import, etc.). Defer.

---

## Migration + rollout impact

- New tables empty → existing channels all NULL-tagged → behaviour preserved exactly.
- Admin creates a group + assigns users + tags channels → bounces kick in within 30 s (next refresh cycle).
- Zero app-side changes required for v1.
- Coordinate with the True Call Hold deploy (parallel session): both touch the server but disjoint files. Ordering is whichever lands second wins the merge — since both designs are committed before either executes, conflicts will be limited to `docs/open_issues.md` (both eventually mark themselves Resolved). Easy to fix on second push.

---

## Dependencies + ordering

- Schema → bridge state → endpoints → dashboard → tests-included-along-the-way.
- No app-side dependency.

Implementation plan: `docs/plans/2026-04-22-call-groups.md`.

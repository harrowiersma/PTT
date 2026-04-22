# Call Groups — True Hiding (Mumble ACL) — Design

**Date:** 2026-04-22
**Scope:** Server only (admin DB + `admin_sqlite` + Murmur ACL table + bridge cert-hash capture + dashboard wiring).
**Branch target:** `main`.
**Depends on:** the shipped call-groups feature (migration `g3b8d4f6e2a9`, commits `a7b7d7d..6136cc5`).

---

## Goal

Make restricted channels **invisible** in the channel tree to users who aren't in the call group — instead of showing them + bouncing on entry + playing a "denied" TTS.

Operator's phrasing: *"hide channels where a user has no access to, instead of telling 'no you're not allowed in' — feels more enterprise style."*

---

## Why the current bounce-based approach isn't enough

Shipped behaviour (commits `c2c30e2` + `6136cc5`):

1. `MurmurClient._on_user_updated` fires on USERUPDATED, calls `_call_group_check`, and `_bounce_from_channel` on fail — does `user.move_in(previous_channel_id)` + whispers a deny TTS.
2. `_sweep_call_group_violators` does the same every 30 s for users grandfathered into restricted channels.

**Observed in prod** (commit `6136cc5` deployed, yuliia in Sales, only harro in Sales group):

```
12:07:11.210 INFO server.murmur.client: call-group: bouncing 'yuliia' (session=6) from channel 9
12:07:38.687 INFO server.murmur.client: call-group: sweep bouncing 'yuliia' (session=6) out of channel 9
12:08:11.736 INFO server.murmur.client: call-group: sweep bouncing 'yuliia' (session=6) out of channel 9
```

The bounce code ran three times. yuliia stayed in Sales until she navigated out *herself* at 12:08:16. Reason: **PTTAdmin is an anonymous bot**. Murmur's default ACL grants `@all` only `SelfRegister` — so `user.move_in(...)` issued by PTTAdmin to move another user is silently rejected on the server side (no `Move` permission). The TTS whisper works because whispering doesn't require that permission.

Additionally, even if the bounce worked, the UX is still "denied after trying to enter" — operator wants the channel to never appear in the carousel in the first place.

---

## Decisions (locked)

1. **Use Murmur's native ACL, not bounce.** Target the channel's `Traverse` + `Enter` permissions with `apply_sub=1`. Denying `Traverse` at a parent scope hides the entire subtree from affected users — the channel literally doesn't appear in the Mumble channel list they see. That's the enterprise UX.

2. **Per-user grants, not access tokens.** Tokens would be cleaner (unregistered users + server-side ACL in one hop) but require the Android app to fetch and pass tokens — splits the work across repos. We register each user in Mumble's sqlite so they have a stable `user_id`, then grant per-user `Enter + Traverse` on restricted channels.

3. **Cert-hash capture on first connect, auto-registration on next connect.** We capture each user's cert hash via the bridge's `USERCREATED` callback the next time they connect. Once captured, the admin writes a Murmur `users` row for them (via `admin_sqlite`), gets a stable `user_id`, and stores it in our admin DB. ACLs can then reference that `user_id`.

4. **Keep bounce-on-entry + sweep as defense in depth.** Path A remains code-resident even after Path B lands — if ACL application ever fails (murmur container restart race, sqlite write glitch), the bounce-based check catches a user who still somehow entered a restricted channel. Belt + braces.

5. **ACL apply triggers a murmur restart.** Same pattern as channel creation in `admin_sqlite.create_channel_and_restart`. Batched: one save from the dashboard that affects N channels produces one restart, not N. Trade-off: ~3 s of Mumble downtime per ACL change batch. Rare (group membership changes are bursty + low-volume). Users auto-reconnect.

6. **First-run cost — users must reconnect once.** Before the next admin restart captures their cert hash + registers them, users remain "unknown" to Murmur and will be caught by the `@all deny Traverse` on any restricted channel (they'd be denied as non-members — correct behaviour but surprising if their admin has already added them to a group). Operator-visible: the dashboard shows a "not yet registered" badge for users with `mumble_cert_hash IS NULL` or `mumble_user_id IS NULL`, plus a one-shot "force-all-reconnect" button that triggers a murmur restart.

7. **Re-parenting, tokens, and granting PTTAdmin `Move` are rejected alternatives.** Re-parenting restricted channels under a hidden parent is fragile and clutters the tree. Access tokens require app changes. Granting PTTAdmin `Move` via ACL would make bounce finally stick but keeps the channel visible + the denied-TTS UX — not hiding.

---

## Schema changes

Alembic migration `h4c9e5a7f3b2_call_groups_hiding.py`, `down_revision = "g3b8d4f6e2a9"`.

### `users` — add two columns

| column | type | notes |
|---|---|---|
| `mumble_cert_hash` | varchar(128) | Nullable. SHA-1 cert fingerprint captured from pymumble's `USERCREATED`/`USERUPDATED` callback. Unique *when non-null*. |
| `mumble_registered_user_id` | int | Nullable. The `user_id` assigned in `mumble-server.sqlite` after registration via `admin_sqlite.register_user`. Unique when non-null. |

Unique partial indexes on both columns (Postgres: `WHERE ... IS NOT NULL`). On SQLite (tests), a plain unique index is fine — all values NULL won't clash.

No changes to the `call_groups` or `user_call_groups` tables — membership already lives there. No `channels` columns change.

### (Optional, phase-2) audit table

Not in v1. `AuditLog` entries for ACL changes are enough.

---

## Bridge changes (cert-hash capture)

`server/murmur/client.py`:

1. Extend `_on_user_created_sync` (already handles presence-label promotion) to also read the user's cert hash and write to `users.mumble_cert_hash` if not yet set.

   pymumble exposes the hash as `user["hash"]` (SHA-1, hex). It's present on `USERCREATED` and updated on `USERUPDATED`. Use the same short-lived sync engine pattern the function already uses.

2. Silent if hash not present (some clients don't send one — typically fresh SuperUser-mode connects). No error — the user just stays unregistered.

3. No collision handling in the bridge: if two DB users happen to share a hash (shouldn't, one device per user), one wins on insert + the other's update silently fails. Logged but non-fatal.

---

## Admin-sqlite helpers (new)

`server/murmur/admin_sqlite.py`:

### `register_user(username: str, cert_hash: str) -> int`

- Inserts into Murmur's `users` table (`server_id=1`, `user_id=<next>`, `name=<username>`, `pw=NULL`, `salt=NULL`).
- Inserts into `user_info` linking `user_id` to the cert hash (key=`user_hash`, value=`<hash>`).
- Bounces the murmur container (same pattern as `create_channel_and_restart`).
- Returns the new `user_id`.
- Serialized under the existing `_admin_lock` so concurrent registrations don't race.

### `set_channel_acl(mumble_channel_id: int, member_user_ids: list[int]) -> None`

Replaces the ACL for a channel with a deny/allow pair:

| priority | user_id | group | apply_here | apply_sub | grantpriv | revokepriv |
|---|---|---|---|---|---|---|
| 1 | NULL | `all` | 1 | 1 | 0 | `0x06` (Enter\|Traverse) |
| 2+i | `member[i]` | NULL | 1 | 1 | `0x0E` (Enter\|Traverse\|Speak) | 0 |

- First `DELETE FROM acl WHERE channel_id = ?`.
- Then batch `INSERT`s.
- Bounces murmur container.

### `clear_channel_acl(mumble_channel_id: int) -> None`

`DELETE FROM acl WHERE channel_id = ?` + restart. Used when a channel is untagged or the group is deleted.

### `batched_acl_apply(changes: list[tuple[int, list[int]]]) -> None`

Takes `(channel_id, member_user_ids)` pairs, applies all, **one restart** at the end. The dashboard's `PUT /channels` or `PUT /members` calls this once per save.

---

## Dashboard wiring

Three existing endpoints trigger ACL application (on top of the DB writes they already do):

1. `PUT /api/call-groups/{id}/members` — after commit, fetch all channels tagged with this group → for each, compute `(channel_mumble_id, [user_ids_of_members])` → call `batched_acl_apply`.

2. `PUT /api/call-groups/{id}/channels` — for each channel added to this group: compute its member `user_ids`, apply ACL. For each channel removed: clear ACL.

3. `DELETE /api/call-groups/{id}` — for each channel previously tagged: clear ACL.

The existing `call_group_state` poller in `server/main.py` is now redundant for enforcement (Murmur itself enforces), but kept as the source of truth for the bounce/sweep defense in depth.

---

## Auto-registration scheduler

A small background task in `server/main.py` lifespan (parallel to the existing pollers):

- Every 60 s, scan `users` where `mumble_cert_hash IS NOT NULL AND mumble_registered_user_id IS NULL`.
- For each, call `admin_sqlite.register_user(username, hash)` → get `user_id` → write back.
- After all pending registrations complete, the poller triggers a recompute of any active ACL so the newly-registered user gets their grants.

Gate the per-iteration loop behind a feature flag `call_groups_hiding` (default off, lit by admin in dashboard) so the feature can be enabled/disabled without a code push.

---

## Operator UX (dashboard)

1. Call Groups tab gains a status pill per user in the modal's member list: "registered" (has a `mumble_registered_user_id`) or "pending cert capture" (no `mumble_cert_hash` yet).

2. Users directory gains a "Mumble registered" column with the same badge.

3. A single button on the Call Groups tab: "Force-all reconnect" — restarts the murmur container. Tells every P50 to reconnect, so any pending `mumble_cert_hash` captures land within ~10 s. Use sparingly.

---

## First-deploy choreography

The migration landing on prod is harmless (adds two nullable columns). But enforcement changes the UX the moment it's turned on. Recommended sequence:

1. Land the schema + bridge cert-hash capture (Tasks 1-2). No observable change yet.
2. Observe prod for ≥1 day — verify every active user has a `mumble_cert_hash` set. Any user without one is someone who hasn't reconnected in 24 h.
3. Land the registration path (Tasks 3-4). Background task registers everyone in one murmur restart cycle.
4. Observe prod for ≥1 day — verify every active user has a `mumble_registered_user_id`.
5. Land the ACL apply path (Tasks 5-6). Flip the feature flag. Test with a single group (Sales) + single channel.
6. If smoke passes, enable for remaining groups.

If anything goes sideways, disable the feature flag — ACL already applied rolls back at the next "refresh" cycle (or an admin explicitly clears them), and the bounce + sweep defense catches any edge cases in the interim.

---

## Edge cases

1. **User changes cert (re-provisioning).** New hash arrives via `USERCREATED`. Bridge writes it to `mumble_cert_hash`, overwriting the old one. Admin's `mumble_registered_user_id` is still valid but points to the OLD cert in Murmur's sqlite — the user can't authenticate. Operator-visible: the user will show "not yet registered" (new hash, no new `user_id`). Admin re-registers on next scheduler pass, gets a new `user_id`, ACLs refresh.

2. **Admin deletes a user.** The on-delete cascade on `users` also clears the admin-side `mumble_registered_user_id`. But Murmur's sqlite still has their registration. Acceptable orphan — harmless (no one connects with that cert anymore). Cleanup: a nightly job or admin "prune Mumble registrations" button.

3. **Call group name collision with Mumble ACL group.** Mumble has built-in groups named `@all`, `@auth`, `@admin`. Our call-group names are user-chosen. No collision risk because we don't use group names — we write per-user ACL rows referencing `user_id`.

4. **Race between ACL apply and a user's move.** The bridge's bounce still runs (defense in depth). If a user somehow enters a channel during the ~3 s murmur restart window, the bounce on the next USERUPDATED catches them.

5. **Empty call group (no members) on a tagged channel.** `batched_acl_apply(channel, [])` writes ONLY the deny-all ACL. Channel is invisible to everyone (including `is_admin=true` users). Fix: `is_admin` users always get added to the allow list regardless of group membership (mirrors the bounce-side bypass). Same semantics, different layer.

6. **`is_admin` toggled.** Same code path as member add/remove — trigger ACL recompute for every channel the user-is_admin affects. For simplicity, just re-apply all ACLs when any `is_admin` changes. Cheap (still one restart).

---

## Rollout kill-switch

The feature flag `call_groups_hiding` (default false) gates everything past Task 4:

- Off: cert hash capture + user registration still runs (data collection). ACL not applied. Behaviour matches today's call-groups (bounce-on-entry + sweep — limited).
- On: ACL applied on every relevant endpoint; bounce + sweep stays as defense.

Turning it off **does not un-apply existing ACL** — that requires calling `clear_channel_acl` for every channel. Expected behaviour: admin turns feature off only if debugging a regression; they call `clear_channel_acl` manually (or a dashboard "Clear all ACL" button) as a second step.

---

## Non-goals

- App changes. Zero touches to the openPTT-app repo.
- Migrating existing bounce/sweep code away — it stays as safety net.
- Per-channel access tokens. Considered + rejected (app change needed).
- ACL changes at sub-channel depth beyond `apply_sub=1`. Channels are one-level under Root in this fleet.
- "Guest" role — anyone in zero groups is still bounced + hidden from tagged channels. Same as today.

---

## Open questions / deferred

1. Per-channel ACL beyond group membership (e.g., read-only vs talk, per-user permissions). Defer.
2. Audit trail for ACL changes beyond the existing `AuditLog` rows. Defer.
3. Traffic log / analytics of who was bounced/hidden when. Defer — presence logs are enough for now.
4. Batch cert-hash backfill (admin upload of pre-known certs) — deferred; the connect-based capture is simpler and covers all real users in a reconnect cycle.

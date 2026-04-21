# Dispatch / Map Merge — Phase 6 Design

**Date:** 2026-04-21
**Replaces:** the OSM routing approach previously sketched as Phase 6.
**Branch target:** `main` (server-side only; Android app unchanged).

---

## Goal

Collapse the dashboard's two related-but-split tabs (`Live → Map` and `Live → Dispatch`) into a single operational dispatch page. Move all admin-y configuration (Saved Locations CRUD, map default, search behaviour, canned messages) onto a new `System → Dispatch Setup` tab.

The operator's daily workflow becomes one page: pick a target → see workers on the map and in a list → send. Setup happens out-of-band on a config tab, not interleaved with operations.

---

## Why not OSM/OSRM routing?

The previous Phase 6 sketch added a self-hosted OSRM container for driving distance + ETA. It was the right *answer* to a different question — operators don't need driving routes today, they need a usable single-page dispatch UI. The routing engine adds an 8th container, monthly PBF refresh ops, and ~200 MB RAM for a feature nobody has asked for. Distance stays as straight-line haversine.

This phase explicitly does not ship OSM routing. If it ever becomes worth doing, the design from the prior sketch can be reused; nothing here forecloses it.

---

## Decisions (locked)

1. **Tab structure** — operational page lives at `Live → Dispatch`; configuration lives at `System → Dispatch Setup`. The existing `Live → Map` sub-tab is removed (its functionality folds into Dispatch).
2. **Results UI** — map markers + ranked list, both. Ship the list first; halo markers on top-N closest workers as a follow-up task.
3. **Recent Dispatches log** — stays on the operational page, below the nearest-workers list. Provides ambient context for the active dispatcher.
4. **Setup knobs** — five cards on Dispatch Setup: Map Home, Search Behaviour (max workers + radius), Saved Locations, Canned Messages.
5. **Storage shape** — three small typed tables instead of a single key/value blob. Worth the extra ceremony for the type-safety and per-field validation.

---

## Backend

### Migration

`server/alembic/versions/e1c4f8a3b5d6_dispatch_settings_and_messages.py`
- `down_revision = "d7e9a1f2b0c3"` (call_logs head).

**`dispatch_settings`** (singleton — only ever one row, `id=1`)
| column | type | notes |
|---|---|---|
| `id` | int PK | always 1; CHECK constraint enforces |
| `map_home_lat` | float | seed 38.72 (Lisbon) |
| `map_home_lng` | float | seed -9.14 |
| `map_home_zoom` | int | seed 11 |
| `max_workers` | int | seed 10; range 1-50 enforced at API |
| `search_radius_m` | int nullable | NULL = unbounded; seed NULL |
| `updated_at` | timestamptz | onupdate=now |
| `updated_by` | varchar(64) nullable | admin username |

Migration seeds the singleton row.

**`dispatch_canned_messages`**
| column | type | notes |
|---|---|---|
| `id` | int PK | autoincrement |
| `label` | varchar(64) | shown in dropdown |
| `message` | varchar(500) | actual dispatch text |
| `sort_order` | int | for stable ordering |
| `created_at` | timestamptz | default now |

No seed; admin populates from the UI.

### Endpoints

All under existing `requires_feature("dispatch")` router gate.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/dispatch/settings` | none | dashboard reads on load (before login completes) so map can centre correctly |
| PUT | `/api/dispatch/settings` | admin | update settings; clamp `max_workers` to 1-50 and `search_radius_m` to ≥ 0 |
| GET | `/api/dispatch/messages` | admin | list canned messages, ordered by `sort_order` |
| POST | `/api/dispatch/messages` | admin | create canned message |
| PUT | `/api/dispatch/messages/{id}` | admin | update label/message/sort_order |
| DELETE | `/api/dispatch/messages/{id}` | admin | delete |

`GET /api/dispatch/nearest` (existing) is modified internally — reads `max_workers` and `search_radius_m` from the settings row, applies as filter + limit. **Response shape unchanged** to preserve dashboard backwards compatibility.

The settings GET being unauthenticated is intentional and minor — the only secret it leaks is "the operator likes their map centred at 38.72, -9.14". Same risk profile as `/api/status/capabilities`.

### Behaviour change for `nearest`

```python
# pseudo-code, in find_nearest()
settings = await get_dispatch_settings(db)  # cached singleton
results = []
for p in positions:
    if p.latitude == 0 and p.longitude == 0:
        continue
    distance = TraccarClient.haversine_distance(...)
    if settings.search_radius_m and distance > settings.search_radius_m:
        continue
    results.append({...})
results.sort(key=lambda x: x["distance_m"])
return results[: settings.max_workers]
```

Settings cached in-process and refreshed on PUT (same pattern as `server/features.py`).

---

## Frontend

### Tab edits in `server/dashboard/index.html`

- **Remove** the `Map` sub-tab from the Live group (line ~817) and delete the `tab-map` content block (lines 928-938). Worker-marker code moves into the merged Dispatch view.
- **Keep** the `Dispatch` sub-tab label in Live; replace its content with the new merged layout.
- **Add** a `Dispatch Setup` sub-tab to the System group (after Features, line ~831): `<div class="sub-tab" data-tab="dispatch-setup" onclick="switchTab('dispatch-setup')" data-feature="dispatch">Dispatch Setup</div>`.
- **Add** a `tab-dispatch-setup` content block.

### Operational page (Live → Dispatch) layout

Top to bottom inside `tab-dispatch`:

1. **Quick-dispatch chips** (existing `#savedLocationsBar` block, kept).
2. **Search bar + Find Nearest button** (existing, kept).
3. **Map card** — full-width Leaflet, 500 px tall. Renders:
   - Worker markers (carry-over from Map tab — `mapMarkers[]`).
   - SOS markers (carry-over).
   - Target pin (new — drops on search/chip-click/map-click).
   - Top-N closest workers get an amber halo marker class (follow-up task; ship without on day one).
4. **Nearest Workers card** — existing 4-column table (Worker · Distance · Last GPS · Action).
5. **Recent Dispatches card** — existing, unchanged.

The map's existing click-to-dispatch behaviour stays. The single change: clicks now also drop a visible target pin and update the same `Nearest Workers` table that search/chips populate (instead of the old `#nearestList` panel which gets removed).

### Setup page (System → Dispatch Setup) layout

Top to bottom inside `tab-dispatch-setup`:

1. **Map Home card** — three numeric inputs (lat / lng / zoom) + Save. Validates lat ∈ [-90, 90], lng ∈ [-180, 180], zoom ∈ [1, 19].
2. **Search Behaviour card** — `max_workers` (1-50) + `search_radius_m` (≥ 0, where 0 means unbounded — UI shows "no limit" hint) + Save.
3. **Saved Locations card** — moved verbatim from current `tab-dispatch`. The existing `dispatch_locations` table and CRUD endpoints are unchanged.
4. **Canned Messages card** — table with columns Label · Message · Order · Actions (edit/delete). Add row at bottom.

### Dispatch modal change

Today's dispatch flow uses `prompt(...)` for the message. Replace with a small modal:
- Dropdown of canned messages (populated from `/api/dispatch/messages`); default option "Custom message…".
- Textarea pre-filled when a canned message is picked; user can still edit before sending.
- Send / Cancel buttons.

Falls back to the dropdown being empty (label-only, free-text) if no canned messages are configured — not a blocker.

### Map default sourcing

On dashboard load (after login):
```js
await fetchDispatchSettings();  // populates window._dispatchSettings
// later, in initMap():
const s = window._dispatchSettings || {map_home_lat: 38.72, map_home_lng: -9.14, map_home_zoom: 11};
map = L.map('mapContainer').setView([s.map_home_lat, s.map_home_lng], s.map_home_zoom);
```

If the API call fails, fall back to Lisbon defaults (matches the seed).

---

## Error handling

| Failure | Behaviour |
|---|---|
| `dispatch_settings` row missing (shouldn't happen post-migration) | Backend returns hardcoded defaults; log warning |
| Invalid lat/lng on PUT | 400 with field-specific message |
| `max_workers` out of range | 400 ("must be 1-50") |
| `search_radius_m < 0` | 400 |
| `/api/dispatch/settings` GET fails on dashboard load | Map uses Lisbon defaults; toast warning |
| Canned messages list empty | Dispatch modal works in free-text-only mode |
| Worker count after radius filter is 0 | Existing empty-state on the table handles it |

---

## Testing strategy

**Unit (backend, pytest):**
- `dispatch_settings` model defaults match seed.
- `nearest` endpoint respects `max_workers` (insert 15 fake positions, expect ≤ 10 by default, ≤ 5 when configured).
- `nearest` endpoint respects `search_radius_m` (positions outside radius excluded).
- Canned-message CRUD round trip.
- Settings PUT clamps invalid values.

**Manual (staging then prod):**
1. Stage on local docker. `docker compose up -d --build admin`.
2. Apply migration. `docker exec ptt-admin-1 alembic upgrade head`.
3. Open dashboard. Verify Map sub-tab gone; Dispatch sub-tab shows merged layout; map centres on Lisbon by default.
4. Open `System → Dispatch Setup`. Verify all 4 cards render. Add a saved location, a canned message, change max_workers to 5.
5. Back on Dispatch. Verify chip appears for the new location, search returns ≤ 5 workers, dispatch modal offers the canned message.
6. Deploy. `docker compose up -d --build admin` on `ptt.harro.ch`.
7. Repeat verification on prod with a real GPS-enabled device.

---

## Non-goals (explicitly out of scope)

- OSM/OSRM driving routing. Distance stays haversine.
- ETA column. Not a thing without routing.
- `dispatch_routing` sub-flag. There's no routing to gate.
- Worker-marker hover ↔ table-row pulse synchronization. Nice-to-have, deferred.
- Halo markers on top-N closest workers. Ship the list first; markers as a separate task in this phase.
- Dispatch role/eligibility filtering ("only drivers"). Defer until anyone asks.
- Saved-location reordering UI. Manual `sort_order` edit is fine for v1.

---

## Ship criteria

Phase 6 is done when:
- Migration `e1c4f8a3b5d6` applied on `ptt.harro.ch`.
- `Live → Map` sub-tab gone; `Live → Dispatch` shows the merged page.
- `System → Dispatch Setup` exists with all four cards working end-to-end.
- Map default is Lisbon out of the box; changing the home in Setup persists across reloads.
- Setting `max_workers=5` makes `/api/dispatch/nearest` return ≤ 5 results.
- Setting `search_radius_m=2000` filters out workers more than 2 km away.
- Dispatch modal offers canned messages from the configured list, with free-text fallback.
- A real dispatch on prod completes end-to-end: search → pick worker → modal → message delivered.
- Each task lands as its own commit; pushed to `origin/main`.

---

## Migration order summary

```
b7c5d9e0f1a2 (provisioning_tokens)
  └── c1a2b3d4e5f6 (feature_flags)
        └── d7e9a1f2b0c3 (call_logs)
              └── e1c4f8a3b5d6 (dispatch_settings + dispatch_canned_messages)  ← this phase
```

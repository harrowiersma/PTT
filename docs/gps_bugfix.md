# GPS Data Not Showing in Dashboard — Bug Fix

## Problem
GPS positions from Traccar Client apps on P50 devices are not visible in the openPTT TRX dashboard (Overview, Map, Dispatch tabs). Traccar shows devices as "online" but the positions API returns empty.

## Root Cause
**Traccar device ownership.** The `/api/positions` endpoint only returns positions for devices owned by the authenticated user. Devices created via `registerUnknown=true` (auto-registration when a device first reports GPS) are not assigned to any Traccar user — they exist in the database but the admin API session can't see their positions.

The harro device (uid 372194) was auto-registered when `registerUnknown` was temporarily enabled. It had `positionId=42` (GPS data existed) but the admin user couldn't see it because no ownership link existed.

## Fix Applied (Session 2026-04-16)
Assigned both devices to the admin user via Traccar permissions API:
```
POST /api/permissions {"userId": 1, "deviceId": 4}  # harro
POST /api/permissions {"userId": 1, "deviceId": 8}  # yuliia
```

After this, `/api/positions` returns harro's GPS data correctly.

## Permanent Fix Needed
The `TraccarClient` in `server/traccar_client.py` needs to be updated:

### 1. Auto-assign devices on creation
When `create_device()` is called, also assign the device to the admin user:
```python
async def create_device(self, name, unique_id):
    # ... create device ...
    # Then assign to admin user
    await self._assign_device_to_admin(device_id)
```

### 2. Fix `get_positions()` to request all devices
Currently `get_positions()` calls `/api/positions` without parameters, which only returns owned devices. Options:
- **Option A:** Always pass `?deviceId=X` for each known device (multiple requests)
- **Option B:** Ensure all devices are assigned to the admin user (current fix)
- **Option C:** Use `/api/reports/route` or direct DB query

**Recommended: Option B** — auto-assign on device creation + a startup check that assigns any orphaned devices.

### 3. Startup device assignment check
On admin service startup, query all devices with `?all=true`, check which ones are unassigned, and assign them to the admin user.

## Files to Modify
- `server/traccar_client.py` — add `_assign_device_to_admin()`, call in `create_device()`, add startup check
- `server/main.py` — call device assignment check on startup (in lifespan)

## How to Verify
1. Check dashboard Overview tab — devices should show GPS coordinates
2. Check Map tab — device markers should appear on the map
3. Check Dispatch tab — "Find Nearest" should return workers with GPS data
4. API check: `GET /api/status/server` should include latitude/longitude for online users

## Weather Bot GPS Bug (Same Root Cause)
The weather bot in `server/weather_bot.py` line 342 also uses name matching:
```python
if p.device_name.lower() == username.lower():
```
This needs to be updated to use the `traccar_device_id` link from the User model, same as was done for `status.py` and `dispatch.py`. Additionally, the weather bot creates its own `TraccarClient()` instance which had the same empty-positions issue (now fixed by assigning devices to admin user).

### Files to fix:
- `server/weather_bot.py` line 338-345 — replace name matching with device ID lookup from DB

### Additional weather bot concerns:
- TinyTTS audio playback via pymumble has never been tested on the live server
- The `_ensure_db_channel` coroutine warning in logs (`was never awaited`) suggests the async/sync bridge has issues
- Double-PTT detection requires audio to be received by the bot — bot must be in the Weather channel or receive audio callbacks globally

## Related
- Traccar devices: harro (id=4, uid=372194), yuliia (id=8, uid=245195)
- Traccar admin: admin@ptt.local / admin
- User-device links in PostgreSQL: `users.traccar_device_id`
- The `?all=true` parameter on `/api/devices` is needed for admin to see all devices (already fixed in traccar_client.py)

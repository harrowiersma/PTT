# Hytera P50 Setup Guide

The openPTT TRX app handles voice PTT, hardware PTT button, channel switching,
and **GPS + battery reporting to Traccar** in a single app — no separate
Traccar Client app is required.

## Prerequisites

- Hytera P50 device
- USB cable for the P50
- Computer with `adb` installed (Android Debug Bridge)
- `openptt-foss-debug.apk` (built from `openPTT-app/`, package `ch.harro.openptt`)

## Step 1: Enable Developer Mode on P50

1. Go to **Settings > About Phone**
2. Tap **Build Number** 7 times to enable Developer Options
3. Go to **Settings > Developer Options**
4. Enable **USB Debugging**

## Step 2: Install openPTT TRX

Connect P50 to computer via USB.

```bash
# Verify device is connected
adb devices

# Install openPTT TRX
adb install -r openptt-foss-debug.apk
```

If a previous version (or HamMumble / Traccar Client) is installed, remove it:

```bash
adb uninstall org.traccar.client   # separate GPS app no longer needed
adb uninstall com.morlunk.mumbleclient  # legacy HamMumble, if present
```

## Step 3: Configure Background Running

The P50 must keep openPTT running in the background:

1. Go to **Settings > Battery > Battery Optimization**
2. Find **openPTT TRX** in the list
3. Set to **Don't optimize** (allows background running)
4. Go to **Settings > Apps > openPTT TRX > Battery**
5. Enable **Allow background activity**
6. Grant **Location** permission (Allow all the time is recommended for dispatch)

## Step 4: Configure openPTT TRX

### Option A: QR Code

1. Open openPTT TRX on the P50
2. Open the PTT Admin dashboard in a browser
3. Navigate to **Users** tab
4. Click **QR** next to the user's name
5. Scan the QR code with the P50's camera — the server connection is auto-configured

### Option B: Manual Configuration

1. Open openPTT TRX on the P50
2. Tap **Add Server** (or the + icon)
3. Enter:
   - **Label**: openPTT
   - **Address**: `voice.harro.ch`
   - **Port**: `443` (SNI-routed through Nginx)
   - **Username**: the username created in the admin dashboard
   - **Password**: the password set during user creation
4. Tap **Save** and **Connect**

### Configure Traccar GPS reporting

1. Open **Settings > General > GPS tracking**
2. Ensure **Report GPS to Traccar** is enabled
3. Set **Traccar server URL** to `http://voice.harro.ch:5055` (OsmAnd protocol endpoint)
4. Grant location permission when prompted on first connect

The app uses the **Mumble username as the Traccar device uniqueId** — the
server auto-creates the Traccar device when the user is created, so no
separate device provisioning is required. GPS reports start when the app
connects to the voice server and stop on disconnect.

## Step 5: Test PTT Button

1. Connect to the server
2. Press the hardware PTT button on the side of the P50
3. Speak into the microphone
4. Release the PTT button — a roger beep plays and is transmitted
5. Verify audio is received on another connected device

The hardware PTT button is handled via the Meig broadcast
(`com.meigsmart.meigkeyaccessibility.onkeyevent`), declared in the app's
manifest — no talkKey configuration is needed.

## Step 6: Verify GPS reporting

1. With the app connected, open the admin dashboard map
2. Within ~60 seconds, the device should appear at its current location
3. Walk ~100m to verify track points update (30s or 50m movement threshold)
4. Battery percentage should also be visible on the device detail

If the device does not appear:

- Check that location permission is granted (**Settings > Apps > openPTT TRX > Permissions > Location > Allow all the time**)
- Confirm the Traccar URL is set correctly in openPTT Settings
- Confirm the Traccar device exists in the server's Traccar admin UI with `uniqueId` = the mumble username
- Check device GPS fix: step outside, wait for first fix (cold starts can take 30–60s)

## Step 7: Lock to PTT App (Optional)

To make the P50 boot directly into openPTT TRX:

1. Go to **Settings > Security > Screen Pinning**
2. Enable screen pinning
3. Open openPTT TRX
4. In the recent apps view, tap the pin icon on openPTT TRX

## Troubleshooting

### PTT button doesn't work
- Force-stop and reopen the app (`adb shell am force-stop ch.harro.openptt`)
- Confirm the app is connected to the server
- Check logs: `adb logcat | grep MeigPtt`

### Audio is choppy
- Check WiFi/cellular signal strength
- Move closer to a WiFi access point
- Check if other apps are using bandwidth

### Connection drops
- Verify battery optimization is disabled for openPTT TRX
- Check that the server address and port are correct
- Verify the server is running: try connecting from another device

### GPS not updating in dashboard
- Check app is connected to the voice server (GPS only reports while connected)
- Confirm **Traccar server URL** in Settings is `http://voice.harro.ch:5055`
- Look for `LocationReporter` lines in `adb logcat` — they show each POST attempt
- Verify the Traccar device `uniqueId` matches the mumble username exactly

### Can't install via adb
- Ensure USB debugging is enabled
- Try a different USB cable
- Run `adb kill-server && adb start-server` and retry

## Existing users migration (one-time)

Previously-registered Traccar devices use random numeric uniqueIds (e.g.
`245195` for harro, `372194` for yuliia). After deploying the unified app,
run the migration endpoint for each existing user to change the Traccar
device's uniqueId to match the mumble username.

The Traccar web UI on port 8082 is not publicly reachable by design — the
migration uses the already-HTTPS'd admin API at `ptt.harro.ch` instead.

```bash
# 1. Get an admin JWT
TOKEN=$(curl -sk -X POST https://ptt.harro.ch/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<admin>","password":"<admin_password>"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

# 2. List users to find their IDs
curl -sk https://ptt.harro.ch/api/users -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool

# 3. Migrate each user (repeat per user_id)
curl -sk -X POST https://ptt.harro.ch/api/users/<user_id>/migrate-traccar-uniqueid \
  -H "Authorization: Bearer $TOKEN"
```

The endpoint is idempotent: if the user already has a Traccar device linked, it
updates the uniqueId in-place (preserving GPS history). If not, it creates one
with `uniqueId = username`. The server-side `traccar_device_id` (Traccar's
internal PK, e.g. 4, 8) is unchanged — only the uniqueId is updated.

After migration, the openPTT app's POSTs (`id=<username>`) will be accepted by
Traccar and positions + battery will appear in the admin dashboard.

# Hytera P50 Setup Guide

## Prerequisites

- Hytera P50 device
- USB cable for the P50
- Computer with adb installed (Android Debug Bridge)
- HamMumble APK file

## Step 1: Enable Developer Mode on P50

1. Go to **Settings > About Phone**
2. Tap **Build Number** 7 times to enable Developer Options
3. Go to **Settings > Developer Options**
4. Enable **USB Debugging**

## Step 2: Install HamMumble

Connect P50 to computer via USB.

```bash
# Verify device is connected
adb devices

# Install HamMumble
adb install HamMumble.apk
```

## Step 3: Configure Background Running

The P50 must keep HamMumble running in the background:

1. Go to **Settings > Battery > Battery Optimization**
2. Find HamMumble in the list
3. Set to **Don't optimize** (allows background running)
4. Go to **Settings > Apps > HamMumble > Battery**
5. Enable **Allow background activity**

## Step 4: Configure HamMumble

### Option A: QR Code (if supported)

1. Open HamMumble on the P50
2. Open the PTT Admin dashboard in a browser
3. Navigate to **Users** tab
4. Click **QR** next to the user's name
5. Scan the QR code with the P50's camera
6. HamMumble auto-configures the connection

### Option B: Manual Configuration

1. Open HamMumble on the P50
2. Tap **Add Server** (or the + icon)
3. Enter:
   - **Label**: Your server name
   - **Address**: `voice.harro.ch` (the voice subdomain)
   - **Port**: `443` (uses SNI routing through Nginx)
   - **Username**: The username created in the admin dashboard
   - **Password**: The password set during user creation
4. Tap **Save** and then **Connect**

## Step 5: Test PTT Button

1. Connect to the server via HamMumble
2. Press the hardware PTT button on the side of the P50
3. Speak into the microphone
4. Release the PTT button
5. Verify audio is received on another connected device

The PTT button should trigger transmission immediately. If it doesn't:
- Check HamMumble settings for PTT button configuration
- Ensure HamMumble has microphone permissions
- Try restarting HamMumble

## Step 6: Lock to PTT App (Optional)

To make the P50 boot directly into HamMumble:

1. Go to **Settings > Security > Screen Pinning**
2. Enable screen pinning
3. Open HamMumble
4. In the recent apps view, tap the pin icon on HamMumble

## Troubleshooting

### PTT button doesn't work
- Ensure HamMumble is the active (foreground) app
- Check HamMumble settings for hardware button configuration
- Restart the app and try again

### Audio is choppy
- Check WiFi/cellular signal strength
- Move closer to a WiFi access point
- Check if other apps are using bandwidth

### Connection drops
- Verify battery optimization is disabled for HamMumble
- Check that the server address and port are correct
- Verify the server is running: try connecting from another device

### Can't install via adb
- Ensure USB debugging is enabled
- Try a different USB cable
- Run `adb kill-server && adb start-server` and retry

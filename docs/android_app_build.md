# openPTT TRX — Android App Build Plan

## Why Fork HamMumble

The current setup requires two separate apps on each device:
1. **HamMumble** — Mumble voice client (PTT, channels, text chat)
2. **Traccar Client** — GPS tracking in background

This creates friction: two installs, two configs, two apps to keep running, two battery optimization exclusions. A single unified app solves all of this.

### HamMumble Assessment

- **Repo:** https://github.com/MichTronics76/HamMumble
- **License:** GPL-3.0 (fork-friendly, must keep open source)
- **Language:** Kotlin 1.8 + Jetpack Compose + Material 3
- **Min Android:** 8.0 (API 26)
- **Not a fork** of Plumble or Mumla — independent implementation
- **Target hardware:** Android devices including cheap Android TV boxes, Hytera P50, any Android with PTT

### What HamMumble Already Does
- Mumble protocol (Opus, CELT, Speex codecs)
- Push-to-Talk, Voice Activity Detection, continuous TX modes
- Channel navigation and switching (hierarchical)
- Channel text chat with timestamps
- Roger beep system (customizable tone 300-3000Hz)
- USB audio device support (SignaLink, RigBlaster, CM108/CM119)
- Serial PTT via USB adapters (DTR/RTS)
- Client certificate (PKCS#12) + auto-registration
- Multi-server management, auto-connect, auto-reconnect
- Real-time latency monitoring, VU meters, input gain control
- Mute/deafen controls

### What HamMumble Does NOT Do
- Private 1-to-1 calling (no whisper, no user-to-user direct audio)
- Private messaging between users
- GPS reporting
- SOS/panic button
- Lone worker check-in
- Dispatch notification display
- Server API integration

## Fork Feature Plan

### Phase 1: Core Integration (Week 1-2)

**1. Built-in GPS Reporting**
- Replace the need for Traccar Client entirely
- Background location service reporting to server using OsmAnd protocol (port 5055)
- Configurable interval (default: 30 seconds)
- Battery-efficient (fused location provider)
- Auto-start on boot

**2. SOS / Panic Button**
- Prominent red SOS button on main screen (or long-press hardware PTT)
- Triggers `POST /api/sos/trigger` with GPS coordinates + device token
- Visual + haptic feedback on activation
- Works even when app is in background (hardware button trigger)

**3. Lone Worker Check-In**
- "Check In" button on main screen
- Calls `POST /api/loneworker/checkin` with username
- Shows check-in status: OK / Due Soon / Overdue
- Optional: auto check-in on PTT press (counts as activity)

### Phase 2: Communication Features (Week 2-3)

**4. Private 1-to-1 Calling**
- "Users" tab showing all online users on the server
- "Call" button per user
- Flow:
  1. App calls `POST /api/calls` with caller + callee usernames
  2. Server creates temporary private channel `_call_Alice_Bob`
  3. Server moves both users into the channel (pymumble `move_in()`)
  4. App shows "In call with Bob" UI state
  5. "End Call" button calls `POST /api/calls/{id}/end`
  6. Server moves both users back to original channels, deletes temp channel
- Incoming call: detect being moved to a `_call_*` channel, show incoming call UI
- Auto-timeout: server ends call after 30 minutes if not manually ended

**5. Dispatch Notifications**
- Monitor incoming Mumble text messages for `DISPATCH:` prefix
- Parse dispatch message and show as prominent notification/alert
- Show on lock screen with sound/vibration
- "Navigate" button that opens GPS coordinates in maps app
- Dispatch history view

### Phase 3: UX Polish (Week 3-4)

**6. QR Code Enrollment**
- Scan QR from dashboard to auto-configure:
  - Mumble server address + port
  - Username + password
  - GPS reporting server
  - All in one scan
- Current QR format: `mumble://username:password@voice.harro.ch:443/`
- Extend to include GPS config: custom `openptt://` URI scheme or query params

**7. Unified Settings**
- Single settings screen for voice + GPS + safety
- Server connection (auto-filled from QR)
- GPS interval
- Lone worker mode toggle
- SOS button configuration
- Audio device selection (keep HamMumble's USB audio support)

**8. Branding**
- Rename to "openPTT TRX"
- New app icon (antenna + signal waves, matching server logo)
- Material 3 theming with openPTT color scheme

## Technical Architecture

```
openPTT TRX App
├── Mumble Layer (existing HamMumble code)
│   ├── Voice engine (Opus/CELT/Speex)
│   ├── Channel management
│   ├── Text messaging
│   └── Certificate management
├── GPS Layer (new)
│   ├── FusedLocationProvider
│   ├── OsmAnd protocol reporter (HTTP to port 5055)
│   └── Background service with foreground notification
├── Safety Layer (new)
│   ├── SOS trigger (REST API call)
│   ├── Lone worker check-in (REST API call)
│   └── Hardware button binding
├── Communication Layer (new)
│   ├── Private call initiation (REST API)
│   ├── Call state detection (channel move detection)
│   └── Dispatch notification parser
└── Config Layer (new)
    ├── QR code scanner + parser
    ├── Unified settings
    └── Server API client (auth, endpoints)
```

## Server-Side Changes Needed

The server already has most of the APIs. Additional work:

1. **Private Call API** — `POST /api/calls`, `POST /api/calls/{id}/end`, `GET /api/calls/active`
   - Creates temp Mumble channel, moves users, tracks state
   - Was prototyped but removed (needs to be rebuilt when app is ready)

2. **Device Auth Token** — devices need a lightweight auth mechanism for API calls
   - Option A: Use Mumble credentials as bearer token (simple)
   - Option B: Separate device JWT issued at QR enrollment time
   - The SOS endpoint already uses `X-SOS-Token`, could extend this pattern

3. **Push Notifications** — for incoming calls and dispatch when app is backgrounded
   - Firebase Cloud Messaging (FCM) or UnifiedPush (self-hosted, no Google dependency)
   - Server sends push when: incoming call, dispatch assigned, SOS triggered, check-in overdue

## Build & Distribution

- **Build system:** Gradle (existing HamMumble setup)
- **Distribution:** APK sideload via `adb install` (P50 doesn't have Play Store)
- **Update mechanism:** Simple version check against server (`GET /api/app/version`)
- **CI:** GitHub Actions — build APK on push, attach to release

## Dependencies

- HamMumble source (GPL-3.0) — the voice/protocol foundation
- Google Play Services Location (for FusedLocationProvider) OR pure Android LocationManager
- OkHttp or Ktor for REST API calls
- CameraX or ZXing for QR scanning
- Material 3 components (already in HamMumble)

## Risks

1. **HamMumble code quality unknown** — need to clone and assess before committing to fork
2. **Mumble protocol internals** — private calling depends on detecting server-initiated channel moves; need to verify HamMumble exposes these events
3. **Battery life** — GPS + Mumble + background services on P50 battery; needs real-world testing
4. **P50 hardware button mapping** — SOS/PTT hardware buttons may need AccessibilityService or vendor-specific APIs
5. **GPL-3.0 obligations** — all modifications must be open source, fork must include license

## Timeline Estimate

| Phase | Scope | Effort |
|-------|-------|--------|
| Phase 1 | GPS + SOS + Check-in | 1-2 weeks |
| Phase 2 | Private calling + Dispatch | 1-2 weeks |
| Phase 3 | QR enrollment + Polish | 1 week |
| Testing | Real P50 device testing | 1 week |
| **Total** | | **4-6 weeks** |

This assumes one Kotlin/Android developer familiar with the Mumble protocol. The server-side work (private call API, push notifications) is ~2-3 days additional.

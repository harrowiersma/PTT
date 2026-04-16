# openPTT TRX — Open Issues

Last updated: 2026-04-16

---

## App Customization (openPTT TRX Android App)

Source: `/Users/harrowiersma/Documents/CLAUDE/openPTT-app`
Plan: `/Users/harrowiersma/.claude/plans/floating-purring-peach.md`
Build: `JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home ANDROID_HOME=~/Library/Android/sdk ./gradlew assembleFossDebug`
APK output: `app/build/outputs/apk/foss/debug/openptt-foss-debug.apk`

### 1. Wire up custom sounds
Sound files are in `app/src/main/res/raw/` (connect.wav, channel_change.wav, roger_beep.wav — converted from user-provided MP3s). Need to create a SoundManager, hook into connect/channel-change callbacks in MumlaService, and silence the default Android notification sound.

### 2. Transmitted roger beep
Roger beep must be **transmitted into the Mumble channel** (not played locally) so all listeners hear end-of-transmission. Inject beep audio into the Humla audio output buffer just before PTT release. Add preference toggle.
- Files: MumlaService.java (onTalkKeyUp, onUserTalkStateUpdated), Settings.java, settings_audio.xml

### 3. Remove donate option
Delete the donate footer from the navigation drawer. It's in MumlaActivity.java lines 307-319, the `if (BuildConfig.FLAVOR.equals("foss"))` block.

### 4. Small screen optimization (240x320 P50)
- Reduce PTT button height from 150dp to 60dp
- Hide ViewPager tab strip (CHANNEL | CHAT) to save vertical space
- Reduce font sizes in channel list
- Create `res/values-small/dimens.xml` with P50 dimensions
- Files: Settings.java, fragment_channel.xml, ChannelListAdapter.java

### 5. Channel knob support
P50 rotary knob generates KEYCODE_F5 (135, counter-clockwise) and KEYCODE_F6 (136, clockwise). Intercept in MumlaActivity.onKeyDown(), maintain sorted channel list, join next/previous channel on turn.
- Files: MumlaActivity.java, MumlaService.java

### 6. Harro's P50 setup
Only yuliia's P50 has openPTT TRX installed. Harro's P50 needs:
- Install openPTT TRX APK via ADB
- Delete harro's old Murmur registration (certificate-bound to HamMumble)
- Configure server: voice.harro.ch:443, username harro
- Map PTT button and set preferences

---

## Server — GPS & Traccar

### 7. Traccar device auto-assignment (permanent fix)
Devices created via `registerUnknown=true` aren't assigned to the admin Traccar user, making their positions invisible to the API. **Hotfix applied** (manually assigned via permissions API) but needs permanent code fix.
- Add `_assign_device_to_admin()` in `server/traccar_client.py`
- Call it in `create_device()` and on startup for orphaned devices
- File: `server/traccar_client.py`, `server/main.py` (lifespan startup)

### 8. Traccar session caching
`TraccarClient` creates a new `httpx.AsyncClient` and re-authenticates on every single API call. Logs show POST `/api/session` every 10 seconds. Should reuse session.
- File: `server/traccar_client.py`

### 9. GPS name matching in weather bot
`server/weather_bot.py` line 342 still uses `p.device_name.lower() == username.lower()` instead of the `traccar_device_id` link from the User model. Same bug that was fixed in status.py and dispatch.py.
- File: `server/weather_bot.py` lines 338-345

---

## Server — Weather Bot

### 10. Weather bot not triggering (PTTAdmin in wrong channel)
The `PYMUMBLE_CLBK_SOUNDRECEIVED` callback only fires for audio in the bot's own channel. PTTAdmin sits in Root (channel 0), so it never hears audio in the Weather channel. Double-PTT detection never fires.
- **Fix:** Move bot to Weather channel on startup: `mm.users.myself.move_in(self._weather_channel_id)`
- **Problem:** Bot can then only hear one channel. SOS acknowledgements happen in Emergency.
- **Proper fix:** Create a second pymumble connection dedicated to weather bot
- File: `server/weather_bot.py` line 231-236

### 11. TinyTTS untested
Text-to-speech via TinyTTS has never been verified working in the Docker container. It pulls PyTorch (~500MB). Audio generation + playback via pymumble `sound_output.add_sound()` is unverified.
- Files: `server/weather_bot.py` (text_to_audio_pcm, _play_audio)

### 12. `_ensure_db_channel` coroutine warning
Startup logs show: `RuntimeWarning: coroutine 'WeatherBot._ensure_db_channel.<locals>._create' was never awaited`. The async/sync bridge in weather_bot.py is broken — `loop.run_until_complete()` is failing silently.
- File: `server/weather_bot.py` lines 238-260

---

## Server — Dispatch

### 13. Dispatch TTS (speak to target user only)
Dispatch currently sends a text message to the user's channel. Should use TinyTTS to generate spoken audio and pymumble `set_whisper(session_id)` to deliver it only to the target user.
```python
mm.sound_output.set_whisper(target_session_id, channel=False)
# play TTS audio
mm.sound_output.remove_whisper()
```
- Falls back to text message if TTS fails
- Files: `server/api/dispatch.py`, `server/murmur/client.py`

---

## Server — Lone Worker

### 14. Lone worker shift system
Currently if `is_lone_worker` is enabled, check-in reminders run 24/7. Needs to be shift-based:
- Activate when user connects to Mumble, deactivate on disconnect
- Max shift duration safety net (auto-end after configurable hours)
- For the future app fork: dedicated shift start/end button
- Files: `server/api/loneworker.py`, `server/main.py`

---

## Server — SOS / Traccar Webhook

### 15. Traccar event webhook 405
Logs show repeated `POST /api/traccar/event HTTP/1.1 405 Method Not Allowed`. The SOS webhook from Traccar to the admin service isn't working — the endpoint exists but the HTTP method doesn't match.
- File: `server/api/sos.py` (check the route decorator — might need `@router.post` vs what Traccar sends)
- Config: `traccar.xml` has `event.forward.url=http://admin:8000/api/traccar/event`

---

## Server — Infrastructure

### 16. Murmur user registration doesn't work
`register_user()` in `murmur/client.py` is a no-op ("pymumble doesn't support server-side user registration"). Users auto-register on first connect. Certificate changes (switching apps) require manually deleting the user from Murmur's SQLite: `sqlite3 /data/mumble-server.sqlite "DELETE FROM users WHERE name='username';"` then restarting Murmur.
- File: `server/murmur/client.py` lines 224-232

### 17. Database migrations (Alembic)
Still using `create_all()`. New columns require manual `ALTER TABLE` on the VPS. Alembic was in the enterprise plan but never set up.
- Files: `server/database.py`, new `server/alembic/` directory

### 18. AAAA DNS record for voice.harro.ch
The IPv6 AAAA record caused P50 connection failures on WiFi (IPv6 not routed). Unclear if it was removed from ClouDNS. Should be verified/removed to prevent issues when SIM cards arrive tomorrow.

---

## Device Configuration

### 19. Traccar Client app configuration
Both P50 devices need Traccar Client configured and verified:
- harro: uid 245195, server ptt.harro.ch:5055
- yuliia: uid 372194, server ptt.harro.ch:5055
- Verify GPS data flows to dashboard after SIM cards arrive

---

## Priority for Next Session

**Must do (blocks testing tomorrow):**
- #6 — Install openPTT TRX on harro's P50
- #18 — Verify AAAA DNS record removed
- #19 — Verify Traccar Client on both P50s

**Should do (improves the product):**
- #1-5 — App customization (sounds, roger beep, donate, small screen, knob)
- #10 — Weather bot channel fix
- #13 — Dispatch TTS whisper

**Can wait:**
- #7-9 — Traccar code fixes (hotfix in place)
- #11-12 — TinyTTS verification
- #14-17 — Infrastructure improvements

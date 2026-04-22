# Hide Offline-Status Users from P50 Channel List — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Hide users marked Offline from every other P50's channel user list within 20 seconds, while leaving them visible to themselves and on the dashboard.

**Architecture:** New `GET /api/users/presence-map` endpoint exposes the whole presence picture in one call. App holds a `PresenceCache` populated every 20 s by `MumlaService` (immediate refresh on connect and after each `postStatus`). `PresenceFilter.isHidden(user, cache, selfName)` mirrors `BotUsers.isBot` and is wired into the four channel-list call sites: `UserRowAdapter.submit`, `UserRowAdapter.onBindViewHolder` (Busy badge), `ChannelCardFragment` member count, `ChannelCarouselFragment` user-band count. A listener interface lets the cache trigger UI redraws when the map changes.

**Tech Stack:** FastAPI + SQLAlchemy (server endpoint), Java + Android (app), no DB migration.

**Companion design doc:** `docs/plans/2026-04-22-hide-offline-from-channel-list-design.md`

---

## Pre-flight — confirm sidecars are still up

```bash
docker ps --format "{{.Names}}" | grep -E "ptt-pytest|ptt-admin"
```

Expected: both `ptt-pytest` and `ptt-admin-1` listed. If `ptt-pytest` is missing, bring it back per yesterday's plan (Pre-flight section of `2026-04-21-user-status-presence.md`).

Verify baseline green before Task 1:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 109 passed (yesterday's end-state).

---

## Phases

1. **Task 1** — `GET /api/users/presence-map` server endpoint + tests.
2. **Task 2** — `PresenceCache` (app-side state holder, 20 s polling lifecycle).
3. **Task 3** — `PresenceFilter` static helpers + `PresenceFilterTest` JUnit.
4. **Task 4** — Wire `PresenceFilter` into `UserRowAdapter.submit` (drop offline rows).
5. **Task 5** — Wire `PresenceFilter.countVisible` into `ChannelCardFragment` + `ChannelCarouselFragment` (member-count badges).
6. **Task 6** — Add Busy amber badge in `UserRowAdapter.onBindViewHolder`.
7. **Task 7** — Wire `PresenceCache` listener → fragment redraw trigger.
8. **Task 8** — Build, install on both P50s (in-place upgrade), deploy server, manual smoke.

Each task is independently committable. Hard-stop after Task 8 to confirm prod is healthy and both P50s reflect the new behaviour before considering the feature shipped.

---

## Task 1: `GET /api/users/presence-map` endpoint + tests

**Files:**
- Modify: `server/api/user_status.py` (append the new route + response model)
- Test: `tests/test_presence_map_api.py`

**Step 1: Write the failing test**

Create `tests/test_presence_map_api.py`:

```python
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from server.models import User


async def _seed(db_session, *triples):
    """triples: (username, status_label, is_audible)."""
    for username, label, audible in triples:
        db_session.add(User(
            username=username, mumble_password="x",
            status_label=label, is_audible=audible,
        ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_presence_map_returns_keyed_dict(client: AsyncClient, db_session):
    await _seed(db_session,
        ("harro", "online", True),
        ("yuliia", "busy", False),
        ("alex", "offline", None),
    )
    r = await client.get("/api/users/presence-map")
    assert r.status_code == 200
    body = r.json()
    assert body["harro"] == {"status_label": "online", "is_audible": True}
    assert body["yuliia"] == {"status_label": "busy", "is_audible": False}
    assert body["alex"] == {"status_label": "offline", "is_audible": None}


@pytest.mark.asyncio
async def test_presence_map_excludes_bots(client: AsyncClient, db_session):
    await _seed(db_session,
        ("PTTAdmin", "online", True),
        ("PTTWeather", "online", True),
        ("PTTPhone-1", "online", True),
        ("PTTPhone-2", "online", True),
        ("real_user", "online", True),
    )
    r = await client.get("/api/users/presence-map")
    assert r.status_code == 200
    body = r.json()
    assert "real_user" in body
    for bot in ("PTTAdmin", "PTTWeather", "PTTPhone-1", "PTTPhone-2"):
        assert bot not in body, f"bot {bot} leaked into presence map"


@pytest.mark.asyncio
async def test_presence_map_no_auth_required(client: AsyncClient, db_session):
    """Anonymous request returns 200 — matches /api/users/status convention."""
    await _seed(db_session, ("solo", "online", True))
    r = await client.get("/api/users/presence-map")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_presence_map_handles_null_status(client: AsyncClient, db_session):
    """A user that never set status surfaces as status_label=null."""
    await _seed(db_session, ("fresh", None, None))
    r = await client.get("/api/users/presence-map")
    body = r.json()
    assert body["fresh"] == {"status_label": None, "is_audible": None}


@pytest.mark.asyncio
async def test_presence_map_lowercases_keys(client: AsyncClient, db_session):
    """Keys are lowercased so the app's case-insensitive lookup works."""
    await _seed(db_session, ("MixedCase", "busy", True))
    r = await client.get("/api/users/presence-map")
    body = r.json()
    assert "mixedcase" in body
    assert "MixedCase" not in body
```

**Step 2: Run test, expect fail**

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_presence_map_api.py -v"
```

Expected: 404 on every test — the route doesn't exist.

**Step 3: Implement the route**

In `server/api/user_status.py`, append below the existing `patch_status` route:

```python
class PresenceEntry(BaseModel):
    status_label: str | None
    is_audible: bool | None


# Lowercased usernames mirroring server/murmur/client.py::BOT_USERNAMES.
# Kept literal here (not imported) so the endpoint doesn't tug the murmur
# module into its dependency graph for one tuple.
_BOT_USERNAMES = {"PTTAdmin", "PTTWeather", "PTTPhone"}


def _is_bot_username(name: str) -> bool:
    """Mirror of MurmurClient._is_bot_username — keep both in sync."""
    if name in _BOT_USERNAMES:
        return True
    return name.startswith("PTTPhone-")


@router.get("/presence-map", response_model=dict[str, PresenceEntry])
async def get_presence_map(db: AsyncSession = Depends(get_db)):
    """Return every (non-bot) user's presence in one shot.

    The radio app polls this every 20 s to decide who to hide from its
    channel user list. Lowercased keys for case-insensitive lookup. No
    auth — matches the GET /status convention; the data leaked is the
    same shape any logged-in user would see.
    """
    rows = (await db.execute(select(User))).scalars().all()
    return {
        u.username.lower(): PresenceEntry(
            status_label=u.status_label,
            is_audible=u.is_audible,
        )
        for u in rows
        if not _is_bot_username(u.username)
    }
```

**Step 4: Run tests**

```bash
docker compose up -d --build admin
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/test_presence_map_api.py -v"
```

Expected: 5 passed.

Full suite for regressions:

```bash
docker exec ptt-pytest sh -c "rm -f test.db && \
  python -m pytest tests/ --ignore=tests/sip_bridge -q"
```

Expected: 114 passed (109 baseline + 5 new).

**Step 5: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git add server/api/user_status.py tests/test_presence_map_api.py
git commit -m "$(cat <<'EOF'
status: GET /api/users/presence-map for app-side channel-list filter

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `PresenceCache` (app-side state holder + 20 s polling lifecycle)

**Files:**
- Create: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/PresenceCache.java`
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/service/MumlaService.java`
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/service/IMumlaService.java`

**Step 1: Create `PresenceCache.java`**

```java
/*
 * Caches the server-side presence map (status_label per username)
 * for the channel-list filter. Polled every 20 s by MumlaService while
 * Mumble is connected; refreshed immediately on connect and after each
 * successful postStatus. Listeners get a callback when the map content
 * actually changes (cheap ref-equals + Map.equals fast-path).
 *
 * All fields are volatile / read on the UI thread; writes happen on a
 * worker thread inside refresh(). Keep the read API allocation-free.
 */
package se.lublin.mumla.channel;

import android.util.Log;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

public class PresenceCache {

    private static final String TAG = "PresenceCache";
    private static final int CONNECT_TIMEOUT_MS = 4000;
    private static final int READ_TIMEOUT_MS = 6000;

    public interface Listener {
        /** Called on a worker thread whenever the cached map content
         *  changes. Implementations should bounce to the UI thread. */
        void onPresenceMapUpdated();
    }

    private final String mAdminUrl;
    private volatile Map<String, String> mStatusByLcUsername = Collections.emptyMap();
    private final Set<Listener> mListeners =
            Collections.synchronizedSet(new LinkedHashSet<>());

    public PresenceCache(String adminUrl) {
        mAdminUrl = adminUrl;
    }

    /** Returns the cached status_label for a user, lowercased lookup.
     *  null if the user is unknown to the cache (treat as visible). */
    public String getStatus(String username) {
        if (username == null) return null;
        return mStatusByLcUsername.get(username.toLowerCase());
    }

    public void addListener(Listener l) { if (l != null) mListeners.add(l); }
    public void removeListener(Listener l) { mListeners.remove(l); }

    /** Background HTTP fetch + cache swap. Safe to call concurrently;
     *  the worst case is two parallel fetches racing to install their
     *  result, both correct. */
    public void refresh() {
        if (mAdminUrl == null || mAdminUrl.isEmpty()) return;
        new Thread(() -> {
            HttpURLConnection conn = null;
            try {
                conn = (HttpURLConnection) new URL(
                        mAdminUrl + "/api/users/presence-map").openConnection();
                conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
                conn.setReadTimeout(READ_TIMEOUT_MS);
                if (conn.getResponseCode() != 200) return;

                InputStream is = conn.getInputStream();
                ByteArrayOutputStream buf = new ByteArrayOutputStream();
                byte[] tmp = new byte[1024];
                int n;
                while ((n = is.read(tmp)) > 0) buf.write(tmp, 0, n);
                JSONObject root = new JSONObject(buf.toString("UTF-8"));

                Map<String, String> next = new HashMap<>(root.length());
                for (java.util.Iterator<String> it = root.keys(); it.hasNext();) {
                    String k = it.next();
                    JSONObject entry = root.optJSONObject(k);
                    if (entry == null) continue;
                    String label = entry.isNull("status_label")
                            ? null : entry.optString("status_label", null);
                    next.put(k, label);  // keys are already lowercase from the server
                }

                Map<String, String> prev = mStatusByLcUsername;
                if (!next.equals(prev)) {
                    mStatusByLcUsername = Collections.unmodifiableMap(next);
                    notifyListeners();
                }
            } catch (Exception e) {
                Log.w(TAG, "presence-map refresh failed: " + e);
            } finally {
                if (conn != null) conn.disconnect();
            }
        }, "presence-refresh").start();
    }

    private void notifyListeners() {
        // Snapshot to avoid CME if a listener removes itself in the callback.
        Listener[] snap;
        synchronized (mListeners) {
            snap = mListeners.toArray(new Listener[0]);
        }
        for (Listener l : snap) {
            try { l.onPresenceMapUpdated(); }
            catch (Exception e) { Log.w(TAG, "listener threw: " + e); }
        }
    }
}
```

**Step 2: Wire `PresenceCache` into `MumlaService`**

In `MumlaService.java`:

(a) Add field declarations (near `private TextToSpeech mTTS;`):

```java
private PresenceCache mPresenceCache;
private java.util.concurrent.ScheduledExecutorService mPresenceScheduler;
private static final int PRESENCE_POLL_SECONDS = 20;
```

(b) In `onCreate()` (after `mSettings = Settings.getInstance(this);`), add:

```java
mPresenceCache = new PresenceCache(mSettings.getAdminUrl());
```

(c) In the `HumlaService.Observer.onConnected()` body (line 160), after the existing `fetchStatus(...)` call, append:

```java
            // Prime the channel-list presence filter and start polling.
            startPresencePolling();
```

(d) In `onDisconnected(HumlaException e)` (line 213), append:

```java
            stopPresencePolling();
```

(e) Add the lifecycle helpers near the existing HTTP helpers (around the `fetchStatus` neighbourhood):

```java
private synchronized void startPresencePolling() {
    if (mPresenceCache == null) return;
    stopPresencePolling();
    mPresenceCache.refresh();  // immediate first fetch
    mPresenceScheduler = java.util.concurrent.Executors.newSingleThreadScheduledExecutor(
            r -> {
                Thread t = new Thread(r, "presence-poll");
                t.setDaemon(true);
                return t;
            });
    mPresenceScheduler.scheduleAtFixedRate(
            mPresenceCache::refresh,
            PRESENCE_POLL_SECONDS, PRESENCE_POLL_SECONDS,
            java.util.concurrent.TimeUnit.SECONDS);
}

private synchronized void stopPresencePolling() {
    if (mPresenceScheduler != null) {
        mPresenceScheduler.shutdownNow();
        mPresenceScheduler = null;
    }
}

public PresenceCache getPresenceCache() {
    return mPresenceCache;
}
```

(f) In `postStatus(...)` `onSuccess` branch (the `if (code >= 200 && code < 300)` block inside the worker thread), trigger an immediate refresh after caching the new state:

```java
            if (code >= 200 && code < 300) {
                if (label != null) mCurrentStatus = label;
                if (isAudible != null) mCurrentAudible = isAudible;
                if (mPresenceCache != null) mPresenceCache.refresh();
                if (onSuccess != null) onSuccess.run();
            }
```

**Step 3: Add `getPresenceCache()` to `IMumlaService`**

In `IMumlaService.java`:

```java
PresenceCache getPresenceCache();
```

Add the matching `import se.lublin.mumla.channel.PresenceCache;` at the top.

**Step 4: Compile-check**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew :app:compileFossDebugJavaWithJavac 2>&1 | tail -10
```

Expected: `BUILD SUCCESSFUL`. No tests run yet — Task 3 adds them.

**Step 5: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/java/se/lublin/mumla/channel/PresenceCache.java \
        app/src/main/java/se/lublin/mumla/service/MumlaService.java \
        app/src/main/java/se/lublin/mumla/service/IMumlaService.java
git commit -m "$(cat <<'EOF'
app: PresenceCache + 20s polling lifecycle in MumlaService

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `PresenceFilter` helpers + JUnit

**Files:**
- Create: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/PresenceFilter.java`
- Create: `openPTT-app/app/src/test/java/se/lublin/mumla/channel/PresenceFilterTest.java`

**Step 1: Write the failing test**

Check whether the test source set already exists:

```bash
ls openPTT-app/app/src/test/java/se/lublin/mumla/ 2>/dev/null || echo "(test source set not yet created)"
```

If absent, that's fine — Gradle picks it up automatically.

Create `openPTT-app/app/src/test/java/se/lublin/mumla/channel/PresenceFilterTest.java`:

```java
package se.lublin.mumla.channel;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Before;
import org.junit.Test;

import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import se.lublin.humla.model.IUser;

public class PresenceFilterTest {

    private PresenceCache mCache;

    @Before
    public void setUp() {
        mCache = new PresenceCache(null /* adminUrl unused — we set state directly */);
    }

    /** Inject a literal map into the cache for assertions; bypasses the
     *  HTTP fetch path because we don't want network in unit tests. */
    private void seed(Map<String, String> map) {
        try {
            java.lang.reflect.Field f =
                    PresenceCache.class.getDeclaredField("mStatusByLcUsername");
            f.setAccessible(true);
            f.set(mCache, java.util.Collections.unmodifiableMap(map));
        } catch (Exception e) { throw new RuntimeException(e); }
    }

    private static IUser fakeUser(String name) {
        IUser u = org.mockito.Mockito.mock(IUser.class);
        org.mockito.Mockito.when(u.getName()).thenReturn(name);
        return u;
    }

    @Test
    public void online_user_is_visible() {
        seed(Map.of("alice", "online"));
        assertFalse(PresenceFilter.isHidden(fakeUser("alice"), mCache, "self"));
    }

    @Test
    public void busy_user_is_visible() {
        seed(Map.of("alice", "busy"));
        assertFalse(PresenceFilter.isHidden(fakeUser("alice"), mCache, "self"));
    }

    @Test
    public void offline_user_is_hidden() {
        seed(Map.of("alice", "offline"));
        assertTrue(PresenceFilter.isHidden(fakeUser("alice"), mCache, "self"));
    }

    @Test
    public void null_status_user_is_visible() {
        Map<String, String> m = new HashMap<>();
        m.put("alice", null);
        seed(m);
        assertFalse(PresenceFilter.isHidden(fakeUser("alice"), mCache, "self"));
    }

    @Test
    public void missing_from_map_user_is_visible() {
        seed(Map.of());
        assertFalse(PresenceFilter.isHidden(fakeUser("alice"), mCache, "self"));
    }

    @Test
    public void self_offline_is_still_visible() {
        seed(Map.of("alice", "offline"));
        assertFalse(PresenceFilter.isHidden(fakeUser("alice"), mCache, "ALICE"));
    }

    @Test
    public void countVisible_drops_offline_keeps_busy() {
        seed(Map.of("a", "online", "b", "busy", "c", "offline"));
        List<IUser> users = Arrays.asList(
                fakeUser("a"), fakeUser("b"), fakeUser("c"));
        assertEquals(2, PresenceFilter.countVisible(users, mCache, "self"));
    }

    @Test
    public void countVisible_keeps_self_even_if_offline() {
        seed(Map.of("a", "offline"));
        List<IUser> users = Arrays.asList(fakeUser("a"));
        assertEquals(1, PresenceFilter.countVisible(users, mCache, "a"));
    }

    @Test
    public void countVisible_null_list_returns_zero() {
        seed(Map.of());
        assertEquals(0, PresenceFilter.countVisible(null, mCache, "self"));
    }
}
```

**Step 2: Confirm Mockito is on the test classpath**

```bash
grep -nE "mockito|testImplementation" openPTT-app/app/build.gradle | head -10
```

If `mockito-core` isn't already a `testImplementation` dependency, add it:

```gradle
dependencies {
    // ...existing...
    testImplementation 'junit:junit:4.13.2'
    testImplementation 'org.mockito:mockito-core:5.7.0'
}
```

**Step 3: Run, expect fail**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew :app:testFossDebugUnitTest 2>&1 | tail -20
```

Expected: compile error — `PresenceFilter` class doesn't exist.

**Step 4: Implement `PresenceFilter`**

Create `openPTT-app/app/src/main/java/se/lublin/mumla/channel/PresenceFilter.java`:

```java
/*
 * Hides Offline-status users from the channel user list. Mirrors
 * BotUsers in shape and intent: pure static helpers, null-safe,
 * called from UserRowAdapter.submit and the channel-card / carousel
 * member counters.
 *
 * "Hidden" means the user marked themself Offline AND they aren't the
 * local device's own user. NULL/unknown status fails open (visible) so
 * a transient cache miss never makes a user vanish unexpectedly.
 */
package se.lublin.mumla.channel;

import java.util.List;

import se.lublin.humla.model.IUser;

public final class PresenceFilter {
    private PresenceFilter() {}

    public static boolean isHidden(IUser user, PresenceCache cache, String selfName) {
        if (user == null || cache == null) return false;
        String name = user.getName();
        if (name == null) return false;
        if (selfName != null && name.equalsIgnoreCase(selfName)) return false;
        return "offline".equals(cache.getStatus(name));
    }

    public static int countVisible(
            List<? extends IUser> users, PresenceCache cache, String selfName) {
        if (users == null) return 0;
        int n = 0;
        for (IUser u : users) {
            if (u == null) continue;
            if (BotUsers.isBot(u)) continue;
            if (isHidden(u, cache, selfName)) continue;
            n++;
        }
        return n;
    }
}
```

**Step 5: Run tests, expect pass**

```bash
./gradlew :app:testFossDebugUnitTest 2>&1 | tail -15
```

Expected: 9 tests pass under `PresenceFilterTest`.

**Step 6: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/java/se/lublin/mumla/channel/PresenceFilter.java \
        app/src/test/java/se/lublin/mumla/channel/PresenceFilterTest.java \
        app/build.gradle
git commit -m "$(cat <<'EOF'
app: PresenceFilter helpers + unit tests (mirrors BotUsers)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Wire `PresenceFilter` into `UserRowAdapter.submit`

**Files:**
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/UserRowAdapter.java`
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/ChannelCarouselFragment.java`

**Step 1: Extend `UserRowAdapter` to accept presence context**

Replace the existing `private final List<IUser> mUsers = new ArrayList<>();` block with:

```java
    private final List<IUser> mUsers = new ArrayList<>();
    private PresenceCache mPresenceCache;
    private String mSelfName;

    /** Fragment calls this once after constructing the adapter, then
     *  re-calls it whenever the connection identity changes. Both args
     *  may be null until the service is bound. */
    public void setPresenceContext(PresenceCache cache, String selfName) {
        mPresenceCache = cache;
        mSelfName = selfName;
    }
```

**Step 2: Drop offline rows in `submit`**

Update the existing `submit(...)` method:

```java
    public void submit(List<? extends IUser> users) {
        mUsers.clear();
        if (users != null) {
            for (IUser u : users) {
                if (u == null || BotUsers.isBot(u)) continue;
                if (PresenceFilter.isHidden(u, mPresenceCache, mSelfName)) continue;
                mUsers.add(u);
            }
        }
        // Stable alphabetical order so the list doesn't jump when the
        // server returns users in a different order after a reconnect.
        Collections.sort(mUsers, (a, b) -> {
            String an = a.getName() == null ? "" : a.getName();
            String bn = b.getName() == null ? "" : b.getName();
            return an.compareToIgnoreCase(bn);
        });
        notifyDataSetChanged();
    }
```

**Step 3: Wire the context in `ChannelCarouselFragment`**

Find the `mUsersAdapter = new UserRowAdapter();` line (around 160). Right after it, add:

```java
        // Channel-list filter: drop Offline users. Service may not be
        // bound yet — set whatever we can now; refreshPresenceContext
        // (Task 7) keeps it in sync as the binding lands.
        if (mService != null) {
            mUsersAdapter.setPresenceContext(
                    mService.getPresenceCache(),
                    safeOwnUsername());
        }
```

Add a small helper near the bottom of the fragment class:

```java
    /** Returns the local user's Mumble username if the service is
     *  bound and connected; null otherwise. */
    private String safeOwnUsername() {
        try {
            if (mService == null || !mService.isConnected()) return null;
            int sid = mService.getSessionId();
            se.lublin.humla.model.IUser self = mService.getSessionUser(sid);
            return self == null ? null : self.getName();
        } catch (Exception e) {
            return null;
        }
    }
```

**Step 4: Compile-check**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew :app:compileFossDebugJavaWithJavac 2>&1 | tail -10
```

Expected: `BUILD SUCCESSFUL`. (`mService.getSessionUser(int)` and `getSessionId()` already exist on `IMumlaService` — verify with `grep -n "getSessionUser\\|getSessionId" openPTT-app/app/src/main/java/se/lublin/mumla/service/IMumlaService.java` if the build complains.)

**Step 5: Re-run unit tests for regressions**

```bash
./gradlew :app:testFossDebugUnitTest 2>&1 | tail -10
```

Expected: 9/9 pass.

**Step 6: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/java/se/lublin/mumla/channel/UserRowAdapter.java \
        app/src/main/java/se/lublin/mumla/channel/ChannelCarouselFragment.java
git commit -m "$(cat <<'EOF'
app: drop offline users from carousel user-row list

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire `countVisible` into the member-count badges

**Files:**
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/ChannelCardFragment.java`
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/ChannelCarouselFragment.java`

**Step 1: ChannelCardFragment — line ~104**

Replace:

```java
                int n = BotUsers.countHumans(channel.getUsers());
```

with:

```java
                PresenceCache cache = mService == null ? null : mService.getPresenceCache();
                String self = safeOwnUsername();
                int n = PresenceFilter.countVisible(channel.getUsers(), cache, self);
```

Add the same `safeOwnUsername()` helper to `ChannelCardFragment` (paste the body from Task 4 step 3). If the fragment already has a similar utility, reuse it instead.

**Step 2: ChannelCarouselFragment — line ~353 (`updateCurrentUsers`)**

Replace:

```java
        int n = BotUsers.countHumans(users);
```

with:

```java
        int n = PresenceFilter.countVisible(
                users, mService == null ? null : mService.getPresenceCache(),
                safeOwnUsername());
```

(`safeOwnUsername()` was added in Task 4.)

**Step 3: Compile-check + unit tests**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew :app:compileFossDebugJavaWithJavac :app:testFossDebugUnitTest 2>&1 | tail -10
```

Expected: `BUILD SUCCESSFUL` + 9/9 unit tests still pass.

**Step 4: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/java/se/lublin/mumla/channel/ChannelCardFragment.java \
        app/src/main/java/se/lublin/mumla/channel/ChannelCarouselFragment.java
git commit -m "$(cat <<'EOF'
app: presence-aware member counts on channel card + carousel

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Busy amber badge in `UserRowAdapter.onBindViewHolder`

**Files:**
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/UserRowAdapter.java`

**Step 1: Add Busy fallback before the existing ONLINE fallback**

In `onBindViewHolder` (lines 99-107), the current trailing `else` paints "ONLINE" green. Insert a Busy branch above it:

```java
        } else if (u.getTalkState() == TalkState.TALKING) {
            h.state.setImageResource(R.drawable.outline_circle_talking_on);
            status = "PTT";
            statusColor = 0xFF00C853;
        } else if (mPresenceCache != null
                   && "busy".equals(mPresenceCache.getStatus(u.getName()))) {
            // Stored presence intent. Self-state and talk-state above
            // still take priority — they're more dynamic.
            h.state.setImageResource(R.drawable.outline_circle_talking_off);
            status = "BUSY";
            statusColor = 0xFFFFBF00;
        } else {
            h.state.setImageResource(R.drawable.outline_circle_talking_off);
            status = "ONLINE";
            statusColor = 0xFF4CAF50;
        }
```

(`mPresenceCache` was added in Task 4 step 1.)

**Step 2: Compile-check + unit tests**

```bash
./gradlew :app:compileFossDebugJavaWithJavac :app:testFossDebugUnitTest 2>&1 | tail -10
```

Expected: `BUILD SUCCESSFUL` + 9/9 unit tests still pass.

**Step 3: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/java/se/lublin/mumla/channel/UserRowAdapter.java
git commit -m "$(cat <<'EOF'
app: amber BUSY badge in channel user-row fallback

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Repaint trigger — `PresenceCache` listener → fragment redraw

Without this, the cache updates every 20 s but the user list only redraws on Mumble events. A remote user toggling Offline would stay visible for minutes if no one talks.

**Files:**
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/ChannelCarouselFragment.java`
- Modify: `openPTT-app/app/src/main/java/se/lublin/mumla/channel/ChannelCardFragment.java`

**Step 1: Carousel — register on bind, unregister on destroy**

Find `onServiceBound()` (or whichever method handles the service binding — grep for `mService = `; in this fragment it's a callback fired when `MumlaService` connects). Add at the end:

```java
        // Refresh whenever the cached presence map changes so a remote
        // user toggling Offline disappears within the next poll cycle
        // even if no Mumble event would otherwise trigger a redraw.
        if (mService != null && mService.getPresenceCache() != null) {
            mUsersAdapter.setPresenceContext(
                    mService.getPresenceCache(), safeOwnUsername());
            mService.getPresenceCache().addListener(mPresenceListener);
        }
```

Add the field + listener implementation near the other private fields:

```java
    private final PresenceCache.Listener mPresenceListener = () -> {
        // Bounce to UI thread; PresenceCache fires on a worker.
        android.os.Handler h = new android.os.Handler(android.os.Looper.getMainLooper());
        h.post(() -> {
            if (mUsersAdapter == null || mService == null) return;
            mUsersAdapter.setPresenceContext(
                    mService.getPresenceCache(), safeOwnUsername());
            refreshCurrent();   // existing helper that re-submits the user list
        });
    };
```

In `onDestroyView()` (or `onPause()`), unregister:

```java
        if (mService != null && mService.getPresenceCache() != null) {
            mService.getPresenceCache().removeListener(mPresenceListener);
        }
```

**Step 2: Channel card — same pattern**

Add an equivalent listener field in `ChannelCardFragment` that re-runs the existing `rebind()` (or whichever method paints the member count). Register on service-bound, unregister on destroy.

**Step 3: Compile-check + unit tests**

```bash
./gradlew :app:compileFossDebugJavaWithJavac :app:testFossDebugUnitTest 2>&1 | tail -10
```

Expected: `BUILD SUCCESSFUL` + 9/9 unit tests still pass.

**Step 4: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/src/main/java/se/lublin/mumla/channel/ChannelCarouselFragment.java \
        app/src/main/java/se/lublin/mumla/channel/ChannelCardFragment.java
git commit -m "$(cat <<'EOF'
app: redraw channel list when presence-map cache changes

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Build, deploy, install, smoke

**Step 1: Push the server commits**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
git push origin main
```

**Step 2: Deploy on prod**

```bash
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch "
    cd /opt/ptt && \
    git pull --ff-only && \
    docker compose up -d --build admin
" 2>&1 | tail -10
```

**Step 3: Smoke the new endpoint**

```bash
curl -s "https://ptt.harro.ch/api/users/presence-map" | python3 -m json.tool | head -20
```

Expected: a JSON object with `harro` + `yuliia` (lowercased) keys, each with `status_label` and `is_audible`. No `PTTAdmin`/`PTTPhone-N`/`PTTWeather` keys.

**Step 4: Push the app commits**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git push origin main
```

**Step 5: Build + install on both P50s (in-place upgrade — keystore matches yesterday's build)**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew clean :app:assembleFossDebug 2>&1 | tail -5

APK=app/build/outputs/apk/foss/debug/openptt-foss-debug.apk
adb -s R259060623 install -r "$APK"
adb -s R259060618 install -r "$APK"
adb -s R259060623 shell "dumpsys package ch.harro.openptt | grep versionName"
adb -s R259060618 shell "dumpsys package ch.harro.openptt | grep versionName"
```

Expected: both report a fresh `versionName` (a `g<sha>-debug` suffix).

**Step 6: End-to-end manual verification**

1. Both P50s connected, both Online → both see each other in their channel's user list.
2. **harro** presses orange → "Busy" → on **yuliia**'s P50, harro's row now shows the **BUSY** amber badge instead of ONLINE green. harro still sees themself with whatever their priority state is (PTT / mute / online — not BUSY since self never overrides own state… actually self DOES see Busy because the badge logic isn't gated by self-name; only the hide rule is. Confirm this in the field — if it surprises you, file a follow-up.).
3. **harro** presses orange again → "Offline" → within 20 s, **yuliia**'s P50 no longer lists harro. **harro** still sees themself in the list.
4. **harro** presses orange again → "Online" → within 20 s, yuliia's P50 lists harro again with the green ONLINE badge.
5. From the dashboard, override **yuliia** to Offline → within 20 s, harro's P50 stops listing yuliia.
6. The carousel page's member-count badge under each channel name reflects the new visible count (e.g. "PHONE — 1 USER" instead of "2 USERS" when one is Offline).

**Step 7: Update `docs/open_issues.md`**

Move "Hide offline-status users from P50 channel user list" from the open list into the Resolved section with today's commit hashes (the open_issues entry was added 2026-04-21 and lives near "Private 1:1 PTT").

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
# Edit docs/open_issues.md per above, then:
git add docs/open_issues.md
git commit -m "$(cat <<'EOF'
docs: hide-offline-from-channel-list resolved (2026-04-22)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

---

## Verification checklist (all phases)

After Task 8:

- [ ] `docker exec ptt-pytest sh -c "rm -f test.db && python -m pytest tests/ --ignore=tests/sip_bridge -q"` → 114 passed.
- [ ] `./gradlew :app:testFossDebugUnitTest` → 9 PresenceFilter tests pass.
- [ ] `curl https://ptt.harro.ch/api/users/presence-map` returns the lowercased keyed dict, bots excluded.
- [ ] Both P50s on the new `versionName`.
- [ ] Operator-side: setting Offline hides the row from every other P50 within 20 s.
- [ ] Operator-side: self always sees own row regardless of own status.
- [ ] Operator-side: Busy users render with the amber BUSY badge.
- [ ] Operator-side: member-count badge under the channel name reflects the visible count.
- [ ] `docs/open_issues.md` updated.

All eight green = feature ships.

---

## Open questions / deferred

Carried over from the design doc:

1. **"Show everyone" toggle** — explicitly deferred. Reopen if operators ask.
2. **Visually distinguishing "hidden because Offline" vs "left the channel"** — out of scope; both render as "not in the list."
3. **Server push instead of polling** — reopen if the fleet grows past ~50 users or if the 20 s lag becomes a complaint.
4. **Filter the dashboard's Live Ops list too** — no. The dashboard is a complete-picture surface; the channel list on the radio is a "who's reachable" surface.

---

## Dependencies + parallelization

- Task 1 (server endpoint) is independent; ship first.
- Tasks 2 → 3 → 4 are sequential (cache → filter helpers → wiring).
- Tasks 5, 6 are independent of each other and of Task 7; can land in any order after Task 4.
- Task 7 (repaint listener) closes the UX gap and should ship before deploy.
- Task 8 is the final deploy gate.

Solo execution: run in order. Estimated 2-3 hours of focused work end-to-end.

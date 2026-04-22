# Deterministic Debug-Signing for openPTT App — Design

**Date:** 2026-04-22
**Scope:** Build-system change in the openPTT-app repo. No behavioural change for end users.
**Branch target:** `main` (openPTT-app).

---

## Goal

Make every `./gradlew assembleFossDebug` build, on every machine, produce an APK with the same signature as the one currently installed on both P50s and served from prod's `/var/openptt/apk/openptt-foss-debug.apk`. End the keystore-mismatch dance that wiped the radios' Mumble creds + admin URL during yesterday's Task 9 install.

---

## Why this isn't release-signing in the conventional sense

A real release-signing setup uses a separate `release.jks` with custodial credentials, ProGuard / R8 minification, and a non-debuggable APK. We deliberately don't need most of that:

- The deployment target is `assembleFossDebug` (matches the prod URL filename `openptt-foss-debug.apk`); `assembleFossRelease` isn't currently wired and produces an unsigned APK.
- The `release` build type is already `debuggable true` (operators rely on `adb shell run-as` for provisioning — see the `release { ... }` comment block in `app/build.gradle`). Switching to a true non-debuggable release would break the provisioning script.
- One developer + (currently) zero CI = the "credentials-management" axis of release-signing has no consumers yet.

What we DO need: a keystore that lives in version control so two laptops produce byte-identical signatures. Debug keystores are widely committed in Android projects exactly for this reason.

---

## Decisions (locked)

1. **Commit `app/debug.keystore` to git** — a copy of the current `~/.android/debug.keystore` on this machine. Migration impact = zero, because that exact keystore already signs every APK currently in the field.
2. **Use the debug keystore's standard credentials** — `storePassword 'android'`, `keyAlias 'androiddebugkey'`, `keyPassword 'android'`. These are the AGP defaults and are widely known; no security loss vs today (where every developer's auto-generated `~/.android/debug.keystore` uses the same defaults).
3. **Wire `signingConfigs.debug` explicitly** in `app/build.gradle`. The existing `if (android.hasProperty("signingConfigs"))` guards in `buildTypes` become functional (they're dead code today).
4. **Don't introduce a separate `release.jks`** — defer until ProGuard / Play-Store / multi-developer needs emerge.
5. **Add `scripts/deploy-apk.sh`** — single command: `./gradlew clean :app:assembleFossDebug` + `scp` + `curl` hash verify. Replaces the ad-hoc shell sequence we ran this morning. Ready for CI to call later.
6. **README "Building" section** — short blurb explaining the keystore choice and how to rotate.

---

## File changes

### `openPTT-app/app/debug.keystore` (new, binary, ~2.6 KB)

Copy of `~/.android/debug.keystore` from this machine. Standard AGP debug keystore.

### `openPTT-app/app/build.gradle`

Add a `signingConfigs { ... }` block at the top of the `android { ... }` block (before `defaultConfig`):

```gradle
    signingConfigs {
        debug {
            storeFile file('debug.keystore')
            storePassword 'android'
            keyAlias 'androiddebugkey'
            keyPassword 'android'
        }
    }
```

The existing `buildTypes.debug { if (android.hasProperty("signingConfigs")) { if (signingConfigs.hasProperty("beta")) { signingConfig = signingConfigs.beta } } }` guard does NOT pick up our new `debug` config (the inner check looks for `beta`). We want the `debug` build type to use `signingConfigs.debug`, but since AGP defaults `buildTypes.debug.signingConfig` to `signingConfigs.debug` automatically when one is defined, no buildTypes change is needed. We will verify this with a build + signature check.

### `openPTT-app/scripts/deploy-apk.sh` (new)

```bash
#!/usr/bin/env bash
# Clean-build the foss-debug APK and ship it to prod's /var/openptt/apk/.
# Future CI calls this exact script.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
APK="${REPO_ROOT}/app/build/outputs/apk/foss/debug/openptt-foss-debug.apk"
PROD_HOST="root@ptt.harro.ch"
PROD_PATH="/var/openptt/apk/openptt-foss-debug.apk"
SSH_KEY="${HOME}/.ssh/id_ed25519_ptt"
PROD_URL="https://ptt.harro.ch/apk/openptt-foss-debug.apk"

export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home

echo "==> Building APK"
( cd "$REPO_ROOT" && ./gradlew clean :app:assembleFossDebug )
LOCAL_HASH=$(md5 -q "$APK")
echo "    local md5: $LOCAL_HASH"

echo "==> Uploading to $PROD_HOST:$PROD_PATH"
scp -i "$SSH_KEY" -q "$APK" "$PROD_HOST:$PROD_PATH"

REMOTE_HASH=$(ssh -i "$SSH_KEY" "$PROD_HOST" "md5sum '$PROD_PATH' | awk '{print \$1}'")
echo "    prod md5:  $REMOTE_HASH"

if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
    echo "!! hash mismatch — upload corrupted?"
    exit 1
fi

echo "==> Done. APK live at $PROD_URL"
```

Make executable (`chmod +x`).

### `openPTT-app/README.md` — add a "Building" section

Short, ~10 lines, explaining the deterministic keystore + how to rotate it if ever needed.

---

## Verification

1. **Reproducibility** — `./gradlew clean :app:assembleFossDebug`, note APK md5. On another machine with a fresh clone, same command → same md5.

2. **Install compatibility** — `adb -s R259060623 install -r app/build/outputs/apk/foss/debug/openptt-foss-debug.apk` on harro's P50 (currently running today's earlier build with the same keystore) → `Success`, no uninstall needed, no SharedPreferences loss.

3. **Deploy script** — `bash scripts/deploy-apk.sh` → APK uploaded, hash matches, `curl https://ptt.harro.ch/apk/openptt-foss-debug.apk | md5` matches local.

---

## Risks + mitigations

- **Anyone with repo access can sign APKs that look like ours.** Mitigation: this is already true today — the well-known AGP debug keystore is the default everywhere. No regression vs status quo. When we want a real release variant for Play Store or for tighter custody, generate a fresh `release.jks` then.
- **Keystore expiration** — AGP debug keystores have a 30-year validity from generation. The current one expires ~2056. Add a follow-up reminder to rotate then 😅.
- **Future migration to `release.jks`** — fully open. Add a `signingConfigs.release { storeFile file('release.jks'); storePassword "$RELEASE_STORE_PASS"... }` block reading from `~/.gradle/gradle.properties`, mark `release.jks` ignored or encrypted-in-repo, wire `buildTypes.release.signingConfig`. Out of scope today.

---

## Out of scope (deferred)

- Real `release.jks` + credentials in `~/.gradle/gradle.properties`.
- ProGuard / R8 minified release variant.
- GitHub Actions to auto-build + deploy on push to main (`scripts/deploy-apk.sh` is ready for CI to call when it lands).

---

## Dependencies + ordering

- One commit, app-side only. No server changes.
- Must precede the next "App rebuild + install on field devices" task (today's #3 True Call Hold) so the rebuild lands cleanly via `install -r` rather than another keystore wipe.

Implementation plan: `docs/plans/2026-04-22-deterministic-debug-signing.md`.

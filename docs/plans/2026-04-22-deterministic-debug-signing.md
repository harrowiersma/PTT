# Deterministic Debug-Signing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `./gradlew assembleFossDebug` produce a byte-identical signature on every machine, matching the APKs already on both P50s and on prod's `/var/openptt/apk/openptt-foss-debug.apk`.

**Architecture:** Commit a copy of the current `~/.android/debug.keystore` to `openPTT-app/app/debug.keystore`. Add an explicit `signingConfigs.debug { ... }` block in `app/build.gradle` so AGP uses the project-local keystore for the `debug` build type. Add a one-shot `scripts/deploy-apk.sh` that builds + scps + verifies hash. Document the choice in the README.

**Tech Stack:** Android Gradle Plugin signing, bash, scp.

**Companion design doc:** `docs/plans/2026-04-22-deterministic-debug-signing-design.md`

---

## Pre-flight — confirm prerequisites

```bash
# 1. JAVA_HOME points at OpenJDK 21
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
java -version 2>&1 | head -1
# Expected: openjdk version "21..."

# 2. Local debug keystore exists (this is what we commit)
ls -la ~/.android/debug.keystore
# Expected: ~2.6 KB file

# 3. SSH key for prod
ls -la ~/.ssh/id_ed25519_ptt
# Expected: file present

# 4. R259060623 plugged in (smoke install -r at the end)
adb devices
# Expected: R259060623 listed as "device"
```

If any of these fail, fix before starting Task 1.

---

## Phases

1. **Task 1** — Commit `app/debug.keystore` + add `signingConfigs.debug` block + verify hash reproducibility on a clean build.
2. **Task 2** — Add `scripts/deploy-apk.sh` + run it end-to-end (build + scp + hash-verify).
3. **Task 3** — Add the README "Building" section + final `install -r` smoke on R259060623.

Each task is independently committable. Hard-stop after Task 3 to confirm the next time anyone touches the app, the install path is friction-free.

---

## Task 1: Commit `debug.keystore` + wire `signingConfigs.debug`

**Files:**
- Create: `openPTT-app/app/debug.keystore` (binary, ~2.6 KB)
- Modify: `openPTT-app/.gitignore` (add an exception for the committed keystore)
- Modify: `openPTT-app/app/build.gradle` (add `signingConfigs { debug { ... } }` block)

**Step 1: Copy the local debug keystore into the project**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
cp ~/.android/debug.keystore app/debug.keystore
ls -la app/debug.keystore
# Expected: ~2.6 KB file
```

**Step 2: Add the `.gitignore` exception**

`openPTT-app/.gitignore` currently has `*.keystore` (line 10). The committed file would be ignored. Add an explicit allow-rule above or below the deny rule.

Open `openPTT-app/.gitignore`, find the line containing `*.keystore`, and append immediately after it:

```
# Project-local debug keystore — committed deliberately so every machine
# produces APKs with the same signature as the ones already on the P50s.
# See docs/plans/2026-04-22-deterministic-debug-signing-design.md.
!app/debug.keystore
```

Verify it's no longer ignored:

```bash
git check-ignore -v app/debug.keystore
# Expected: NO output (file is now tracked-eligible). Exit code 1 means "not ignored".
```

**Step 3: Add the `signingConfigs` block to `app/build.gradle`**

In `openPTT-app/app/build.gradle`, find the `android {` block opener at line 46. Insert this block as the FIRST child inside `android { }` (i.e. before `defaultConfig`, around line 47):

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

The existing `buildTypes.debug` block at line 125ish has guards looking for a `beta` config — leave them alone. AGP automatically wires `buildTypes.debug.signingConfig` to `signingConfigs.debug` when one is defined; the explicit-`beta` override in the file's existing logic is for a separate `beta` flavor we're not touching.

**Step 4: Build to capture the new APK hash**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
./gradlew clean :app:assembleFossDebug 2>&1 | tail -5
# Expected: BUILD SUCCESSFUL

NEW_HASH=$(md5 -q app/build/outputs/apk/foss/debug/openptt-foss-debug.apk)
echo "new-build hash: $NEW_HASH"
```

**Step 5: Verify the keystore is project-local (not `~/.android/`)**

```bash
keytool -printcert -jarfile app/build/outputs/apk/foss/debug/openptt-foss-debug.apk 2>&1 | grep -E "Owner|Subject|SHA-?1:" | head -4
# Expected: Owner = CN=Android Debug, ... — confirms debug-keystore signature
```

The signature should be identical to the previously-shipped APK (since the keystore IS the same `~/.android/debug.keystore` — just relocated). The APK file hash will likely differ slightly run-to-run (timestamps embedded in zip metadata), but the SIGNING CERT will be byte-identical.

Quick proof: extract the signing cert from both the new build and the prod-served APK and compare:

```bash
# Cert from new local build
keytool -printcert -jarfile app/build/outputs/apk/foss/debug/openptt-foss-debug.apk 2>&1 | grep "SHA1:" > /tmp/new-cert.txt
# Cert from prod-served APK (downloads ~15 MB)
curl -fsSL https://ptt.harro.ch/apk/openptt-foss-debug.apk -o /tmp/prod.apk
keytool -printcert -jarfile /tmp/prod.apk 2>&1 | grep "SHA1:" > /tmp/prod-cert.txt
diff /tmp/new-cert.txt /tmp/prod-cert.txt
# Expected: NO output (identical SHA-1 fingerprints)
```

If the diff shows output, the keystore in `app/debug.keystore` differs from the one signing prod's APK. STOP — investigate before committing.

**Step 6: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add app/debug.keystore app/build.gradle .gitignore
git commit -m "$(cat <<'EOF'
build: commit deterministic debug.keystore + wire signingConfigs.debug

The keystore is a copy of the previously-machine-local
~/.android/debug.keystore — the same one currently signing every APK
on both P50s and on prod's /var/openptt/apk/openptt-foss-debug.apk.
Migration impact = zero; install -r on existing devices succeeds
without uninstall. Closes the keystore-mismatch dance that wiped
SharedPreferences on yesterday's first install.

Design doc: docs/plans/2026-04-22-deterministic-debug-signing-design.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git log -1 --pretty=full
```

**Self-review checklist:**
- [ ] `app/debug.keystore` exists (~2.6 KB).
- [ ] `.gitignore` has `!app/debug.keystore` exception line.
- [ ] `app/build.gradle` has the new `signingConfigs.debug` block right inside `android { ... }`.
- [ ] `git check-ignore app/debug.keystore` produces no output (file is tracked-eligible).
- [ ] Clean build produces an APK whose signing cert SHA-1 matches the prod-served APK's SHA-1 (the diff in Step 5 is empty).
- [ ] Commit has Co-Authored-By trailer.
- [ ] `git status` clean.

---

## Task 2: `scripts/deploy-apk.sh`

**Files:**
- Create: `openPTT-app/scripts/deploy-apk.sh` (executable)

**Step 1: Create the script**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
mkdir -p scripts
```

Write `openPTT-app/scripts/deploy-apk.sh` with this exact content:

```bash
#!/usr/bin/env bash
# Clean-build the foss-debug APK and ship it to prod's /var/openptt/apk/.
# Future CI calls this exact script.
#
# Requires:
#   - JAVA_HOME=openjdk21 (script sets it for you on macOS+Homebrew layout)
#   - ~/.ssh/id_ed25519_ptt with access to root@ptt.harro.ch
#   - openPTT-app/app/debug.keystore committed (committed in
#     2026-04-22 deterministic-debug-signing).
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
APK="${REPO_ROOT}/app/build/outputs/apk/foss/debug/openptt-foss-debug.apk"
PROD_HOST="root@ptt.harro.ch"
PROD_PATH="/var/openptt/apk/openptt-foss-debug.apk"
SSH_KEY="${HOME}/.ssh/id_ed25519_ptt"
PROD_URL="https://ptt.harro.ch/apk/openptt-foss-debug.apk"

if [[ -z "${JAVA_HOME:-}" ]]; then
    export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
fi

step() { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }
ok()   { printf '    \033[32mOK\033[0m %s\n' "$1"; }
fail() { printf '\n\033[1;31m!! \033[0m %s\n' "$1" >&2; exit 1; }

step "Building APK"
( cd "$REPO_ROOT" && ./gradlew clean :app:assembleFossDebug ) \
    || fail "gradle build failed"
LOCAL_HASH=$(md5 -q "$APK")
ok "local md5: $LOCAL_HASH ($(wc -c <"$APK") bytes)"

step "Uploading to $PROD_HOST:$PROD_PATH"
scp -i "$SSH_KEY" -q "$APK" "$PROD_HOST:$PROD_PATH" \
    || fail "scp failed"

step "Verifying remote hash"
REMOTE_HASH=$(ssh -i "$SSH_KEY" "$PROD_HOST" \
    "md5sum '$PROD_PATH' | awk '{print \$1}'")
ok "prod md5:  $REMOTE_HASH"

if [[ "$LOCAL_HASH" != "$REMOTE_HASH" ]]; then
    fail "hash mismatch — upload corrupted? local=$LOCAL_HASH remote=$REMOTE_HASH"
fi

step "Done"
ok "APK live at $PROD_URL"
```

Make it executable:

```bash
chmod +x scripts/deploy-apk.sh
```

**Step 2: Run it end-to-end**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
./scripts/deploy-apk.sh 2>&1 | tail -25
```

Expected output ends with:
```
==> Done
    OK APK live at https://ptt.harro.ch/apk/openptt-foss-debug.apk
```

**Step 3: External smoke — fetch the APK via HTTPS**

```bash
curl -fsSL https://ptt.harro.ch/apk/openptt-foss-debug.apk -o /tmp/prod.apk
PROD_MD5=$(md5 -q /tmp/prod.apk)
LOCAL_MD5=$(md5 -q /Users/harrowiersma/Documents/CLAUDE/openPTT-app/app/build/outputs/apk/foss/debug/openptt-foss-debug.apk)
echo "local: $LOCAL_MD5"
echo "prod:  $PROD_MD5"
[[ "$PROD_MD5" == "$LOCAL_MD5" ]] && echo "MATCH" || echo "MISMATCH"
```

Expected: `MATCH`.

**Step 4: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add scripts/deploy-apk.sh
git commit -m "$(cat <<'EOF'
build: scripts/deploy-apk.sh — one-command build + scp + hash-verify

Replaces the ad-hoc 'gradle ; scp ; curl' sequence we ran by hand on
2026-04-22. Single entry point any developer (or future CI step) calls
to refresh prod's /apk/openptt-foss-debug.apk.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Self-review checklist:**
- [ ] `scripts/deploy-apk.sh` exists, executable bit set.
- [ ] Script ran end-to-end without errors.
- [ ] Local APK md5 == prod-served APK md5 == prod-on-disk md5.
- [ ] Commit has Co-Authored-By trailer.

---

## Task 3: README "Building" section + final smoke

**Files:**
- Modify: `openPTT-app/README.md` (add a "Building" section)

**Step 1: Locate insertion point**

The current README starts with the project description, then likely has sections about flavors/install. Insert the new "Building" section before any existing build/install instructions, or as a new H2 right after the intro paragraph if no such section exists.

Run this to see the existing structure:

```bash
grep -n "^## " /Users/harrowiersma/Documents/CLAUDE/openPTT-app/README.md | head -20
```

If a `## Building` section already exists, EDIT it. Otherwise, INSERT the new section above the first existing `## ` heading.

**Step 2: Add the section**

Append (or insert) this block in `openPTT-app/README.md`:

```markdown
## Building

The canonical build target is `assembleFossDebug`; the resulting APK
(`app/build/outputs/apk/foss/debug/openptt-foss-debug.apk`) is what ships
to the field and to prod's `/var/openptt/apk/openptt-foss-debug.apk`.

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home  # macOS + Homebrew openjdk21
./gradlew clean :app:assembleFossDebug
```

### Signing key

`app/debug.keystore` is committed to the repo on purpose. It's a copy of
a stock Android-Gradle-Plugin debug keystore (well-known default
password `android`), so every machine produces APKs with the same
signature as the ones already installed on field devices. This lets
`adb install -r` succeed without uninstalling first — which would wipe
the device's SharedPreferences and Mumble credentials.

If the keystore ever needs rotation:

1. Generate a fresh one (`keytool -genkey -v -keystore app/debug.keystore -storepass android -keypass android -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10950`).
2. Plan a coordinated re-flash of every field device (each will need
   `adb uninstall ch.harro.openptt` first, followed by re-provisioning
   to restore Mumble creds).
3. Update prod's served APK via `scripts/deploy-apk.sh`.

If you need a real release-signing setup (Play Store, ProGuard, custodial
credentials), generate a separate `release.jks`, add a
`signingConfigs.release { ... }` block reading from
`~/.gradle/gradle.properties`, and wire `buildTypes.release.signingConfig`.
That's deliberately deferred — see
`docs/plans/2026-04-22-deterministic-debug-signing-design.md`.

### Deploying

After a build, push the APK to prod with:

```bash
./scripts/deploy-apk.sh
```

This rebuilds, uploads to `/var/openptt/apk/openptt-foss-debug.apk`,
and verifies hash parity. Future CI calls the same script.
```

**Step 3: Final smoke — `install -r` on R259060623**

```bash
adb -s R259060623 install -r /Users/harrowiersma/Documents/CLAUDE/openPTT-app/app/build/outputs/apk/foss/debug/openptt-foss-debug.apk
```

Expected: `Performing Streamed Install` then `Success`. NO `INSTALL_FAILED_UPDATE_INCOMPATIBLE`.

```bash
adb -s R259060623 shell "dumpsys package ch.harro.openptt | grep versionName"
```

Expected: `versionName=3.7.3-N-g<sha>-debug` matching the latest commit (this verifies the build the script just deployed is the one on the device).

Don't reset the app, don't re-provision — the install-in-place keeping SharedPreferences intact is the entire point of this exercise.

**Step 4: Commit**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/openPTT-app
git add README.md
git commit -m "$(cat <<'EOF'
docs: README "Building" section explains deterministic-keystore choice

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Self-review checklist:**
- [ ] README has a "Building" section explaining the canonical target, JAVA_HOME, the keystore choice, rotation procedure, and the deploy script.
- [ ] `adb install -r` succeeded on R259060623 without prompts about keystore mismatch.
- [ ] `versionName` on the device matches the latest commit's SHA.
- [ ] Commit has Co-Authored-By trailer.

---

## Verification checklist (all phases)

After Task 3:

- [ ] `app/debug.keystore` is committed and tracked.
- [ ] `app/build.gradle` has an explicit `signingConfigs.debug` block.
- [ ] `git check-ignore app/debug.keystore` returns nothing (no longer ignored).
- [ ] `keytool -printcert` on a fresh local build matches the SHA-1 fingerprint of prod's APK.
- [ ] `./scripts/deploy-apk.sh` runs end-to-end with hash-match.
- [ ] `adb install -r` succeeds on R259060623 without uninstall.
- [ ] README documents the choice + the rotation procedure.
- [ ] No app code changed (build-system change only).

All eight green = deterministic debug-signing ships.

---

## Open questions / deferred (carried from design doc)

1. **Real `release.jks`** — Play Store / ProGuard / multi-developer / CI custody. Defer until a consumer appears.
2. **GitHub Actions auto-deploy** — `scripts/deploy-apk.sh` is ready for CI to call; the workflow file itself is out of scope today.
3. **Keystore expiration in 2056** — add a calendar reminder. Or, more pragmatically, ignore until 2055.

---

## Dependencies + ordering

- Tasks 1 → 2 → 3 are strictly sequential (each builds on the previous).
- Task 1 must precede the next "App rebuild + install on field devices" task in our schedule (today's True Call Hold = #3) so the rebuild lands cleanly via `install -r` rather than triggering another keystore wipe.

Solo execution: ~30-45 minutes end-to-end including the hash-verification round trip.

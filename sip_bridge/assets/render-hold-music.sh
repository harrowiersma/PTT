#!/usr/bin/env bash
# Re-render the hold-music slin8 asset from a source MP3.
#
# Usage: bash sip_bridge/assets/render-hold-music.sh /path/to/source.mp3
#
# Output: sip_bridge/assets/hold-music.slin8 — 8 kHz mono signed 16-bit
# little-endian PCM. Read directly by audiosocket_bridge._get_hold_frames
# at startup. Commit the regenerated .slin8 alongside this script.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 /path/to/source.mp3" >&2
    exit 1
fi

SRC="$1"
OUT_DIR=$(cd "$(dirname "$0")" && pwd)
OUT="${OUT_DIR}/hold-music.slin8"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg not installed (brew install ffmpeg / apt install ffmpeg)" >&2
    exit 1
fi

if [[ ! -f "$SRC" ]]; then
    echo "source not found: $SRC" >&2
    exit 1
fi

ffmpeg -nostdin -loglevel error -y \
    -i "$SRC" \
    -ac 1 -ar 8000 -f s16le \
    "$OUT"

BYTES=$(wc -c <"$OUT")
SECONDS=$((BYTES / 16000))
FRAMES=$((BYTES / 320))
printf "rendered: %s\n  bytes:  %d\n  seconds: %d\n  frames:  %d (20ms each)\n" \
    "$OUT" "$BYTES" "$SECONDS" "$FRAMES"

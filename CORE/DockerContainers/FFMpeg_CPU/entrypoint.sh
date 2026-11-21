#!/bin/bash
# MOD node entrypoint — bridges the worker /data contract to ffmpeg.
# The worker stages one input file at /data/input/ and uploads whatever
# this writes to /data/output/. Config comes from env vars only.

echo "=== FFmpeg (CPU) ===" >&2

# Fail-closed: print a JSON error on stdout so vet-container smoke_run
# (step 5) can parse it.  Diagnostics go to stderr.

json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

die() {
    printf '{"error":"%s","code":"%s"}\n' "$(json_escape "$1")" "$(json_escape "$2")" >&1
    exit 1
}

# Find the single input file the worker staged into /data/input.
INPUT_FILE=$(find /data/input -maxdepth 1 -type f | head -1)

if [ -z "$INPUT_FILE" ]; then
    echo "ERROR: no input file found in /data/input" >&2
    die "no input file found in /data/input" "NO_INPUT"
fi
echo "Input: $INPUT_FILE" >&2

mkdir -p /data/output

# FFMPEG_ARGS is the user-supplied middle args (filters, codecs, etc.).
# TARGET_FORMAT is the output container/extension (default mp4).
FFMPEG_ARGS="${FFMPEG_ARGS:-}"
TARGET_FORMAT="${TARGET_FORMAT:-mp4}"

BASENAME=$(basename "$INPUT_FILE")
STEM="${BASENAME%.*}"
OUTPUT_FILE="/data/output/${STEM}.${TARGET_FORMAT}"

# Brief 1667: browsers cannot play MPEG-4 Part 2, which is what ffmpeg picks
# for mp4/mov/m4v when the build has no H.264 encoder (CE build is LGPL).
# Probe at runtime rather than hardcoding, so a paid build with an H.264
# encoder still gets H.264. An explicit user codec in FFMPEG_ARGS always wins.
BROWSER_PLAYABLE_CONTAINERS="mp4 mov m4v"

# Probe ONCE and pick the encoder NAME (the old probe was a bare yes/no, so
# convert.py/entrypoint could only say "an H.264 encoder exists").
# Preference: libx264 (paid, --enable-libx264) > libopenh264 (CE: Cisco
# binary, BSD-2, ffmpeg stays --disable-gpl LGPL) > hardware h264_* (nvenc /
# vaapi / videotoolbox). Empty = none.
FFMPEG_ENCODERS=$(ffmpeg -hide_banner -encoders 2>/dev/null || true)

pick_h264_encoder() {
    if printf '%s\n' "$FFMPEG_ENCODERS" | grep -q 'libx264'; then
        printf 'libx264'
        return
    fi
    if printf '%s\n' "$FFMPEG_ENCODERS" | grep -q 'libopenh264'; then
        printf 'libopenh264'
        return
    fi
    hw=$(printf '%s\n' "$FFMPEG_ENCODERS" | grep -oE 'h264_[A-Za-z0-9_]+' | head -1)
    printf '%s' "$hw"
}

arg_sets_codec() {
    local stream="$1" tok
    for tok in $FFMPEG_ARGS; do
        case "$tok" in
            -c:"$stream"|-codec:"$stream"|-"$stream"codec) return 0 ;;
        esac
    done
    return 1
}

AUTO_ARGS=""
case " $BROWSER_PLAYABLE_CONTAINERS " in
    *" $TARGET_FORMAT "*)
        H264_ENCODER=$(pick_h264_encoder)
        if [ -z "$H264_ENCODER" ]; then
            if arg_sets_codec v || arg_sets_codec a; then
                :  # explicit user codec always wins
            else
                # VP9 is only muxable into the mp4 muxer, so mov/m4v targets are
                # written as an mp4-container file (same extension, browser-playable).
                AUTO_ARGS="-c:v libvpx-vp9 -c:a aac -strict -2"
                [ "$TARGET_FORMAT" != "mp4" ] && AUTO_ARGS="$AUTO_ARGS -f mp4"
            fi
        elif [ "$H264_ENCODER" = libx264 ]; then
            :  # paid build: ffmpeg's own mp4/mov default is libx264; add nothing
        else
            # libopenh264 (CE) or a hardware h264_* encoder: ffmpeg's default
            # video codec for the container is not guaranteed to resolve to it,
            # so name it explicitly. An explicit user codec still wins.
            arg_sets_codec v || AUTO_ARGS="-c:v $H264_ENCODER"
        fi
        ;;
esac

echo "Args: '$FFMPEG_ARGS$AUTO_ARGS' -> $OUTPUT_FILE" >&2

# shellcheck disable=SC2086
if ! ffmpeg -y -i "$INPUT_FILE" $FFMPEG_ARGS $AUTO_ARGS "$OUTPUT_FILE"; then
    echo "ERROR: ffmpeg exited non-zero" >&2
    die "ffmpeg exited non-zero" "FFMPEG_FAILURE"
fi

if [ ! -f "$OUTPUT_FILE" ]; then
    echo "ERROR: expected output not written: $OUTPUT_FILE" >&2
    die "expected output not written: $OUTPUT_FILE" "OUTPUT_NOT_WRITTEN"
fi
echo "=== done: $OUTPUT_FILE ===" >&2

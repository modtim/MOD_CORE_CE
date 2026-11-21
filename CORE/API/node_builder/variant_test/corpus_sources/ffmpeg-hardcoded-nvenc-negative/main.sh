#!/bin/sh
# ffmpeg-hardcoded-nvenc-negative — corpus synthetic (manifest entry 16).
#
# DELIBERATE NEGATIVE CASE: the encoder is HARD-CODED to h264_nvenc (NVIDIA
# NVENC). This script performs NO runtime encoder detection and NO fallback.
# It MUST exit non-zero (recorded incompatible) on every non-NVIDIA
# accelerator — that runtime failure is the expected corpus verdict, not a
# bug to fix. Do not add an encoder auto-detect fallback; it would defeat
# the point of the entry. The IMAGE still builds fine everywhere (ffmpeg is
# present); only the RUN fails off-NVIDIA.

set -eu

IN_DIR=/data/input
OUT_DIR=/data/output

mkdir -p "$OUT_DIR"

IN=""
for f in "$IN_DIR"/*; do
    [ -e "$f" ] && IN="$f" && break
done
if [ -z "$IN" ]; then
    echo "no input file in $IN_DIR" >&2
    exit 2
fi

OUT="$OUT_DIR/output.mp4"

# h264_nvenc is NVIDIA-only. No fallback: on non-NVIDIA hosts the encoder
# init fails and ffmpeg exits non-zero by design.
ffmpeg -hide_banner -loglevel error -y \
    -i "$IN" \
    -c:v h264_nvenc -preset p4 -rc vbr -cq 23 \
    -c:a aac -b:a 128k \
    "$OUT"

echo "nvenc encode ok: $OUT"

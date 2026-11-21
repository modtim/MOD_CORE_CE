#!/bin/bash
set -e

echo "=== 3D Thumbnail Generator ===" >&2
echo "Content: ${CONTENT_HASH:0:12}... (${FILE_EXTENSION})" >&2

mkdir -p /data/output

exec python3 /app/generate_thumbnail.py

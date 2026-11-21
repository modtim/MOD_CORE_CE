#!/usr/bin/env python3
"""
Image Converter — converts between image formats.

Uses ffmpeg for most conversions, Pillow as fallback. Also extracts a frame
from video files (webm/mp4 -> png).

Env vars:
  TARGET_FORMAT   Target extension (e.g., "png", "jpg", "tif")

CE ships an LGPL-only FFmpeg build (brief 1724: no --enable-gpl, no H.264), so
exotic inputs fall back to Pillow at runtime. Fail-closed contract: every
failure prints exactly one JSON line to stdout and exits 1; success leaves
stdout empty.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def die(msg, code):
    """Print a JSON error line to stdout and exit 1 (fail-closed contract)."""
    print(json.dumps({"code": code, "error": msg}, sort_keys=True), flush=True)
    sys.exit(1)


def find_input():
    for f in Path('/data/input').iterdir():
        if f.is_file() and not f.name.startswith('.'):
            return f
    return None


def main():
    target = os.getenv('TARGET_FORMAT', 'png').lower().lstrip('.')
    input_path = Path('/data/input')

    if not input_path.is_dir():
        die("input directory /data/input does not exist", "NO_INPUT")

    input_file = find_input()

    if not input_file:
        log("ERROR: No input file found")
        die("no input file found in /data/input", "NO_INPUT")

    src_ext = input_file.suffix.lower().lstrip('.')
    output_path = Path('/data/output') / f"{input_file.stem}.{target}"
    os.makedirs('/data/output', exist_ok=True)

    log("=== Image Converter ===")
    log(f"  {input_file.name} ({src_ext}) -> {output_path.name} ({target})")

    if src_ext == target:
        shutil.copy2(input_file, output_path)
        log("  Same format — copied")
    else:
        # Video frame extraction (webm/mp4/etc -> image)
        VIDEO_EXTS = {'mp4', 'mov', 'avi', 'webm', 'mkv'}
        if src_ext in VIDEO_EXTS:
            cmd = ['ffmpeg', '-y', '-i', str(input_file),
                   '-vframes', '1', '-q:v', '2', str(output_path)]
        else:
            cmd = ['ffmpeg', '-y', '-i', str(input_file), str(output_path)]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            # The LGPL CE build only carries the image codecs this node needs,
            # so an exotic input can legitimately miss an encoder: Pillow is the
            # fallback. Keep stderr diagnostics, then fail closed if both lose.
            log(f"  ffmpeg failed: {result.stderr[-300:]}")
            try:
                from PIL import Image
                img = Image.open(input_file)
                if target in ('jpg', 'jpeg') and img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')
                img.save(output_path)
                log("  Converted via Pillow (ffmpeg failed)")
            except Exception as e:
                log(f"  Pillow fallback failed: {e}")
                die("image conversion failed (ffmpeg and Pillow)", "PROCESSING_FAILURE")
        else:
            log("  Converted via ffmpeg")

    if output_path.exists():
        log(f"  Done! {output_path.stat().st_size:,} bytes")
    else:
        log("  ERROR: Output not created")
        die("expected output not written", "OUTPUT_NOT_WRITTEN")


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        die("unexpected error", "INTERNAL")

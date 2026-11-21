#!/usr/bin/env python3
"""
Audio Converter — converts between audio formats via ffmpeg.

Env vars:
  TARGET_FORMAT   Target extension (e.g., "mp3", "wav", "flac", "ogg", "aac")
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
    print(json.dumps({"error": msg, "code": code}), flush=True)
    sys.exit(1)


def find_input():
    for f in Path('/data/input').iterdir():
        if f.is_file() and not f.name.startswith('.'):
            return f
    return None


def main():
    target = os.getenv('TARGET_FORMAT', 'mp3').lower().lstrip('.')
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

    log(f"=== Audio Converter ===")
    log(f"  {input_file.name} ({src_ext}) -> {output_path.name} ({target})")

    if src_ext == target:
        shutil.copy2(input_file, output_path)
        log("  Same format — copied")
    else:
        # Audio codec selection
        codec_map = {
            'mp3': ['-c:a', 'libmp3lame', '-q:a', '2'],
            'wav': ['-c:a', 'pcm_s16le'],
            'flac': ['-c:a', 'flac'],
            'ogg': ['-c:a', 'libvorbis', '-q:a', '5'],
            'aac': ['-c:a', 'aac', '-b:a', '192k'],
            'm4a': ['-c:a', 'aac', '-b:a', '192k'],
        }
        codec_args = codec_map.get(target, [])
        cmd = ['ffmpeg', '-y', '-i', str(input_file)] + codec_args + [str(output_path)]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"  ERROR: {result.stderr[-500:]}")
            die("ffmpeg conversion failed", "CONVERSION_FAILURE")
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

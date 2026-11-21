#!/usr/bin/env python3
"""
Video Converter — converts between video formats via ffmpeg.

Env vars:
  TARGET_FORMAT   Target extension (e.g., "mp4", "mov", "webm")

CE ships an LGPL-only FFmpeg build (no H.264), so mp4/mov targets fall back to
VP9 + FFmpeg-native AAC at runtime; a paid build that has an H.264 encoder
still gets H.264. Fail-closed contract: every failure prints exactly one JSON
line to stdout and exits 1; success leaves stdout empty.
"""

import json
import os
import re
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


def pick_h264_encoder():
    """Name of the best H.264 encoder this ffmpeg build has, or None.

    Probed at runtime (not assumed) so the same convert.py works on a paid
    build that ships --enable-libx264 and on the CE build, which stays
    --disable-gpl LGPL and carries Cisco's libopenh264 (BSD-2) instead.
    Preference order: libx264 > libopenh264 > hardware h264_* (nvenc /
    vaapi / videotoolbox). The old probe returned a bare yes/no, which made
    convert.py hard-code libx264 and die "Unknown encoder 'libx264'" on a
    libopenh264 or hardware build.
    """
    try:
        result = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                                capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    encoders = result.stdout
    for name in ('libx264', 'libopenh264'):
        if re.search(rf'(^|[^A-Za-z0-9_]){name}([^A-Za-z0-9_]|$)', encoders):
            return name
    hw = re.search(r'h264_[A-Za-z0-9]+', encoders)
    return hw.group(0) if hw else None


def h264_args(encoder):
    """Video/audio args for an H.264 target, built per encoder.

    libx264 keeps the exact paid-build args (byte-for-byte). libopenh264 is a
    constrained-baseline encoder: it rejects -preset ("Unrecognized option
    'preset'"), -crf, -tune, -profile:v and -x264-params, so it gets the
    encoder plus FFmpeg's own defaults (the old path set no -b:v bitrate, so
    none is invented here). Hardware encoders (h264_nvenc / h264_vaapi) take
    neither the x264 -preset fast value nor x264 tuning knobs.
    """
    if encoder == 'libx264':
        return ['-c:v', 'libx264', '-c:a', 'aac', '-preset', 'fast']
    return ['-c:v', encoder, '-c:a', 'aac', '-strict', '-2']


def build_cmd(input_file, target, output_path):
    """ffmpeg argv for one conversion (pure: no subprocess except the probe).

    Split out of main() so a unit test can assert the exact argv per encoder
    without ever starting ffmpeg.
    """
    if target in ('mp4', 'mov'):
        encoder = pick_h264_encoder()
        if encoder:
            # An H.264 encoder is available: pick the args THAT encoder
            # accepts (libx264 keeps the paid args byte-for-byte; the
            # CE build's Cisco libopenh264 rejects -preset).
            return ['ffmpeg', '-y', '-i', str(input_file)] + \
                h264_args(encoder) + [str(output_path)]
        # CE (LGPL) build has no H.264: VP9 + FFmpeg-native AAC. VP9
        # muxes into the mp4 muxer but not mov, so mov/m4v targets are
        # written into an mp4 container (same extension, playable).
        cmd = ['ffmpeg', '-y', '-i', str(input_file),
               '-c:v', 'libvpx-vp9', '-c:a', 'aac', '-strict', '-2']
        if target != 'mp4':
            cmd += ['-f', 'mp4']
        return cmd + [str(output_path)]
    if target == 'webm':
        return ['ffmpeg', '-y', '-i', str(input_file),
                '-c:v', 'libvpx-vp9', '-c:a', 'libopus', str(output_path)]
    return ['ffmpeg', '-y', '-i', str(input_file), str(output_path)]


def main():
    target = os.getenv('TARGET_FORMAT', 'mp4').lower().lstrip('.')
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

    log("=== Video Converter ===")
    log(f"  {input_file.name} ({src_ext}) -> {output_path.name} ({target})")

    if src_ext == target:
        shutil.copy2(input_file, output_path)
        log("  Same format — copied")
    else:
        cmd = build_cmd(input_file, target, output_path)

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

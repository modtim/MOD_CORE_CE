#!/usr/bin/env python3
"""
HDR/EXR Converter — tone-maps HDR images to standard PNG/JPG for viewing.

Uses ACES filmic tone mapping to compress the dynamic range into
displayable values while preserving detail in highlights and shadows.

Env vars:
  TARGET_FORMAT   Target extension (default: "png")
"""

import json
import os
import sys
import shutil
from pathlib import Path
import numpy as np


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def die(msg, code):
    """Print a JSON error line to stdout and exit 1 (fail-closed contract)."""
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    sys.exit(1)


def find_input():
    for f in Path('/data/input').iterdir():
        if f.is_file() and not f.name.startswith('.'):
            return f
    return None


def aces_tonemap(hdr, exposure=1.0):
    """ACES filmic tone mapping curve."""
    x = hdr * exposure
    a = 2.51
    b = 0.03
    c = 2.43
    d = 0.59
    e = 0.14
    return np.clip((x * (a * x + b)) / (x * (c * x + d) + e), 0, 1)


def load_hdr(path):
    """Load HDR/EXR image as float numpy array."""
    ext = path.suffix.lower()

    if ext == '.exr':
        try:
            import OpenEXR
            import Imath

            exr = OpenEXR.InputFile(str(path))
            header = exr.header()
            dw = header['dataWindow']
            w = dw.max.x - dw.min.x + 1
            h = dw.max.y - dw.min.y + 1

            # Read RGB channels
            pt = Imath.PixelType(Imath.PixelType.FLOAT)
            channels = {}
            for ch in ['R', 'G', 'B']:
                raw = exr.channel(ch, pt)
                channels[ch] = np.frombuffer(raw, dtype=np.float32).reshape(h, w)

            img = np.stack([channels['R'], channels['G'], channels['B']], axis=-1)
            return img
        except Exception as e:
            log(f"  OpenEXR failed ({e}), trying imageio...")

    # Fallback: imageio handles .hdr and some .exr
    import imageio
    img = imageio.imread(str(path))
    return img.astype(np.float32)


def main():
    target = os.getenv('TARGET_FORMAT', 'png').lower().lstrip('.')
    input_file = find_input()

    if not input_file:
        log("ERROR: No input file found")
        die("no input file found in /data/input", "NO_INPUT")

    src_ext = input_file.suffix.lower().lstrip('.')
    output_path = Path('/data/output') / f"{input_file.stem}.{target}"
    os.makedirs('/data/output', exist_ok=True)

    log(f"=== HDR/EXR Converter ===")
    log(f"  {input_file.name} ({src_ext}) → {output_path.name} ({target})")

    if src_ext == target:
        shutil.copy2(input_file, output_path)
        log("  Same format — copied")
    else:
        try:
            # Load HDR data
            hdr = load_hdr(input_file)
            log(f"  Loaded: {hdr.shape[1]}x{hdr.shape[0]}, range [{hdr.min():.2f}, {hdr.max():.2f}]")

            # Auto-exposure: normalize so median brightness is ~0.18 (middle gray)
            luminance = 0.2126 * hdr[:, :, 0] + 0.7152 * hdr[:, :, 1] + 0.0722 * hdr[:, :, 2]
            median_lum = max(np.median(luminance), 1e-6)
            exposure = 0.18 / median_lum
            log(f"  Auto exposure: {exposure:.2f}")

            # Tone map
            ldr = aces_tonemap(hdr, exposure)

            # Gamma correction (linear → sRGB)
            ldr = np.power(ldr, 1.0 / 2.2)

            # Convert to 8-bit
            ldr_8bit = (ldr * 255).astype(np.uint8)

            # Save
            from PIL import Image
            img = Image.fromarray(ldr_8bit)
            if target in ('jpg', 'jpeg'):
                img.save(output_path, quality=95)
            else:
                img.save(output_path)

            log(f"  Tone-mapped and saved")
        except Exception as e:
            log(f"  ERROR: {e}")
            die(f"conversion failed: {e}", "PROCESSING_FAILURE")

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

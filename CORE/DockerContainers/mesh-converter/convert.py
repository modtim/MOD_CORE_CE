#!/usr/bin/env python3
"""
Mesh Converter — converts between 3D mesh formats via trimesh.

Env vars:
  TARGET_FORMAT   Target extension (e.g., "glb", "obj", "stl", "ply")
"""

import json
import os
import shutil
import sys
from pathlib import Path


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def die(msg, code):
    """Print a JSON error line to stdout and exit 1 (fail-closed contract)."""
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    sys.exit(1)


# Real mesh formats this node converts. Everything else that may sit in
# /data/input — a .mtl material sidecar, texture images, a glTF .bin buffer —
# is a COMPANION file that trimesh loads automatically next to the mesh; it is
# never the conversion input. Picking the .mtl was the bug ("file_type 'mtl'
# not supported") when an OBJ arrived with its material sidecar.
MESH_EXTS = {"obj", "glb", "gltf", "stl", "ply", "fbx", "dae", "3ds",
             "off", "3mf", "x3d", "usd", "usdz", "usda", "usdc"}
SIDECAR_EXTS = {"mtl", "png", "jpg", "jpeg", "tga", "bmp", "tif", "tiff",
                "exr", "hdr", "bin", "txt", "json"}


def find_input():
    files = [f for f in Path('/data/input').iterdir()
             if f.is_file() and not f.name.startswith('.')]
    # Prefer a real mesh file; skip material/texture/buffer companions.
    meshes = sorted(f for f in files
                    if f.suffix.lower().lstrip('.') in MESH_EXTS)
    if meshes:
        return meshes[0]
    # Fallback: any file that isn't a known sidecar (be permissive for odd exts).
    others = sorted(f for f in files
                    if f.suffix.lower().lstrip('.') not in SIDECAR_EXTS)
    return others[0] if others else None


def main():
    target = os.getenv('TARGET_FORMAT', 'glb').lower().lstrip('.')
    input_file = find_input()

    if not input_file:
        log("ERROR: No input file found")
        die("no input file found in /data/input", "NO_INPUT")

    src_ext = input_file.suffix.lower().lstrip('.')
    output_path = Path('/data/output') / f"{input_file.stem}.{target}"
    os.makedirs('/data/output', exist_ok=True)

    log(f"=== Mesh Converter ===")
    log(f"  {input_file.name} ({src_ext}) → {output_path.name} ({target})")

    if src_ext == target:
        shutil.copy2(input_file, output_path)
        log("  Same format — copied")
    else:
        try:
            import trimesh
            scene_or_mesh = trimesh.load(str(input_file))
            if isinstance(scene_or_mesh, trimesh.Scene):
                mesh = trimesh.util.concatenate(scene_or_mesh.geometry.values())
            else:
                mesh = scene_or_mesh

            mesh.export(str(output_path), file_type=target)
            log("  Converted via trimesh")
        except Exception as e:
            log(f"  ERROR: {e}")
            die("trimesh conversion failed", "PROCESSING_FAILURE")

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

#!/usr/bin/env python3
"""
CAD Converter — converts STEP/STP files to GLB for browser viewing.

Uses cadquery/pythonocc to read CAD files, tessellates them to triangles,
and exports as GLB via trimesh. The GLB can then be viewed by the
existing Three.js 3D viewer.

Env vars:
  TARGET_FORMAT   Target extension (default: "glb")
"""

import json
import os
import sys
import shutil
from pathlib import Path

SUPPORTED_SOURCE_FORMATS = ('step', 'stp')


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


def convert_step_to_glb(input_path, output_path):
    """Convert STEP files to GLB using cadquery + trimesh."""
    import cadquery as cq
    import trimesh
    import numpy as np

    log("  Loading CAD file with cadquery...")

    # Read STEP. cadquery/OCP ships no IGES importer (importStep/importDXF/
    # importSVG only), so IGES inputs are rejected in main() before this point.
    ext = input_path.suffix.lower()
    if ext in ('.step', '.stp'):
        result = cq.importers.importStep(str(input_path))
    else:
        raise ValueError(f"Unsupported CAD format: {ext} (supported: step, stp)")

    log("  Tessellating to triangles...")

    # Tessellate the shape to get triangles
    vertices_list = []
    faces_list = []
    vertex_offset = 0

    for shape in result.vals():
        try:
            # Get the tessellation
            tess = shape.tessellate(0.1)  # tolerance
            verts, tris = tess

            if not verts or not tris:
                continue

            # Convert to numpy arrays
            v = np.array([(p.x, p.y, p.z) for p in verts], dtype=np.float64)
            f = np.array(tris, dtype=np.int32) + vertex_offset

            vertices_list.append(v)
            faces_list.append(f)
            vertex_offset += len(v)
        except Exception as e:
            log(f"  Warning: could not tessellate shape: {e}")
            continue

    if not vertices_list:
        raise ValueError("No geometry could be extracted from the CAD file")

    vertices = np.vstack(vertices_list)
    faces = np.vstack(faces_list)

    log(f"  Tessellated: {len(vertices)} vertices, {len(faces)} faces")

    # Create trimesh and export as GLB
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces)
    mesh.export(str(output_path), file_type='glb')
    log(f"  Exported GLB: {output_path.stat().st_size:,} bytes")


def main():
    target = os.getenv('TARGET_FORMAT', 'glb').lower().lstrip('.')
    input_path = Path('/data/input')

    if not input_path.is_dir():
        die("input directory /data/input does not exist", "NO_INPUT")

    input_file = find_input()

    if not input_file:
        die("no input file found in /data/input", "NO_INPUT")

    if not target:
        die("TARGET_FORMAT is empty or not a format name", "BAD_PARAM")

    src_ext = input_file.suffix.lower().lstrip('.')
    output_path = Path('/data/output') / f"{input_file.stem}.{target}"
    os.makedirs('/data/output', exist_ok=True)

    log(f"=== CAD Converter ===")
    log(f"  {input_file.name} ({src_ext}) → {output_path.name} ({target})")

    if src_ext == target:
        shutil.copy2(input_file, output_path)
        log("  Same format — copied")
    elif src_ext not in SUPPORTED_SOURCE_FORMATS:
        die(f"unsupported source CAD format: {src_ext} (supported: step, stp)", "BAD_PARAM")
    else:
        try:
            convert_step_to_glb(input_file, output_path)
        except Exception as e:
            log(f"  ERROR: {e}")
            die("CAD conversion failed", "PROCESSING_FAILURE")

    if output_path.exists():
        log(f"  Done! {output_path.stat().st_size:,} bytes")
    else:
        die("expected output not written", "OUTPUT_NOT_WRITTEN")


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        die("unexpected error", "INTERNAL")

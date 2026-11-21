#!/usr/bin/env python3
"""
USD Converter — converts USD/USDA/USDC files to GLB for browser viewing.

Uses OpenUSD (pxr) to read USD scenes, extracts mesh geometry,
and exports as GLB via trimesh.

Env vars:
  TARGET_FORMAT   Target extension (default: "glb")
"""

import json
import os
import sys
import shutil
from pathlib import Path


# Error codes (JSON error contract)
NO_INPUT = "NO_INPUT"
BAD_PARAM = "BAD_PARAM"
PROCESSING_FAILURE = "PROCESSING_FAILURE"
OUTPUT_NOT_WRITTEN = "OUTPUT_NOT_WRITTEN"
INTERNAL = "INTERNAL"


def die(msg, code):
    """Print a JSON error line to stdout and exit 1 (fail-closed contract)."""
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    sys.exit(1)

# worker-mediated-node-credentials: auth headers now come from the shared helper,
# which fetches short-lived run_task tokens from the worker on demand (no static
# API_KEY in this container). Drop-in replacement for the old inline functions.
from mod_node_auth import get_headers, get_service_headers  # noqa: E402


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def find_input():
    for f in Path('/data/input').iterdir():
        if f.is_file() and not f.name.startswith('.'):
            return f
    return None


def convert_usd_to_glb(input_path, output_path):
    """Convert USD/USDA/USDC to GLB using OpenUSD + trimesh.

    Extracts: geometry, normals, UVs, vertex colors, and textures.
    """
    import numpy as np
    import trimesh
    from PIL import Image
    import io

    from pxr import Usd, UsdGeom, UsdShade, Sdf

    log("  Loading USD stage...")
    stage = Usd.Stage.Open(str(input_path))
    if not stage:
        raise ValueError("Failed to open USD stage")

    input_dir = input_path.parent
    meshes = []

    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):
            continue

        mesh_prim = UsdGeom.Mesh(prim)
        points = mesh_prim.GetPointsAttr().Get()
        face_counts = mesh_prim.GetFaceVertexCountsAttr().Get()
        face_indices = mesh_prim.GetFaceVertexIndicesAttr().Get()

        if not points or not face_counts or not face_indices:
            continue

        verts = np.array(points, dtype=np.float64)

        # Triangulate faces
        tris = []
        idx = 0
        for count in face_counts:
            if count == 3:
                tris.append([face_indices[idx], face_indices[idx + 1], face_indices[idx + 2]])
            elif count == 4:
                tris.append([face_indices[idx], face_indices[idx + 1], face_indices[idx + 2]])
                tris.append([face_indices[idx], face_indices[idx + 2], face_indices[idx + 3]])
            else:
                for i in range(1, count - 1):
                    tris.append([face_indices[idx], face_indices[idx + i], face_indices[idx + i + 1]])
            idx += count

        if not tris:
            continue

        faces = np.array(tris, dtype=np.int32)

        # Extract normals
        normals = None
        try:
            normals_attr = mesh_prim.GetNormalsAttr().Get()
            if normals_attr:
                normals = np.array(normals_attr, dtype=np.float64)
                if len(normals) == len(face_indices):
                    # Face-varying normals — expand to per-vertex (approximate)
                    normals = None  # trimesh handles this better
        except Exception:
            pass

        # Extract UVs (primvar "st", "st0", "UVMap", etc.)
        uvs = None
        try:
            for pv_name in ['st', 'st0', 'st1', 'UVMap', 'uv', 'Texture']:
                pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar(pv_name)
                if pv and pv.Get():
                    uv_data = np.array(pv.Get(), dtype=np.float64)
                    uv_indices = pv.GetIndices()
                    if uv_indices:
                        # Indexed UVs — map to per-vertex
                        uv_idx = np.array(uv_indices, dtype=np.int32)
                        if len(uv_idx) == len(face_indices):
                            # Face-varying — need to remap
                            pass  # Complex remapping, skip for now
                        elif len(uv_data) >= len(verts):
                            uvs = uv_data[:len(verts)]
                    elif len(uv_data) >= len(verts):
                        uvs = uv_data[:len(verts)]
                    break
        except Exception:
            pass

        # Extract vertex colors (primvar "displayColor")
        colors = None
        try:
            color_pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar('displayColor')
            if color_pv and color_pv.Get():
                color_data = np.array(color_pv.Get(), dtype=np.float64)
                if len(color_data) == 1:
                    # Single color for entire mesh
                    colors = np.tile(color_data[0], (len(verts), 1))
                elif len(color_data) >= len(verts):
                    colors = color_data[:len(verts)]
                # Convert RGB to RGBA (0-255)
                if colors is not None:
                    if colors.max() <= 1.0:
                        colors = (colors * 255).astype(np.uint8)
                    alpha = np.full((len(colors), 1), 255, dtype=np.uint8)
                    colors = np.hstack([colors[:, :3], alpha])
        except Exception:
            pass

        # Build trimesh with visual data
        kwargs = {'vertices': verts, 'faces': faces, 'process': False}
        if normals is not None and len(normals) == len(verts):
            kwargs['vertex_normals'] = normals

        tri_mesh = trimesh.Trimesh(**kwargs)

        if colors is not None:
            tri_mesh.visual = trimesh.visual.ColorVisuals(mesh=tri_mesh, vertex_colors=colors)
        elif uvs is not None:
            # Try to find a texture file
            texture = _find_texture(prim, stage, input_dir)
            if texture is not None:
                material = trimesh.visual.texture.SimpleMaterial(image=texture)
                tri_mesh.visual = trimesh.visual.TextureVisuals(uv=uvs, material=material)

        meshes.append(tri_mesh)

    if not meshes:
        raise ValueError("No mesh geometry found in USD stage")

    if len(meshes) == 1:
        scene = trimesh.Scene(meshes[0])
    else:
        scene = trimesh.Scene(meshes)

    total_verts = sum(len(m.vertices) for m in meshes)
    total_faces = sum(len(m.faces) for m in meshes)
    log(f"  Extracted: {total_verts} vertices, {total_faces} faces, {len(meshes)} mesh(es)")

    scene.export(str(output_path), file_type='glb')
    log(f"  Exported GLB: {output_path.stat().st_size:,} bytes")


def _find_texture(prim, stage, input_dir):
    """Find diffuse texture by traversing the USD shader graph."""
    from PIL import Image
    try:
        from pxr import UsdShade

        binding = UsdShade.MaterialBindingAPI(prim)
        mat, _ = binding.ComputeBoundMaterial()
        if not mat:
            return None

        # Find all UsdUVTexture shaders under this material
        mat_prim = mat.GetPrim()
        for child in mat_prim.GetAllChildren():
            if not child.IsA(UsdShade.Shader):
                continue
            shader = UsdShade.Shader(child)
            shader_id = shader.GetShaderId()

            # UsdUVTexture is the standard texture node
            if shader_id == 'UsdUVTexture':
                file_inp = shader.GetInput('file')
                if file_inp:
                    val = file_inp.Get()
                    if val:
                        # AssetPath — extract the path string
                        tex_path = str(val)
                        # Remove @ delimiters if present (e.g., @0/model_baseColor.jpg@)
                        tex_path = tex_path.strip('@').strip()
                        if tex_path:
                            img = _load_texture(tex_path, input_dir)
                            if img:
                                log(f"  Found texture: {tex_path}")
                                return img

        # Also check if the material name hints at a texture file
        for child in mat_prim.GetAllChildren():
            if child.GetName().startswith('tex_'):
                shader = UsdShade.Shader(child)
                file_inp = shader.GetInput('file')
                if file_inp and file_inp.Get():
                    tex_path = str(file_inp.Get()).strip('@').strip()
                    img = _load_texture(tex_path, input_dir)
                    if img:
                        log(f"  Found texture: {tex_path}")
                        return img

    except Exception as e:
        log(f"  Texture extraction error: {e}")
    return None


def _load_texture(tex_path, input_dir):
    """Load a texture image from a file path."""
    from PIL import Image
    import os

    # Try absolute path
    if os.path.isfile(tex_path):
        return Image.open(tex_path)
    # Try relative to input dir
    rel = os.path.join(str(input_dir), tex_path)
    if os.path.isfile(rel):
        return Image.open(rel)
    # Try just the filename in the input dir
    basename = os.path.basename(tex_path)
    for f in input_dir.rglob('*'):
        if f.name == basename and f.is_file():
            return Image.open(str(f))
    return None


def main():
    target = os.getenv('TARGET_FORMAT', 'glb').lower().lstrip('.')
    input_file = find_input()

    if not input_file:
        log("ERROR: No input file found")
        die("no input file found in /data/input", NO_INPUT)

    src_ext = input_file.suffix.lower().lstrip('.')
    output_path = Path('/data/output') / f"{input_file.stem}.{target}"
    os.makedirs('/data/output', exist_ok=True)

    log(f"=== USD Converter ===")
    log(f"  {input_file.name} ({src_ext}) → {output_path.name} ({target})")

    if src_ext == target:
        shutil.copy2(input_file, output_path)
        log("  Same format — copied")
    elif src_ext == 'usdz':
        # USDZ is a zip of USD + textures — extract and convert
        import zipfile
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(input_file) as zf:
                zf.extractall(tmpdir)
            # Find the .usd/.usda/.usdc file inside
            usd_file = None
            for f in Path(tmpdir).rglob('*'):
                if f.suffix.lower() in ('.usd', '.usda', '.usdc'):
                    usd_file = f
                    break
            if usd_file:
                convert_usd_to_glb(usd_file, output_path)
            else:
                die("no USD file found inside USDZ archive", PROCESSING_FAILURE)
    else:
        convert_usd_to_glb(input_file, output_path)

    if not output_path.exists():
        die("output file was not created", OUTPUT_NOT_WRITTEN)

    log(f"  Done! {output_path.stat().st_size:,} bytes")

    # Callback: register the GLB preview on the original cog
    api_url = os.getenv("API_URL", "").strip()
    api_key = os.getenv("API_KEY", "").strip()
    cog_id = os.getenv("COG_ID", "").strip()
    tenant_id = os.getenv("TENANT_ID", "").strip()
    preview_type = os.getenv("PREVIEW_TYPE", "").strip()

    if api_url and api_key and cog_id and preview_type == "glb":
        try:
            import hashlib
            # Compute hash of the GLB for storage reference
            with open(output_path, 'rb') as f:
                glb_hash = hashlib.sha256(f.read()).hexdigest()

            import requests
            resp = requests.post(
                f"{api_url}/v1/assets/service/set-thumbnail",
                json={
                    "cog_id": int(cog_id),
                    "thumbnail_path": glb_hash,
                    "tenant_id": int(tenant_id),
                    "preview_type": "glb",
                },
                headers=get_service_headers(),
                timeout=10,
            )
            if resp.status_code in (200, 201):
                log(f"  Registered GLB preview: {glb_hash[:16]}...")
            else:
                log(f"  WARNING: GLB callback returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log(f"  WARNING: GLB callback failed: {e}")


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        die(f"internal error: {e}", INTERNAL)

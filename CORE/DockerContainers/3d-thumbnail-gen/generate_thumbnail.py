#!/usr/bin/env python3
"""
3D Thumbnail Generator — renders a PNG preview from a 3D model file.

Runs headless via OSMesa (no GPU required). Downloads the source file
from MinIO, loads it with trimesh, renders a 300x300 PNG with pyrender,
uploads the thumbnail back to MinIO, and notifies the API.

Environment variables:
  CONTENT_HASH      SHA256 hash of the source 3D file in MinIO
  FILE_EXTENSION    File extension (glb, obj, stl, ply, fbx, gltf)
  COG_ID            Cog ID to update with thumbnail metadata
  TENANT_ID         Tenant ID
  MINIO_ENDPOINT    MinIO endpoint (host:port)
  MINIO_ACCESS_KEY  MinIO access key
  MINIO_SECRET_KEY  MinIO secret key
  MINIO_SESSION_TOKEN  (optional) STS session token
  MINIO_BUCKET      Tenant bucket name
  API_URL           API base URL for callback
  API_KEY           Service API key
  THUMBNAIL_SIZE    Square render size in px (256/512/1024/2048; default 512)
  BACKGROUND_COLOR  Background fill (transparent/white/black/gray; default
                    transparent — matches the node's configuration_schema
                    extra_variables; the worker passes those UPPER-CASE)
"""

import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime

# worker-mediated-node-credentials: auth headers now come from the shared helper,
# which fetches short-lived run_task tokens from the worker on demand (no static
# API_KEY in this container). Drop-in replacement for the old inline pattern.
from mod_node_auth import get_headers  # noqa: E402

import numpy as np
from PIL import Image


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def get_minio_client():
    from minio import Minio

    endpoint = os.environ.get("S3_ENDPOINT") or os.environ.get("MINIO_ENDPOINT")
    # Strip protocol prefix if present
    for prefix in ("http://", "https://"):
        if endpoint.startswith(prefix):
            endpoint = endpoint[len(prefix):]

    secure = os.environ.get("S3_SECURE") or os.environ.get("MINIO_SECURE") or "false"
    secure = secure.lower() == "true"

    return Minio(
        endpoint,
        access_key=os.environ.get("S3_ACCESS_KEY") or os.environ.get("MINIO_ACCESS_KEY"),
        secret_key=os.environ.get("S3_SECRET_KEY") or os.environ.get("MINIO_SECRET_KEY"),
        session_token=(os.environ.get("S3_SESSION_TOKEN") or os.environ.get("MINIO_SESSION_TOKEN")) or None,
        secure=secure,
    )


def download_source(mc, bucket, content_hash, ext):
    """Download the 3D file from MinIO to a temp file."""
    tmp = tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False)
    try:
        response = mc.get_object(bucket, content_hash)
        for chunk in response.stream(8192):
            tmp.write(chunk)
        response.close()
        response.release_conn()
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise
    tmp.close()
    return tmp.name


def load_mesh(filepath, ext):
    """Load a 3D mesh using trimesh, with CAD and USD fallbacks."""
    import trimesh

    ext = ext.lower().lstrip('.')

    # IGES has no reader in this container: _load_cad_mesh uses cadquery
    # importStep (STEP only). Refuse IGES with a clear error instead of
    # misreading it. The Dashboard preview reads IGES client-side (OCCT).
    if ext in ('iges', 'igs'):
        raise ValueError(
            "IGES/IGS is not supported by the 3D thumbnail generator "
            "(cadquery reads STEP/STP only); convert the file to STEP or STP first"
        )

    # CAD formats (STEP/STP) — use cadquery to tessellate, then wrap as trimesh
    if ext in ('step', 'stp'):
        return _load_cad_mesh(filepath)

    # USD formats — use OpenUSD to extract geometry, then wrap as trimesh
    if ext in ('usd', 'usda', 'usdc'):
        return _load_usd_mesh(filepath)

    # USDZ — a zip container of USD + textures; unwrap (guarded) then reuse the USD loader
    if ext == 'usdz':
        import shutil

        inner = _unwrap_usdz_package(filepath)
        try:
            return _load_usd_mesh(inner)
        finally:
            shutil.rmtree(os.path.dirname(inner), ignore_errors=True)

    # Standard 3D formats — trimesh handles directly
    try:
        scene_or_mesh = trimesh.load(filepath, force="scene")
    except Exception:
        scene_or_mesh = trimesh.load(filepath, force="mesh")

    if isinstance(scene_or_mesh, trimesh.Scene):
        if len(scene_or_mesh.geometry) == 0:
            raise ValueError("Empty scene — no geometry found")
        mesh = trimesh.util.concatenate(scene_or_mesh.geometry.values())
    else:
        mesh = scene_or_mesh

    return mesh


def _load_cad_mesh(filepath):
    """Load STEP/STP via cadquery importStep and return a trimesh (IGES is refused upstream)."""
    import cadquery as cq
    import trimesh

    result = cq.importers.importStep(str(filepath))

    vertices_list, faces_list = [], []
    offset = 0
    for shape in result.vals():
        try:
            verts, tris = shape.tessellate(0.1)
            if not verts or not tris:
                continue
            v = np.array([(p.x, p.y, p.z) for p in verts], dtype=np.float64)
            f = np.array(tris, dtype=np.int32) + offset
            vertices_list.append(v)
            faces_list.append(f)
            offset += len(v)
        except Exception:
            continue

    if not vertices_list:
        raise ValueError("No geometry in CAD file")

    return trimesh.Trimesh(
        vertices=np.vstack(vertices_list),
        faces=np.vstack(faces_list),
    )


def _load_usd_mesh(filepath):
    """Load USD via OpenUSD (pxr) and return a trimesh."""
    from pxr import Usd, UsdGeom
    import trimesh

    stage = Usd.Stage.Open(str(filepath))
    if not stage:
        raise ValueError("Failed to open USD stage")

    vertices_list, faces_list = [], []
    offset = 0
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):
            continue
        mesh = UsdGeom.Mesh(prim)
        points = mesh.GetPointsAttr().Get()
        face_counts = mesh.GetFaceVertexCountsAttr().Get()
        face_indices = mesh.GetFaceVertexIndicesAttr().Get()
        if not points or not face_counts or not face_indices:
            continue

        v = np.array(points, dtype=np.float64)
        tris = []
        idx = 0
        for count in face_counts:
            for i in range(1, count - 1):
                tris.append([face_indices[idx], face_indices[idx + i], face_indices[idx + i + 1]])
            idx += count

        if tris:
            f = np.array(tris, dtype=np.int32) + offset
            vertices_list.append(v)
            faces_list.append(f)
            offset += len(v)

    if not vertices_list:
        raise ValueError("No mesh geometry in USD stage")

    return trimesh.Trimesh(
        vertices=np.vstack(vertices_list),
        faces=np.vstack(faces_list),
    )

def _unwrap_usdz_package(filepath):
    """Unwrap a .usdz (zip of USD + textures) into a temp dir; return the inner USD path.

    Guards (untrusted archive): entry count, per-member and total uncompressed size,
    archive compressed size, per-member compression ratio, and member names (no '..',
    no absolute paths, no symlinks/devices). Never calls extractall; members are
    written one at a time under a sanitised relative path. Raises ValueError on any
    malformed, over-cap, or nameless archive — the caller's error shape is unchanged.
    """
    import os
    import shutil
    import stat
    import tempfile
    import zipfile
    from pathlib import Path

    def _is_special(info):
        return info.is_dir() or stat.S_ISLNK(info.external_attr >> 16)

    max_entries = 512
    max_member_bytes = 64 * 1024 * 1024
    max_total_bytes = 256 * 1024 * 1024
    max_archive_bytes = 64 * 1024 * 1024
    max_ratio = 32

    with zipfile.ZipFile(str(filepath)) as zf:
        infos = zf.infolist()

    if not infos:
        raise ValueError("USDZ archive is empty")
    if len(infos) > max_entries:
        raise ValueError(f"USDZ archive has too many entries ({len(infos)})")

    total = 0
    inner_name = None
    for info in infos:
        name = info.filename
        if _is_special(info):
            raise ValueError(f"USDZ member is not a regular file: {name}")
        if name.startswith(('/', '\\')):
            raise ValueError(f"USDZ member name is absolute: {name}")
        parts = name.replace('\\', '/').split('/')
        if any(p == '..' for p in parts):
            raise ValueError(f"USDZ member name traverses parent: {name}")
        if info.file_size > max_member_bytes:
            raise ValueError(f"USDZ member uncompressed size too large: {name}")
        total += info.file_size
        if total > max_total_bytes:
            raise ValueError(f"USDZ total uncompressed size too large ({total} bytes)")
        if info.file_size / max(1, info.compress_size) > max_ratio:
            raise ValueError(f"USDZ member compression ratio too high: {name}")
        if inner_name is None and Path(name).suffix.lower() in ('.usd', '.usda', '.usdc'):
            inner_name = name

    if inner_name is None:
        raise ValueError("No USD file found inside USDZ archive")

    tmpdir = tempfile.mkdtemp(prefix="usdz_")
    try:
        with zipfile.ZipFile(str(filepath)) as zf:
            for info in infos:
                safe = info.filename.replace('\\', '/')
                dest = os.path.join(tmpdir, safe)
                if not os.path.realpath(dest).startswith(os.path.realpath(tmpdir) + os.sep):
                    raise ValueError(f"USDZ member resolves outside temp dir: {info.filename}")
                parent = os.path.dirname(dest)
                if not os.path.exists(parent):
                    os.makedirs(parent, exist_ok=True)
                with zf.open(info) as src, open(dest, 'wb') as out:
                    shutil.copyfileobj(src, out, 65536)
        return os.path.join(tmpdir, inner_name.replace('\\', '/'))
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


# Node configuration_schema extra_variables (Thumbnail3D_MESH_PROCESS_Node.py)
# arrive as UPPER-CASE env vars: worker_tasks.py does
#   for key, value in extra_vars.items(): container_env[key.upper()] = str(value)
# so the names below are THUMBNAIL_SIZE and BACKGROUND_COLOR.
ALLOWED_THUMBNAIL_SIZES = (256, 512, 1024, 2048)
DEFAULT_THUMBNAIL_SIZE = 512
# The node's declared default for background_color is "transparent". The old
# script hard-coded an opaque navy [30, 30, 46, 255] which no schema value ever
# produced; the script default now follows the node default (see result file).
BACKGROUND_COLORS = {
    "transparent": [0, 0, 0, 0],
    "white": [255, 255, 255, 255],
    "black": [0, 0, 0, 255],
    "gray": [128, 128, 128, 255],
}
DEFAULT_BACKGROUND_COLOR = "transparent"


def _parse_render_params(env):
    """Pure parser for the node's render params -> (size, bg_rgba).

    `env` is a mapping-like object (os.environ works). Never raises: an
    absent/blank/invalid value falls back to the node's declared default and
    logs in the container's existing style.
    """
    raw_size = str(env.get("THUMBNAIL_SIZE", "") or "").strip()
    size = DEFAULT_THUMBNAIL_SIZE
    if raw_size:
        try:
            parsed = int(raw_size)
        except ValueError:
            log(f"  THUMBNAIL_SIZE={raw_size!r} is not an integer; using {DEFAULT_THUMBNAIL_SIZE}")
            parsed = None
        if parsed in ALLOWED_THUMBNAIL_SIZES:
            size = parsed
        elif parsed is not None:
            log(f"  THUMBNAIL_SIZE={parsed} not in {ALLOWED_THUMBNAIL_SIZES}; using {DEFAULT_THUMBNAIL_SIZE}")
    else:
        log(f"  THUMBNAIL_SIZE unset; using {DEFAULT_THUMBNAIL_SIZE}")

    raw_bg = str(env.get("BACKGROUND_COLOR", "") or "").strip().lower()
    bg = BACKGROUND_COLORS[DEFAULT_BACKGROUND_COLOR]
    if raw_bg:
        if raw_bg in BACKGROUND_COLORS:
            bg = BACKGROUND_COLORS[raw_bg]
        else:
            log(f"  BACKGROUND_COLOR={raw_bg!r} not in {sorted(BACKGROUND_COLORS)}; "
                f"using {DEFAULT_BACKGROUND_COLOR}")
    else:
        log(f"  BACKGROUND_COLOR unset; using {DEFAULT_BACKGROUND_COLOR}")

    return size, bg


def render_thumbnail(mesh, width=300, height=300, bg_color=None):
    """Render a mesh to a PNG image using pyrender with OSMesa.

    width/height are the square render size; bg_color is the [r,g,b,a] scene
    background (None -> the node's declared default, transparent).
    """
    import pyrender

    if bg_color is None:
        bg_color = list(BACKGROUND_COLORS[DEFAULT_BACKGROUND_COLOR])
    transparent = int(bg_color[3]) == 0

    # Center and normalize the mesh
    centroid = mesh.centroid
    mesh.vertices -= centroid
    scale = mesh.extents.max()
    if scale > 0:
        mesh.vertices /= scale

    # Create pyrender scene (bg from THUMBNAIL_SIZE/BACKGROUND_COLOR wiring;
    # alpha 0 needs the RGBA render flag so the PNG really carries alpha)
    scene = pyrender.Scene(bg_color=list(bg_color), ambient_light=[0.3, 0.3, 0.3])

    # Add mesh
    material = pyrender.MetallicRoughnessMaterial(
        baseColorFactor=[0.7, 0.7, 0.7, 1.0],
        metallicFactor=0.2,
        roughnessFactor=0.6,
    )
    py_mesh = pyrender.Mesh.from_trimesh(mesh, material=material)
    scene.add(py_mesh)

    # Add lights
    light1 = pyrender.DirectionalLight(color=[1.0, 1.0, 1.0], intensity=3.0)
    scene.add(light1, pose=_look_at([2, 3, 2], [0, 0, 0]))

    light2 = pyrender.DirectionalLight(color=[0.6, 0.6, 0.8], intensity=1.5)
    scene.add(light2, pose=_look_at([-2, 1, -1], [0, 0, 0]))

    # Camera at 45 degrees
    camera = pyrender.PerspectiveCamera(yfov=np.pi / 4.0)
    camera_pose = _look_at([1.2, 0.8, 1.2], [0, 0, 0])
    scene.add(camera, pose=camera_pose)

    # Render (RGBA flag when the background is transparent, so the PNG keeps
    # alpha 0 background pixels)
    renderer = pyrender.OffscreenRenderer(width, height)
    try:
        if transparent:
            color, _ = renderer.render(scene, flags=pyrender.RenderFlags.RGBA)
        else:
            color, _ = renderer.render(scene)
    finally:
        renderer.delete()

    image = Image.fromarray(np.asarray(color))
    if transparent and image.mode != "RGBA":
        image = image.convert("RGBA")
    return image


def _look_at(eye, target):
    """Create a 4x4 camera pose matrix looking from eye to target."""
    eye = np.array(eye, dtype=float)
    target = np.array(target, dtype=float)
    up = np.array([0, 1, 0], dtype=float)

    forward = target - eye
    forward = forward / np.linalg.norm(forward)
    right = np.cross(forward, up)
    right = right / np.linalg.norm(right)
    up = np.cross(right, forward)

    pose = np.eye(4)
    pose[:3, 0] = right
    pose[:3, 1] = up
    pose[:3, 2] = -forward
    pose[:3, 3] = eye
    return pose


def upload_thumbnail(mc, bucket, content_hash, image):
    """Upload the thumbnail PNG to MinIO."""
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    size = buf.getbuffer().nbytes

    thumb_path = f"thumbnails/{content_hash}.png"
    mc.put_object(
        bucket,
        thumb_path,
        buf,
        length=size,
        content_type="image/png",
    )
    return thumb_path


def notify_api(cog_id, thumbnail_path, tenant_id):
    """Call the API to update cog metadata with thumbnail path."""
    import requests

    api_url = os.environ.get("API_URL", "").strip()
    if not api_url or not (os.environ.get("MOD_TASK_TOKEN_URL") or os.environ.get("API_KEY")):
        log("  No API_URL/API_KEY — skipping callback")
        return

    try:
        resp = requests.post(
            f"{api_url}/v1/assets/service/set-thumbnail",
            headers=get_headers(),
            json={
                "cog_id": int(cog_id),
                "thumbnail_path": thumbnail_path,
                "tenant_id": int(tenant_id),
            },
            timeout=30,
        )
        if resp.ok:
            log(f"  API callback: OK")
        else:
            log(f"  API callback: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"  API callback failed: {e}")


def main():
    started = time.time()

    content_hash = os.environ["CONTENT_HASH"]
    ext = os.environ.get("FILE_EXTENSION", "glb").lower().lstrip(".")
    cog_id = os.environ.get("COG_ID", "")
    tenant_id = os.environ.get("TENANT_ID", "")
    bucket = os.environ["MINIO_BUCKET"]

    log(f"  Source: {content_hash[:12]}... ({ext})")

    mc = get_minio_client()

    # Check if thumbnail already exists (dedup)
    thumb_path = f"thumbnails/{content_hash}.png"
    try:
        mc.stat_object(bucket, thumb_path)
        log(f"  Thumbnail already exists, skipping render")
        # Still notify API in case metadata is missing
        if cog_id:
            notify_api(cog_id, thumb_path, tenant_id)
        write_result("skipped", thumb_path, 0, time.time() - started)
        return
    except Exception:
        pass  # Doesn't exist, proceed

    # Check file size (skip very large files)
    try:
        stat = mc.stat_object(bucket, content_hash)
        size_mb = stat.size / (1024 * 1024)
        if size_mb > 500:
            log(f"  File too large ({size_mb:.0f} MB), skipping thumbnail")
            write_result("skipped_too_large", "", 0, time.time() - started)
            return
    except Exception as e:
        log(f"  Could not stat source file: {e}")

    # Download
    log("  Downloading source file...")
    filepath = download_source(mc, bucket, content_hash, ext)
    log(f"  Downloaded to {filepath}")

    try:
        # Load mesh
        log("  Loading mesh...")
        mesh = load_mesh(filepath, ext)
        log(f"  Loaded: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")

        # Render (node extra_variables THUMBNAIL_SIZE / BACKGROUND_COLOR)
        log("  Rendering thumbnail...")
        thumb_size_px, bg_rgba = _parse_render_params(os.environ)
        image = render_thumbnail(mesh, width=thumb_size_px, height=thumb_size_px, bg_color=bg_rgba)
        log(f"  Rendered: {image.size[0]}x{image.size[1]}")

        # Upload
        log("  Uploading thumbnail...")
        thumb_path = upload_thumbnail(mc, bucket, content_hash, image)
        thumb_size = image.size[0] * image.size[1] * 4  # approximate
        log(f"  Uploaded: {thumb_path}")

        # Notify API
        if cog_id:
            notify_api(cog_id, thumb_path, tenant_id)

        duration = time.time() - started
        log(f"  Done in {duration:.1f}s")
        write_result("success", thumb_path, thumb_size, duration)

    except Exception as e:
        duration = time.time() - started
        log(f"  ERROR: {e}")
        write_result("error", "", 0, duration, str(e))
    finally:
        os.unlink(filepath)


def write_result(status, thumbnail_path, size_bytes, duration, error=None):
    result = {
        "status": status,
        "thumbnail_path": thumbnail_path,
        "size_bytes": size_bytes,
        "duration_seconds": round(duration, 2),
    }
    if error:
        result["error"] = error

    out_path = "/data/output/thumbnail_result.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    # Also stdout for worker to capture
    print(json.dumps(result))


if __name__ == "__main__":
    main()

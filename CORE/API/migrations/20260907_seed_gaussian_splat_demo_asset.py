"""node-trial-runs / cross-tenant demo assets — seed the flagship demo sample.

The priced flagship SYSTEM node "Gaussian Splat" (id 258 in prod) shipped with
NULL sample_input_ref/demo_workflow_ref, so marketplace /try returned 400
"This node has no demo to try". This script seeds a REAL platform-owned demo
sample and attaches it, so /try resolves and dispatches for ANY tenant.

What it creates (under the PLATFORM tenant, id 1):
  - a small sample-image Cog (+Version +ContentBlob +File) whose bytes are put
    into the platform bucket (tenant-1-files/{sha256}); AND
  - sets node 258's:
      * sample_input_ref = str(<platform sample cog id>)   (an INTEGER cog id —
        the format the trial dispatch copies cross-tenant into the requester's
        own scope; see services/node_trial.materialize_platform_demo_input), and
      * demo_workflow_ref = a compact curated graph image_input(363) ->
        gaussian_splat(258) -> return_assets_export(630).

Idempotent: re-running is a no-op once the node already points at a valid
platform sample cog. Only touches the one flagship system node; never touches
tenant-owned rows.

Run inside the API container:
    # dry run (default — shows what it WOULD do, writes nothing):
    docker exec my_fastapi_app python3 migrations/20260907_seed_gaussian_splat_demo_asset.py
    # activate:
    docker exec my_fastapi_app python3 migrations/20260907_seed_gaussian_splat_demo_asset.py --commit
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import sys
import zlib

logger = logging.getLogger(__name__)

PLATFORM_TENANT_ID = 1

# Prod ids (from the task brief); resolved by id first, then by node_kind so the
# script still works if the ids differ in a given deployment.
FLAGSHIP_NODE_ID = 258
FLAGSHIP_NODE_KIND = "gaussian_splat_process"
IMAGE_INPUT_ID = 363
IMAGE_INPUT_KIND = "image_input"
RETURN_ID = 630
RETURN_KIND = "return_assets_export"


def _sample_png_bytes(width: int = 64, height: int = 64) -> bytes:
    """A dependency-free, valid RGB PNG (a simple diagonal gradient). Kept small;
    real GPU execution of the splat is out of scope — this proves the asset path."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None) per scanline
        for x in range(width):
            raw += bytes((
                (x * 255) // max(1, width - 1),
                (y * 255) // max(1, height - 1),
                ((x + y) * 255) // max(1, width + height - 2),
            ))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit, colour type 2 (RGB)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def _resolve_node(db, node_id, node_kind):
    import models
    nd = db.query(models.NodeDefinition).filter(
        models.NodeDefinition.id == node_id,
        models.NodeDefinition.tenant_id.is_(None),
    ).first()
    if nd is not None:
        return nd
    return db.query(models.NodeDefinition).filter(
        models.NodeDefinition.node_kind == node_kind,
        models.NodeDefinition.tenant_id.is_(None),
    ).first()


def _platform_owner_id(db):
    import models
    u = db.query(models.User).filter(
        models.User.tenant_id == PLATFORM_TENANT_ID,
        models.User.is_platform_admin.is_(True),
    ).first()
    if u is None:
        u = db.query(models.User).filter(
            models.User.tenant_id == PLATFORM_TENANT_ID).first()
    return u.id if u else None


def seed_demo_asset(db, *, commit: bool = False) -> dict:
    """Core (testable) seed. Returns a summary dict. Writes only when commit."""
    import hashlib
    import models

    result: dict = {"created_cog_id": None, "node_id": None, "skipped": False, "actions": []}

    node = _resolve_node(db, FLAGSHIP_NODE_ID, FLAGSHIP_NODE_KIND)
    if node is None:
        result["skipped"] = True
        result["actions"].append(
            f"flagship node (id={FLAGSHIP_NODE_ID}, kind={FLAGSHIP_NODE_KIND}) not found — nothing to seed")
        return result
    result["node_id"] = node.id

    tenant = db.query(models.Tenant).filter(models.Tenant.id == PLATFORM_TENANT_ID).first()
    if tenant is None:
        result["skipped"] = True
        result["actions"].append(f"platform tenant {PLATFORM_TENANT_ID} does not exist — cannot seed")
        return result

    # Idempotency — already pointing at a valid platform sample cog?
    existing_ref = (node.sample_input_ref or "").strip()
    if existing_ref.isdigit():
        cog = db.query(models.Cog).filter(
            models.Cog.id == int(existing_ref),
            models.Cog.tenant_id == PLATFORM_TENANT_ID,
        ).first()
        if cog is not None and cog.latest_version_id:
            result["skipped"] = True
            result["created_cog_id"] = cog.id
            result["actions"].append(
                f"node {node.id} already has a valid platform sample cog {cog.id} — no-op")
            return result

    png = _sample_png_bytes()
    content_hash = hashlib.sha256(png).hexdigest()
    owner_id = _platform_owner_id(db)
    input_nd = _resolve_node(db, IMAGE_INPUT_ID, IMAGE_INPUT_KIND)
    return_nd = _resolve_node(db, RETURN_ID, RETURN_KIND)

    # Compact curated graph (column is VARCHAR(500)). Uses resolved node-def ids.
    graph = {
        "nodes": [
            {"id": "input-1", "nd": (input_nd.id if input_nd else IMAGE_INPUT_ID), "kind": IMAGE_INPUT_KIND},
            {"id": "process-1", "nd": node.id, "kind": node.node_kind},
            {"id": "return-1", "nd": (return_nd.id if return_nd else RETURN_ID), "kind": RETURN_KIND},
        ],
        "edges": [["input-1", "process-1"], ["process-1", "return-1"]],
    }
    demo_workflow_ref = json.dumps(graph, separators=(",", ":"))
    if len(demo_workflow_ref) > 500:  # guard the column width
        demo_workflow_ref = None
        result["actions"].append("WARN: curated graph exceeds 500 chars — demo_workflow_ref left NULL")

    result["actions"].append(
        f"create platform sample cog '{node.name} — demo sample' "
        f"(tenant {PLATFORM_TENANT_ID}, {len(png)} bytes, sha256={content_hash[:12]}…)")
    result["actions"].append(f"put object tenant-{PLATFORM_TENANT_ID}-files/{content_hash[:12]}…")
    result["actions"].append(f"set node {node.id}.sample_input_ref = <new cog id>")
    result["actions"].append(
        f"set node {node.id}.demo_workflow_ref = {demo_workflow_ref!r}" if demo_workflow_ref
        else f"leave node {node.id}.demo_workflow_ref unchanged")

    if not commit:
        return result

    # --- WRITE ---
    from io import BytesIO
    from utils import storage as _storage

    scheme = os.getenv("STORAGE_SCHEME", "minio")
    cfg = _storage.resolve_backend(PLATFORM_TENANT_ID, db)
    bucket = cfg.bucket or f"tenant-{PLATFORM_TENANT_ID}-files"
    client = _storage.build_client_for_config(cfg)
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed: could not ensure bucket %s: %s", bucket, exc)
    client.put_object(bucket_name=bucket, object_name=content_hash,
                      data=BytesIO(png), length=len(png), content_type="image/png")
    storage_path = f"{scheme}://{bucket}/{content_hash}"

    demo_name = f"{node.name} — demo sample"
    cog = models.Cog(
        name=demo_name, cog_type=models.CogType.FILE,
        tenant_id=PLATFORM_TENANT_ID, owner_id=owner_id,
        is_system_generated=True, is_hidden=True,
        data_classification="public",
    )
    db.add(cog)
    db.flush()

    blob = db.query(models.ContentBlob).filter(
        models.ContentBlob.id == content_hash,
        models.ContentBlob.tenant_id == PLATFORM_TENANT_ID,
    ).first()
    if blob is None:
        blob = models.ContentBlob(
            id=content_hash, tenant_id=PLATFORM_TENANT_ID, size_bytes=len(png),
            mime_type="image/png", file_extension="png",
            storage_path=storage_path, cog_id=cog.id)
        db.add(blob)
    else:
        blob.storage_path = storage_path
        blob.cog_id = cog.id
    db.flush()

    ver = models.Version(
        cog_id=cog.id, tenant_id=PLATFORM_TENANT_ID, content_id=content_hash,
        version_number=1, name=demo_name, author_user_id=owner_id)
    db.add(ver)
    db.flush()
    cog.latest_version_id = ver.id

    if owner_id is not None:
        db.add(models.File(
            cog_id=cog.id, tenant_id=PLATFORM_TENANT_ID, user_id=owner_id,
            name=demo_name + ".png", storage_path=storage_path,
            mime_type="image/png", size_bytes=len(png)))
    db.flush()

    node.sample_input_ref = str(cog.id)
    if demo_workflow_ref:
        node.demo_workflow_ref = demo_workflow_ref
    db.flush()

    result["created_cog_id"] = cog.id
    result["actions"].append(f"WROTE platform sample cog id={cog.id}; node {node.id} now demo-enabled")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="apply (default is dry run)")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    from database import SessionLocal

    db = SessionLocal()
    try:
        summary = seed_demo_asset(db, commit=args.commit)
        for line in summary["actions"]:
            print(f"{'WRITE' if args.commit else 'DRY  '}  {line}")
        if args.commit and not summary["skipped"]:
            db.commit()
            print("Applied.")
        elif args.commit:
            print("Nothing to apply.")
        else:
            print("(dry run — re-run with --commit)")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

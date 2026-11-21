"""2400GX-c1 (launch-completion-megaplan Group X) — external seed routes logic.

A tenant admin registers a PENDING seed (server-minted multipart upload slots
under the tenant-pinned `{project}/{seed_id}/` prefix of the `stack-snapshots`
bucket), an EXTERNAL host PUTs the parts through presigned URLs, then commit
completes the uploads server-side, validates the objects (size caps + magic
bytes) and writes a NORMAL snapshot record into `storage_refs["snapshots"]`
plus `source:"external"` / `posture` / `seed_id` / `verified_at:null` — so
`restore_stack` (persistent_stacks.py:1711) and rehearse need no change.

Security: keys are minted here, never accepted from a client (S1); presigned
URLs appear only in the parts response (S3); caps + ≤16 parts + format
whitelist are checked at create AND re-checked at commit (S5); no client URL
is ever fetched (S7). CE note: this path never calls `require_paid` — a
logical part may REGISTER on CE (owner Q-C1 pending); restoring a logical
snapshot stays wherever it is today (non-CE engine).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import secrets as _secrets

from sqlalchemy.orm import Session

from services import platform_settings as _psr
from utils import storage
from minio.api import Part
from utils.paid_import import optional_paid
from product_names import themed_logger

from routers.persistent_stacks._shared import (
    SNAPSHOT_BUCKET,
    _audit_stack,
    _get_owned,
    _project_name,
    _stack_volumes,
    logger,
)

logger = themed_logger(__name__, 'stacks')

_MAX_SEED_PARTS = 16


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_BUCKET_RACE_CODES = {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}


def _ensure_snapshot_bucket(client) -> None:
    """Create the platform SNAPSHOT_BUCKET on first use (nothing else creates it
    on a fresh install). Idempotent: an existing bucket is left untouched and a
    concurrent create (already owned / already exists) counts as success. Any
    other S3 error propagates unchanged. Tenant isolation stays the
    {project}/{seed_id}/ key prefix; the bucket is private (no ACL set)."""
    if client.bucket_exists(SNAPSHOT_BUCKET):
        return
    try:
        client.make_bucket(SNAPSHOT_BUCKET)
    except Exception as exc:  # minio.error.S3Error carries .code
        if getattr(exc, "code", None) in _BUCKET_RACE_CODES:
            return
        raise


def _expires_at(db=None) -> str:
    ttl = _setting("stack.seed.url_ttl_seconds", db=db)
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()


def _err(status_code: int, msg: str, code: str):
    """The project's code-carrying error shape (routers/audit_logs.py:42):
    HTTPException(status, detail={"detail": msg, "code": code})."""
    from fastapi import HTTPException
    return HTTPException(status_code=status_code,
                         detail={"detail": msg, "code": code})


_MAGIC_PGDMP = b"PGDMP"
_MAGIC_TARGZ = b"\x1f\x8b"
_SNIFF_BYTES = 512

# Settings (DB-backed platform-settings registry, registered the way
# services/stack_backup.py:47 registers stacks.snapshot_retention).
SETTINGS = {
    "stack.seed.max_part_bytes": 64 * 1024**3,
    "stack.seed.max_total_bytes": 128 * 1024**3,
    "stack.seed.part_bytes": 64 * 1024**2,
    "stack.seed.url_ttl_seconds": 3600,
    "stack.seed.max_uncompressed_bytes": 512 * 1024**3,
}
for _k, _v in SETTINGS.items():
    _psr.register(_k, int, _v, group="stacks",
                  description="External seed upload limit.")

# 2400GX-cUI-5a — the browser commit path: the server lists the chunks of the
# upload IT minted (S3 ListParts) instead of the client sending each ETag, so
# no CORS `Access-Control-Expose-Headers: ETag` is needed (versitygw v1.7.0
# only has --cors-allow-origin). The explicit-ETag list path stays for the CLI.
_SERVER_LIST = "server"
_LIST_PAGE_PARTS = 1000  # S3 ListParts returns at most 1000 chunks per page
                         # (minio api.py:3401 `max_parts` default; versitygw
                         # caps the same way)


def _server_listed_parts(client, label: str, p: dict, db) -> list:
    """ListParts on the seed's OWN server-minted slot (`p["upload_id"]` +
    `p["key"]`, both created in register_seed:154) — the client never supplies
    an upload id or key, so the body cannot steer which upload is listed.
    minio api.py:3401 `_list_parts(bucket_name, object_name, upload_id,
    max_parts=None, part_number_marker=None, extra_headers=None,
    extra_query_params=None) -> ListPartsResult`; ListPartsResult
    (minio/datatypes.py:290) carries `parts: list[Part]`,
    `is_truncated: bool` and `next_part_number_marker`. One page is <=1000
    parts and a 64 GB / 64 MB slot can hold 1024 chunks, so this paginates:
    `next_part_number_marker` is the last part number listed (strictly
    increasing) and the page bound is DERIVED from the declared chunk count:
    ceil(cap / _LIST_PAGE_PARTS) + 1, so a lowered `stack.seed.part_bytes`
    (64 GB / 8 MB = 8192 chunks) still lists every chunk. The bound is NOT a
    fixed constant — a fixed 8 pages silently truncates at 8000.
    Guards run BEFORE complete: the part numbers must be exactly 1..n (no
    gap/duplicate) and n must not exceed ceil(p["size_bytes"] /
    stack.seed.part_bytes) — the chunk count register_seed advertised
    (register_seed return, stack_seed.py:227 `parts_total`; the seed's part
    record persists only key/size_bytes/upload_id, so the bound is
    recomputed from the setting, exactly as register_seed did). Zero chunks
    returns [] so the caller's existing "no chunks" branch raises. A listing
    that is still truncated when the bound (or a non-increasing marker)
    stops the loop is DENIED — complete is never called on a truncated
    listing. No ETag or upload id is logged."""
    part_bytes = max(1, _setting("stack.seed.part_bytes", db=db))
    cap = max(1, math.ceil(int(p["size_bytes"]) / part_bytes))
    listed: list = []
    marker = None
    truncated = False
    for _page in range(math.ceil(cap / _LIST_PAGE_PARTS) + 1):
        result = client._list_parts(SNAPSHOT_BUCKET, p["key"], p["upload_id"],
                                    part_number_marker=marker)
        listed.extend(result.parts)
        truncated = bool(getattr(result, "is_truncated", False))
        if not truncated:
            break
        nxt = getattr(result, "next_part_number_marker", None)
        try:
            nxt_n = int(nxt)
        except (TypeError, ValueError):
            nxt_n = 0
        if nxt_n <= (int(marker) if marker is not None else 0):
            break  # truncated + marker stuck -> denied below
        marker = str(nxt_n)
    if truncated:
        raise _err(422, f"part {label} has more chunks than declared",
                   "seed_part_unknown")
    out = [{"part_number": int(getattr(x, "part_number", 0) or 0),
            "etag": str(getattr(x, "etag", "") or "")} for x in listed]
    if out:
        nums = sorted(x["part_number"] for x in out)
        if nums != list(range(1, len(nums) + 1)):
            raise _err(422, f"part {label} chunks have a gap or duplicate",
                       "seed_part_unknown")
        if len(nums) > cap:
            raise _err(422, f"part {label} has more chunks than declared",
                       "seed_part_unknown")
    return out


def _setting(key: str, db=None):
    return int(_psr.get_setting(key, db=db))


def _logical_part_identity(stack, service_name: str):
    """(engine, ok) for a compose service that declares `x-mod.backup:
    logical`. Reuses the paid engine detector (services/stack_logical_backup,
    the same one `take_snapshot` (_shared.py:5160) uses) when present; on CE
    that module is absent, so the declared posture + the image base (the same
    one-line base extraction find_engine_service uses) name the dump instead —
    registering a logical part is CE-allowed (owner Q-C1 pending)."""
    services = (stack.compose_spec or {}).get("services") or {}
    svc = services.get(service_name)
    if not isinstance(svc, dict):
        return None, False
    xmod = svc.get("x-mod")
    posture = xmod.get("backup") if isinstance(xmod, dict) else None
    if posture != "logical":
        return None, False
    _lb = optional_paid("services.stack_logical_backup")
    if _lb is not None:
        engine, name, _svc = _lb.find_engine_service(stack)
        if engine and name == service_name:
            return engine, True
    image = str(svc.get("image") or "")
    base = image.split(":", 1)[0].rsplit("/", 1)[-1]
    return (base or None), bool(base)


def register_seed(db: Session, stack_id: int, user, body: dict) -> dict:
    """Validate + create storage_refs["seeds"][seed_id] (pending) and
    initiate one server-side multipart upload per part. Returns the 202
    payload (never a presigned URL — those come from the parts route only)."""
    stack = _get_owned(db, stack_id, user)
    logical = body.get("logical")
    volumes = body.get("volumes") or {}
    if not logical and not volumes:
        raise _err(422, "seed must declare at least one part", "seed_invalid")
    parts: dict[str, dict] = {}
    project = _project_name(stack)
    seed_id = "seed-" + _secrets.token_hex(8)
    max_part = _setting("stack.seed.max_part_bytes", db=db)
    max_total = _setting("stack.seed.max_total_bytes", db=db)
    total = 0
    if logical:
        engine, ok = _logical_part_identity(stack, str(logical.get("service") or ""))
        fmt = logical.get("format")
        size = int(logical.get("size_bytes") or 0)
        if not ok or fmt != "pg_dump_custom" or size <= 0:
            raise _err(422, "logical part rejected", "seed_invalid")
        if size > max_part:
            raise _err(422, "logical part exceeds cap", "seed_too_large")
        total += size
        key = f"{project}/{seed_id}/{engine}.dump"
        parts["logical:db"] = {"key": key, "size_bytes": size}
    declared = set(_stack_volumes(stack))
    for vol, spec in volumes.items():
        if len(parts) >= _MAX_SEED_PARTS:
            raise _err(422, "too many parts", "seed_invalid")
        size = int((spec or {}).get("size_bytes") or 0)
        storage_name = f"{project}_{vol}"
        if storage_name not in declared or size <= 0:
            raise _err(422, f"volume {vol} rejected", "seed_invalid")
        if size > max_part:
            raise _err(422, f"volume {vol} exceeds cap", "seed_too_large")
        total += size
        parts[f"volume:{vol}"] = {
            "key": f"{project}/{seed_id}/{storage_name}.tar.gz", "size_bytes": size}
    if len(parts) > _MAX_SEED_PARTS or total > max_total:
        raise _err(422, "seed exceeds size caps", "seed_too_large")
    part_bytes = max(1, _setting("stack.seed.part_bytes", db=db))
    client = storage.get_service_client()
    _ensure_snapshot_bucket(client)
    for label, p in parts.items():
        p["upload_id"] = client._create_multipart_upload(
            SNAPSHOT_BUCKET, p["key"], {})
    refs = dict(stack.storage_refs or {})
    seeds = dict(refs.get("seeds") or {})
    seeds[seed_id] = {
        "status": "pending", "parts": parts,
        "created_at": _now_iso(), "created_by": user.id,
    }
    refs["seeds"] = seeds
    stack.storage_refs = refs  # reassign so SQLAlchemy detects the JSONB change
    db.commit()
    _audit_stack(db, "STACK_SEED_CREATED", stack, user,
                 extra={"seed_id": seed_id, "parts": sorted(parts)})
    db.commit()
    return {
        "seed_id": seed_id,
        "part_bytes": part_bytes,
        "uploads": [{"part": label, "upload_id": p["upload_id"],
                     "parts_total": max(1, math.ceil(p["size_bytes"] / part_bytes)),
                     "expires_at": _expires_at(db)}
                    for label, p in parts.items()],
    }


def _seed_parts(db: Session, stack_id: int, user, seed_id: str) -> tuple:
    """(stack, seed record) or 404 seed_not_found / 409 already committed."""
    stack = _get_owned(db, stack_id, user)
    seed = ((stack.storage_refs or {}).get("seeds") or {}).get(seed_id)
    if not seed:
        raise _err(404, "seed not found", "seed_not_found")
    return stack, seed


def part_urls(db: Session, stack_id: int, user, seed_id: str, part: str,
              from_part: int, count: int) -> dict:
    """Presigned `upload_part` PUT URLs for a window of one part's chunks.
    The host is resolved exactly as the file-download presigned URLs are
    today: utils.storage.get_presigned_client (bound to the EXTERNAL
    endpoint, utils/storage.py:307) — the same client utils/s3_client.py:376
    hands the cog download routes (routers/cogs/assets_cogs_files.py:3998).
    Never a hand-built host (S7)."""
    stack, seed = _seed_parts(db, stack_id, user, seed_id)
    p = (seed.get("parts") or {}).get(part)
    if not p:
        raise _err(422, f"unknown part {part}", "seed_part_unknown")
    if from_part < 1 or count < 1 or count > 100:
        raise _err(422, "bad part window", "seed_invalid")
    part_bytes = max(1, _setting("stack.seed.part_bytes", db=db))
    total = max(1, math.ceil(int(p["size_bytes"]) / part_bytes))
    if from_part + count - 1 > total:
        raise _err(422, "part window past end", "seed_invalid")
    presign = storage.get_presigned_client()
    urls = []
    for n in range(from_part, from_part + count):
        urls.append({
            "part_number": n,
            "url": presign.get_presigned_url(
                "PUT", SNAPSHOT_BUCKET, p["key"],
                expires=timedelta(seconds=_setting("stack.seed.url_ttl_seconds", db=db)),
                extra_query_params={"uploadId": p["upload_id"],
                                    "partNumber": str(n)}),
        })
    return {"part": part, "urls": urls, "expires_at": _expires_at(db)}


def commit_seed(db: Session, stack_id: int, user, seed_id: str,
                body: dict) -> dict:
    """Complete each multipart upload server-side, HEAD + sniff every
    object, then write a normal snapshot record + mark the seed committed."""
    stack, seed = _seed_parts(db, stack_id, user, seed_id)
    if seed.get("status") == "committed":
        raise _err(409, "seed already committed", "seed_already_committed")
    parts = seed.get("parts") or {}
    submitted = body.get("parts") or {}
    if set(submitted) != set(parts):
        raise _err(422, "part set mismatch", "seed_part_unknown")
    max_part = _setting("stack.seed.max_part_bytes", db=db)
    max_total = _setting("stack.seed.max_total_bytes", db=db)
    postures = set()
    total = 0
    client = storage.get_service_client()
    for label, p in parts.items():
        etags = submitted[label]
        if etags == _SERVER_LIST:
            # cUI-5a: the server lists the chunks of ITS OWN minted upload
            # (ListParts, paginated + gap/dup/count-guarded) — the body names
            # no upload id/key, so it cannot steer the listing.
            etags = _server_listed_parts(client, label, p, db)
        elif isinstance(etags, str):
            raise _err(422, f"part {label} must be a chunk list or \"server\"",
                       "seed_invalid")
        if not isinstance(etags, list) or not etags:
            raise _err(422, f"part {label} has no chunks", "seed_part_unknown")
        for e in etags:
            n = int(e.get("part_number") or 0)
            if n < 1:
                raise _err(422, "bad part_number", "seed_invalid")
        client._complete_multipart_upload(
            SNAPSHOT_BUCKET, p["key"], p["upload_id"],
            [Part(part_number=int(e["part_number"]), etag=str(e["etag"]))
             for e in etags])
        stat = client.stat_object(SNAPSHOT_BUCKET, p["key"])
        size = int(getattr(stat, "size", 0) or 0)
        total += size
        if size > int(p["size_bytes"]) or size > max_part:
            raise _err(422, "object exceeds declared size / cap", "seed_too_large")
        head = client.get_object(SNAPSHOT_BUCKET, p["key"], length=_SNIFF_BYTES).read()
        if label.startswith("logical:"):
            if not head.startswith(_MAGIC_PGDMP):
                raise _err(422, "archive_rejected", "archive_rejected")
            postures.add("logical")
        else:
            if not head.startswith(_MAGIC_TARGZ):
                raise _err(422, "archive_rejected", "archive_rejected")
            postures.add("volume")
    if total > max_total:
        raise _err(422, "seed exceeds total cap", "seed_too_large")
    posture = "logical" if postures == {"logical"} else "volume"
    snap_id = seed_id.split("-", 1)[1]
    # The snapshot record keeps take_snapshot's shape (_shared.py:5189-5196):
    # `volumes` maps the REAL docker volume names (project-prefixed) to keys,
    # and a logical part is filed under the `logical:<engine>` pseudo-key plus
    # the structured `logical` entry — exactly what `_take_logical_snapshot`
    # writes — so restore_stack (persistent_stacks.py:1711) and rehearse find
    # it with no change.
    vol_keys = {}
    logical = None
    for label, p in parts.items():
        if label == "logical:db":
            engine = p["key"].rsplit("/", 1)[1][:-len(".dump")]
            vol_keys[f"logical:{engine}"] = p["key"]
            logical = {"engine": engine, "key": p["key"]}
        else:
            vol_keys[p["key"].rsplit("/", 1)[1][:-len(".tar.gz")]] = p["key"]
    snap = {"bucket": SNAPSHOT_BUCKET, "volumes": vol_keys,
            "created_at": _now_iso(), "source": "external",
            "posture": posture, "seed_id": seed_id,
            "verified_at": None, "verify_error": None}
    if logical:
        snap["logical"] = logical
    refs = dict(stack.storage_refs or {})
    snaps = dict(refs.get("snapshots") or {})
    snaps[snap_id] = snap
    seeds = dict(refs.get("seeds") or {})
    seeds[seed_id] = {**seed, "status": "committed"}
    refs["snapshots"] = snaps
    refs["seeds"] = seeds
    stack.storage_refs = refs  # reassign so SQLAlchemy detects the JSONB change
    db.commit()
    _audit_stack(db, "STACK_SEED_COMMITTED", stack, user,
                 extra={"seed_id": seed_id, "snapshot_id": snap_id})
    db.commit()
    return {"snapshot_id": snap_id}

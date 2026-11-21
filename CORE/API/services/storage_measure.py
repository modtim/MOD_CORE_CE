"""The ONE authoritative per-tenant storage measurement (megaplan Q.1a / 25.2).

Before this module two jobs wrote ``tenants.storage_used_bytes`` with different
formulas (a main.py daemon loop summing files + workflow outputs + node images,
and the scheduler's ``storage_metrics`` job summing content_blobs), so the
value flipped depending on which ran last. This is now the only writer.

Canonical definition — ``total_bytes`` (the number quota is enforced against):

    SUM(content_blobs.size_bytes) for the tenant
  + live ``files`` rows whose storage_path has no content_blob   (unbacked)
  + workflow_node_outputs whose content_hash has no content_blob (unbacked,
    counted once per distinct hash)

Why: storage is content-addressed. ``content_blobs`` has one row per physical
object in ``tenant-{id}-files`` (PK = (sha256, tenant_id)). ``files`` rows and
``workflow_node_outputs`` rows are *references* to those blobs — summing them
(the old main.py loop) double-counts every file that is also a blob and every
output that is re-referenced (stage tenant 1: 3353 output rows = 7.2 GB, but
only 193 distinct hashes = 0.44 GB). The "unbacked" components only catch
legacy/partial rows that never got a blob, so nothing stored is under-counted.

Informational (NOT in total_bytes):
  * ``node_image_bytes`` — container images live in Harbor, billed by the
    Harbor-usage snapshot, not the tenant bucket.
  * ``bucket_bytes`` — actual object bytes listed from ``tenant-{id}-files``;
    None when the backend is unreachable (``bucket_ok=False``). Used for drift
    detection only, so an outage never moves a tenant's quota.
  * ``byo_bytes`` — per tenant-owned StorageConnection (BYO-S3), best-effort,
    cached ~6h in-process, None on error. Owner decision 09-29 (round 7,
    A.9): bytes sitting in a tenant's OWN bucket are NOT MOD's storage to
    bill or cap — ``byo_bytes`` is reported here for visibility only and is
    deliberately never folded into ``total_bytes`` / ``db_tracked_bytes``
    (the number ``tenants.storage_used_bytes`` is set from, and the number
    every quota/billing reader — ``utils/usage_enforcement.py``,
    ``services/subscriptions_service.py``, etc. — reads). See
    ``measure_tenant_storage()`` below for where that exclusion is enforced.

Transaction discipline: DB reads happen first, the read transaction is ended,
THEN any network I/O runs, and only afterwards is a write transaction opened.
Network I/O inside an open transaction pins a PgBouncer backend (known
platform-wide outage mode).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

BYO_CACHE_TTL_SECONDS = int(os.getenv("STORAGE_MEASURE_BYO_CACHE_SECONDS", str(6 * 3600)))
_byo_cache: Dict[int, tuple] = {}  # connection_id -> (monotonic_ts, bytes)
_byo_lock = threading.Lock()

_DB_SUMS_SQL = text("""
    SELECT
      (SELECT COALESCE(SUM(cb.size_bytes), 0) FROM content_blobs cb
        WHERE cb.tenant_id = :tid) AS blob_bytes,
      (SELECT COUNT(*) FROM content_blobs cb WHERE cb.tenant_id = :tid) AS blob_count,
      (SELECT COALESCE(SUM(f.size_bytes), 0) FROM files f
        WHERE f.tenant_id = :tid AND f.deleted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM content_blobs cb
                          WHERE cb.tenant_id = f.tenant_id
                            AND cb.storage_path = f.storage_path)) AS unbacked_file_bytes,
      (SELECT COALESCE(SUM(x.sz), 0) FROM (
          SELECT w.content_hash, MAX(w.size_bytes) AS sz
          FROM workflow_node_outputs w
          WHERE w.tenant_id = :tid
            AND NOT EXISTS (SELECT 1 FROM content_blobs cb
                            WHERE cb.tenant_id = w.tenant_id
                              AND cb.id = w.content_hash)
          GROUP BY w.content_hash) x) AS unbacked_output_bytes,
      (SELECT COALESCE(SUM(nd.image_size_bytes), 0) FROM node_definitions nd
        WHERE nd.tenant_id = :tid AND nd.deleted_at IS NULL) AS node_image_bytes
""")


def house_bucket_name(tenant_id: int) -> str:
    return f"tenant-{tenant_id}-files"


def _list_bucket_bytes(cfg, bucket: str) -> int:
    """Sum object sizes in ``bucket`` using a client built by utils/storage.
    Raises on any backend error (callers translate to None). Stubbed in tests."""
    from utils.storage import build_client_for_config
    client = build_client_for_config(cfg)
    total = 0
    for obj in client.list_objects(bucket, recursive=True):
        total += int(getattr(obj, "size", 0) or 0)
    return total


def _house_config():
    from utils.storage import get_storage_config
    return get_storage_config()


def _end_read_txn(db: Session) -> None:
    """Close the read transaction before network I/O so no PgBouncer backend is
    pinned while we wait on S3. Only commits when the session holds no pending
    ORM changes (never flushes a caller's half-built unit of work)."""
    if db.new or db.dirty or db.deleted:
        logger.warning("storage_measure: session has pending changes; not ending read txn")
        return
    db.commit()


def _db_components(db: Session, tenant_id: int) -> Dict[str, int]:
    row = db.execute(_DB_SUMS_SQL, {"tid": tenant_id}).mappings().one()
    return {k: int(row[k] or 0) for k in (
        "blob_bytes", "blob_count", "unbacked_file_bytes",
        "unbacked_output_bytes", "node_image_bytes")}


def _byo_targets(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """Resolve (id, name, StorageConfig) for each live BYO connection — DB/Vault
    reads only, no network to the tenant's endpoint."""
    import models
    from crud import storage_connection_cud as sc_crud
    from utils.storage import build_byo_config

    q = db.query(models.StorageConnection).filter(
        models.StorageConnection.tenant_id == tenant_id)
    if hasattr(models.StorageConnection, "deleted_at"):
        q = q.filter(models.StorageConnection.deleted_at.is_(None))
    targets = []
    for conn in q.all():
        entry: Dict[str, Any] = {"connection_id": conn.id, "name": conn.name,
                                 "bucket": conn.bucket, "is_active": bool(conn.is_active),
                                 "cfg": None}
        with _byo_lock:
            hit = _byo_cache.get(conn.id)
        if hit and time.monotonic() - hit[0] < BYO_CACHE_TTL_SECONDS:
            targets.append(entry)  # cached — skip the Vault decrypt entirely
            continue
        try:
            secret = sc_crud.get_connection_secret(db, conn)
            if secret:
                entry["cfg"] = build_byo_config(
                    endpoint=conn.endpoint, region=conn.region, bucket=conn.bucket,
                    access_key=conn.access_key, secret_key=secret, secure=conn.secure,
                    db=db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("storage_measure: tenant %s BYO connection %s unresolvable: %s",
                           tenant_id, conn.id, exc)
        targets.append(entry)
    return targets


def _measure_byo(target: Dict[str, Any], now: float) -> Optional[int]:
    cid = target["connection_id"]
    with _byo_lock:
        hit = _byo_cache.get(cid)
    if hit and now - hit[0] < BYO_CACHE_TTL_SECONDS:
        return hit[1]
    if target["cfg"] is None:
        return None
    try:
        size = _list_bucket_bytes(target["cfg"], target["bucket"])
    except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
        logger.info("storage_measure: BYO connection %s unreachable: %s", cid, exc)
        return None
    with _byo_lock:
        _byo_cache[cid] = (now, size)
    return size


def measure_tenant_storage(db: Session, tenant_id: int, *,
                           include_network: bool = True) -> Dict[str, Any]:
    """Full storage breakdown for one tenant. ``total_bytes`` is canonical."""
    comps = _db_components(db, tenant_id)
    byo_targets = _byo_targets(db, tenant_id) if include_network else []
    _end_read_txn(db)  # no transaction held across the network calls below

    # Owner decision 09-29 (round 7, A.9): BYO-S3 storage is not MOD's to bill
    # or cap. `total_bytes` (== `tenants.storage_used_bytes` after a write) is
    # deliberately built ONLY from house-bucket-backed components; `byo_bytes`
    # (below, network-only) is never added in. Do not add it here.
    total = comps["blob_bytes"] + comps["unbacked_file_bytes"] + comps["unbacked_output_bytes"]
    result: Dict[str, Any] = {
        "tenant_id": tenant_id,
        **comps,
        "db_tracked_bytes": total,
        "total_bytes": total,
        "bucket": house_bucket_name(tenant_id),
        "bucket_bytes": None,
        "bucket_ok": False,
        "byo": [],
        "byo_bytes": None,
    }
    if not include_network:
        return result

    try:
        result["bucket_bytes"] = _list_bucket_bytes(_house_config(), result["bucket"])
        result["bucket_ok"] = True
    except Exception as exc:  # noqa: BLE001 — outage must not move the quota number
        logger.info("storage_measure: tenant %s bucket %s unreachable: %s",
                    tenant_id, result["bucket"], exc)

    now = time.monotonic()
    byo_total: Optional[int] = None
    for t in byo_targets:
        b = _measure_byo(t, now)
        result["byo"].append({"connection_id": t["connection_id"], "name": t["name"],
                              "bucket": t["bucket"], "is_active": t["is_active"], "bytes": b})
        if b is not None:
            byo_total = (byo_total or 0) + b
    # Visibility only (owner decision A.9) — `byo_bytes`/`byo` are reported on
    # the result dict for dashboards/admin views but are NEVER merged into
    # `total_bytes`/`db_tracked_bytes` above, so they never reach
    # `tenants.storage_used_bytes` (quota + billing never see BYO bytes).
    result["byo_bytes"] = byo_total
    return result


def storage_alert_level(used: Optional[int], cap: Optional[int]) -> str:
    """Pure threshold helper shared by the tenant + fleet storage endpoints.
    ``cap`` is None (no quota, e.g. on-prem unlimited plans) or non-positive
    (misconfigured) -> always "ok"; the percentage is undefined without a cap.
    80% -> "warn_80", 90% -> "crit_90" (crit wins at the boundary)."""
    if used is None or cap is None or cap <= 0:
        return "ok"
    pct = (used / cap) * 100
    if pct >= 90:
        return "crit_90"
    if pct >= 80:
        return "warn_80"
    return "ok"


_BY_WORKFLOW_SQL = text("""
    SELECT w.id AS workflow_id, w.name AS workflow_name, SUM(x.sz) AS bytes
    FROM (
        SELECT wr.workflow_id, wno.content_hash, MAX(wno.size_bytes) AS sz
        FROM workflow_node_outputs wno
        JOIN workflow_runs wr ON wr.id = wno.workflow_run_id
        WHERE wno.tenant_id = :tid
        GROUP BY wr.workflow_id, wno.content_hash
    ) x
    JOIN workflows w ON w.id = x.workflow_id
    GROUP BY w.id, w.name
    ORDER BY bytes DESC
    LIMIT :top_n
""")

_BY_RUN_SQL = text("""
    SELECT wr.id AS run_id, wr.workflow_id, wr.started_at, SUM(x.sz) AS bytes
    FROM (
        SELECT wno.workflow_run_id, wno.content_hash, MAX(wno.size_bytes) AS sz
        FROM workflow_node_outputs wno
        WHERE wno.tenant_id = :tid
        GROUP BY wno.workflow_run_id, wno.content_hash
    ) x
    JOIN workflow_runs wr ON wr.id = x.workflow_run_id
    GROUP BY wr.id, wr.workflow_id, wr.started_at
    ORDER BY wr.started_at DESC NULLS LAST, wr.id DESC
    LIMIT :top_n
""")

# by_type split: every blob classified by whether it's referenced by a
# workflow output (outputs), else by a file (uploaded), else neither
# (other — e.g. a blob only referenced by a node image or an orphan).
# Adding the two "unbacked" components from `_db_components` reconciles
# exactly to `total_bytes` — nothing is double-counted or dropped.
_BY_TYPE_SQL = text("""
    SELECT
      (SELECT COALESCE(SUM(cb.size_bytes), 0) FROM content_blobs cb
        WHERE cb.tenant_id = :tid AND EXISTS (
          SELECT 1 FROM workflow_node_outputs wno
          WHERE wno.tenant_id = cb.tenant_id AND wno.content_hash = cb.id)) AS output_blob_bytes,
      (SELECT COALESCE(SUM(cb.size_bytes), 0) FROM content_blobs cb
        WHERE cb.tenant_id = :tid AND NOT EXISTS (
          SELECT 1 FROM workflow_node_outputs wno
          WHERE wno.tenant_id = cb.tenant_id AND wno.content_hash = cb.id)
        AND EXISTS (
          SELECT 1 FROM files f
          WHERE f.tenant_id = cb.tenant_id AND f.deleted_at IS NULL
            AND f.storage_path = cb.storage_path)) AS uploaded_blob_bytes,
      (SELECT COALESCE(SUM(cb.size_bytes), 0) FROM content_blobs cb
        WHERE cb.tenant_id = :tid AND NOT EXISTS (
          SELECT 1 FROM workflow_node_outputs wno
          WHERE wno.tenant_id = cb.tenant_id AND wno.content_hash = cb.id)
        AND NOT EXISTS (
          SELECT 1 FROM files f
          WHERE f.tenant_id = cb.tenant_id AND f.deleted_at IS NULL
            AND f.storage_path = cb.storage_path)) AS other_blob_bytes
""")


def tenant_storage_breakdown(db: Session, tenant_id: int, *, top_n: int = 20) -> Dict[str, Any]:
    """DB-only breakdown on top of `measure_tenant_storage` (no network I/O,
    so this stays fast — megaplan Q.5a). `total_bytes` is the same canonical
    number; `by_type`/`by_workflow`/`by_run` all dedup by `content_hash` so
    the same physical blob referenced from two runs is counted once."""
    base = measure_tenant_storage(db, tenant_id, include_network=False)

    ty = db.execute(_BY_TYPE_SQL, {"tid": tenant_id}).mappings().one()
    by_type = {
        "outputs_bytes": int(ty["output_blob_bytes"] or 0) + base["unbacked_output_bytes"],
        "uploaded_bytes": int(ty["uploaded_blob_bytes"] or 0) + base["unbacked_file_bytes"],
        "other_bytes": int(ty["other_blob_bytes"] or 0),
    }

    by_workflow = [
        {"workflow_id": r["workflow_id"], "workflow_name": r["workflow_name"],
         "bytes": int(r["bytes"] or 0)}
        for r in db.execute(_BY_WORKFLOW_SQL, {"tid": tenant_id, "top_n": top_n}).mappings().all()
    ]
    by_run = [
        {"run_id": r["run_id"], "workflow_id": r["workflow_id"],
         "started_at": r["started_at"].isoformat() if r["started_at"] else None,
         "bytes": int(r["bytes"] or 0)}
        for r in db.execute(_BY_RUN_SQL, {"tid": tenant_id, "top_n": top_n}).mappings().all()
    ]

    base["by_type"] = by_type
    base["by_workflow"] = by_workflow
    base["by_run"] = by_run
    return base


def _worker_disk_entry(worker) -> Dict[str, Any]:
    """One fleet-view row from a WorkerLicense's `hardware_specs` JSON.
    Heartbeat disk fields are additive/optional (older worker binaries never
    sent them) — every field defaults to None rather than raising, and
    `percent_used`/`alert_level` are only computed when both total and free
    are present."""
    hw = worker.hardware_specs or {}
    disk_total_gb = hw.get("disk_total_gb")
    disk_free_gb = hw.get("disk_free_gb")
    percent_used = None
    alert_level = "ok"
    if disk_total_gb is not None and disk_free_gb is not None and disk_total_gb > 0:
        used_gb = disk_total_gb - disk_free_gb
        percent_used = used_gb / disk_total_gb * 100
        alert_level = storage_alert_level(used_gb, disk_total_gb)
    return {
        "worker_name": worker.worker_name,
        "hostname": worker.hostname,
        "disk_total_gb": disk_total_gb,
        "disk_free_gb": disk_free_gb,
        "images_bytes": hw.get("images_bytes"),
        "model_cache_bytes": hw.get("model_cache_bytes"),
        "workdirs_bytes": hw.get("workdirs_bytes"),
        "reclaimable_bytes": hw.get("reclaimable_bytes"),
        "percent_used": percent_used,
        "alert_level": alert_level,
    }


def fleet_storage_snapshot(db: Session, *, top_n: int = 20) -> Dict[str, Any]:
    """Platform-admin fleet view (megaplan Q.5a): per-worker disk fields from
    the latest heartbeat, the manager-host snapshot, and the top tenants by
    storage_used_bytes. DB-only — no network calls, no per-tenant network
    bucket listing."""
    import models
    from utils.paid_import import optional_paid
    from services.manager_disk import measure_manager_disk

    # Paid-only: worker list is empty when the module is absent on CE.
    _crud = optional_paid("crud.tenant_identity")
    workers = [
        _worker_disk_entry(w) for w in (_crud.licensed_workers(db) if _crud is not None else [])
    ]

    tenant_rows = (
        db.query(models.Tenant.id, models.Tenant.name, models.Tenant.storage_used_bytes)
        .order_by(models.Tenant.storage_used_bytes.desc().nullslast())
        .limit(top_n)
        .all()
    )
    top_tenants = [
        {"tenant_id": r[0], "tenant_name": r[1], "storage_used_bytes": int(r[2] or 0)}
        for r in tenant_rows
    ]

    return {
        "workers": workers,
        "manager": measure_manager_disk(db),
        "top_tenants": top_tenants,
    }


def worker_owner_tenant_ids(db: Session) -> set[int]:
    """FX.67.s1 — tenants that own a licensed worker: the fleet view reports
    that worker's disk rows, so the admin-storage router checks the lockbox
    gate against these too (the top_tenants ids come from the snapshot)."""
    import models
    return {
        int(tenant_id)
        for (tenant_id,) in db.query(models.WorkerLicense.owner_tenant_id).filter(
            models.WorkerLicense.deleted_at.is_(None),
        ).all()
        if tenant_id
    }


def recompute_all(db: Session, *, include_network: bool = True) -> Dict[str, int]:
    """Recompute ``tenants.storage_used_bytes`` for every tenant from
    ``measure_tenant_storage().total_bytes``. Measurement (incl. network) runs
    first with no open transaction; writes happen after, per tenant, so one bad
    tenant never blocks the rest. Failures are logged, never swallowed."""
    import models

    tenant_ids = [tid for (tid,) in db.query(models.Tenant.id).order_by(models.Tenant.id).all()]
    _end_read_txn(db)

    counts = {"tenants": len(tenant_ids), "measured": 0, "updated": 0,
              "unchanged": 0, "failed": 0, "bucket_unreachable": 0, "drift": 0}
    measured: Dict[int, Dict[str, Any]] = {}
    for tid in tenant_ids:
        try:
            m = measure_tenant_storage(db, tid, include_network=include_network)
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            logger.error("storage_measure: measuring tenant %s failed: %s", tid, exc, exc_info=True)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            continue
        measured[tid] = m
        counts["measured"] += 1
        if include_network and not m["bucket_ok"]:
            counts["bucket_unreachable"] += 1
        elif m["bucket_bytes"] is not None and m["bucket_bytes"] != m["blob_bytes"]:
            counts["drift"] += 1
            logger.info("storage_measure: tenant %s bucket drift: bucket=%s blobs=%s",
                        tid, m["bucket_bytes"], m["blob_bytes"])

    # Write phase — network I/O is finished.
    for tid, m in measured.items():
        try:
            tenant = db.query(models.Tenant).filter(models.Tenant.id == tid).first()
            if tenant is None:
                continue
            new_total = int(m["total_bytes"])
            if int(tenant.storage_used_bytes or 0) == new_total:
                counts["unchanged"] += 1
                continue
            tenant.storage_used_bytes = new_total
            db.commit()
            counts["updated"] += 1
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            logger.error("storage_measure: writing tenant %s failed: %s", tid, exc, exc_info=True)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    _end_read_txn(db)
    logger.info("storage_measure.recompute_all: %s", counts)
    return counts

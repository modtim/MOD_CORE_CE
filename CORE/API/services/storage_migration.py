"""BYO-S3 migrate-on-activate (WS4.Q17.16).

Orchestrates the copy-on-activate flow for
``routers/storage_connections.py::activate_storage_connection``:

- ``objects_to_migrate`` enumerates a tenant's content-addressed objects.
  ``models.ContentBlob.id`` IS the object's sha256 hash AND its storage key
  (see the ContentBlob docstring in models.py — "Pure SHA256 hash (no
  extension) in MinIO"), so it doubles as both the copy manifest and the
  A.6 "expected hash" source with no separate listing call needed.
- ``activate_with_migration`` is the router's entrypoint: zero objects
  activates immediately (``migration_status='verified'``); otherwise it
  arms ``migration_status='pending'`` and enqueues the copy task — the
  connection stays inactive until the worker verifies success.
- ``enqueue_migration`` inserts the ONE runless ``ApiTaskQueue`` row the Go
  worker drains (owner decision A.5: the copy itself is Go-worker-native,
  no container), following the same direct-insert pattern
  ``services/weights_mirror.py`` / ``services/cogs_shared_service.py`` use
  for runless worker tasks.
- ``complete_migration`` / ``fail_migration`` land the terminal state; they
  are called from the worker's ``/complete`` and ``/failed`` report
  handlers in ``routers/worker_tasks.py``.

ISO 27001 (no hard deletes): this module never deletes or touches objects
in the OLD backend. A migration only ever COPIES; the source bucket is left
exactly as it was, active connection or not.

GSEC R-088 / FX.32 (owner decision Q13=A) — the house-side duplicate:
copying to the tenant's BYO bucket leaves a full duplicate behind in the
house ``tenant-{id}-files`` bucket. After a VERIFIED migration (object count
plus per-object checksum/ETag/size match — ``verify_migration``), the house
copy is soft-deleted (the manifest rows stay; only the house bytes are
scheduled for destruction) and enqueued for the existing privacy-erasure
sweep, which purges them once ``storage.byo_house_copy_grace_days`` (default
30) has passed. No new deleter is built: the erasure path is the FX.15 sweep
(``services/scheduler.py::_run_privacy_erasure``), which stays report-only
for the tenant's own bucket and never touches a customer bucket. Every step
is audit-logged. A partial or failed verification schedules nothing.

FX.162.p5b: this module is explicitly EXEMPT from the FX.162 CE compliance
guard — it is api_allow in open-core/manifest.yaml ("Storage/HF (6/8)"), not
denied, so it ships and imports unconditionally in a CE build. Its own legal
hold check already goes through `utils.paid_import.optional_paid` (CE
fallback: no hold -> migration proceeds), so no further CE gating belongs
here. Covered by tests/test_fx162_p5b_ce_guard_harness.py.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

NODE_KIND = "storage_migration"
OUTPUT_NAMESPACE = "storage_migration"

NODE_ID_PREFIX = "storage-migration-"

#: The worker-task retry path rewrites output_namespace to ``attempt_<n>`` /
#: ``attempt_<n>_p<ts>`` (routers/worker_tasks.py), so a retried migration task
#: no longer carries OUTPUT_NAMESPACE. Found on a real S3 run: after one
#: requeue the credentials/complete/failed hooks and the status read stopped
#: recognising the task (connection stuck at "running", no creds minted).
_RETRY_NAMESPACE_RE = re.compile(r"^attempt_\d+(_p\d+)?$")


def is_migration_task(task: "models.ApiTaskQueue", payload: Optional[dict] = None) -> bool:
    """True only for a server-enqueued BYO-S3 copy task: standalone (no
    workflow/node run), ``node_kind=storage_migration``, our ``node_id``
    prefix, and either our output namespace or a retry rewrite of it. None of
    that is reachable from a tenant-created task (those always carry a run)."""
    if task.workflow_run_id is not None or task.node_run_id is not None:
        return False
    payload = payload if payload is not None else (task.task_payload or {})
    if payload.get("node_kind") != NODE_KIND:
        return False
    ns = task.output_namespace or ""
    if ns == OUTPUT_NAMESPACE:
        return True
    return bool(_RETRY_NAMESPACE_RE.match(ns)) and str(task.node_id or "").startswith(NODE_ID_PREFIX)


#: GSEC R-088 — grace window before the privacy-erasure sweep purges the
#: house-side duplicate of a verified BYO migration.
GRACE_SETTING = "storage.byo_house_copy_grace_days"
GRACE_DEFAULT_DAYS = 30

#: Audit event names (models.AuditAction values reused, none added).
VERIFIED_EVENT = models.AuditAction.CONTENT_TRANSFER_INTERNAL
SCHEDULED_EVENT = models.AuditAction.CONTENT_DELETE_INITIATED
PURGED_EVENT = models.AuditAction.CONTENT_PURGE_COMPLETED


def objects_to_migrate(db: Session, tenant_id: int) -> List[dict]:
    """Every content-addressed object this tenant currently owns, as
    ``{key, sha256, size_bytes}``. ``ContentBlob.id`` is both the sha256
    hash and the storage key, so this list is directly the copy manifest —
    no separate S3 LIST call against the source backend is needed."""
    rows = (
        db.query(models.ContentBlob.id, models.ContentBlob.size_bytes)
        .filter(models.ContentBlob.tenant_id == tenant_id)
        .all()
    )
    return [
        {"key": row.id, "sha256": row.id, "size_bytes": int(row.size_bytes or 0)}
        for row in rows
    ]


def _house_bucket(tenant_id: int) -> str:
    return f"tenant-{tenant_id}-files"


def _migration_spec(conn_id: int, source_bucket: str, dest_bucket: str,
                    objects: List[dict], skip_missing_source: bool) -> dict:
    """The nested spec the Go worker decodes (transport.StorageMigrationSpec).

    FIX-BYO-MIGRATION-SKIP-MISSING: the opt-in flag is added ONLY when true,
    so with the default (false) the payload — and therefore the whole
    fail-closed path — is byte-for-byte what it was before this task.
    """
    spec = {
        "storage_connection_id": conn_id,
        "source_bucket": source_bucket,
        "dest_bucket": dest_bucket,
        "objects": objects,
    }
    if skip_missing_source:
        spec["skip_missing_source"] = True
    return spec


def enqueue_migration(
    db: Session,
    conn: "models.StorageConnection",
    objects: List[dict],
    skip_missing_source: bool = False,
) -> "models.ApiTaskQueue":
    """Arm ``conn``'s migration bookkeeping and insert the ApiTaskQueue row
    the Go worker drains. The source side is whichever backend currently
    serves the tenant: a prior ACTIVE BYO connection if one exists (and
    isn't ``conn`` itself), else the house default ``tenant-{id}-files``.
    """
    from crud import storage_connection_cud as sc_crud

    source_conn = sc_crud.get_active_connection(db, tenant_id=conn.tenant_id)
    if source_conn is not None and source_conn.id == conn.id:
        # Re-activating the same connection (e.g. after a credential fix) —
        # there is no "prior backend" distinct from this one to copy from.
        source_conn = None

    source_connection_id = source_conn.id if source_conn is not None else None
    source_bucket = (
        source_conn.bucket if source_conn is not None else _house_bucket(conn.tenant_id)
    )

    conn.migration_status = "pending"
    conn.migration_started_at = None
    conn.migration_completed_at = None
    conn.migrated_object_count = 0
    conn.migrated_bytes = 0
    conn.migration_error = None

    task = models.ApiTaskQueue(
        workflow_run_id=None,  # runless worker task — belongs to no workflow run
        node_run_id=None,
        node_id=f"{NODE_ID_PREFIX}{conn.id}",
        tenant_id=conn.tenant_id,
        queue_name="CPU",
        priority=5,
        task_payload={
            "node_kind": NODE_KIND,
            "purpose": "storage_connection_migration",
            "dest_connection_id": conn.id,
            "source_connection_id": source_connection_id,
            "source_bucket": source_bucket,
            "dest_bucket": conn.bucket,
            "objects": objects,
            # The Go worker only takes its native copy branch when the payload
            # decodes `storage_migration` (transport.StorageMigrationSpec:
            # storage_connection_id / source_bucket / dest_bucket / objects).
            # Without this key the task falls through to the container path
            # and fails on harbor credentials. The flat keys above stay: the
            # completion/failure hooks and verify_migration read them.
            "storage_migration": _migration_spec(conn.id, source_bucket, conn.bucket, objects, skip_missing_source),
            # No container image, no download/upload sidecar — the Go
            # worker runs this natively (owner decision A.5).
            "skip_download": True,
            "skip_upload": True,
        },
        status=models.ApiTaskStatus.PENDING,
        max_attempts=3,
        output_namespace=OUTPUT_NAMESPACE,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    db.refresh(conn)

    # Best-effort worker wake-up, AFTER the commit (same pattern as
    # weights_mirror._enqueue_mirror_task) so the network call never runs
    # with an open transaction.
    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:  # noqa: BLE001 — wake-up must never fail the enqueue
        pass

    logger.info(
        "storage_migration: enqueued task %d for connection %d (tenant %d), %d object(s) "
        "from %s to %s",
        task.id, conn.id, conn.tenant_id, len(objects), source_bucket, conn.bucket,
    )
    return task


def activate_with_migration(
    db: Session,
    conn: "models.StorageConnection",
    skip_missing_source: bool = False,
) -> "models.StorageConnection":
    """Entrypoint for ``POST /storage-connections/{id}/activate``.

    Zero objects: nothing to move, activate immediately with
    ``migration_status='verified'``. Otherwise: arm the migration and
    enqueue the copy job; ``is_active`` stays False until the worker's
    completion hook (``complete_migration``) verifies success.
    """
    from crud import storage_connection_cud as sc_crud

    objects = objects_to_migrate(db, conn.tenant_id)
    if not objects:
        now = datetime.now(timezone.utc)
        conn.migration_status = "verified"
        conn.migration_started_at = now
        conn.migration_completed_at = now
        conn.migrated_object_count = 0
        conn.migrated_bytes = 0
        conn.migration_error = None
        db.commit()
        db.refresh(conn)
        return sc_crud.activate_connection(db, conn=conn)

    enqueue_migration(db, conn, objects, skip_missing_source=skip_missing_source)
    return conn


def migration_status_for_connection(
    db: Session, conn: "models.StorageConnection",
) -> dict:
    """The read-only migration bookkeeping for a connection, shaped for
    ``GET /storage-connections/{id}/migration-status`` (WS4.Q17.17).

    ``total_objects`` is the manifest size of the enqueued copy task (the
    number of objects the worker was asked to copy, from the task payload) —
    the latest ``storage_migration`` task row for this connection, regardless
    of that task's terminal status, so the numbers keep making sense after a
    run completes or fails. Null when no copy task exists (never migrated, or
    a zero-object immediate activation that never enqueued one).
    """
    row = (
        db.query(models.ApiTaskQueue)
        .filter(
            models.ApiTaskQueue.tenant_id == conn.tenant_id,
            models.ApiTaskQueue.node_id == f"{NODE_ID_PREFIX}{conn.id}",
        )
        .order_by(models.ApiTaskQueue.id.desc())
        .first()
    )
    total_objects = None
    if row is not None:
        payload = row.task_payload or {}
        objects = payload.get("objects")
        if isinstance(objects, list):
            total_objects = len(objects)
    # FIX-BYO-MIGRATION-SKIP-MISSING — make the skipped count OBVIOUS in the
    # response: missing_source_count is the TRUE total (uncapped), missing_
    # source_keys lists up to MISSING_KEY_REPORT_CAP of them, and
    # skipped_missing is the opt-in flag this migration ran with. A
    # `verified` migration with skipped_missing=true and
    # migrated_object_count < total_objects is exactly "verified, but N
    # source objects were already gone" — never a silent data loss.
    skip_opt_in = False
    skip = {"missing_source_count": 0, "missing_source_keys": []}
    if row is not None:
        skip_opt_in = bool(((row.task_payload or {}).get("storage_migration") or {}).get("skip_missing_source"))
        if skip_opt_in:
            skip = _skip_report(row)
    return {
        "connection_id": conn.id,
        "is_active": bool(conn.is_active),
        "migration_status": conn.migration_status or "none",
        "migration_started_at": conn.migration_started_at,
        "migration_completed_at": conn.migration_completed_at,
        "migrated_object_count": conn.migrated_object_count or 0,
        "migrated_bytes": conn.migrated_bytes or 0,
        "migration_error": conn.migration_error,
        "total_objects": total_objects,
        "skipped_missing": skip_opt_in,
        "missing_source_count": skip["missing_source_count"],
        "missing_source_keys": skip["missing_source_keys"],
    }


#: FIX-BYO-MIGRATION-SKIP-MISSING — cap on how many skipped keys the status
#: response lists (the counter is uncapped). Mirrors the Go worker's
#: storageMigrationMissingKeyCap.
MISSING_KEY_REPORT_CAP = 50


def _skip_report(task: "models.ApiTaskQueue") -> dict:
    """The worker's skip report for a migrate-on-activate task, normalized.

    ``result_payload["storage_migration_report"]`` is the flat field the Go
    worker adds to the completion body when it skipped missing SOURCE keys
    (transport.StorageMigrationReport). Absent on every other task and on
    old workers -> ``{"missing_source_count": 0, "missing_source_keys": []}``
    so every pre-existing path behaves byte-for-byte as before.
    """
    raw = None
    try:
        raw = (task.result_payload or {}).get("storage_migration_report")
    except Exception:  # noqa: BLE001 — a malformed report must not raise
        raw = None
    if not isinstance(raw, dict):
        return {"missing_source_count": 0, "missing_source_keys": []}
    try:
        count = int(raw.get("missing_source_count") or 0)
    except (TypeError, ValueError):
        count = 0
    keys = raw.get("missing_source_keys")
    if not isinstance(keys, list):
        keys = []
    keys = [str(k) for k in keys if isinstance(k, str)][:MISSING_KEY_REPORT_CAP]
    return {"missing_source_count": max(count, 0), "missing_source_keys": keys}


def complete_migration(db: Session, task: "models.ApiTaskQueue") -> None:
    """Worker reported COMPLETED for a storage_migration task: sanity-check
    the reported counts and flip the connection active. Called from
    ``routers/worker_tasks.py::report_task_complete`` (best-effort — never
    raises into that handler's response)."""
    from crud import storage_connection_cud as sc_crud

    payload = task.task_payload or {}
    # BYO move-out (IMPL-BYO-MOVE-OUT-A-API): the SAME worker hook lands here
    # for a move-out chunk task (same node_kind/namespace, so the retry
    # recognition keeps working). Route it to the move-out accounting, which
    # NEVER activates a connection and NEVER schedules the house-copy erasure
    # (the house bucket is the DESTINATION there — design s5).
    if payload.get("move_out"):
        return complete_move_out(db, task)
    conn = sc_crud.get_connection(
        db, conn_id=payload.get("dest_connection_id"), tenant_id=task.tenant_id,
    )
    if conn is None:
        logger.error(
            "storage_migration: task %d completed but connection %s not found for tenant %d",
            task.id, payload.get("dest_connection_id"), task.tenant_id,
        )
        return

    # FIX-BYO-MIGRATION-SKIP-MISSING — the worker's skip report (flat
    # "storage_migration_report" on the completion body, same idiom as
    # hf_cache_report) says which SOURCE keys it found gone and, because the
    # activation opted in, skipped. A skipped key is NOT verified and NOT
    # copied, so it must not count toward migrated_object_count — but it no
    # longer fails the migration either. Absent report / flag false = the
    # fail-closed check below is exactly as before.
    # FIX-BYO-MIGRATION-SKIP-MISSING — the worker's skip report (flat
    # "storage_migration_report" on the completion body, same idiom as
    # hf_cache_report) says which SOURCE keys it found gone and, because the
    # activation opted in, skipped. A skipped key is NOT verified and NOT
    # copied, so it must not count toward migrated_object_count — but it no
    # longer fails the migration either. Absent report / flag false = the
    # fail-closed check below is exactly as before.
    skip_report = _skip_report(task)
    skip_opt_in = bool((payload.get("storage_migration") or {}).get("skip_missing_source"))
    expected = len(payload.get("objects") or [])
    # A report can never claim more skips than the manifest had: clamp before
    # it feeds the arithmetic, so a rogue/oversized count cannot turn a short
    # migration into a "verified" one.
    missing_count = min(max(skip_report["missing_source_count"], 0), expected) if skip_opt_in else 0
    actual = conn.migrated_object_count or 0
    if actual < expected - missing_count:
        # Never trust a "success" that under-reports what it was asked to
        # migrate — a dropped/short progress report must not silently
        # activate a connection that's missing objects.
        conn.migration_status = "failed"
        conn.migration_error = (
            f"migration reported complete but only {actual}/{expected} objects "
            "were confirmed migrated"
        )
        conn.migration_completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.error(
            "storage_migration: task %d under-reported (%d/%d objects) — marked failed",
            task.id, actual, expected,
        )
        return

    conn.migration_status = "verified"
    conn.migration_completed_at = datetime.now(timezone.utc)
    conn.migration_error = None
    db.commit()
    db.refresh(conn)
    sc_crud.activate_connection(db, conn=conn)

    # GSEC R-088 — per-object verification, then the house-copy erasure
    # schedule. Best-effort: a verification failure leaves the connection
    # active (the copy is what the tenant is reading) but records the error and
    # schedules nothing, so the house duplicate stays until it is re-verified.
    try:
        report = verify_migration(db, task)
        if report.get("ok"):
            schedule_house_copy_erasure(db, task, report)
        else:
            conn.migration_error = report.get("error") or (
                f"per-object verification incomplete: {report.get('checked')}/"
                f"{report.get('expected')} objects verified"
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 — must not raise into the report handler
        logger.exception("storage_migration: R-088 verification failed for task %d: %s", task.id, exc)

    logger.info(
        "storage_migration: task %d verified (%d objects, %d bytes) — connection %d activated for tenant %d",
        task.id, actual, conn.migrated_bytes or 0, conn.id, conn.tenant_id,
    )


def grace_days_for(db: Session) -> int:
    """GSEC R-088 — the house-copy grace window (platform setting, default 30)."""
    from services import platform_settings as _psr
    try:
        return int(_psr.get_setting(GRACE_SETTING, db=db) or GRACE_DEFAULT_DAYS)
    except Exception:  # noqa: BLE001 — documented default must survive a lookup blip
        return GRACE_DEFAULT_DAYS


def _audit(db: Session, tenant_id: int, action: models.AuditAction, details: dict) -> None:
    """Append one audit row (ids/counts only — no object content). Best-effort:
    an audit failure must not undo the migration state it describes."""
    try:
        db.add(models.AuditEvent(tenant_id=tenant_id, event_name=action.value, details=details))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("storage_migration: audit write failed (%s): %s", action.value, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def verify_migration(
    db: Session,
    task: "models.ApiTaskQueue",
    per_object: Optional[List[dict]] = None,
) -> dict:
    """GSEC R-088 — per-object verification of a completed BYO copy.

    ``per_object`` is the worker's per-object report
    (``{key, size_bytes, checksum}`` — checksum is the SHA-256 / ETag the
    destination returned). When it is absent (an older worker that only
    reports cumulative counts) the destination is re-read here, one StatObject
    per manifest key, against the tenant's own backend.

    Expected values come from the copy manifest, which is the ContentBlob
    manifest: ``ContentBlob.id`` IS the object's sha256 and its storage key,
    so the manifest is both the copy list and the expected-hash source.

    The report lands in ``task_payload['verification_report']`` — the same
    JSONB the migration state already lives in — and is returned. A partial or
    failed verification schedules nothing and logs a clear error.
    """
    payload = task.task_payload or {}
    manifest = payload.get("objects") or []
    dest_bucket = payload.get("dest_bucket")
    if not manifest:
        return {"ok": False, "error": "no copy manifest on the task", "checked": 0}

    client = None
    if per_object is None:
        from utils import storage as ustorage
        try:
            cfg = ustorage.resolve_backend(task.tenant_id, db)
            client = ustorage.build_client_for_config(cfg)
        except Exception as exc:  # noqa: BLE001 — verification must fail loudly, not silently
            return {"ok": False, "error": f"destination backend unreachable: {exc}", "checked": 0}

    # FIX-BYO-MIGRATION-SKIP-MISSING — keys the worker reported as MISSING
    # ON THE SOURCE (and the activation opted in to skipping) are neither
    # verified nor a failure: they are counted separately and never enter
    # `checked`, so a skipped key can never satisfy the "everything verified"
    # bar. Flag false / old worker (no report) -> this set is empty and the
    # loop below is byte-for-byte the old behaviour.
    skip_opt_in = bool((payload.get("storage_migration") or {}).get("skip_missing_source"))
    skip = _skip_report(task) if skip_opt_in else {"missing_source_count": 0, "missing_source_keys": []}
    missing_keys = set(skip["missing_source_keys"])
    missing_count = min(skip["missing_source_count"], len(manifest))

    reported = {r.get("key"): r for r in (per_object or [])}
    mismatches: List[dict] = []
    checked = 0
    for obj in manifest:
        key = obj.get("key")
        if key in missing_keys:
            continue
        want_size = int(obj.get("size_bytes") or 0)
        want_sha = (obj.get("sha256") or "").lower()
        got = reported.get(key)
        if got is None and client is not None:
            try:
                info = client.stat_object(dest_bucket, key)
                # NOT the ETag: an S3 ETag is an MD5 (or "<md5>-<parts>" for a
                # multipart upload), never a SHA-256, so comparing it to the
                # manifest hash failed every object on a real S3 run. The
                # worker stamps the verified SHA-256 as x-amz-meta-sha256 on
                # every object it writes (storage_migration.go); read that.
                # A backend that drops user metadata yields "" here, which the
                # check below treats as size-only (the worker already re-read
                # and re-hashed the destination bytes).
                meta = getattr(info, "metadata", None) or {}
                got = {
                    "key": key,
                    "size_bytes": int(getattr(info, "size", 0) or 0),
                    "checksum": str(
                        meta.get("x-amz-meta-sha256")
                        or meta.get("X-Amz-Meta-Sha256")
                        or ""
                    ).strip('"').lower(),
                }
            except Exception as exc:  # noqa: BLE001 — a missing object IS a verification failure
                mismatches.append({"key": key, "error": f"dest object not found: {exc}"})
                continue
        if got is None:
            mismatches.append({"key": key, "error": "not reported by the worker"})
            continue
        got_size = int(got.get("size_bytes") or 0)
        got_sha = (got.get("checksum") or "").lower()
        if got_size != want_size:
            mismatches.append({"key": key, "error": f"size mismatch: want {want_size} got {got_size}"})
            continue
        if want_sha and got_sha and got_sha != want_sha:
            mismatches.append({"key": key, "error": f"checksum mismatch: want {want_sha} got {got_sha}"})
            continue
        checked += 1

    # FIX-BYO-MIGRATION-SKIP-MISSING — the bar is "every NON-missing object
    # verified". `expected` stays the manifest size (the tenant still owns
    # those rows); the skip counts are reported separately so nothing about
    # them is hidden. When the worker skipped MORE keys than the report lists
    # (cap 50) the unlisted keys cannot be attributed, so verification stays
    # fail-closed (a mismatch entry per unattributed key) — the migration
    # outcome the completion hook already decided is untouched, but the
    # house-copy erasure is not scheduled on an unverifiable manifest.
    ok = not mismatches and checked + missing_count == len(manifest)
    report = {
        "ok": ok,
        "checked": checked,
        "expected": len(manifest),
        "missing_source_count": missing_count,
        "missing_source_keys": skip["missing_source_keys"],
        "mismatches": mismatches[:50],
        "dest_bucket": dest_bucket,
        "house_bucket": _house_bucket(task.tenant_id),
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    task_payload = dict(payload)
    task_payload["verification_report"] = report
    task.task_payload = task_payload
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(task, "task_payload")
    db.commit()

    if not ok:
        logger.error(
            "storage_migration: verification FAILED for task %d (tenant %d): "
            "%d/%d objects verified, %d mismatch(es) — house copy NOT scheduled",
            task.id, task.tenant_id, checked, len(manifest), len(mismatches),
        )
        return report

    _audit(db, task.tenant_id, VERIFIED_EVENT, {
        "task_id": task.id,
        "connection_id": payload.get("dest_connection_id"),
        "dest_bucket": dest_bucket,
        "objects_verified": checked,
        "bytes_verified": sum(int(o.get("size_bytes") or 0) for o in manifest),
    })
    return report


def schedule_house_copy_erasure(
    db: Session,
    task: "models.ApiTaskQueue",
    report: dict,
) -> Optional[datetime]:
    """GSEC R-088 — after a VERIFIED migration, soft-delete the house-side
    duplicate and enqueue it for the existing privacy-erasure sweep with the
    grace date (``storage.byo_house_copy_grace_days``, default 30).

    No new deleter: the manifest rows stay (ISO 27001 — nothing is hard
    deleted); only the house bytes are scheduled, and the FX.15 sweep
    (`services/scheduler.py::_run_privacy_erasure`) is what destroys them.
    The tenant's own bucket is never touched. Scheduling state lives in the
    same JSONB the migration state already lives in.
    """
    if not report.get("ok"):
        logger.error(
            "storage_migration: house copy NOT scheduled for task %d — verification "
            "not clean (%d/%d)", task.id, report.get("checked", 0), report.get("expected", 0),
        )
        return None

    payload = task.task_payload or {}
    manifest = payload.get("objects") or []
    grace = grace_days_for(db)
    now = datetime.now(timezone.utc)
    due = now + timedelta(days=grace)

    task_payload = dict(payload)
    task_payload["house_copy_erasure"] = {
        "status": "scheduled",
        "house_bucket": _house_bucket(task.tenant_id),
        "objects": [o.get("key") for o in manifest],
        "bytes": sum(int(o.get("size_bytes") or 0) for o in manifest),
        "scheduled_at": now.isoformat(),
        "grace_days": grace,
        "erasure_due_at": due.isoformat(),
        "connection_id": payload.get("dest_connection_id"),
    }
    task.task_payload = task_payload
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(task, "task_payload")
    db.commit()

    _audit(db, task.tenant_id, SCHEDULED_EVENT, {
        "task_id": task.id,
        "connection_id": payload.get("dest_connection_id"),
        "house_bucket": _house_bucket(task.tenant_id),
        "objects_scheduled": len(manifest),
        "bytes_scheduled": sum(int(o.get("size_bytes") or 0) for o in manifest),
        "grace_days": grace,
        "erasure_due_at": due.isoformat(),
        "reason": "byo_migration_house_copy",
    })
    logger.info(
        "storage_migration: house copy for task %d (tenant %d) soft-deleted and "
        "scheduled for erasure on %s (grace %d days, %d object(s))",
        task.id, task.tenant_id, due.isoformat(), grace, len(manifest),
    )
    return due


def fail_migration(db: Session, task: "models.ApiTaskQueue", error_message: Optional[str]) -> None:
    """Worker reported FAILED for a storage_migration task: leave the
    connection inactive with the error recorded. Originals in the OLD
    backend are untouched — this job only ever copies, never deletes (ISO
    27001 no-hard-delete)."""
    from crud import storage_connection_cud as sc_crud

    payload = task.task_payload or {}
    # BYO move-out: the failure lands on the JOB row only — the source
    # connection is never touched (still ACTIVE, serving backend unchanged).
    if payload.get("move_out"):
        return fail_move_out(db, task, error_message)
    conn = sc_crud.get_connection(
        db, conn_id=payload.get("dest_connection_id"), tenant_id=task.tenant_id,
    )
    if conn is None:
        logger.error(
            "storage_migration: task %d failed but connection %s not found for tenant %d",
            task.id, payload.get("dest_connection_id"), task.tenant_id,
        )
        return
    conn.migration_status = "failed"
    conn.migration_error = (error_message or "")[:2000]
    conn.migration_completed_at = datetime.now(timezone.utc)
    conn.is_active = False
    db.commit()
    logger.warning(
        "storage_migration: task %d failed for connection %d (tenant %d): %s",
        task.id, conn.id, conn.tenant_id, conn.migration_error,
    )


def run_house_copy_erasure(db: Session) -> dict:
    """GSEC R-088 — the grace-period sweep, run by the existing privacy-erasure
    job (services/scheduler.py::_run_privacy_erasure).

    For every storage_migration task whose house copy was scheduled and whose
    ``storage.byo_house_copy_grace_days`` window has elapsed, destroy the
    HOUSE-side duplicate bytes only. This is the FX.15 mechanism, not a new
    deleter: the same ``utils.storage`` client + append-only erasure ledger +
    audit row the privacy-erasure sweep uses. Rows are never hard-deleted —
    the manifest and the schedule record stay; only bytes go. The tenant's own
    BYO bucket is never touched (BYO stays report-only), and legal holds fail
    closed exactly as the sweep's do.
    """
    from services import platform_settings as _psr
    if not _psr.get_setting("privacy.erasure_job_enabled", db=db):
        return {"enabled": False, "tenants": 0, "objects": 0, "bytes": 0}

    # FX.162.p5 — CE carve: legal holds are a paid compliance service, so the
    # hold lookup is lazy/guarded here (CE fallback = no holds, nothing skipped
    # by a hold; the erasure sweep itself is CE and still runs).
    from utils.paid_import import optional_paid
    _lhold = optional_paid("services.legal_hold")
    # 1788f — CE carve: services.erasure_ledger is a paid module, so CE has no
    # erasure ledger to write to. The purge itself is CE and still runs; the
    # audit event below is the only record, so CE never claims an erasure-ledger
    # entry that was never written.
    _erledger = optional_paid("services.erasure_ledger")
    from utils import storage as ustorage

    now = datetime.now(timezone.utc)
    tasks = (
        db.query(models.ApiTaskQueue)
        .filter(or_(
            models.ApiTaskQueue.output_namespace == OUTPUT_NAMESPACE,
            models.ApiTaskQueue.node_id.like(f"{NODE_ID_PREFIX}%"),  # retried rows
        ))
        .all()
    )
    out = {"enabled": True, "tenants": 0, "objects": 0, "bytes": 0}
    for task in tasks:
        payload = task.task_payload or {}
        sched = payload.get("house_copy_erasure")
        if not isinstance(sched, dict) or sched.get("status") != "scheduled":
            continue
        due_raw = sched.get("erasure_due_at")
        try:
            due = datetime.fromisoformat(due_raw)
        except (TypeError, ValueError):
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if due > now:
            continue

        tenant_id = task.tenant_id

        # The house copy is only a redundant duplicate while the tenant is
        # actually served from the BYO bucket. If the connection was
        # deactivated (or deleted) during the grace window, the house bucket
        # is the tenant's live backend again and these bytes are the ONLY
        # copy of anything not yet re-copied: never purge them.
        from crud import storage_connection_cud as _sc_crud
        _dest_id = sched.get("connection_id")
        _dest = (
            _sc_crud.get_connection(db, conn_id=_dest_id, tenant_id=tenant_id)
            if _dest_id is not None else None
        )
        if _dest_id is not None and (_dest is None or not _dest.is_active):
            sched = dict(sched)
            sched["status"] = "cancelled"
            sched["cancelled_at"] = now.isoformat()
            sched["cancel_reason"] = "dest_connection_not_active"
            task_payload = dict(payload)
            task_payload["house_copy_erasure"] = sched
            task.task_payload = task_payload
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "task_payload")
            db.commit()
            logger.warning(
                "byo_house_copy_erasure: tenant %s connection %s is not active — house "
                "copy erasure cancelled, house bytes preserved",
                tenant_id, sched.get("connection_id"),
            )
            continue

        try:
            # FX.162.p5 — CE carve: _lhold is None on CE, so no tenant is hold-
            # skipped here (the fail-closed except below is unchanged).
            if _lhold is not None and _lhold.is_held(db, tenant_id=tenant_id):
                logger.info(
                    "byo_house_copy_erasure: skipping tenant %s — legal hold active; "
                    "house bytes preserved", tenant_id,
                )
                continue
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.error(
                "byo_house_copy_erasure: hold lookup failed for tenant %s — failing "
                "closed (skipping): %s", tenant_id, exc,
            )
            continue

        house_bucket = sched.get("house_bucket") or _house_bucket(tenant_id)
        keys = [k for k in (sched.get("objects") or []) if k]
        cfg = ustorage.get_storage_config()
        client = ustorage.build_client_for_config(cfg)
        removed = 0
        nbytes = 0
        errors: List[str] = []
        try:
            for key in keys:
                try:
                    info = client.stat_object(house_bucket, key)
                    nbytes += int(getattr(info, "size", 0) or 0)
                    client.remove_object(house_bucket, key)
                    removed += 1
                except Exception as exc:  # noqa: BLE001 — one object must not abort the sweep
                    errors.append(f"{key}: {str(exc)[:200]}")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:200])

        sched = dict(sched)
        sched["status"] = "purged"
        sched["purged_at"] = now.isoformat()
        sched["objects_removed"] = removed
        sched["bytes_removed"] = nbytes
        sched["errors"] = errors[:20]
        task_payload = dict(payload)
        task_payload["house_copy_erasure"] = sched
        task.task_payload = task_payload
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(task, "task_payload")

        db.add(models.AuditEvent(
            tenant_id=tenant_id,
            event_name=PURGED_EVENT.value,
            details={
                "task_id": task.id,
                "bucket": house_bucket,
                "house_copy": True,
                "objects_removed": removed,
                "bytes_removed": nbytes,
                "grace_days": sched.get("grace_days"),
                "errors": errors[:20],
            },
        ))
        if _erledger is not None:
            try:
                _erledger.append_erasure(
                    db, subject_type="tenant", subject_id=tenant_id,
                    erased_at=now, scope="byo_house_copy", tenant_id=tenant_id,
                )
            except Exception as exc:  # noqa: BLE001 — a ledger failure must not abort the purge
                logger.error("byo_house_copy_erasure: ledger write failed for tenant %s: %s", tenant_id, exc)
        db.commit()

        out["tenants"] += 1
        out["objects"] += removed
        out["bytes"] += nbytes
        logger.info(
            "byo_house_copy_erasure: tenant %s house bucket %s purged (objects=%d bytes=%d)",
            tenant_id, house_bucket, removed, nbytes,
        )
    return out


# =============================================================================
# BYO move-out (IMPL-BYO-MOVE-OUT-A-API, design results-1110/DESIGN-BYO-
# MOVE-OUT-FLOW.md s2/s3/s5). The move-out is the SAME copy task with the
# direction inverted: source = the tenant's BYO bucket, dest = the house
# bucket (or another owned connection). It NEVER flips is_active, NEVER
# schedules the house-copy erasure (the house bucket is the DESTINATION
# there), and NEVER deletes a source object (soft delete only).
# =============================================================================

#: Manifest chunk size (design s5 "Large counts"): one ApiTaskQueue row per
#: 500 keys so a 100k-object tenant never builds one huge JSONB payload.
MOVE_OUT_CHUNK_SIZE = 500

#: node_id shape for a move-out chunk task: it keeps NODE_ID_PREFIX so the
#: existing worker hooks (routers/worker_tasks.py, not editable here) still
#: recognise a RETRIED row (whose output_namespace was rewritten to
#: attempt_<n>) via is_migration_task's prefix branch; the -mv<job>-c<chunk>
#: suffix keeps it distinct from the exact-match migrate-on-activate node_id
#: (migration_status_for_connection) and gives cancel/verify a deterministic
#: address per chunk.
def _move_out_node_id(conn_id: int, job_id: int, chunk: int) -> str:
    return f"{NODE_ID_PREFIX}{conn_id}-mv{job_id}-c{chunk}"


#: Task-status set that means "a move is still in flight" for the
#: move_out_already_running 409.
_MOVE_OUT_ACTIVE_STATUSES = (
    models.ApiTaskStatus.PENDING,
    models.ApiTaskStatus.ASSIGNED,
    models.ApiTaskStatus.CLAIMED,
    models.ApiTaskStatus.RUNNING,
)

_MOVE_OUT_JOB_ACTIVE_STATUSES = ("pending", "running")


def move_out_node_id(conn_id: int, job_id: int, chunk: int) -> str:
    """Public alias (tests/IMPL-C): the deterministic node_id of one chunk."""
    return _move_out_node_id(conn_id, job_id, chunk)


def move_out_manifest_for_connection(
    db: Session, conn: "models.StorageConnection",
) -> List[dict]:
    """The LIST-driven copy manifest for a move-out (design s5 CONCERN A):
    the PHYSICAL keys in the tenant's own bucket, not the content_blobs
    manifest — the guard and the move must measure the SAME set or the
    409 could never clear on a shared bucket. Foreign keys are copied too
    (they are the tenant's own data); there is no foreign-key scope: the
    deactivate guard later checks this SAME move-time LIST (including any
    foreign keys present at move time), so a foreign file left in a shared
    bucket keeps the deactivation guard (409) until the customer removes
    it. Raises (pin/transport) — the route decides the refusal."""
    from utils.storage import resolve_backend, byo_list_keys

    cfg = resolve_backend(conn.tenant_id, db)
    if not cfg.is_byo:
        raise ValueError("the tenant's own store is not resolvable")
    # Manifest rows keep the copy-task shape ({key, size_bytes?, sha256?}):
    # a LIST page carries the key (and size where the backend reports it);
    # the hash is unknown for foreign keys, so verification of those stays
    # key-existence-only (documented).
    return [{"key": k} for k in byo_list_keys(cfg, bucket=conn.bucket)]


def move_out_already_running(db: Session, conn: "models.StorageConnection") -> bool:
    """True while any move-out chunk task for this connection is still in
    flight (design s3: reuse the already-running idea, scoped to move-out
    node_ids so a migrate-on-activate never blocks a move-out)."""
    return (
        db.query(models.ApiTaskQueue)
        .filter(
            models.ApiTaskQueue.tenant_id == conn.tenant_id,
            models.ApiTaskQueue.node_id.like(f"{NODE_ID_PREFIX}{conn.id}-mv%"),
            models.ApiTaskQueue.status.in_(_MOVE_OUT_ACTIVE_STATUSES),
        )
        .count()
        > 0
    )


def enqueue_move_out(
    db: Session,
    conn: "models.StorageConnection",
    job,
    objects: List[dict],
    dest_bucket: str,
    dest_connection_id: Optional[int] = None,
    chunk: int = 0,
) -> "models.ApiTaskQueue":
    """One chunk task row, same wire shape the Go worker decodes
    (transport.StorageMigrationSpec), direction inverted: source_bucket is
    the BYO bucket, dest_bucket the house/target bucket. Carries the
    move-out markers (move_out/move_out_job_id) the completion/failure
    hooks branch on, and NEVER a house_copy_erasure schedule (s5: the
    house bucket is the destination here)."""
    task = models.ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=_move_out_node_id(conn.id, job.id, chunk),
        tenant_id=conn.tenant_id,
        queue_name="CPU",
        priority=5,
        task_payload={
            "node_kind": NODE_KIND,
            "purpose": "storage_connection_move_out",
            "move_out": True,
            "move_out_job_id": job.id,
            "move_out_connection_id": conn.id,
            "source_connection_id": conn.id,
            "source_bucket": conn.bucket,
            "dest_bucket": dest_bucket,
            "dest_connection_id": dest_connection_id,
            "objects": objects,
            "storage_migration": _migration_spec(
                conn.id, conn.bucket, dest_bucket, objects,
                skip_missing_source=False,
            ),
            "skip_download": True,
            "skip_upload": True,
        },
        status=models.ApiTaskStatus.PENDING,
        max_attempts=3,
        output_namespace=OUTPUT_NAMESPACE,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def start_move_out(
    db: Session,
    conn: "models.StorageConnection",
    target_bucket: str,
    dest_connection_id: Optional[int] = None,
):
    """POST /move-out body: LIST -> job row -> chunk tasks -> audit.
    Returns (job, first_task). The source stays ACTIVE the whole time
    (design s2.6: a failed move leaves the serving backend unchanged)."""
    from crud import storage_connection_cud as sc_crud

    manifest = move_out_manifest_for_connection(db, conn)
    chunks = [
        manifest[i:i + MOVE_OUT_CHUNK_SIZE]
        for i in range(0, len(manifest), MOVE_OUT_CHUNK_SIZE)
    ] or [[]]
    job = sc_crud.create_move_job(
        db, conn=conn, target_bucket=target_bucket,
        total_objects=len(manifest), chunks_total=len(chunks),
    )
    first_task = None
    for idx, chunk in enumerate(chunks):
        task = enqueue_move_out(
            db, conn=conn, job=job, objects=chunk,
            dest_bucket=target_bucket, dest_connection_id=dest_connection_id,
            chunk=idx,
        )
        if first_task is None:
            first_task = task
    _audit(db, conn.tenant_id, VERIFIED_EVENT, {
        "connection_id": conn.id,
        "source_bucket": conn.bucket,
        "target_bucket": target_bucket,
        "total_objects": len(manifest),
        "reason": "byo_move_out_started",
    })
    logger.info(
        "move-out: job %d started for connection %d (tenant %d), %d object(s) "
        "in %d chunk(s) from %s to %s",
        job.id, conn.id, conn.tenant_id, len(manifest), len(chunks),
        conn.bucket, target_bucket,
    )
    return job, first_task


def complete_move_out(db: Session, task: "models.ApiTaskQueue") -> None:
    """Worker reported COMPLETED for a move-out chunk: account the chunk on
    the job row; when every chunk is done the move is `verified`. NEVER
    activates a connection, NEVER schedules the house-copy erasure (the
    house bucket is the destination). Called from complete_migration's
    move-out branch (routers/worker_tasks.py stays unchanged)."""
    from crud import storage_connection_cud as sc_crud

    payload = task.task_payload or {}
    conn = sc_crud.get_connection(
        db, conn_id=payload.get("move_out_connection_id"), tenant_id=task.tenant_id,
    )
    job = sc_crud.get_move_job(
        db, conn=conn, job_id=payload.get("move_out_job_id"),
    ) if conn is not None else None
    if job is None:
        logger.error(
            "move-out: task %d completed but job %s/connection %s not found for tenant %d",
            task.id, payload.get("move_out_job_id"),
            payload.get("move_out_connection_id"), task.tenant_id,
        )
        return
    if job.status == "cancelled":
        logger.info(
            "move-out: task %d completed after job %d was cancelled; leaving the "
            "job cancelled (the user asked for it to stop).", task.id, job.id,
        )
        return

    manifest = payload.get("objects") or []
    skip = _skip_report(task)
    moved = max(len(manifest) - min(skip["missing_source_count"], len(manifest)), 0)
    job.chunks_done = (job.chunks_done or 0) + 1
    job.moved_object_count = (job.moved_object_count or 0) + moved
    job.moved_bytes = (job.moved_bytes or 0) + sum(
        int(o.get("size_bytes") or 0) for o in manifest)
    if job.chunks_done >= (job.chunks_total or 0):
        job.status = "verified"
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        _audit(db, task.tenant_id, VERIFIED_EVENT, {
            "connection_id": conn.id,
            "move_out_job_id": job.id,
            "source_bucket": payload.get("source_bucket"),
            "target_bucket": payload.get("dest_bucket"),
            "moved_object_count": int(job.moved_object_count or 0),
            "reason": "byo_move_out_verified",
        })
        logger.info(
            "move-out: job %d verified (%d/%d chunks, %d objects) for connection "
            "%d (tenant %d) — the source stays ACTIVE until the user confirms.",
            job.id, job.chunks_done, job.chunks_total,
            int(job.moved_object_count or 0), conn.id, task.tenant_id,
        )
    else:
        job.status = "running"
        db.commit()
        logger.info("move-out: chunk %d/%d done for job %d",
                    job.chunks_done, job.chunks_total, job.id)


def fail_move_out(db: Session, task: "models.ApiTaskQueue",
                  error_message: Optional[str]) -> None:
    """Worker reported FAILED for a move-out chunk: the JOB row records the
    failure only. The source connection is untouched (ACTIVE, serving
    backend unchanged) and no object anywhere is deleted — retry is the
    user's next move (design s2.6)."""
    from crud import storage_connection_cud as sc_crud

    payload = task.task_payload or {}
    conn = sc_crud.get_connection(
        db, conn_id=payload.get("move_out_connection_id"), tenant_id=task.tenant_id,
    )
    job = (sc_crud.get_move_job(db, conn=conn, job_id=payload.get("move_out_job_id"))
           if conn is not None else None)
    if job is None:
        logger.error("move-out: task %d failed but its job row is gone (task %d)",
                     task.id, task.id)
        return
    if job.status == "cancelled":
        return
    job.status = "failed"
    job.error = (error_message or "")[:2000]
    job.completed_at = datetime.now(timezone.utc)
    db.commit()
    logger.warning(
        "move-out: job %d failed for connection %d (tenant %d): %s — source "
        "left ACTIVE, nothing deleted.",
        job.id, conn.id, task.tenant_id, job.error,
    )


def move_out_mod_keys(db: Session, conn: "models.StorageConnection") -> Optional[List[str]]:
    """The MOD-key-scoped guard's key set (design s5 CONCERN A / owner
    default): the manifest of the tenant's LATEST VERIFIED move-out job, or
    None when no verified move exists (the deactivate guard then stays the
    whole-bucket check, byte-for-byte as before)."""
    from crud import storage_connection_cud as sc_crud

    job = sc_crud.get_move_job(db, conn=conn, status="verified")
    if job is None:
        return None
    keys: List[str] = []
    for chunk in range(int(job.chunks_total or 0)):
        row = (
            db.query(models.ApiTaskQueue)
            .filter(
                models.ApiTaskQueue.tenant_id == conn.tenant_id,
                models.ApiTaskQueue.node_id == _move_out_node_id(
                    conn.id, job.id, chunk),
            )
            .first()
        )
        objects = (row.task_payload or {}).get("objects") if row is not None else None
        if isinstance(objects, list):
            keys.extend(str(o.get("key")) for o in objects if o.get("key"))
    return keys


def byo_deactivate_guard_holds(db: Session, conn: "models.StorageConnection",
                               cfg) -> bool:
    """The ONE deactivate data-safety predicate, BYO-DEACTIVATE-BLOCK plus the
    BYO move-out owner default (design s5 CONCERN A / OWNER CALL):

    - no verified move-out yet -> the WHOLE-BUCKET check
      (``utils.storage.byo_holds_objects``), byte-for-byte as before;
    - a VERIFIED move-out exists -> the MOD-KEY-SCOPED check
      (``utils.storage.byo_mod_keys_present`` over that move's manifest):
      the customer's own foreign files become informational, so the 409
      clears after a completed move even on a shared bucket. A MOD key that
      is still physically at the source (e.g. an upload during the move)
      re-blocks with the same 409.

    Raises (pin/transport) — the CALLER decides fail-open/fail-closed, same
    contract as ``byo_holds_objects``.
    """
    from utils.storage import byo_holds_objects, byo_mod_keys_present

    keys = move_out_mod_keys(db, conn)
    if keys is None:
        return byo_holds_objects(cfg, bucket=conn.bucket)
    return byo_mod_keys_present(cfg, keys, bucket=conn.bucket)


def move_status_for_connection(db: Session, conn: "models.StorageConnection") -> dict:
    """GET /move-status payload: the job row (latest move for this
    connection) plus the skip report of its chunk tasks. Nulls/zeroes when
    no move was ever started (the route still answers 200: 'no move')."""
    from crud import storage_connection_cud as sc_crud

    job = sc_crud.get_move_job(db, conn=conn)
    out = {
        "connection_id": conn.id,
        "is_active": bool(conn.is_active),
        "move_status": job.status if job is not None else "none",
        "started_at": job.created_at if job is not None else None,
        "completed_at": job.completed_at if job is not None else None,
        "moved_object_count": int(job.moved_object_count or 0) if job is not None else 0,
        "moved_bytes": int(job.moved_bytes or 0) if job is not None else 0,
        "total_objects": int(job.total_objects or 0) if job is not None else None,
        "chunks_total": int(job.chunks_total or 0) if job is not None else 0,
        "chunks_done": int(job.chunks_done or 0) if job is not None else 0,
        "move_error": job.error if job is not None else None,
        "skipped_missing": False,
        "missing_source_count": 0,
        "missing_source_keys": [],
    }
    if job is None:
        return out
    # The skip report rides each chunk task's completion body; merge across
    # chunks (same uncapped-count + capped-key-list shape as migrate-
    # on-activate, FIX-BYO-MIGRATION-SKIP-MISSING).
    total_missing = 0
    listed: List[str] = []
    for chunk in range(int(job.chunks_total or 0)):
        row = (
            db.query(models.ApiTaskQueue)
            .filter(
                models.ApiTaskQueue.tenant_id == conn.tenant_id,
                models.ApiTaskQueue.node_id == _move_out_node_id(
                    conn.id, job.id, chunk),
            )
            .first()
        )
        if row is None:
            continue
        report = _skip_report(row)
        total_missing += report["missing_source_count"]
        for k in report["missing_source_keys"]:
            if len(listed) >= MISSING_KEY_REPORT_CAP:
                break
            if k not in listed:
                listed.append(k)
    out["missing_source_count"] = total_missing
    out["missing_source_keys"] = listed
    out["skipped_missing"] = total_missing > 0
    return out


def cancel_move_out(db: Session, conn: "models.StorageConnection"):
    """POST /move-out/cancel: stop the in-flight move. The job row goes to
    'cancelled' (history, never deleted — soft delete only) and every
    still-pending chunk task is cancelled; chunks already claimed by a
    worker finish their copy, which is harmless (the dest is MOD-owned and
    content-addressed). The connection itself is untouched (still ACTIVE).
    Returns the job, or None when nothing is running (route -> 409)."""
    from crud import storage_connection_cud as sc_crud

    job = sc_crud.get_move_job(db, conn=conn, statuses=_MOVE_OUT_JOB_ACTIVE_STATUSES)
    if job is None:
        return None
    job.status = "cancelled"
    job.completed_at = datetime.now(timezone.utc)
    job.error = "cancelled by user"
    for chunk in range(int(job.chunks_total or 0)):
        (
            db.query(models.ApiTaskQueue)
            .filter(
                models.ApiTaskQueue.tenant_id == conn.tenant_id,
                models.ApiTaskQueue.node_id == _move_out_node_id(
                    conn.id, job.id, chunk),
                models.ApiTaskQueue.status.in_(
                    (models.ApiTaskStatus.PENDING, models.ApiTaskStatus.ASSIGNED)),
            )
            .update({models.ApiTaskQueue.status: models.ApiTaskStatus.CANCELLED})
        )
    db.commit()
    _audit(db, conn.tenant_id, SCHEDULED_EVENT, {
        "connection_id": conn.id,
        "move_out_job_id": job.id,
        "reason": "byo_move_out_cancelled",
    })
    logger.info("move-out: job %d cancelled for connection %d (tenant %s)",
                job.id, conn.id, conn.tenant_id)
    return job

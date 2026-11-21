"""GSEC R-113 — temp-prefix sweep for tenant object-storage buckets.

The ``uploads/temp/`` and ``downloads/temp/`` prefixes in every tenant
bucket (``tenant-{id}-files``) hold transient staging objects: in-flight
browser uploads (multipart sessions + PUT'd temp objects) and generated
zip archives. Neither prefix had a lifecycle, so abandoned multipart
uploads and temp objects lived forever.

This sweep (daily, off-peak, platform setting gated) does, per tenant:

  1. aborts incomplete multipart uploads under the temp prefixes that were
     initiated more than ``storage.temp_sweep_max_age_hours`` ago;
  2. removes committed temp objects older than that age — but only ones
     NOT referenced by in-flight work: a non-terminal WorkflowRun
     (queued/running/pending_action/blocked) in the same tenant, or a
     non-terminal DownloadJob (pending/preparing/downloading/
     creating_zip/uploading) for the same tenant;
  3. skips tenants under an active legal hold (services.legal_hold).
     Fail closed: if the hold check itself errors, the tenant is skipped.

ISO 27001: temp objects are transient staging, not records, so removing
them is permitted — but only under the exact two temp prefixes (nothing
else in the bucket is ever touched) and only past the age floor. Every
sweep writes ONE append-only audit row per tenant
(models.AuditAction.ADMIN_SYSTEM_MAINTENANCE, resource_type
``temp_object_sweep``) carrying COUNTS + prefix only — never customer
file names.

No external (S3/storage) call is made inside an open DB transaction:
referencing DB reads close their own session before the storage client
is ever touched, and the audit row is written in a separate short
session afterwards.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TEMP_PREFIXES: Tuple[str, ...] = ("uploads/temp/", "downloads/temp/")

def _active_run_statuses():
    # WorkflowRun statuses that mean "in flight" — their zips/uploads must be
    # kept. Everything else (success/completed/failed/failure) is terminal.
    import models
    return (models.TaskStatus.QUEUED, models.TaskStatus.RUNNING,
            models.TaskStatus.PENDING_ACTION, models.TaskStatus.BLOCKED)


def _active_download_job_statuses():
    # DownloadJob statuses that mean the zip is still being built/uploaded.
    import models
    return (models.DownloadJobStatus.PENDING,
            models.DownloadJobStatus.PREPARING,
            models.DownloadJobStatus.DOWNLOADING,
            models.DownloadJobStatus.CREATING_ZIP,
            models.DownloadJobStatus.UPLOADING)


@dataclass
class TenantSweepResult:
    tenant_id: int
    bucket: str
    multipart_uploads_aborted: int = 0
    objects_removed: int = 0
    objects_checked: int = 0
    skipped: Optional[str] = None          # reason when nothing was swept
    error: Optional[str] = None            # per-tenant failure (never raises)


@dataclass
class SweepReport:
    max_age_hours: int = 0
    tenants_swept: int = 0
    tenants_skipped: int = 0
    tenants_errored: int = 0
    total_multipart_uploads_aborted: int = 0
    total_objects_removed: int = 0
    per_tenant: List[TenantSweepResult] = field(default_factory=list)


def _tenant_result(tenant_id: int, bucket: str, **kw) -> TenantSweepResult:
    return TenantSweepResult(tenant_id=tenant_id, bucket=bucket, **kw)


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _has_active_inflight_work(db, tenant_id: int) -> bool:
    """True when the tenant has a non-terminal WorkflowRun or a
    non-terminal DownloadJob (either keeps temp objects alive)."""
    import models
    run = (
        db.query(models.WorkflowRun.id)
        .filter(models.WorkflowRun.tenant_id == tenant_id,
                models.WorkflowRun.status.in_(_active_run_statuses()))
        .first()
    )
    if run is not None:
        return True
    dj = (
        db.query(models.DownloadJob.id)
        .filter(models.DownloadJob.tenant_id == tenant_id,
                models.DownloadJob.status
                .in_(_active_download_job_statuses()))
        .first()
    )
    return dj is not None


def _list_stale_multipart(client, bucket: str,
                          cutoff: datetime) -> List[Tuple[str, str]]:
    """(object_name, upload_id) pairs under the temp prefixes initiated
    before ``cutoff`` (naive-initiated_time treated as UTC)."""
    stale: List[Tuple[str, str]] = []
    for prefix in TEMP_PREFIXES:
        res = client._list_multipart_uploads(  # noqa: SLF001 — same private call used by the upload router
            bucket, prefix=prefix, max_uploads=1000)
        for u in (getattr(res, "uploads", None) or []):
            name = getattr(u, "object_name", None)
            up_id = getattr(u, "upload_id", None)
            if not name or not up_id or not name.startswith(prefix):
                continue
            initiated = _utc(getattr(u, "initiated_time", None))
            if initiated is None or initiated < cutoff:
                stale.append((name, up_id))
    return stale


def _sweep_tenant(tenant_id: int, client: Any, bucket: str,
                  cutoff: datetime, session_factory,
                  *, skip_tenant=None) -> TenantSweepResult:
    """Sweep one tenant's bucket. Never raises: any failure is captured
    in the result (the sweep keeps going for other tenants)."""
    res = _tenant_result(tenant_id, bucket)
    try:
        # (0) Legal hold — checked first, on its own closed DB session,
        # BEFORE any storage call. Fail closed: error => skip.
        if skip_tenant is not None and skip_tenant(tenant_id):
            res.skipped = "legal_hold"
            return res

        # (1) In-flight reference check — short DB session, closed before
        # any storage call (no external call inside an open transaction).
        db = session_factory()
        try:
            keep_all = _has_active_inflight_work(db, tenant_id)
        finally:
            db.close()
        if keep_all:
            res.skipped = "inflight_work"
            return res

        # (2) Multipart aborts (storage calls only).
        for name, up_id in _list_stale_multipart(client, bucket, cutoff):
            try:
                client._abort_multipart_upload(bucket, name, up_id)  # noqa: SLF001
                res.multipart_uploads_aborted += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "temp_sweep tenant=%s: abort multipart %s failed: %s",
                    tenant_id, up_id, e)

        # (3) Committed temp objects.
        for prefix in TEMP_PREFIXES:
            for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
                name = getattr(obj, "object_name", None)
                if not name or not name.startswith(prefix) or name.endswith("/"):
                    continue
                last_mod = _utc(getattr(obj, "last_modified", None))
                res.objects_checked += 1
                if last_mod is None or last_mod >= cutoff:
                    continue
                try:
                    client.remove_object(bucket, name)
                    res.objects_removed += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "temp_sweep tenant=%s: remove %s failed: %s",
                        tenant_id, prefix, e)
    except Exception as e:  # noqa: BLE001 — one tenant never kills the sweep
        res.error = str(e)[:300]
        logger.error("temp_sweep tenant=%s failed: %s", tenant_id, e)
    return res


def _audit_row(db, tenant_id: int, res: TenantSweepResult,
               max_age_hours: int) -> None:
    """One append-only row per tenant per sweep: counts + prefix only."""
    import audit
    import models
    from services.scheduler import _resolve_audit_actor_for_tenant
    actor = _resolve_audit_actor_for_tenant(db, tenant_id)
    if not actor:
        return  # mirrors the §11b GC convention: no actor, no row
    audit.log_resource_lifecycle_event(
        db=db,
        action=models.AuditAction.ADMIN_SYSTEM_MAINTENANCE,
        actor_user_id=actor,
        tenant_id=tenant_id,
        resource_type="temp_object_sweep",
        resource_id=0,
        metadata={
            "maintenance_type": "temp_object_sweep",
            "prefixes": list(TEMP_PREFIXES),
            "max_age_hours": max_age_hours,
            "bucket": res.bucket,
            "multipart_uploads_aborted": res.multipart_uploads_aborted,
            "objects_checked": res.objects_checked,
            "objects_removed": res.objects_removed,
            "skipped": res.skipped,
            "error": res.error,
        },
    )


def _default_session_factory():
    """Return a callable that yields a fresh, short-lived DB session.

    The scheduler path uses database.SessionLocal. Tests inject their own
    factory bound to the conftest session so no database.SessionLocal is
    ever called from a test."""
    from database import SessionLocal
    return SessionLocal


def run_temp_object_sweep(
    session_factory=None,
    client_factory=None,
) -> Dict[str, Any]:
    """One full sweep. Returns a report dict (JSON-safe).

    The scheduler tick calls this; the platform-setting gate is checked
    by the caller (services.scheduler) and re-checked here so a direct
    call is also a no-op when disabled.

    ``session_factory`` (callable -> DB session) and ``client_factory``
    (callable tenant_id -> (client, bucket_name)) are injectable for
    tests; by default they build real sessions/clients."""
    from services.platform_settings import get_setting
    if not get_setting("storage.temp_sweep_enabled"):
        return {"swept": 0, "skipped": 0, "errored": 0,
                "multipart_uploads_aborted": 0, "objects_removed": 0,
                "disabled": True}
    max_age_hours = max(
        24, int(get_setting("storage.temp_sweep_max_age_hours")))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    if session_factory is None:
        session_factory = _default_session_factory()

    import models

    report = SweepReport(max_age_hours=max_age_hours)
    # Tenant enumeration: short DB session, closed before storage.
    db = session_factory()
    try:
        tenant_ids = [
            int(t[0]) for t in
            db.query(models.Tenant.id).order_by(models.Tenant.id).all()
        ]
    finally:
        db.close()

    if client_factory is None:
        client_factory = _default_client_factory

    for tenant_id in tenant_ids:
        # GSEC R-088 — the reported bucket must be the tenant's effective
        # bucket (BYO-aware), not the hard-coded house one; the factory below
        # still wins when it resolves successfully.
        from utils.minio_client import get_tenant_bucket_name
        bucket = get_tenant_bucket_name(tenant_id)
        try:
            client, bucket = client_factory(tenant_id)
        except Exception as e:  # noqa: BLE001
            report.tenants_errored += 1
            report.per_tenant.append(
                _tenant_result(tenant_id, bucket, error=str(e)[:300]))
            continue
        res = _sweep_tenant(tenant_id, client, bucket, cutoff, session_factory,
                            skip_tenant=_make_skip_if_held(session_factory))
        report.per_tenant.append(res)
        if res.error:
            report.tenants_errored += 1
        elif res.skipped:
            report.tenants_skipped += 1
        else:
            report.tenants_swept += 1
        report.total_multipart_uploads_aborted += res.multipart_uploads_aborted
        report.total_objects_removed += res.objects_removed
        # Audit row: its own short session (no open txn around storage).
        try:
            adb = session_factory()
            try:
                _audit_row(adb, tenant_id, res, max_age_hours)
            finally:
                adb.close()
        except Exception as e:  # noqa: BLE001 — audit gap, not fatal
            logger.warning("temp_sweep tenant=%s: audit row failed: %s",
                           tenant_id, e)

    logger.info(
        "temp_sweep: swept=%d skipped=%d errored=%d aborted_mpu=%d "
        "removed_objects=%d (max_age_hours=%d)",
        report.tenants_swept, report.tenants_skipped, report.tenants_errored,
        report.total_multipart_uploads_aborted, report.total_objects_removed,
        max_age_hours)
    return {
        "swept": report.tenants_swept,
        "skipped": report.tenants_skipped,
        "errored": report.tenants_errored,
        "multipart_uploads_aborted": report.total_multipart_uploads_aborted,
        "objects_removed": report.total_objects_removed,
        "disabled": False,
    }


def _default_client_factory(tenant_id: int) -> Tuple[Any, str]:
    """Build the real (client, bucket) for a tenant via MinioClient."""
    from utils.minio_client import MinioClient
    mc = MinioClient(tenant_id=tenant_id)
    bucket = mc.bucket_name or f"tenant-{tenant_id}-files"
    return mc.client, bucket


def _make_skip_if_held(session_factory):
    """Fail-closed legal-hold gate: any error (including the lookup's own
    fail-closed True) => skip the tenant."""
    def _skip_if_held(tenant_id: int) -> bool:
        try:
            from utils.paid_import import optional_paid
            _lhold = optional_paid("services.legal_hold")
            if _lhold is None:  # FX.162.p5 — CE carve: no legal holds exist
                return False
            db = session_factory()
            try:
                return bool(_lhold.is_held(db, tenant_id=tenant_id))
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 — fail closed
            logger.error("temp_sweep tenant=%s: hold check failed — skipping "
                         "(fail closed): %s", tenant_id, e)
            return True
    return _skip_if_held

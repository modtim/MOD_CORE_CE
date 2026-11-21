"""add-workflow-triggers §6 — watch-trigger evaluators (file events, thresholds).

Match observed state/events against active triggers and run them through the
shared pipeline. `fire_file_event_triggers` serves BOTH delivery sources of the
one `file_event` type: S3 bucket notifications (source=s3, scope=bucket)
and the on-prem worker filesystem watcher (source=fs, scope=watched-path).
The legacy `source="minio"` id is accepted as an alias of `s3` (dual-read):
triggers persisted before the rename keep working, new code writes `s3`.
"""
from __future__ import annotations

import fnmatch

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')

# Storage-neutral source vocabulary. `s3` is canonical; `minio` is the legacy
# persisted value, read as an alias. Never a bare rename of persisted data.
SOURCE_S3 = "s3"
LEGACY_SOURCE_MINIO = "minio"


def _normalize_source(value: str | None) -> str:
    """Canonical source id: new name wins, legacy `minio` aliases to `s3`."""
    v = (value or SOURCE_S3)
    return SOURCE_S3 if v == LEGACY_SOURCE_MINIO else v


def _matches(cfg: dict, key: str) -> bool:
    if cfg.get("prefix") and not key.startswith(cfg["prefix"]):       # folder watch
        return False
    if cfg.get("suffix") and not key.endswith(cfg["suffix"]):         # file-type watch
        return False
    if cfg.get("glob") and not fnmatch.fnmatch(key, cfg["glob"]):     # pattern watch
        return False
    return True


def fire_file_event_triggers(db, *, source: str, scope: str, key: str,
                             event_type: str = "created") -> int:
    """Fire FILE_EVENT triggers matching (source, scope, key, event). `scope` is the
    bucket name for source=s3 (legacy alias: minio), or the watched path-root for
    source=fs. Returns the number dispatched. Never raises."""
    source = _normalize_source(source)
    from models import WorkflowTrigger, WorkflowTriggerType
    from services.triggers import pipeline
    fired = 0
    try:
        triggers = (db.query(WorkflowTrigger).filter(
            WorkflowTrigger.trigger_type == WorkflowTriggerType.FILE_EVENT,
            WorkflowTrigger.is_active.is_(True),
            WorkflowTrigger.deleted_at.is_(None)).all())
        for trig in triggers:
            cfg = trig.configuration or {}
            if _normalize_source(cfg.get("source", SOURCE_S3)) != source:
                continue
            scope_cfg = cfg.get("bucket") if source == SOURCE_S3 else cfg.get("path")
            if scope_cfg != scope:
                continue
            if cfg.get("event_type", "created") not in ("any", event_type):
                continue
            if not _matches(cfg, key):
                continue
            ctx = {
                "event_key": f"file:{source}:{scope}:{key}:{event_type}:{trig.id}",
                "file": {"source": source, "scope": scope, "key": key,
                         "event_type": event_type},
                "inputs": ({"object": {"source": source, "scope": scope, "key": key}}
                           if cfg.get("forward_object_path") else None),
            }
            try:
                if pipeline.evaluate_and_dispatch(db, trig, ctx).get("dispatched"):
                    fired += 1
            except Exception:  # noqa: BLE001
                logger.exception("file-event trigger %s fire failed", trig.id)
                try:
                    db.rollback()
                except Exception:
                    pass
    except Exception:  # noqa: BLE001
        logger.exception("fire_file_event_triggers failed (%s/%s)", source, scope)
    return fired


def emit_object_event(db, *, tenant_id: int, key: str,
                      event_type: str = "created") -> int:
    """API-side emission of a file_event — the replacement for MinIO bucket
    notifications (remove-minio-versitygw Gate 3, design D4).

    The API owns the authoritative object-mutation points (confirm-upload,
    delete, worker-output confirmation), so it emits the same `file_event` the
    `/triggers/minio` webhook consumer used to receive, in-process. This is
    backend-agnostic: it fires for versitygw and BYO-S3 too, where the storage
    backend will never call us back.

    `scope` is the tenant bucket name (`tenant-{id}-files`), matching how
    file_event triggers are configured (source=s3, legacy alias minio). `key` is the object's
    logical name (the user-facing filename), so suffix/glob matches behave as a
    user expects. Never raises."""
    if not tenant_id or not key:
        return 0
    try:
        from utils.minio_client import get_tenant_bucket_name
        scope = get_tenant_bucket_name(tenant_id, db)
    except Exception:  # noqa: BLE001
        logger.exception("emit_object_event: could not resolve bucket for tenant %s", tenant_id)
        return 0
    return fire_file_event_triggers(db, source=SOURCE_S3, scope=scope, key=key,
                                    event_type=event_type)


def normalize_s3_event(event_name: str) -> str:
    """Map a MinIO/S3 eventName to our event_type vocabulary."""
    en = (event_name or "").lower()
    if "objectcreated" in en:
        return "created"
    if "objectremoved" in en:
        return "deleted"
    if "objectaccessed" in en:
        return "accessed"
    return "modified"


# --- §6.4/§6.5 system / resource / observability thresholds ----------------

def _metric_queue_depth(db, trig) -> float:
    from models import ApiTaskQueue, ApiTaskStatus
    q = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status.notin_([ApiTaskStatus.COMPLETED, ApiTaskStatus.FAILED]))
    if trig.tenant_id:
        q = q.filter(ApiTaskQueue.tenant_id == trig.tenant_id)
    return float(q.count())


def _metric_workers_online(db, trig) -> float:
    from models import WorkerLicense, WorkerStatus
    return float(db.query(WorkerLicense).filter(
        WorkerLicense.status == WorkerStatus.LICENSED,
        WorkerLicense.deleted_at.is_(None)).count())


def _metric_failed_runs(db, trig) -> float:
    """Error-rate proxy (§6.5): failed runs in the configured window."""
    from datetime import datetime, timedelta, timezone
    from models import WorkflowRun, TaskStatus
    win = int((trig.configuration or {}).get("window_seconds", 300))
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=win)
    q = db.query(WorkflowRun).filter(WorkflowRun.status == TaskStatus.FAILED,
                                     WorkflowRun.completed_at >= cutoff)
    if trig.tenant_id:
        q = q.filter(WorkflowRun.tenant_id == trig.tenant_id)
    return float(q.count())


METRIC_SOURCES = {
    "queue_depth": _metric_queue_depth,
    "workers_online": _metric_workers_online,
    "failed_runs": _metric_failed_runs,
}


def _cmp_num(op: str, a: float, b: float) -> bool:
    return {"gt": a > b, "lt": a < b, "gte": a >= b, "lte": a <= b}.get(op, False)


def _armed(db, trig) -> bool:
    from models import TriggerEventLog
    row = (db.query(TriggerEventLog)
           .filter(TriggerEventLog.trigger_id == trig.id,
                   TriggerEventLog.event_key == "th-armed")
           .order_by(TriggerEventLog.id.desc()).first())
    return True if row is None else bool((row.state or {}).get("armed", True))


def _set_armed(db, trig, armed: bool) -> None:
    from models import TriggerEventLog
    row = (db.query(TriggerEventLog)
           .filter(TriggerEventLog.trigger_id == trig.id,
                   TriggerEventLog.event_key == "th-armed").first())
    if row is None:
        db.add(TriggerEventLog(trigger_id=trig.id, tenant_id=trig.tenant_id,
                               event_key="th-armed", state={"armed": armed}))
    else:
        row.state = {"armed": armed}
    db.commit()


def evaluate_system_threshold_triggers(db) -> int:
    """Evaluate SYSTEM_THRESHOLD triggers each scheduler tick. Fire on a not-breaching
    → breaching transition; re-arm only when the metric crosses back past `rearm_value`
    (hysteresis, so a metric hovering at the threshold doesn't flap). Returns count fired."""
    from models import WorkflowTrigger, WorkflowTriggerType
    from services.triggers import pipeline
    fired = 0
    triggers = (db.query(WorkflowTrigger).filter(
        WorkflowTrigger.trigger_type == WorkflowTriggerType.SYSTEM_THRESHOLD,
        WorkflowTrigger.is_active.is_(True),
        WorkflowTrigger.deleted_at.is_(None)).all())
    for trig in triggers:
        try:
            cfg = trig.configuration or {}
            fn = METRIC_SOURCES.get(cfg.get("metric"))
            if not fn:
                continue
            val = fn(db, trig)
            op, thr = cfg["op"], float(cfg["value"])
            breaching = _cmp_num(op, val, thr)
            armed = _armed(db, trig)
            if breaching and armed:
                ctx = {"event_key": f"th:{trig.id}",
                       "metric": {"name": cfg["metric"], "value": val, "threshold": thr},
                       "inputs": {"metric": cfg["metric"], "value": val}}
                if pipeline.evaluate_and_dispatch(db, trig, ctx).get("dispatched"):
                    fired += 1
                _set_armed(db, trig, False)
            elif not breaching and not armed:
                rearm = float(cfg.get("rearm_value", thr))
                reset = ((op in ("gt", "gte") and val < rearm)
                         or (op in ("lt", "lte") and val > rearm))
                if reset:
                    _set_armed(db, trig, True)
        except Exception:  # noqa: BLE001
            logger.exception("system_threshold trigger %s eval failed", trig.id)
            try:
                db.rollback()
            except Exception:
                pass
    return fired

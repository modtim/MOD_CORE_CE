"""GSEC FX.115 p2 — the lockdown breakage report (report-first, no enforcement).

FX.115 p1a/p1b gave the worker eight node-container lockdown knobs
(worker.node_lockdown_* — nonroot, cap-drop ALL, no-new-privileges, seccomp,
read-only rootfs + scratch tmpfs, and the pids/memory/cpu floors), each
off|report|enforce and defaulting to report. The worker already logs one
structured `lockdown-report` line per node container. This service turns that
same verdict — which the worker now rides back on the task result
(`lockdown_report`, the Go node_lockdown.go planMap) — into the answer the
OWNER needs before flipping a flag: for every node kind, which flags WOULD
change under enforce, which are already satisfied (the current fleet ships
most posture, so most flags are no-ops and the report must say so plainly),
which nodes are deliberately exempt, and how many nodes a flag would touch.

No live-container work happens here: the report is built from verdicts the
worker already produced. The optional dry-run trigger only ENQUEUES a
report-mode run of the system node set on one chosen worker (the same runless
system-task shape services/hf_cache_relayout.py uses); running it is the
worker's job, and tests use fakes.

Per (node kind, flag) the LATEST verdict wins in
models.NodeLockdownVerdict; every verdict ever reported also stays on its
task row (result_payload.lockdown_report), so the audit trail is the ledger
and this table is only the aggregation key.
"""
from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

NODE_KIND = "node_lockdown_report"
OUTPUT_NAMESPACE = "node_lockdown_report"
PURPOSE = "node_lockdown_report"
EVENT_NAME = "node_lockdown_report"
# The eight flags the layer reports, in the order planMap emits them, plus
# the scratch-tmpfs set the rootfs knob adds (reported, never a mode knob).
# FX.115 p4 adds the RUNTIME knob (hosted gVisor / runsc) to the same
# vocabulary: it is a mode knob like the others (off|report|enforce) and an
# exception row can relax it per node kind (`kind:gvisor` = that node stays
# on the daemon's default runtime). Its two fail-soft reasons — GPU nodes
# skipped (nvproxy) and runsc simply not registered on the host — ride the
# report as the extra `gvisor_note` / `gvisor_runtimes` keys, which the
# verdict row's `detail` carries so the owner sees WHY a node was not
# switched, not just that it was not.
FLAG_KEYS = (
    "nonroot", "capdrop", "noprivs", "seccomp", "rootfs",
    "limits_pids", "limits_memory", "limits_cpu", "gvisor",
)
SCRATCH_FLAG = "scratch_tmpfs"
# The per-flag suffixes planMap uses for each verdict triple.
_MODE, _MISSING, _WOULD, _APPLIED = "_mode", "_missing", "_would_apply", "_applied"
# FX.115 p4: the runtime knob's extra report keys (not a verdict triple).
_GVISOR_EXTRA = ("gvisor_runtime", "gvisor_note", "gvisor_runtimes")
# Online = licensed AND seen within this window (mirrors the model-cache
# registry's stale-heartbeat rule of thumb, same as hf_cache_relayout).
ONLINE_SEEN_WITHIN_MIN = 10


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _flag_verdicts(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Flatten one planMap payload into {flag: verdict}.

    A malformed/partial report yields whatever flags it does carry — never a
    crash, and never a verdict invented from a missing key (an absent flag
    means "not reported", which the caller must not record)."""
    if not isinstance(report, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for flag in FLAG_KEYS:
        mode = report.get(flag + _MODE)
        if mode is None:
            continue
        out[flag] = {
            "mode": mode,
            "missing": bool(report.get(flag + _MISSING, False)),
            "would_apply": bool(report.get(flag + _WOULD, False)),
            "applied": bool(report.get(flag + _APPLIED, False)),
        }
    # FX.115 p4: the runtime knob's fail-soft reason rides its verdict row's
    # detail, so "not switched" distinguishes gpu_skipped / runsc_unavailable
    # from "already on runsc". Absent keys stay absent (an older worker that
    # never reports gvisor produces no gvisor row at all, above).
    gv = out.get("gvisor")
    if gv is not None:
        extras = {k: report[k] for k in _GVISOR_EXTRA if k in report}
        if extras:
            gv["detail"] = extras
    if SCRATCH_FLAG in report:
        paths = report.get(SCRATCH_FLAG) or []
        out[SCRATCH_FLAG] = {
            "mode": report.get("rootfs" + _MODE, "report"),
            "missing": bool(paths),
            "would_apply": bool(paths),
            "applied": bool(paths),
            "detail": {"paths": list(paths)},
        }
    return out


def ingest_verdicts(db, task, report: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Persist one task's lockdown verdict into models.NodeLockdownVerdict.

    Latest-wins per (node kind, flag): the live row is UPDATED in place
    (never a row per task — the ledger already lives on the task rows), so
    re-running a node refreshes the same row and `last_seen_at` moves.
    Returns {"recorded": n, "skipped": m} so a test can pin that an empty or
    malformed report records nothing."""
    verdicts = _flag_verdicts(report)
    if not verdicts:
        return {"recorded": 0, "skipped": 0}
    kind = (report.get("node_kind") or "").strip()
    if not kind:
        # A verdict with no subject cannot be keyed; the log line still exists
        # but the aggregation would invent a bucket, so refuse it.
        return {"recorded": 0, "skipped": len(verdicts)}
    exempt = bool(report.get("exempt", False))
    exempt_reason = report.get("exempt_reason") or None
    seen = _now()
    recorded = 0
    for flag, v in verdicts.items():
        existing = (db.query(models.NodeLockdownVerdict)
                    .filter(models.NodeLockdownVerdict.node_kind == kind,
                            models.NodeLockdownVerdict.flag == flag,
                            models.NodeLockdownVerdict.deleted_at.is_(None))
                    .first())
        if existing is not None:
            existing.mode = v["mode"]
            existing.missing = v["missing"]
            existing.would_apply = v["would_apply"]
            existing.applied = v["applied"]
            existing.exempt = exempt
            existing.exempt_reason = exempt_reason
            existing.cap_value = _cap_for(report, flag)
            existing.detail = v.get("detail")
            existing.worker_name = task.claimed_by_worker
            existing.task_id = task.id
            existing.last_seen_at = seen
            recorded += 1
            continue
        db.add(models.NodeLockdownVerdict(
            node_kind=kind, flag=flag,
            mode=v["mode"], missing=v["missing"],
            would_apply=v["would_apply"], applied=v["applied"],
            exempt=exempt, exempt_reason=exempt_reason,
            cap_value=_cap_for(report, flag),
            detail=v.get("detail"),
            worker_name=task.claimed_by_worker,
            task_id=task.id, last_seen_at=seen,
        ))
        recorded += 1
    db.commit()
    return {"recorded": recorded, "skipped": len(verdicts) - recorded}


def _cap_for(report: Dict[str, Any], flag: str) -> Optional[int]:
    """The value the layer requested for this flag (0 = no floor requested)."""
    key = {"limits_pids": "limits_pids_cap",
           "limits_memory": "limits_memory_cap_mb",
           "limits_cpu": "limits_cpu_cap_cores"}.get(flag)
    if key is None:
        return None
    value = report.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# --- aggregation -----------------------------------------------------------

def _live_verdicts(db: Session) -> List[models.NodeLockdownVerdict]:
    return (db.query(models.NodeLockdownVerdict)
            .filter(models.NodeLockdownVerdict.deleted_at.is_(None))
            .all())


def report(db: Session) -> Dict[str, Any]:
    """The owner-facing report: per node kind -> flags needed / already
    satisfied / exempt, plus the enforce-preview "safe to enforce flag X:
    N nodes affected".

    "already satisfied" is the p1b note made visible: the current fleet ships
    most posture, so most flags report missing=false and enforcing them is a
    NO-OP — flipping those settings changes nothing on those nodes. A flag is
    only "affected" under enforce when would_apply is true (the layer would
    actually write something), which is what the owner is counting on."""
    per_kind: Dict[str, Dict[str, Any]] = {}
    flag_hits: Dict[str, Dict[str, int]] = {}
    # FX.115 p3: the DB exception rows, so an exempt flag can quote the
    # REASON the owner recorded (the verdict row only carries the machine
    # tag of which surface exempted it). Keyed (kind, flag) -> reason.
    reasons: Dict[Tuple[str, str], str] = {}
    for ex in _live_exceptions(db):
        reasons[(ex.node_kind, ex.flag)] = ex.reason
    for row in _live_verdicts(db):
        bucket = per_kind.setdefault(
            row.node_kind, {"flags_needed": [], "flags_satisfied": [],
                            "flags_exempt": [], "last_seen_at": None})
        entry = {
            "flag": row.flag,
            "mode": row.mode,
            "missing": bool(row.missing),
            "would_apply": bool(row.would_apply),
            "applied": bool(row.applied),
            "cap_value": row.cap_value,
            "detail": row.detail,
            "worker_name": row.worker_name,
            "last_seen_at": (row.last_seen_at.isoformat().replace("+00:00", "Z")
                             if row.last_seen_at else None),
        }
        if row.exempt:
            entry["exempt_reason"] = row.exempt_reason
            # FX.115 p3: the owner's recorded justification for this exact
            # (kind, flag) exemption, when the exception table has it — the
            # verdict row only names which surface exempted it, so the report
            # quotes the reason the admin wrote.
            entry["exception_reason"] = reasons.get(
                (row.node_kind, row.flag))
            bucket["flags_exempt"].append(entry)
        elif row.would_apply:
            bucket["flags_needed"].append(entry)
        else:
            bucket["flags_satisfied"].append(entry)
        if bucket["last_seen_at"] is None or (row.last_seen_at and
                                              row.last_seen_at > _parse(bucket["last_seen_at"])):
            bucket["last_seen_at"] = entry["last_seen_at"]
        hits = flag_hits.setdefault(row.flag, {"nodes": 0, "nodes_affected": 0,
                                               "nodes_exempt": 0})
        hits["nodes"] += 1
        if row.would_apply and not row.exempt:
            hits["nodes_affected"] += 1
        if row.exempt:
            hits["nodes_exempt"] += 1
    summary = []
    for flag, hits in sorted(flag_hits.items()):
        summary.append({
            "flag": flag,
            "nodes_seen": hits["nodes"],
            "nodes_affected": hits["nodes_affected"],
            "nodes_exempt": hits["nodes_exempt"],
            # "safe to enforce" = enforcing changes nothing on any node the
            # fleet has reported (every verdict is already-hardened or exempt).
            "safe_to_enforce": hits["nodes_affected"] == 0,
        })
    return {"node_kinds": per_kind, "flags": summary}


def _parse(iso: Optional[str]) -> datetime.datetime:
    """ISO string -> datetime for the last-seen max() above; epoch on None."""
    if not iso:
        return datetime.datetime(1970, 1, tzinfo=datetime.timezone.utc)
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return datetime.datetime(1970, 1, tzinfo=datetime.timezone.utc)


# --- dry-run trigger (enqueue only) ----------------------------------------

def system_node_kinds(db: Session) -> List[str]:
    """The system node set: distinct node_kind values on system NodeDefinitions
    (tenant_id NULL = system-shared). These are the nodes the owner wants
    covered before flipping a flag — the palette every tenant gets."""
    rows = (db.query(models.NodeDefinition)
            .filter(models.NodeDefinition.tenant_id.is_(None),
                    models.NodeDefinition.node_kind.isnot(None),
                    models.NodeDefinition.name.isnot(None))
            .all())
    out: List[str] = []
    for nd in rows:
        kind = (nd.node_kind or "").strip()
        if kind and kind not in out:
            out.append(kind)
    return sorted(out)


def online_workers(db: Session) -> List[models.WorkerLicense]:
    """Licensed workers seen within the freshness window (same rule as the
    HF re-layout's, so both dry-run jobs agree on who is up)."""
    cutoff = _now() - datetime.timedelta(minutes=ONLINE_SEEN_WITHIN_MIN)
    rows = db.query(models.WorkerLicense).filter(
        models.WorkerLicense.status == models.WorkerStatus.LICENSED,
        models.WorkerLicense.deleted_at.is_(None)).all()
    return [w for w in rows if w.last_seen_at and w.last_seen_at >= cutoff]


def enqueue_report_run(db: Session, worker_name: str, kinds: List[str]) -> Dict[str, int]:
    """One runless system task per node kind, pinned to ONE worker.

    ENQUEUE ONLY — nothing here runs anything. The task rides the same
    runless-system-task shape services/hf_cache_relayout.py uses (task_type
    'system' so the profiling gate never touches it, ASSIGNED + assigned_to_worker
    so only the named box can claim it, output_namespace so the report can find
    it back). The payload carries the node kind the executor will report on plus
    the report-mode marker; under the shipped defaults every lockdown knob is
    already 'report', so the run writes nothing to any container and its whole
    output is the verdict the report endpoint then reads.

    Returns {node_kind: task_id}."""
    enqueued: Dict[str, int] = {}
    for kind in kinds:
        task = models.ApiTaskQueue(
            workflow_run_id=None,
            node_run_id=None,
            node_id=f"lockdown-report-{worker_name}",
            tenant_id=1,  # platform system task
            queue_name="CPU",
            priority=5,
            task_type="system",
            task_payload={
                "node_kind": kind,
                "purpose": PURPOSE,
                "lockdown_report": {"report_only": True,
                                    "system_node_set": True},
                "skip_download": True,
                "skip_upload": True,
            },
            status=models.ApiTaskStatus.ASSIGNED,
            assigned_to_worker=worker_name,
            max_attempts=1,
            output_namespace=OUTPUT_NAMESPACE,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        enqueued[kind] = task.id

        # Best-effort worker wake-up, AFTER the commit so the Valkey call never
        # runs with an open transaction (same pattern as ml_retrain / weights_mirror).
        # Without it the mod-worker only finds this task on its next 30s poll.
        try:
            import redis

            r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
            r.publish(f"task:available:{task.queue_name}", "1")
        except Exception:  # noqa: BLE001 — wake-up must never fail the enqueue
            pass
    logger.info("node_lockdown_report: enqueued %d report-mode tasks for %s",
                len(enqueued), worker_name)
    return enqueued


def trigger(db: Session, worker_name: Optional[str]) -> Dict[str, Any]:
    """Trigger the dry run: pick the worker (or the first online one) and
    enqueue the system node set against it."""
    kinds = system_node_kinds(db)
    if worker_name:
        online = [w for w in online_workers(db) if w.worker_name == worker_name]
        if not online:
            return {"enqueued": {}, "worker": worker_name,
                    "reason": "worker_not_online", "total_tasks": 0}
    else:
        workers = online_workers(db)
        if not workers:
            return {"enqueued": {}, "worker": None,
                    "reason": "no_online_worker", "total_tasks": 0}
        worker_name = workers[0].worker_name
    enqueued = enqueue_report_run(db, worker_name, kinds)
    return {"enqueued": enqueued, "worker": worker_name,
            "total_tasks": len(enqueued), "reason": None}


def latest_task(db: Session, worker_name: str, kind: str) -> Optional["models.ApiTaskQueue"]:
    return (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.output_namespace == OUTPUT_NAMESPACE,
                    models.ApiTaskQueue.assigned_to_worker == worker_name,
                    models.ApiTaskQueue.task_payload["node_kind"].as_string() == kind)
            .order_by(models.ApiTaskQueue.id.desc())
            .first())


# --- FX.115 p3: per-node lockdown exceptions (reason REQUIRED) -------------

# The derived heartbeat key carrying the DB-backed exception list (the legacy
# worker.node_lockdown_exempt_nodes setting keeps working as a union fallback
# for one release; see routers/worker_management._resolve_worker_tunables).
EXEMPT_LIST_KEY = "worker.node_lockdown_exempt_list"
# The reason the worker stamps on a verdict relaxed by THIS list (the API-side
# report resolves the human-readable reason off the DB row itself).
EXEMPT_LIST_REASON = "exempt_list_setting"
# Every flag an exception row may name: the mode knobs (p1a's three + p1b's
# five + p4's runtime knob) + the scratch tmpfs set the rootfs knob adds
# (the same spelling node_lockdown_verdicts stores, so one row relaxes
# exactly one reported flag). FX.115 p4: `gvisor` is nameable, which is how
# a node kind stays on the daemon's default runtime (runc) while the rest of
# the fleet moves to runsc.
RELAXABLE_FLAGS = FLAG_KEYS + (SCRATCH_FLAG,)


def _live_exceptions(db: Session) -> List[models.NodeLockdownException]:
    """Rows not soft-deleted (expiry is resolved separately by the callers,
    so `exception_rows` can show an expired row as active=false instead of
    hiding it — the owner wants to see a stale exception, not lose it)."""
    return (db.query(models.NodeLockdownException)
            .filter(models.NodeLockdownException.deleted_at.is_(None))
            .all())


def _exception_active(row, now: datetime.datetime) -> bool:
    """A live row whose expiry has passed is IGNORED (not deleted) — the
    exception simply stops applying until the admin renews or revokes it."""
    return row.expires_at is None or row.expires_at >= now


def exception_rows(db: Session) -> List[Dict[str, Any]]:
    """The owner-facing list: live, unexpired exceptions with their reason,
    author, and expiry (the whole point of p3 — an exemption nobody can
    justify is exactly what a compliance audit must be able to see)."""
    now = _now()
    return [{"id": row.id,
            "node_kind": row.node_kind,
            "flag": row.flag,
            "reason": row.reason,
            "created_by_user_id": row.created_by_user_id,
            "created_at": (row.created_at.isoformat().replace("+00:00", "Z")
                           if row.created_at else None),
            "expires_at": (row.expires_at.isoformat().replace("+00:00", "Z")
                           if row.expires_at else None),
            "active": _exception_active(row, now)}
            for row in _live_exceptions(db)]


def create_exception(db: Session, node_kind: str, flag: str,
                     reason: Optional[str], *,
                     expires_at: Optional[datetime.datetime] = None,
                     user_id: Optional[int] = None) -> Dict[str, Any]:
    """Create (or refresh) the exception for (node_kind, flag).

    The reason is REQUIRED: blank/whitespace raises ValueError (the router
    turns that into a 400) because an exemption with no recorded justification
    is precisely the finding a compliance audit reports. The flag must be one
    the lockdown layer reports, and the node kind must be non-blank — a blank
    kind can never match the worker's exempt() anyway, so accepting it would
    mint an exception that does nothing and looks like one that does.

    Latest-wins: an existing live row for the pair is refreshed in place
    (reason/expiry/author move), never stacked, so the partial unique index
    on (node_kind, flag) WHERE deleted_at IS NULL keeps holding.
    """
    kind = (node_kind or "").strip()
    if not kind:
        raise ValueError("node_kind required")
    if (flag or "") not in RELAXABLE_FLAGS:
        raise ValueError(f"unknown flag {flag!r}")
    if not (reason or "").strip():
        raise ValueError("reason required")
    now = _now()
    existing = (db.query(models.NodeLockdownException)
                .filter(models.NodeLockdownException.node_kind == kind,
                        models.NodeLockdownException.flag == flag,
                        models.NodeLockdownException.deleted_at.is_(None))
                .first())
    if existing is not None:
        existing.reason = reason.strip()
        existing.created_by_user_id = user_id
        existing.expires_at = expires_at
        db.commit()
        return {"id": existing.id, "node_kind": kind, "flag": flag,
                "reason": existing.reason, "created": False}
    row = models.NodeLockdownException(
        node_kind=kind, flag=flag, reason=reason.strip(),
        created_by_user_id=user_id, expires_at=expires_at)
    db.add(row)
    db.commit()
    return {"id": row.id, "node_kind": kind, "flag": flag,
            "reason": row.reason, "created": True}


def revoke_exception(db: Session, exception_id: int,
                     *, user_id: Optional[int] = None) -> Dict[str, Any]:
    """SOFT delete (ISO 27001: no hard delete). The tombstone names its actor
    as well as the audit row, so the record stays attributable."""
    row = (db.query(models.NodeLockdownException)
           .filter(models.NodeLockdownException.id == exception_id,
                   models.NodeLockdownException.deleted_at.is_(None))
           .first())
    if row is None:
        return {"id": exception_id, "revoked": False}
    row.deleted_at = _now()
    row.deleted_by_user_id = user_id
    db.commit()
    return {"id": exception_id, "revoked": True,
            "node_kind": row.node_kind, "flag": row.flag}


def exempt_list_value(db: Session) -> str:
    """The DERIVED heartbeat value: legacy allow-list ∪ live DB exception rows.

    Entries are comma-separated: a bare `kind` (from the legacy
    worker.node_lockdown_exempt_nodes setting, which keeps working as a
    fallback/union for one release) and `kind:flag` per live, unexpired DB
    row. The worker's exempt() matches the bare forms exactly as before and
    its new flagExempt() arm reads the qualified forms; expiry is resolved
    HERE (an expired row is simply absent), so the worker never has to know
    what a date means. Returns "" when nothing is exempted (the key is then
    omitted from the heartbeat entirely, leaving an untouched fleet
    byte-for-byte unaffected).
    """
    out: List[str] = []
    legacy = _ps_get("worker.node_lockdown_exempt_nodes", db)
    for k in (legacy or "").split(","):
        k = k.strip()
        if k and k not in out:
            out.append(k)
    now = _now()
    for row in _live_exceptions(db):
        if not _exception_active(row, now):
            continue
        entry = f"{row.node_kind}:{row.flag}"
        if entry not in out:
            out.append(entry)
    return ",".join(out)


def _ps_get(key: str, db: Session) -> Any:
    from services import platform_settings as _ps
    try:
        return _ps.get_setting(key, db=db)
    except Exception:  # noqa: BLE001 — a missing row means "nothing exempt"
        return None

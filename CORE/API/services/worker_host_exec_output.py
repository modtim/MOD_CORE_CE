"""services/worker_host_exec_output.py -- hx.6 (opsx worker-host-exec).

Output ingest for host-exec jobs (worker-authenticated POST) plus the
building blocks for the live-tail SSE stream in routers/worker_host_exec_output.py.

Chunk objects live under `exec-output/{job_id}/{seq:08d}.{stream}`. Tenant
isolation (repo convention, tenant-{id}-files): a tenant-owned exec job
(job.tenant_id set) stores its chunks in that tenant's own bucket; only
platform/shared-pool jobs (tenant_id is None -- no owning tenant) fall back
to the `exec-output` system bucket. The total stored bytes per job are
capped by `worker.host_exec_max_output_bytes` (hx.2 setting): once the cap
is exceeded the job row gets output_truncated=True and further chunk data
is dropped -- the ingest still answers 200. Duplicate `seq` is idempotent
(the chunk object already exists -> nothing appended). On `final` the job
closes: status = succeeded iff exit_code == 0 else failed, exit_code +
finished_at are stamped and output_object_key records the chunk prefix.

Audit: one row is written per job, not per chunk -- appending a row to the
append-only audit table on every streamed chunk would flood it. A row is
only written when a chunk closes the job (final) or when a chunk first
pushes the job over the output cap (the truncation transition).

ISO 27001: no hard delete of job rows or output objects here (retention tooling
ages them out). Chunk DATA is never logged -- only ids, sizes and flags.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
import audit
from services import worker_host_exec_settings as settings
try:
    from services import task_output_scan as tos
except ImportError:
    tos = None

from product_names import themed_logger

logger = themed_logger(__name__, 'workers')

OUTPUT_BUCKET = "exec-output"
_CHUNK_PREFIX = "exec-output"

TERMINAL_STATUSES = ("succeeded", "failed", "timeout", "cancelled")

_POLL_INTERVAL_S = 1.0

# Fortress 3.5 (958a) — the host-exec ingest scans a chunk's bytes BEFORE the
# chunk object is stored, through the SAME scanner and the SAME posture key
# every other Fortress 3.5 path uses (`fortress.task_output_scan_mode`, off /
# shadow / enforce). `output_scan_blocked` mirrors executor.go:1348
# (outputScanBlockedReason) so the job's refusal reason reads like every other
# refused durable write. Chunk CONTENT is never logged -- codes/counts only.
OUTPUT_SCAN_BLOCKED_REASON = "output_scan_blocked"
OUTPUT_SCAN_MODE_KEY = "fortress.task_output_scan_mode"
_STREAM_MAX_SECONDS = 3600.0  # hx.6 hard cap: a live tail never runs past 1h


def get_output_client():
    """Object-storage seam. Tests monkeypatch ``utils.storage.get_service_client``
    so no real S3 call happens in the suite."""
    from utils.storage import get_service_client
    return get_service_client()


def _bucket_for_job(job: "models.WorkerHostExecJob") -> str:
    """Tenant-owned jobs (job.tenant_id set) get their own bucket -- the
    house default `tenant-{id}-files`, same convention as every other
    tenant-scoped object (utils/storage.py, services/file_processing.py).
    Shared/platform-pool jobs (tenant_id is None, no owning tenant) use the
    `exec-output` system bucket instead."""
    tenant_id = getattr(job, "tenant_id", None)
    return f"tenant-{tenant_id}-files" if tenant_id is not None else OUTPUT_BUCKET


def _prefix_for(job_id: int) -> str:
    return f"{_CHUNK_PREFIX}/{job_id}/"


def _chunk_key(job_id: int, seq: int, stream: str) -> str:
    return f"{_prefix_for(job_id)}{seq:08d}.{stream}"


def _seq_from_key(key: str) -> Optional[int]:
    try:
        return int(key.rsplit("/", 1)[-1].split(".", 1)[0])
    except ValueError:
        return None


def _stream_from_key(key: str) -> str:
    return key.rsplit(".", 1)[-1] if "." in key.rsplit("/", 1)[-1] else ""


def _ensure_bucket(client, bucket: str) -> None:
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def _object_absent(exc: Exception) -> bool:
    return getattr(exc, "code", "") in ("NoSuchKey", "NoSuchBucket")


# 1518b — CE has no scanner (`tos` fails to import), so host-exec output is
# never blocked on that edition. It must also never be stored as if it were
# a verified-clean scan: `_warn_scan_unavailable_once` logs the "no scanner
# on this edition" WARNING exactly once per job (never per chunk -- a
# chatty stream would otherwise flood the log with the same line), and
# `record_chunk` below records the closing/truncation audit row as
# scan_unavailable instead of omitting any trace.
_CE_SCAN_UNAVAILABLE_WARNED: set = set()
_CE_SCAN_UNAVAILABLE_WARNED_MAX = 4096  # bound: a long-lived CE process must
# not grow this set forever (one entry per host-exec job ever run); reset
# instead of tracking per-job TTLs -- an occasional repeat warning after a
# reset is harmless, unbounded growth is not.


def _warn_scan_unavailable_once(job_id) -> None:
    if job_id in _CE_SCAN_UNAVAILABLE_WARNED:
        return
    if len(_CE_SCAN_UNAVAILABLE_WARNED) >= _CE_SCAN_UNAVAILABLE_WARNED_MAX:
        _CE_SCAN_UNAVAILABLE_WARNED.clear()
    _CE_SCAN_UNAVAILABLE_WARNED.add(job_id)
    logger.warning(
        "host-exec output scanning unavailable for job %s: no scanner on "
        "this edition (CE) -- output is stored UNSCANNED, never verified "
        "clean",
        job_id,
    )


def _scan_chunk(db: Session, job: "models.WorkerHostExecJob", seq: int,
                payload: bytes) -> dict:
    """Fortress 3.5 (958a) — scan one chunk's bytes BEFORE they are stored.

    Reuses the EXISTING scanner (`services/task_output_scan.scan_task_result`)
    via its inline-bytes artefact path (`_serialise_artefacts` feeds a bytes
    value straight to `oss.scan_output`), so there is no second scanner and no
    new signature. Posture comes from the shared registry key
    `fortress.task_output_scan_mode` (off / shadow / enforce), resolved for
    the job's tenant exactly as every other Fortress 3.5 call site does.

    Never raises: a scanner/settings blow-up must not 500 the ingest, so it
    yields a not-blocked verdict carrying the exception TYPE only (same belt-
    and-braces shape as routers/worker_tasks.py:3136). Both the CE (`tos is
    None`) branch and the exception branch return `error: True` and NO
    `mode` key -- `record_chunk` treats them identically (1518b): never
    "blocked", but never silently "clean" either.
    """
    try:
        if tos is None:
            _warn_scan_unavailable_once(getattr(job, "id", None))
            return {"blocked": False, "error": True, "mode": "unavailable"}
        return tos.scan_task_result(
            db,
            tenant_id=getattr(job, "tenant_id", None),
            result={"output": payload},
        )
    except Exception as e:  # noqa: BLE001 -- a scan must never break ingest
        logger.warning(
            "host-exec output scan failed for job %s seq %s: %s",
            getattr(job, "id", None), seq, type(e).__name__,
        )
        return {"blocked": False, "error": True}


def _shadow_log(db: Session, job: "models.WorkerHostExecJob", seq: int,
                stream: str, verdict: dict) -> None:
    """shadow mode: log finding CODES/COUNTS only (never chunk content),
    worded exactly as `apply_task_output_scan` (task_output_scan.py:631)
    words it -- and never block."""
    findings = verdict.get("findings") or []
    if verdict.get("mode") != "shadow" or not findings:
        return
    counts: dict = {}
    for f in findings:
        code = f"{f.get('scanner', '?')}/{f.get('category', '?')}"
        counts[code] = counts.get(code, 0) + 1
    logger.warning(
        "host-exec output scan shadow findings for job %s: %d finding(s) %s",
        getattr(job, "id", None), len(findings), counts,
    )


def record_chunk(
    db: Session,
    job: "models.WorkerHostExecJob",
    seq: int,
    stream: str,
    data_b64: str,
    final: bool = False,
    exit_code: Optional[int] = None,
) -> dict:
    """Ingest one output chunk. Returns a summary dict for the router.

    Idempotent on duplicate `seq` (the chunk object already exists -> no
    append, still 200). Enforces the `worker.host_exec_max_output_bytes`
    cap: once the stored total would exceed it, set output_truncated=True
    and drop the chunk's data (still 200). On `final`, close the job:
    status = succeeded iff exit_code == 0 else failed, stamp exit_code +
    finished_at, record the output_object_key prefix.
    """
    if stream not in ("stdout", "stderr"):
        raise HTTPException(
            status_code=422,
            detail="stream must be 'stdout' or 'stderr'",
        )
    try:
        payload = base64.b64decode(data_b64)
    except Exception:  # noqa: BLE001 -- bad base64 is a client error
        raise HTTPException(
            status_code=422,
            detail="data_b64 is not valid base64",
        )

    tenant_id = getattr(job, "tenant_id", None)
    cap = settings.host_exec_max_output_bytes(db, tenant_id=tenant_id)
    client = get_output_client()
    bucket = _bucket_for_job(job)
    _ensure_bucket(client, bucket)
    prefix = _prefix_for(job.id)

    key = _chunk_key(job.id, seq, stream)
    try:
        st = client.stat_object(bucket, key)
        duplicate = st is not None
    except Exception as e:  # noqa: BLE001 -- NoSuchKey means "not stored yet"
        if not _object_absent(e):
            raise
        duplicate = False

    accepted = False
    was_truncated = bool(job.output_truncated)
    truncated = was_truncated
    blocked = False
    error = False
    verdict: dict = {}
    if not duplicate and not truncated:
        # Fortress 3.5 (958a) — scan BEFORE the durable write: the host-exec
        # path has no other pre-store scan (the bytes never reach
        # services/task_output_scan.py from /workers/tasks/.../complete).
        verdict = _scan_chunk(db, job, seq, payload)
        _shadow_log(db, job, seq, stream, verdict)
        blocked = bool(verdict.get("blocked"))
        # 1518b — `error=True` covers BOTH _scan_chunk fallbacks (CE's
        # `tos is None` and the scanner-exception path): never blocking,
        # but never allowed to read as a scanned-clean chunk either. See
        # the closing-audit-row handling below.
        error = bool(verdict.get("error"))

    if not duplicate and not truncated and not blocked:
        stored = _stored_bytes(client, bucket, prefix)
        if stored + len(payload) > cap:
            job.output_truncated = True
            truncated = True
            db.commit()
        else:
            client.put_object(
                bucket, key, BytesIO(payload),
                length=len(payload),
                content_type="application/octet-stream",
            )
            accepted = True

    closed = False
    if blocked and job.status in ("queued", "running"):
        # enforce + block-severity verdict: the chunk is NOT stored, so the
        # job must not keep streaming into a write that can never land. Close
        # it as a failure whose reason says WHY (mirrors executor.go:1348
        # outputScanBlockedReason) -- the worker's ingest POST answers 200
        # (a permanent verdict: retrying is pointless, hostExecPostRetries
        # would otherwise burn 3 attempts and then declare the stream broken)
        # and the job's status/exit_code are terminal, so the worker stops
        # streaming instead of retrying forever.
        job.status = "failed"
        job.exit_code = None
        job.finished_at = datetime.now(timezone.utc)
        job.output_object_key = prefix
        db.commit()
        closed = True

    if final:
        if job.status in ("queued", "running"):
            if exit_code is None:
                # A final without an exit code can't claim success; the
                # runner always sends one, so treat a missing code as a
                # failure rather than guessing 0.
                exit_code = 1
            job.status = "succeeded" if exit_code == 0 else "failed"
            job.exit_code = exit_code
            job.finished_at = datetime.now(timezone.utc)
            job.output_object_key = prefix
            db.commit()
            closed = True

    newly_truncated = truncated and not was_truncated
    if closed or newly_truncated:
        # One audit row per job-level event (final close, a scan-blocked close,
        # or the chunk that first tripped the cap) -- NOT per chunk, which would
        # flood the append-only audit table on a chatty process.
        metadata = {
            "host_exec_action": "output",
            "job_id": job.id,
            "worker_id": job.worker_id,
            "seq": seq,
            "stream": stream,
            "truncated": truncated,
            "final": bool(final),
            "closed": closed,
        }
        if blocked:
            # The durable "why": the refusal reason mirrors executor.go:1348
            # (outputScanBlockedReason) and the finding CODES (scanner/category
            # only, capped) -- never chunk content, never Finding.detail.
            metadata["blocked_reason"] = OUTPUT_SCAN_BLOCKED_REASON
            metadata["scan_mode"] = verdict.get("mode")
            metadata["finding_codes"] = tos.finding_codes(
                verdict.get("findings") or []) if tos is not None else []
        elif error:
            # 1518b — the chunk was NOT blocked (CE has no scanner / the
            # scanner raised), but it must never read like a verified-clean
            # scan either: the closing audit row carries scan_mode
            # "unavailable" plus an explicit scan_unavailable flag.
            metadata["scan_mode"] = verdict.get("mode") or "unavailable"
            metadata["scan_unavailable"] = True
        audit.log_security_event(
            db,
            models.AuditAction.ADMIN_WORKER_MODIFY,
            user_id=getattr(job, "actor_user_id", None),
            tenant_id=tenant_id,
            metadata=metadata,
        )
    result = {
        "job_id": job.id,
        "seq": seq,
        "accepted": accepted,
        "duplicate": duplicate,
        "truncated": truncated,
        "final": bool(final),
        "status": job.status,
        "blocked": blocked,
        "blocked_reason": (OUTPUT_SCAN_BLOCKED_REASON if blocked else None),
    }
    if blocked:
        # Owner decision 11-25: every refusal point raises an alert. The
        # response above is already built and the job close + audit row are
        # already committed, so the alert (own commit, rollback on error) can
        # never change the outcome.
        _alert_blocked_chunk(db, job, tenant_id, verdict)
    return result


HOSTEXEC_ALERT_RULE = "hostexec_output_scan_blocked"


def _alert_blocked_chunk(db: Session, job: "models.WorkerHostExecJob",
                         tenant_id, verdict: dict) -> None:
    """Compliance-refusal alert for a host-exec output chunk blocked by the
    pre-store scan (seventh refusal point). Paid-only module: absent on CE =
    skipped. Never raises; deduped per (job, rule) by the shared module."""
    job_id = None
    try:
        from types import SimpleNamespace
        from utils.paid_import import optional_paid
        cra = optional_paid("services.compliance_refusal_alert")
        if cra is None:
            return
        job_id = job.id
        codes = tos.finding_codes(verdict.get("findings") or []) \
            if tos is not None else []
        shim = SimpleNamespace(tenant_id=tenant_id, id=None,
                               workflow_run_id=None, node_run_id=None)
        cra.alert_refusal_committed(
            db, shim, gate="host_exec_output", rule=HOSTEXEC_ALERT_RULE,
            reason=f"{OUTPUT_SCAN_BLOCKED_REASON}: {codes}"[:300],
            mode=str(verdict.get("mode") or "enforce"), kind="hostexec",
            subject=f"hostexec:{job_id}:{HOSTEXEC_ALERT_RULE}",
            text=(f"Host-exec job #{job_id} output was blocked by the output "
                  f"safety scan ({OUTPUT_SCAN_BLOCKED_REASON}). "
                  f"{cra.OUTPUT_BLOCKED}"),
            tail=cra.OUTPUT_BLOCKED)
    except Exception as exc:  # noqa: BLE001 - alert must never change outcome
        logger.warning("refusal alert failed for host-exec job %s: %s",
                       job_id, type(exc).__name__)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _stored_bytes(client, bucket: str, prefix: str) -> int:
    """Total bytes of stored chunk objects under `prefix` (list is
    bounded by the cap: we stop summing once it is reached)."""
    total = 0
    for obj in client.list_objects(bucket, prefix=prefix):
        try:
            total += int(getattr(obj, "size", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def list_chunks(job: "models.WorkerHostExecJob") -> list:
    """Stored chunks for a job in seq order: [{seq, stream, data_b64}].
    Reads from the same tenant-scoped (or system, for shared-pool jobs)
    bucket the job's chunks were written to -- see `_bucket_for_job`."""
    client = get_output_client()
    bucket = _bucket_for_job(job)
    prefix = _prefix_for(job.id)
    out = []
    for obj in client.list_objects(bucket, prefix=prefix):
        seq = _seq_from_key(obj.key)
        if seq is None:
            continue
        try:
            raw = client.get_object(bucket, obj.key).read()
        except Exception as e:  # noqa: BLE001 -- absent mid-list: skip
            if not _object_absent(e):
                raise
            continue
        out.append({
            "seq": seq,
            "stream": _stream_from_key(obj.key),
            "data_b64": base64.b64encode(raw).decode("ascii"),
        })
    out.sort(key=lambda c: (c["seq"], c["stream"]))
    return out


def next_chunks(job: "models.WorkerHostExecJob", seen: set) -> list:
    """Chunks not yet emitted (replay + poll share this; a chunk's identity
    is (seq, stream), so `seen` keeps emission monotonic)."""
    return [c for c in list_chunks(job)
            if (c["seq"], c["stream"]) not in seen]


async def stream_events(db: Session, job: "models.WorkerHostExecJob"):
    """Yield SSE event payloads for the live tail (router wraps them).

    Replays stored chunks in seq order, then polls every 1s until the job
    reaches a terminal status; final event carries {status, exit_code,
    truncated}. Hard cap 1h (hx.6). Never emits chunk data beyond the
    base64 the client itself stored."""
    deadline = asyncio.get_running_loop().time() + _STREAM_MAX_SECONDS
    job_id = job.id
    seen: set = set()
    while True:
        for chunk in next_chunks(job, seen):
            seen.add((chunk["seq"], chunk["stream"]))
            yield {"kind": "chunk", **chunk}
        job = db.get(models.WorkerHostExecJob, job_id)
        if job is None:
            yield {"kind": "final", "status": "unknown",
                   "exit_code": None, "truncated": False}
            return
        if job.status in TERMINAL_STATUSES:
            yield {
                "kind": "final",
                "status": job.status,
                "exit_code": job.exit_code,
                "truncated": bool(job.output_truncated),
            }
            return
        if asyncio.get_running_loop().time() >= deadline:
            yield {"kind": "final", "status": job.status,
                   "exit_code": job.exit_code,
                   "truncated": bool(job.output_truncated),
                   "stream_capped": True}
            return
        await asyncio.sleep(_POLL_INTERVAL_S)

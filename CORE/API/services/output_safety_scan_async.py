"""compliance-fortress 3.5 (sz-fortress-p34.3) — ASYNC output-safety scan of
every worker-reported workflow output.

`services/output_safety_scanners.py` (3.1/3.2, sz-fortress-p34.1) is a fully
built registry of scanners, but nothing on the live run-output path ever
called it — only the unrelated `services/image_rescan.py` container-image
job and tests did. This module wires it in, following the EXACT pattern
`services/malware_scan_async.py` already established for large uploads:
scanning can be slow (NSFW/Presidio backends, large artefacts) so it must
NEVER run inline on the request handler that reports task completion. The
hook instead queues an `ApiTaskQueue` row on the "SYSTEM" queue — invisible
to the Go worker by construction — and `services/system_tasks.py`'s
`drain_pending_system_tasks` tick (already running) is the consumer, exactly
like `scan_pending_artefact` for malware.

FEATURE POSTURE (unchanged behaviour when off, by design): `queue_async_scan`
reads the SAME platform setting `output_safety_scanners.scan_output()` itself
gates on (`compliance.output_safety_scan_enabled`, default False via
`output_safety_scanners.enabled()`). When it is off, `queue_async_scan` is a
pure no-op (returns None, queues nothing) — zero behaviour change for every
tenant until the compliance vertical turns it on.

RESULTS go to the EXISTING append-only `models.OutputSafetyScanResult` table
(fortress 3.4, sz-fortress-p34.2) — one row per scan, tenant-scoped, never
updated. A `warn` or `block` verdict here is recorded/audited ONLY: this
module never blocks delivery (the output has already been recorded and is
already deliverable by the time this async job runs) and never rejects
anything — end-user rejection/override is a separate follow-up
(sz-fortress-p34.4). `block` verdicts are still worth a distinct AuditEvent
so compliance tooling can alert on them without polling the results table.

A scan job that cannot even fetch the artefact's bytes (storage hiccup) is
rescheduled with backoff, same shape as malware_scan_async._reschedule; after
a few attempts it gives up and records a `verdict="error"` row instead of
leaving the finding silently missing.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import event
from sqlalchemy.orm import Session

import models
from services import output_safety_scanners as oss
from services import platform_settings as psr  # leaf module: no audit cycle
from services import output_scan_exemptions as ose  # leaf module: 818's consult
try:
    from services import commercial_scanners as cs  # leaf (oss/psr/secrets only): no cycle
except ImportError:
    # CE: no commercial-scanner module ships — guard, not reclassify.
    cs = None

logger = logging.getLogger(__name__)

#: Queue routing — same SYSTEM lane malware_scan_async uses (Go worker
#: ignores it by construction; drain_pending_system_tasks is the consumer).
_QUEUE = "SYSTEM"
_NODE_PREFIX = "output-safety-scan-"
#: The task_function name system_tasks.py's whitelist + drain special-case
#: must both know (see the one-line registration there).
TASK_FUNCTION = "scan_output_artefact"

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 30

#: FORTRESS 3.6 (slice a) — tenant-scoped enforcement switch. OFF (the default)
#: keeps this module byte-for-byte identical to 3.5: a `block` verdict is
#: recorded + audited, nothing is blocked. ON, the verdict additionally
#: quarantines the scanned artefact (the SAME soft flag FX.90 p2b owns —
#: `Cog.malware_quarantine`, honoured by every download/stream/run-input gate),
#: fails the NodeRun and flips a still-RUNNING WorkflowRun to FAILED through
#: crud/workflows_domain.conditional_cancel_run. Never a DELETE (ISO 27001).
BLOCK_ENFORCED_SETTING = "fortress.output_block_enforced"
BLOCK_ENFORCED_DEFAULT = False

psr.register(
    BLOCK_ENFORCED_SETTING,
    bool,
    BLOCK_ENFORCED_DEFAULT,
    group="compliance",
    description="3.6a — enforce a `block` output-safety verdict instead of "
                "merely recording it. False (default): the verdict is recorded "
                "in OutputSafetyScanResult + audited, and the output stays "
                "deliverable exactly as 3.5 shipped. True for a tenant: the "
                "artefact carries the malware-quarantine soft flag (downloads "
                "and run-input use are refused with 423 until a platform admin "
                "releases it), the producing NodeRun is FAILED, and a still-"
                "RUNNING WorkflowRun is failed through the shared atomic "
                "cancel helper. Nothing is ever deleted.",
)


def _block_enforced(db, tenant_id: Optional[int]) -> bool:
    """Tenant-scoped read of the enforcement switch; a settings outage keeps
    the default posture (off = 3.5's record-only behaviour)."""
    try:
        return bool(psr.get_setting(BLOCK_ENFORCED_SETTING, db=db,
                                    tenant_id=tenant_id))
    except Exception as e:  # noqa: BLE001 — settings/DB outage
        logger.debug("output_safety_scan_async: block-enforced read failed: %s", e)
        return BLOCK_ENFORCED_DEFAULT


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Queue (called from routers/worker_tasks.py _record_task_outputs)
# ---------------------------------------------------------------------------


def queue_async_scan(
    db: Session, *, content_hash: str, tenant_id: int,
    node_run_id: Optional[int] = None, workflow_run_id: Optional[int] = None,
    mime: Optional[str] = None,
) -> Optional[models.ApiTaskQueue]:
    """Queue the async output-safety scan job (no commit; caller's seam owns it).

    No-op — returns None, queues nothing — when `content_hash` is empty OR
    `compliance.output_safety_scan_enabled` is off (the SAME setting
    `output_safety_scanners.scan_output()` itself reads via `_feature_on()`),
    so the run-completion path is byte-for-byte unchanged while the feature
    is off (its default).
    """
    if not content_hash:
        return None
    try:
        if not oss.enabled(db=db):
            return None
    except Exception as e:  # noqa: BLE001 — a settings outage must not queue
        logger.debug("output_safety_scan_async: enabled() read failed: %s", e)
        return None

    task = models.ApiTaskQueue(
        workflow_run_id=workflow_run_id,
        node_run_id=node_run_id,
        node_id=_NODE_PREFIX + content_hash[:48],
        tenant_id=tenant_id,
        queue_name=_QUEUE,
        priority=1,
        task_type="processing",
        task_payload={
            "node_kind": "output_safety_scan_async",
            "task_type": "system_internal",
            "task_function": TASK_FUNCTION,
            "content_hash": content_hash,
            "mime": mime,
        },
        status=models.ApiTaskStatus.PENDING,
        max_attempts=_MAX_ATTEMPTS,
    )
    db.add(task)
    # FX.165 p1b — this module never commits (callers own the seam, see the
    # docstring above), so publishing here would fire BEFORE the caller's
    # commit — a worker could wake, find no visible row, and go back to
    # sleep. Register a one-shot after_commit listener on this exact session
    # instead, so the wake fires once (and only once) the row is actually
    # durable. Best-effort: a dead Valkey must never fail the enqueue.
    queue_name = task.queue_name

    def _publish_wake(_session):
        try:
            from routers.worker_tasks import _publish_event

            _publish_event(f"task:available:{queue_name}")
        except Exception:  # noqa: BLE001 — wake-up must never fail the enqueue
            logger.debug("output safety scan wake publish on %s failed", queue_name)

    event.listen(db, "after_commit", _publish_wake, once=True)
    return task


# ---------------------------------------------------------------------------
# Blob fetch (same storage layer malware_scan_async.py's own scan job uses:
# tenant-scoped MinioClient + a bounded chunked stream — never the whole
# object in memory for a large artefact that wouldn't be scanned past the
# byte cap anyway).
# ---------------------------------------------------------------------------


def _scheme() -> str:
    """Storage URL scheme (deployment config, not behaviour). `s3` is the
    primary default; `minio` stays a REAL accepted value for existing
    deployments (STORAGE_SCHEME=minio)."""
    return os.getenv("STORAGE_SCHEME", "s3")


def _storage_schemes() -> tuple:
    """Schemes accepted when parsing a stored storage_path (dual-read).

    `s3` is the primary default; `minio` stays a REAL accepted value so an
    existing deployment with STORAGE_SCHEME=minio keeps its old behaviour.
    Stored values are never rewritten.
    """
    configured = _scheme()
    return tuple(dict.fromkeys((configured, "s3", "minio")))


def _storage_object_key(storage_path: str, bucket: str) -> str:
    """Object key for a `s3://bucket/key` (or legacy `minio://bucket/key`) path.

    Dual-read (MinIO->S3 rename, piece P10e-b1): new writes carry `s3://`,
    stored values from before still hold `minio://`. Both are understood here;
    nothing is ever rewritten.
    """
    if not storage_path:
        return ""
    for scheme in _storage_schemes():
        if storage_path.startswith(f"{scheme}://{bucket}/"):
            return storage_path[len(f"{scheme}://{bucket}/"):]
        if storage_path.startswith(f"{scheme}://"):
            after = storage_path[len(f"{scheme}://"):]
            return "/".join(after.split("/")[1:]) if "/" in after else ""
    return storage_path


def _fetch_bytes(blob: "models.ContentBlob", tenant_id: int, cap: int) -> bytes:
    """Stream up to `cap` bytes of the artefact (one chunk at a time)."""
    from utils.minio_client import MinioClient

    mc = MinioClient(tenant_id=tenant_id)
    bucket = mc.bucket_name
    key = _storage_object_key(getattr(blob, "storage_path", None), bucket) or blob.id
    resp = mc.client.get_object(bucket, key)
    try:
        buf = bytearray()
        for chunk in resp.stream(65536):
            buf.extend(chunk)
            if len(buf) > cap:
                break
        return bytes(buf[:cap])
    finally:
        resp.close()
        resp.release_conn()


# ---------------------------------------------------------------------------
# Verdict + the job itself
# ---------------------------------------------------------------------------


def _verdict_for(findings: list) -> str:
    """Highest severity present → pass|warn|block. Never "error" here — that
    verdict is reserved for a scan job that could not run at all."""
    severities = {f.severity for f in findings}
    if oss.SEVERITY_BLOCK in severities:
        return "block"
    if oss.SEVERITY_WARN in severities:
        return "warn"
    return "pass"


def _finding_dict(f) -> dict:
    return {
        "scanner": f.scanner,
        "category": f.category,
        "severity": f.severity,
        "detail": f.detail,
        "span": list(f.span) if f.span else None,
    }


def _record_result(
    db: Session, *, tenant_id: int, workflow_run_id: Optional[int],
    node_run_id: Optional[int], content_hash: str, mime: Optional[str],
    verdict: str, findings: list,
) -> None:
    """Append-only write to the fortress-p34.2 results table. `output_ref` is
    the content_hash handle — never the scanned bytes."""
    db.add(models.OutputSafetyScanResult(
        tenant_id=tenant_id,
        workflow_run_id=workflow_run_id,
        node_run_id=node_run_id,
        output_ref=content_hash,
        mime=mime,
        scanner="output_safety_scan_async",
        verdict=verdict,
        findings=[_finding_dict(f) for f in findings],
    ))
    if verdict == "block":
        # A distinct AuditEvent so compliance tooling can alert without
        # polling the results table — masked findings only, never raw output.
        db.add(models.AuditEvent(
            tenant_id=tenant_id,
            event_name="OUTPUT_SAFETY_SCAN_BLOCK_FINDING",
            details={
                "content_hash": content_hash, "mime": mime,
                "node_run_id": node_run_id, "workflow_run_id": workflow_run_id,
                "categories": sorted({f.category for f in findings
                                      if f.severity == oss.SEVERITY_BLOCK}),
            },
        ))


def _enforce_block(
    db: Session, *, tenant_id: int, blob: "models.ContentBlob",
    content_hash: str, mime: Optional[str], node_run_id: Optional[int],
    workflow_run_id: Optional[int], findings: list,
) -> None:
    """FORTRESS 3.6 slice a — a `block` verdict ENFORCES, not just records.

    Only ever called when `fortress.output_block_enforced` is ON for the
    tenant. Applies exactly what FX.90 p2b/p3a already apply to an infected
    artefact — `files_service.quarantine_artefact` sets the soft
    `Cog.malware_quarantine` flag (the download/stream/run-input gates in
    routers/cogs/assets_cogs_files.py + services/files_service.check_run_quarantined_inputs
    already refuse a flagged artefact with 423) — then fails the producing
    NodeRun and flips a still-QUEUED/RUNNING WorkflowRun through
    crud/workflows_domain.conditional_cancel_run, the same shared atomic helper
    services/sub_workflow_runtime.py uses when a child run fails. Never a
    DELETE, never raw output content in any message (categories only).
    """
    from crud.workflows_domain import conditional_cancel_run
    from services import files_service

    categories = sorted({f.category for f in findings
                         if f.severity == oss.SEVERITY_BLOCK})
    message = f"output_safety_blocked: {categories}"[:500]

    # 1. Quarantine the artefact — the SAME flag malware_scan_async sets.
    cog_id = getattr(blob, "cog_id", None)
    if cog_id is not None:
        files_service.quarantine_artefact(
            db, cog_id=cog_id, tenant_id=tenant_id,
            source="output-safety-scan", artefact=content_hash,
            signature=None, detail=message,
        )

    # 2. Fail the NodeRun that produced it.
    if node_run_id is not None:
        node_run = (db.query(models.NodeRun)
                    .filter(models.NodeRun.id == node_run_id).first())
        if node_run is not None:
            node_run.status = models.TaskStatus.FAILED
            node_run.completed_at = _now()
            node_run.error_message = message

    # 3. A still-QUEUED/RUNNING run must not keep dispatching work.
    if workflow_run_id is not None:
        conditional_cancel_run(
            db, workflow_run_id,
            completed_at=_now(),
            error_message=message,
        )

    # 4. One extra AuditEvent so the enforcement (not just the finding) is
    #    alertable — masked categories only, never the artefact's bytes.
    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        event_name="OUTPUT_SAFETY_BLOCK_ENFORCED",
        details={
            "content_hash": content_hash, "mime": mime,
            "node_run_id": node_run_id, "workflow_run_id": workflow_run_id,
            "categories": categories, "quarantined_cog_id": cog_id,
            "enforced": True,
        },
    ))


def _blocking_findings(findings: list) -> list:
    return [f for f in findings if f.severity == oss.SEVERITY_BLOCK]


def _exemption_scope(db: Session, task: models.ApiTaskQueue) -> tuple:
    """Fortress 3.5-e — resolve the exemption-consult scope from the linkage
    ``scan_one`` already has (never from this scan job's own pseudo node_kind).

    ``workflow_id``: ``ApiTaskQueue.workflow_run`` (ORM relationship over
    ``workflow_run_id``) → ``WorkflowRun.workflow_id``.
    ``node_kind``: ``task.node_run_id`` → ``NodeRun.workflow_node`` →
    ``WorkflowNode.node_kind`` (the node_definition-backed property).

    Anything unresolvable stays ``None`` — the 818 contract: an unresolved
    dimension only ever matches a NULL (wildcard) exemption row, never a
    narrowly-scoped one.
    """
    workflow_id = None
    workflow_run = getattr(task, "workflow_run", None)
    if workflow_run is not None:
        workflow_id = getattr(workflow_run, "workflow_id", None)

    node_kind = None
    if getattr(task, "node_run_id", None) is not None:
        node_run = (db.query(models.NodeRun)
                    .filter(models.NodeRun.id == task.node_run_id).first())
        workflow_node = getattr(node_run, "workflow_node", None) if node_run else None
        if workflow_node is not None:
            node_kind = getattr(workflow_node, "node_kind", None) or None
    return workflow_id, node_kind


def _append_exemption_override(
    db: Session, *, tenant_id: int, task: models.ApiTaskQueue,
    content_hash: str, mime: Optional[str], blocking: list, exemptions: list,
) -> None:
    """Fortress 3.5-e — the async sibling of 818's
    ``task_output_scan._append_exemption_override``: a NEW append-only
    ``output_safety_scan_results`` row (verdict kept ``block``, the original
    verdict row is never rewritten/downgraded) carrying
    ``overridden_by_user_id`` + ``override_reason``, plus the
    ``OUTPUT_SAFETY_SCAN_OVERRIDDEN`` AuditEvent naming the exemption ids and
    masked categories only. Unlike the inline path, the async path already
    persisted a plain block row, so this is the SECOND row — the pair is the
    durable record of finding + pre-authorisation. ``overridden_by_user_id``
    is the exemption's own ``approved_by_user_id`` (dual-control approver =
    accountable actor; no interactive user on this drain path).
    """
    exemption_ids = sorted({e.id for e in exemptions})
    db.add(models.OutputSafetyScanResult(
        tenant_id=tenant_id,
        workflow_run_id=getattr(task, "workflow_run_id", None),
        node_run_id=getattr(task, "node_run_id", None),
        output_ref=content_hash,
        mime=mime,
        scanner="output_safety_scan_async",
        verdict="block",
        findings=[_finding_dict(f) for f in blocking],
        overridden_by_user_id=exemptions[0].approved_by_user_id,
        override_reason=f"auto-exempted via output_scan_exemptions id={exemption_ids}",
    ))
    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        event_name="OUTPUT_SAFETY_SCAN_OVERRIDDEN",
        details={
            "content_hash": content_hash, "mime": mime,
            "node_run_id": getattr(task, "node_run_id", None),
            "workflow_run_id": getattr(task, "workflow_run_id", None),
            "exemption_ids": exemption_ids,
            "categories": sorted({f.category for f in blocking}),
        },
    ))


def _apply_exemptions(
    db: Session, *, task: models.ApiTaskQueue, tenant_id: int,
    content_hash: str, mime: Optional[str], findings: list,
) -> bool:
    """Fortress 3.5-e — True when EVERY blocking finding is covered by a live
    exemption (818's ``exemption_covers``, imported not duplicated). Partial
    coverage returns False (block stands). Caller wraps this in try/except: a
    blow-up here must never un-block, only lose the paired audit record."""
    blocking = _blocking_findings(findings)
    if not blocking:
        return False
    workflow_id, node_kind = _exemption_scope(db, task)
    covering: list = []
    for f in blocking:
        hit = ose.exemption_covers(
            db, tenant_id=tenant_id, workflow_id=workflow_id,
            node_kind=node_kind, scanner=f.scanner, finding_code=f.category,
        )
        if hit is None:
            return False  # partial (or zero) coverage -> stays blocked
        covering.append(hit)
    _append_exemption_override(db, tenant_id=tenant_id, task=task,
                               content_hash=content_hash, mime=mime,
                               blocking=blocking, exemptions=covering)
    return True


def _commercial_findings(db: Session, tenant_id: Optional[int], data: bytes,
                         mime: Optional[str]) -> list:
    """BUG-860b — async mirror of 846's inline `_commercial_findings`
    (services/task_output_scan.py): run fresh tenant-BOUND commercial scanner
    instances instead of the tenant-less registry objects.

    `oss.scan_output()` below already ran the module-level, tenant-LESS
    instances registered in `services/commercial_scanners.py`; those can only
    ever report a tenant-less `not_scanned` gap (noise on every image row, and
    a real vendor block is never enforced). The caller drops that result and
    calls this instead. A tenant that is not entitled or has the vendor
    disabled gets NO finding at all here (silent, not a gap) —
    `should_attempt()` gates that without touching `scan()`'s own (louder)
    gate. Same shape as the inline path; not imported from it, to keep this
    module's leaf-import discipline.
    """
    kind = oss.kind_for(mime)
    out: list = []
    # CE guard: no commercial-scanner module ships, so there are no
    # tenant-bound commercial scanners to run (a real CE capability gap,
    # not a bug) — iterate an empty list.
    for scanner in (cs.tenant_scanners(tenant_id, db) if cs is not None else []):
        if kind not in scanner.kinds or not scanner.should_attempt():
            continue
        out.extend(scanner.scan(data, mime=mime or ""))
    return out


def scan_one(
    db: Session, task: models.ApiTaskQueue, *, fetch_bytes=None,
) -> str:
    """Run one queued output-safety scan job.

    Returns "pass"|"warn"|"block"|"error"|"skip". Never raises: a fetch
    failure reschedules with backoff (same self-healing shape as
    malware_scan_async.scan_one); giving up records a verdict="error" row so
    the gap is visible, never silently missing.
    """
    payload = task.task_payload or {}
    content_hash = payload.get("content_hash")
    tenant_id = task.tenant_id
    mime = payload.get("mime")

    if not content_hash:
        task.status = models.ApiTaskStatus.FAILED
        task.error_message = "output safety scan: payload missing content_hash"
        task.completed_at = _now()
        return "skip"

    task.status = models.ApiTaskStatus.RUNNING
    task.attempt_count = (task.attempt_count or 0) + 1
    db.commit()

    blob = (
        db.query(models.ContentBlob)
        .filter(models.ContentBlob.id == content_hash,
                models.ContentBlob.tenant_id == tenant_id)
        .first()
    )
    if blob is None:
        task.status = models.ApiTaskStatus.COMPLETED
        task.error_message = "content blob not found — nothing to scan"
        task.completed_at = _now()
        db.commit()
        return "skip"

    cap = oss.max_bytes() + 1
    try:
        fetcher = fetch_bytes or _fetch_bytes
        data = fetcher(blob, tenant_id, cap)
    except Exception as e:  # noqa: BLE001 — storage hiccup, not a verdict
        return _reschedule(db, task, detail=f"{type(e).__name__}")

    findings = oss.scan_output(data, mime)
    # BUG-860b — mirror 846's inline pattern: drop the tenant-less
    # commercial-scanner result the generic registry call above produced (it
    # can only ever be a tenant-less gap) and re-run those scanners bound to
    # the real tenant, so a real vendor block is enforced below.
    # CE guard: with no commercial-scanner module, no finding is ever
    # classified "commercial", so all findings pass through unfiltered
    # (correct: CE has none to filter).
    findings = [
        f for f in findings
        if f.scanner not in (cs.COMMERCIAL_SCANNERS if cs is not None else ())
    ]
    findings.extend(_commercial_findings(db, tenant_id, data, mime))
    verdict = _verdict_for(findings)
    _record_result(db, tenant_id=tenant_id, workflow_run_id=task.workflow_run_id,
                   node_run_id=task.node_run_id, content_hash=content_hash,
                   mime=mime, verdict=verdict, findings=findings)
    # FORTRESS 3.5-e — a `block` whose blocking findings are ALL covered by
    # live output_scan_exemptions rows gets 818's override pattern appended
    # (a NEW append-only row + AuditEvent; the verdict row above is never
    # rewritten) and is then NOT enforced below. Partial coverage leaves the
    # block standing untouched. Fail-closed: any blow-up in the consult keeps
    # the block (and enforcement) exactly as 3.6a would have done.
    # Commit the 3.5 verdict row BEFORE attempting enforcement: a scanned
    # output commonly has no Cog yet (routers/worker_tasks.py's
    # _record_task_outputs only writes WorkflowNodeOutput + ContentBlob, no
    # Cog), so `_enforce_block` below may never reach `quarantine_artefact`
    # (whose internal audit commit would otherwise have made this durable by
    # accident). Commit here so the recorded verdict can never be rolled back
    # by an enforcement-step failure.
    db.commit()
    # FORTRESS 3.5-e — a `block` whose blocking findings are ALL covered by
    # live output_scan_exemptions rows (818's `exemption_covers`, imported
    # not duplicated) gets 818's override pattern appended — a NEW append-only
    # results row (verdict kept "block", never a rewrite of the row committed
    # just above) + an OUTPUT_SAFETY_SCAN_OVERRIDDEN AuditEvent carrying the
    # exemption ids — and is then NOT enforced below. Partial coverage leaves
    # the block standing. Runs AFTER the verdict-row commit so a consult
    # blow-up can never roll the verdict back; fail-closed either way.
    exempted = False
    if verdict == "block":
        try:
            exempted = _apply_exemptions(
                db, task=task, tenant_id=tenant_id,
                content_hash=content_hash, mime=mime, findings=findings,
            )
        except Exception as e:  # noqa: BLE001 — fail closed, block stands
            logger.warning(
                "fortress-3.5e: exemption consult failed for %s (task %d): %s",
                content_hash[:12], task.id, type(e).__name__,
            )
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    if verdict == "block" and not exempted and _block_enforced(db, tenant_id):
        # FORTRESS 3.6a — the setting is the whole switch: OFF leaves this
        # path byte-identical to 3.5 (record + audit only, output still
        # deliverable). Enforcement is best-effort, same contract as
        # malware_scan_async.notify_infected: a failure here must never lose
        # the verdict that already stands, nor break scan_one's documented
        # "never raises" contract.
        try:
            _enforce_block(db, tenant_id=tenant_id, blob=blob,
                           content_hash=content_hash, mime=mime,
                           node_run_id=task.node_run_id,
                           workflow_run_id=task.workflow_run_id, findings=findings)
        except Exception as e:  # noqa: BLE001 — the verdict already stands
            logger.warning(
                "fortress-3.6a: block-enforcement failed for %s (task %d): %s",
                content_hash[:12], task.id, e,
            )
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    task.status = models.ApiTaskStatus.COMPLETED
    task.result_payload = {"verdict": verdict, "finding_count": len(findings)}
    task.completed_at = _now()
    db.commit()
    return verdict


def _reschedule(db: Session, task: models.ApiTaskQueue, *, detail: str) -> str:
    attempts = task.attempt_count or 1
    task.error_message = f"output safety scan error: {detail}"[:500]
    if attempts >= (task.max_attempts or _MAX_ATTEMPTS):
        payload = task.task_payload or {}
        task.status = models.ApiTaskStatus.FAILED
        task.completed_at = _now()
        _record_result(
            db, tenant_id=task.tenant_id, workflow_run_id=task.workflow_run_id,
            node_run_id=task.node_run_id,
            content_hash=payload.get("content_hash") or "",
            mime=payload.get("mime"), verdict="error", findings=[],
        )
        db.commit()
        logger.warning("Async output-safety scan gave up after %d attempts "
                       "(task %s): %s", attempts, task.id, detail)
    else:
        task.status = models.ApiTaskStatus.PENDING
        task.start_after = _now() + timedelta(
            seconds=_BACKOFF_SECONDS * (2 ** (attempts - 1)))
        db.commit()
    return "error"

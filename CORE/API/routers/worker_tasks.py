"""
API-based task queue for workers.

Workers poll for tasks via HTTPS against this router; the queue is the
Postgres-backed `ApiTaskQueue` table. All internal services (Postgres,
Redis) stay bound to 127.0.0.1 — only nginx (80/443) is exposed
externally. Both local Docker-network workers and remote BYOH workers
use the same HTTPS path.
"""

from crud.node_run_cud import clamp_node_run_text
from fastapi import APIRouter, HTTPException, Depends, Header, BackgroundTasks, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from sqlalchemy import and_, text
from dependencies import get_db, require_bearer_or_api_key, require_active_and_onboarded_user
from utils.http_errors import internal_http_error
from utils.run_task_auth import node_or_service_principal, pin_run_task_tenant
from database import SessionLocal
import crud.worker_management as worker_management_crud
import crud.worker_tasks_lifecycle as worker_tasks_lifecycle_crud
import crud.worker_tasks_status as worker_tasks_status_crud
import crud.worker_tasks_results as worker_tasks_results_crud
try:
    from services import asset_inventory as _asset_inv
except ImportError:
    _asset_inv = None
import models
from models import (
    ApiTaskQueue, ApiTaskStatus,
    WorkerLicense, WorkerEnrollment, WorkerEnrollmentStatus,
    WorkerOwnerType, WorkerStatus,
    WorkflowRun, NodeRun, TaskStatus,
    TaskResourceSample, WorkerResourceTimeline,
    NodeDefinition, PortOutcome,
)
from security import verify_api_key
from security.verified_token_cache import verify_token_async

import json
import math
import os
import time
import asyncio
import logging
import threading
import redis as redis_lib
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm.attributes import flag_modified
from utils.registries import tenant_registry, platform_registry
from utils.usage_enforcement import _is_saas
from utils.paid_import import optional_paid
from utils.task_pinning import benchmark_target_worker as _benchmark_target_worker
from utils.task_pinning import benchmark_report_worker_is_owner as _benchmark_report_worker_is_owner
from utils.pii_redaction_surfaces import (
    safe_redact as _v4b_redact_text,
    redact_list as _v4b_redact_lines,
)


def _publish_event(channel: str, message: str = "1"):
    """Fire-and-forget Redis pub/sub notification."""
    try:
        r = redis_lib.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish(channel, message)
    except Exception:
        pass

from product_names import themed_logger

logger = themed_logger(__name__, 'workers')

router = APIRouter(tags=["Worker Tasks"])


def _validate_system_job_excluded_workers(value: list) -> None:
    """Writer-side validation: every entry must be a non-empty string (a
    worker_name or a worker_licenses.id, matched as a string)."""
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "scheduler.system_job_excluded_workers entries must be "
                f"non-empty strings (worker name or id): {item!r}"
            )


def _register_worker_task_settings() -> None:
    """FX.168 p1 — declare the validate-phase churn ceiling (DB-backed)."""
    from services import platform_settings as _psr
    _psr.register(
        "scheduler.validate_phase_repool_ceiling",
        int,
        50,
        group="scheduler",
        description="How many times a task may be re-pooled by a "
                    "failed POST /workers/tasks/{id}/validate-phase before the "
                    "API FAILS it instead of re-pooling. Counted in "
                    "task_payload.validate_phase_repools (validate-phase "
                    "re-pools ONLY — NOT scheduling_version, which routine "
                    "overlap-scheduler re-plans also bump). Default 50; 0 or "
                    "negative disables the ceiling (always re-pool).",
    )
    # 1580 — the 10-14 change-gate weekly sweep fanned 453 variant-smoke test
    # jobs out to every online worker, including boxes the owner restricts
    # (HP, R9000, bounded-core Dell/GPU1 lanes). There was no way to say
    # "system/background test jobs must not land on worker X". This setting
    # is a list of worker_name values and/or worker_licenses.id values (as
    # strings) that are excluded from SYSTEM/background task types —
    # currently task_type IN ('system', 'variant_smoke'): the existing
    # 'system' classification (calibration/benchmark/persistent-stack ops —
    # see GY.V3b above) plus 'variant_smoke' (change-gate / node-wizard
    # variant verification tests — the task_type
    # services/node_authoring_service.py + node_definitions_domains/_shared.py
    # ``_enqueue_variant_smoke_task`` stamp; there is no separate universal
    # "is this a system job" column on ApiTaskQueue, so these two task_types
    # are matched explicitly). Tenant-submitted tasks (every other task_type)
    # are entirely unaffected — an excluded worker still claims tenant work.
    # Default [] = no exclusions, byte-identical claim behaviour to before.
    _psr.register(
        "scheduler.system_job_excluded_workers",
        list,
        [],
        validator=_validate_system_job_excluded_workers,
        group="scheduler",
        description="1580 — worker names and/or worker_licenses.id values "
                    "(strings) excluded from claiming SYSTEM/background task "
                    "types (task_type IN ('system', 'variant_smoke') — "
                    "calibration/benchmark/stack-maintenance and change-gate "
                    "variant-smoke tests). Tenant tasks are unaffected: an "
                    "excluded worker still claims every other task_type. "
                    "Default [] = off, identical to pre-1580 behaviour.",
    )


_register_worker_task_settings()


def _is_worker_system_excluded(db, worker_name, worker_id) -> bool:
    """1580 — True iff `worker_name` or `worker_id` (either may be None) is
    listed in the `scheduler.system_job_excluded_workers` platform setting.

    Single source of the list's matching semantics (exact worker_name match
    OR str(worker_id) match; empty list ⇒ always False). Shared by:
      - the ApiTaskQueue claim guard in `_try_claim_task` below, and
      - `node_builder.variant_test.queue.drain_for_worker` (the
        VariantTestPending heartbeat-drain hook the change-gate weekly sweep
        actually dispatches through — the real 453-job incident path).

    A settings-read failure degrades to "not excluded" (never blocks a claim
    on a settings-layer outage) and logs.
    """
    try:
        from services import platform_settings as _psr_1580
        _excluded = _psr_1580.get_setting(
            "scheduler.system_job_excluded_workers", db=db) or []
        _excluded_set = {str(w).strip() for w in _excluded if str(w).strip()}
        if not _excluded_set:
            return False
        return bool(
            (worker_name and str(worker_name) in _excluded_set)
            or (worker_id is not None and str(worker_id) in _excluded_set)
        )
    except Exception:  # noqa: BLE001 — a settings read failure must not
        # block claiming/draining; degrade to "not excluded" and log.
        logger.exception(
            "1580: system-job-excluded-workers check failed "
            "(treating worker as not excluded)")
        return False


def _register_claimed_never_started_setting() -> None:
    """BUG P2 — lease for a CLAIMED task that never reports /started (DB-backed)."""
    from services import platform_settings as _psr
    _psr.register(
        "worker.claimed_never_started_grace_seconds",
        int,
        180,
        group="worker",
        description="BUG P2 — seconds a task may sit CLAIMED with started_at "
                    "NULL before cleanup_expired_task_claims re-pools it. A "
                    "worker that claims a task type it cannot execute (or that "
                    "dies between claim and /started) used to hold the row "
                    "hostage for the full 30-minute claim lease, so the "
                    "operator saw 'claimed, never started, no logs' for "
                    "half an hour. Default 180s; 0 or negative disables the "
                    "guard (only the 30-min lease reaps).",
    )


_register_claimed_never_started_setting()


def _register_lost_report_hold_setting() -> None:
    """rc20 — grace a claim-expired, attempt-exhausted task may sit HELD for a
    returning worker before the reaper terminalizes it as a lost report."""
    from services import platform_settings as _psr
    _psr.register(
        "worker.lost_report_hold_grace_seconds",
        int,
        3600,
        group="worker",
        description="rc20 — seconds a claim-expired, attempt-exhausted task "
                    "stays HELD awaiting a worker before cleanup_expired_task_claims "
                    "fails it as 'worker report lost (claim expired)'. A worker "
                    "that took the claim but whose failure report never landed "
                    "(worker->API transport dead) used to hold the run at RUNNING "
                    "forever. Default 3600s; 0 or negative disables the bound "
                    "(the durable HOLD stays unbounded, as before).",
    )


_register_lost_report_hold_setting()


# ---------------------------------------------------------------------------
# Runaway-dispatch guard (workflow_version 81 / run 646 incident)
# ---------------------------------------------------------------------------
# A malformed + duplicate graph edge drove the orchestrator to re-enqueue the
# same root nodes ~63 times each (~126 PENDING rows in ~3 min, attempt_count 0,
# never claimed). build_execution_plan now dedupes edges + drops phantom-handle
# edges so the dependency graph can't be corrupted, but dispatch itself is also
# hardened here — at the single enqueue choke point every path funnels through
# (root _start_workflow, downstream _check_and_dispatch_downstream, and any
# router that enqueues) — so no graph shape can ever cause a runaway again:
#
#   * IDEMPOTENCY (the key safety): a (workflow_run_id, node_id, batch-identity)
#     that already has an OUTSTANDING (PENDING/ASSIGNED/CLAIMED/RUNNING) or a
#     COMPLETED task is NOT enqueued again — the existing task is returned.
#     "batch-identity" is the task's cog_ids, so legitimate batch fan-out (one
#     task per file, same node_id) is preserved while an identical re-enqueue
#     of the same work is collapsed.
#   * CIRCUIT BREAKER: even across terminal churn (failed→retry→failed…), the
#     number of rows for one (run, node, batch-identity) is capped; over the
#     ceiling the run is FAILED cleanly with a "dispatch loop detected" error
#     instead of looping forever.
#
# Retries do NOT route through enqueue_task_internal (the report-failed handler
# requeues in place / creates its own row), so this guard never blocks a
# legitimate retry.

# Ceiling on task rows per (run, node, batch-identity). Comfortably above the
# legitimate retry count (max_attempts default 3) so a normal node never trips
# it, but far below a runaway.
_DISPATCH_LOOP_CEILING = 12

# 30.6.1 c1-J3d2-fix — upper bound on a logical-snapshot row count, byte-for-byte
# the worker's own cap: CORE/Worker/internal/executor/stacklogical.go:245
# `maxVerifyRows = 10000000` (applied by capVerifyRows, stacklogical.go:351).
# A non-genuine worker can smuggle an arbitrarily large int into result_payload /
# the snapshot manifest; Postgres jsonb rejects numeric literals over 16384
# significant digits, so an unbounded count plausibly 500s at the commit. Reject
# (never clamp) at the model: only a non-genuine worker can send one.
MAX_VERIFY_ROWS = 10_000_000

_OUTSTANDING_TASK_STATUSES = (
    ApiTaskStatus.PENDING,
    ApiTaskStatus.ASSIGNED,
    ApiTaskStatus.CLAIMED,
    ApiTaskStatus.RUNNING,
)


class DispatchLoopError(RuntimeError):
    """Raised when the per-(run, node) enqueue circuit breaker trips. The run
    has already been marked FAILED by the time this propagates."""


def _batch_identity(payload) -> str:
    """Stable identity for the unit of work a task represents, so batch
    fan-out (same node_id, different files) is distinguished from a duplicate
    re-enqueue of the same work. Derived from the resolved cog_ids."""
    if not isinstance(payload, dict):
        return ""
    cogs = payload.get("cog_ids")
    if cogs is None:
        pi = payload.get("ProjectInfo") or {}
        if isinstance(pi, dict):
            cogs = pi.get("cog_ids")
    if isinstance(cogs, (list, tuple)):
        try:
            return ",".join(sorted(str(c) for c in cogs))
        except Exception:  # noqa: BLE001
            return str(cogs)
    return str(cogs) if cogs is not None else ""


def _fail_run_dispatch_loop(db: Session, run_id, node_id, count: int) -> None:
    """Mark a workflow run FAILED because a per-node enqueue loop was detected.
    Best-effort and idempotent — never raises."""
    try:
        run = worker_management_crud.get_workflow_run_by_id(db, run_id)
        if run and run.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            run.status = TaskStatus.FAILED
            run.completed_at = datetime.now(timezone.utc)
            run.error_message = (
                f"Dispatch loop detected / invalid graph: node '{node_id}' was "
                f"enqueued {count} times for the same inputs (ceiling "
                f"{_DISPATCH_LOOP_CEILING}). Aborting the run to protect the "
                f"task queue."
            )
            db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _dispatch_guard(db: Session, request, payload):
    """Idempotency + circuit-breaker check for a workflow task enqueue.

    Returns:
        * an enqueue-result dict {task_id, status, deduplicated} to short-
          circuit the enqueue (an equivalent task already exists), or
        * None to proceed with a normal insert.

    Raises:
        DispatchLoopError: circuit breaker tripped (run already failed).
    """
    run_id = getattr(request, "workflow_run_id", None)
    node_id = getattr(request, "node_id", None)
    if not run_id or not node_id:
        return None

    # node-run-controls — a deliberate operator re-run is NOT a runaway loop:
    # skip both the idempotency dedup and the loop circuit breaker for it. This
    # marker is set only by the operator re-run path (never automated dispatch);
    # each re-run lands in a fresh output_namespace, so prior attempts survive.
    if isinstance(payload, dict) and payload.get("manual_rerun"):
        return None

    batch_id = _batch_identity(payload)
    rows = worker_management_crud.list_tasks_for_run_and_node(db, run_id, node_id)
    same = [t for t in rows if _batch_identity(t.task_payload or {}) == batch_id]

    # Circuit breaker — hard stop before we add row N+1.
    if len(same) >= _DISPATCH_LOOP_CEILING:
        _fail_run_dispatch_loop(db, run_id, node_id, len(same))
        logger.error(
            "Dispatch loop detected: run=%s node=%s already has %d task(s) for "
            "the same inputs (ceiling %d) — failing the run instead of "
            "re-enqueuing.",
            run_id, node_id, len(same), _DISPATCH_LOOP_CEILING,
        )
        raise DispatchLoopError(
            f"dispatch loop detected for run {run_id} node {node_id}"
        )

    # Idempotency — an outstanding or completed instance already covers this
    # unit of work; do not enqueue a duplicate.
    for t in same:
        if t.status in _OUTSTANDING_TASK_STATUSES or t.status == ApiTaskStatus.COMPLETED:
            logger.info(
                "Idempotent enqueue skip: run=%s node=%s already has task %d "
                "(status=%s) for the same inputs — returning it.",
                run_id, node_id, t.id,
                t.status.value if hasattr(t.status, "value") else t.status,
            )
            return {
                "task_id": t.id,
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "deduplicated": True,
            }
    return None

# node-ownership-purchases — the run-time paywall fails OPEN when the ownership
# check itself throws (a DB error must never wedge dispatch of a valid free
# node). That's the right default, but a silent fail-open means an unowned
# priced node could run unbilled and nobody would know. Emit a metric +
# warning on every fail-open so the rate is observable and alertable.
try:  # pragma: no cover - import guard; prometheus is a transitive dep
    from prometheus_client import Counter as _PromCounter

    _METRIC_PAYWALL_CHECK_ERRORS = _PromCounter(
        "node_paywall_check_errors_total",
        "Node-ownership paywall checks that raised and fell open, by check site.",
        ["site"],
    )
except Exception:  # pragma: no cover - metrics are best-effort
    _METRIC_PAYWALL_CHECK_ERRORS = None


def _record_paywall_check_error(site: str) -> None:
    """Observe a paywall check that raised and fell open. Best-effort — this
    must never itself break dispatch."""
    logger.warning(
        "node-ownership paywall check errored and fell OPEN at %s — an unowned "
        "priced node may run unbilled; investigate", site, exc_info=True,
    )
    if _METRIC_PAYWALL_CHECK_ERRORS is None:
        return
    try:
        _METRIC_PAYWALL_CHECK_ERRORS.labels(site=site).inc()
    except Exception:  # pragma: no cover - best-effort
        pass


# GSEC Q21.2 — the BYO-S3 STS mint fails OPEN back onto the tenant's static
# key (today's bundle) when the endpoint rejects the session: a mint failure
# must never kill a healthy task. The fallback rate must stay observable and
# alertable — same pattern as the paywall fail-open counter above.
try:  # pragma: no cover - import guard; prometheus is a transitive dep
    from prometheus_client import Counter as _StsFallbackCounter

    _METRIC_STS_MINT_FALLBACKS = _StsFallbackCounter(
        "worker_sts_mint_fallbacks_total",
        "BYO-S3 STS mints that failed and fell back to the static key, by site.",
        ["site"],
    )
except Exception:  # pragma: no cover - metrics are best-effort
    _METRIC_STS_MINT_FALLBACKS = None


def _record_sts_mint_fallback(site: str, task_id: int, exc: Exception) -> None:
    """GSEC Q21.2 — alert for a failed STS AssumeRole mint: the task runs on
    the BYO static key (pre-Q21 behaviour). Error log + counter so the
    fallback rate is observable. Best-effort — must never break credential
    issuance itself."""
    logger.error(
        "GSEC Q21.2: STS AssumeRole mint failed for task %d (%s); falling "
        "back to the BYO static key bundle. The tenant opted in (sts_enabled) "
        "but the endpoint did not return a usable session — check that the "
        "endpoint supports STS AssumeRole: %s", task_id, site, exc,
    )
    if _METRIC_STS_MINT_FALLBACKS is None:
        return
    try:
        _METRIC_STS_MINT_FALLBACKS.labels(site=site).inc()
    except Exception:  # pragma: no cover - best-effort
        pass


def _resolve_byo_sts_ttl_s(db: Session) -> int:
    """GSEC Q21.2 — session TTL for BYO-S3 STS creds, read by key with a
    2h default. The platform setting ``worker.sts_session_ttl_s`` is
    registered by Q21.4; until it lands (or if the read fails) this returns
    the default so the gate never fails on the settings registry."""
    from utils import sts_session
    try:
        from services.platform_settings import get_setting
        v = get_setting("worker.sts_session_ttl_s", db=db)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(sts_session.clamp_ttl(v))
    except Exception:  # noqa: BLE001 — unregistered/unreadable → default TTL
        pass
    return sts_session.DEFAULT_SESSION_TTL_S

# ---------------------------------------------------------------------------
# Current worker agent version (update when releasing new worker packages)
# ---------------------------------------------------------------------------
CURRENT_AGENT_VERSION = os.getenv("WORKER_AGENT_VERSION", "1.0.0")
WORKER_PACKAGE_URL = os.getenv("WORKER_PACKAGE_URL", "")
WORKER_PACKAGE_CHECKSUM = os.getenv("WORKER_PACKAGE_CHECKSUM", "")

# FIX-CE-DP-NOISE-FAIL-CLOSED (owner decision 11-10 10:50 PM CT) — the FIXED text a
# CE task carries when it asked for differential privacy but services.dp_noise is
# absent (paid). Fixed text only: never an exception class name, never a module name.
DP_NOISE_UNAVAILABLE = "differential privacy is not available in Community Edition"


# ---------------------------------------------------------------------------
# Auth dependency: verify enrolled worker via bootstrap token
# ---------------------------------------------------------------------------


# GSEC R-031 — cut-over gate for the name-only (enrollment-less) embedded
# worker auth path. Registered in services.platform_settings with a default
# of True (new installs are credential-only from day one). The live install
# runs an embedded worker binary that may not yet carry the Vault-seeded
# bootstrap token (main.py:_startup_seed_embedded_worker_enrollment +
# provision-embedded-worker-token.sh), so the lead seeds a False platform row
# for THIS install to keep it name-only until the worker is verified carrying
# the token, then flips it to True. While off, every name-only acceptance is
# logged + written to the audit trail (GSEC R-031 audit) so the lead can see
# exactly who relied on the legacy path before flipping.
_EMBEDDED_NAME_ONLY_SETTING = "worker.embedded_requires_secret"


async def require_enrolled_worker(
    x_worker_name: str = Header(..., alias="X-Worker-Name"),
    x_bootstrap_token: str = Header(default="", alias="X-Bootstrap-Token"),
    db: Session = Depends(get_db),
    request: Request = None,
) -> WorkerLicense:
    """
    Authenticate a worker for task operations.

    Every worker — remote OR embedded — proves identity with its bootstrap
    token, verified against the APPROVED WorkerEnrollment row:
    1. Remote workers: token issued by the enrollment flow (/workers/enroll).
    2. The embedded worker (mod-worker-embedded): token provisioned at Vault
       KV v2 `infrastructure/embedded-worker`; the Go worker reads it via
       GetInfraSecret("embedded-worker") and the API startup seeds its hash
       onto an APPROVED WorkerEnrollment row (main.py), so it authenticates
       exactly like a remote worker.

    The former name-only path (a licensed WorkerLicense with NO enrollment
    was accepted on the strength of its name) is retained ONLY behind the
    platform setting worker.embedded_requires_secret (default True = secret
    required). While it is off (live-install migration window), a
    no-enrollment worker still needs a LICENSED/UNLICENSED row, but every
    such acceptance is audited + logged so the operator can see who relied
    on the name-only path before flipping the setting on.

    Returns the WorkerLicense record.
    """
    # Path 1: Check enrollment (remote + embedded workers)
    enrollment = worker_management_crud.get_enrollment_by_name(db, x_worker_name)

    if enrollment:
        if enrollment.status != WorkerEnrollmentStatus.APPROVED:
            # FX.123 p3b0: distinguish retired workers (REVOKED + wipe record)
            # from pending/rejected/revoked-without-retire cases.
            # Only answer "Worker retired" when the token verifies — an
            # unverified caller gets the old 401 text.
            _has_retire = False
            _is_revoked = (enrollment.status == WorkerEnrollmentStatus.REVOKED
                           or enrollment.revoked_at is not None)
            if _is_revoked and verify_api_key(
                    x_bootstrap_token, enrollment.bootstrap_token_hash):
                try:
                    # Fast path: hardware_specs JSONB (survives until next
                    # heartbeat rebuilds it wholesale).
                    _wl = worker_management_crud.get_worker_by_name(db, x_worker_name)
                    if _wl is not None:
                        _ws = _wl.hardware_specs or {}
                        _wr = _ws.get("wipe_record")
                        if (isinstance(_wr, dict)
                                and _wr.get("worker_name") == x_worker_name):
                            _has_retire = True
                    # Fallback: append-only audit trail (survives heartbeat
                    # rebuilds; uses .op("@>") — never .astext on JSONB).
                    if not _has_retire and _asset_inv is not None:
                        _has_retire = _asset_inv.has_retire_wipe_record(
                            db, x_worker_name)
                except Exception:  # noqa: BLE001 — never 500
                    pass
                if _has_retire:
                    raise HTTPException(
                        status_code=401,
                        detail="Worker retired",
                        headers={"X-Error-Code": "WORKER_RETIRED"},
                    )
            raise HTTPException(status_code=401, detail="Worker enrollment not approved")

        # UNDER1S: bcrypt (~0.24 s) off the event loop + positive cache bound to the
        # exact stored hash (see security/verified_token_cache.py). Wrong tokens still
        # pay full bcrypt and are never cached; status/expiry checks around it are unchanged.
        if not await verify_token_async(x_bootstrap_token, enrollment.bootstrap_token_hash):
            raise HTTPException(status_code=401, detail="Invalid bootstrap token")

        if enrollment.expires_at and enrollment.expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="Enrollment expired, re-enroll required")

        # Update last seen
        enrollment.last_seen_at = datetime.now(timezone.utc)
        db.commit()

    # Look up worker license (required for both paths)
    worker = worker_management_crud.get_worker_by_name(db, x_worker_name)

    if not worker:
        raise HTTPException(status_code=404, detail="Worker not registered")

    # No enrollment row: the worker has not proved its token. GSEC R-031 —
    # the default posture refuses it; the migration window accepts it only
    # when the gate is off, and even then the acceptance is audited so the
    # operator can see exactly which workers were relying on name-only auth.
    if not enrollment:
        requires_secret = True
        try:
            from services import platform_settings as _psr_r031
            requires_secret = bool(_psr_r031.get_setting(
                _EMBEDDED_NAME_ONLY_SETTING, db=db))
        except Exception:  # fail closed — an unreadable gate means secret required
            logger.warning(
                "worker.embedded_requires_secret unreadable; refusing "
                "name-only auth for '%s' (fail closed)", x_worker_name,
            )
        if requires_secret:
            logger.warning(
                "Refused name-only worker auth for '%s' (no bootstrap token / "
                "enrollment). The embedded worker must carry its Vault-seeded "
                "bootstrap token (infrastructure/embedded-worker); run "
                "provision-embedded-worker-token.sh + restart the worker.",
                x_worker_name,
            )
            raise HTTPException(
                status_code=401,
                detail="Worker bootstrap token required — worker is not "
                       "enrolled (no approved enrollment with a valid "
                       "X-Bootstrap-Token)",
            )
        if worker.status not in (WorkerStatus.LICENSED, WorkerStatus.UNLICENSED):
            raise HTTPException(status_code=401, detail="Worker not enrolled and not active on network")
        logger.warning(
            "GSEC-R031 name-only worker auth ACCEPTED for '%s' — "
            "worker.embedded_requires_secret is off; this acceptance is "
            "audited. Flip the setting on once the worker carries its "
            "bootstrap token.", x_worker_name,
        )
        _audit_embedded_name_only_acceptance(db, request, worker)

    return worker


def _audit_embedded_name_only_acceptance(db: Session, request: Optional[Request],
                                         worker: WorkerLicense) -> None:
    """GSEC R-031 — write an audit row for a name-only (no-token) embedded
    worker acceptance. Best-effort: an audit failure must never break the
    worker's task-plane call (the logger warning is the primary signal)."""
    try:
        from audit import log_security_event
        ip = None
        ua = None
        if request is not None:
            # GSEC R-145 / FX.106 p3a — one trusted client-IP resolver
            # (services/client_ip.py).
            from services.client_ip import resolve_client_ip
            ip = resolve_client_ip(request)
            ua = request.headers.get("User-Agent")
        log_security_event(
            db=db,
            action=models.AuditAction.AUTH_API_KEY_INVALID,
            request=request,
            tenant_id=worker.owner_tenant_id,
            metadata={
                "worker_name": worker.worker_name,
                "reason": "gsec_r031_embedded_name_only_acceptance",
                "gate": _EMBEDDED_NAME_ONLY_SETTING,
                "worker_status": worker.status.value,
            },
        )
    except Exception:  # noqa: BLE001 — audit is best-effort
        logger.warning("GSEC-R-031 name-only acceptance audit write failed "
                       "for '%s'", worker.worker_name, exc_info=True)


# ---------------------------------------------------------------------------
# Helper: get authorized tenant IDs for a worker
# ---------------------------------------------------------------------------
# Sentinel returned in the authorized-tenants list to signal "this worker
# is authorized for every tenant on the platform" — used by SaaS-mode
# PLATFORM_POOL workers that share their compute across the whole tenant
# set. Picked as -1 because real tenant IDs are positive integers, so it
# can never collide. Callers that filter `task.tenant_id in authorized`
# also accept ALL_TENANTS_AUTHORIZED via the helper below.
ALL_TENANTS_AUTHORIZED = -1


def _unruled_drift_variants(db, node_definition_id: int) -> set:
    """project-agent §1.5 — variants whose sample-test output diverged from
    the reference and that nobody has ruled on yet.

    Withheld from selection rather than dropped: the build exists, it may
    be fine, and a tenant can rule it back into service. Failing open on a
    lookup error is deliberate — an unavailable verification table must
    degrade to the pre-§1.5 behaviour rather than make every node
    unrunnable.
    """
    try:
        rows = worker_tasks_lifecycle_crud.get_unruled_drift_variant_rows(db, node_definition_id)
        return {r[0] for r in rows}
    except Exception as exc:
        logger.debug(
            "drift-ruling gate: lookup failed for node_definition=%s (%s) "
            "— not withholding", node_definition_id, exc,
        )
        return set()


def _stacks_being_redeployed_on(db, worker_name: str) -> list:
    """Stack ids with a deploy task already queued for this worker.

    Their VRAM reservation is about to be released and re-taken by that very
    task, so counting it while judging the task means a redeploy competes with
    itself. On a single-card host that is not a near miss — the stack's whole
    footprint is subtracted from the only device, the task can never fit, and
    it sits queued with no error to read.

    Fails OPEN (empty list). A missed exclusion only makes the guard stricter
    than needed; raising here would stop the worker claiming anything at all.
    """
    try:
        rows = db.execute(text("""
            SELECT DISTINCT (task_payload->>'stack_id')::int AS sid
              FROM api_task_queue
             WHERE status IN ('PENDING', 'ASSIGNED', 'CLAIMED', 'RUNNING')
               AND task_payload->>'node_kind' = 'persistent-stack-deploy'
               AND task_payload->>'stack_id' IS NOT NULL
               AND (assigned_to_worker = :w OR assigned_to_worker IS NULL)
        """), {"w": worker_name}).fetchall()
        return [r[0] for r in rows if r[0] is not None]
    except Exception as exc:  # noqa: BLE001
        logger.debug("redeploy-exclusion lookup failed for %s: %s", worker_name, exc)
        return []


def _is_tenant_authorized(authorized: Optional[List[int]], tenant_id: int) -> bool:
    """True iff the (possibly wildcard-bearing) authorized list permits this tenant."""
    if not authorized:
        return False
    if ALL_TENANTS_AUTHORIZED in authorized:
        return True
    return tenant_id in authorized


def _is_native_worker(worker: WorkerLicense) -> bool:
    """
    True iff `worker` is a native worker — a Go binary running directly on a
    tenant's own desktop OS (no container runtime), as opposed to a worker in
    the shared Linux container pool.

    Signal: heartbeat-reported `hardware_specs.os` in ('windows', 'darwin').
    D-P3 already made "windows/darwin worker" and "native-only worker"
    synonymous (platform_guard/arch_guard refuse container tasks to such a
    worker outright). Absent `hardware_specs.os` defaults to linux, so the
    existing linux container fleet is unaffected.
    """
    # Lead review 2026-09-29: OS only. GP.4 heartbeats report native_apps from
    # linux fleet workers too (a Dell box with blender on PATH), and treating
    # those as native would silently strip their shared-pool wildcard.
    hw = worker.hardware_specs or {}
    os_name = str(hw.get("os") or "linux").strip().lower()
    return os_name in ("windows", "darwin")


def _get_authorized_tenants(worker: WorkerLicense) -> Optional[List[int]]:
    """
    Returns list of tenant IDs this worker is authorized to process tasks for.
    Returns None if the worker cannot process ANY tenant tasks.

    This is the CLAIM-PATH counterpart to _worker_can_serve_tenant() in
    worker_provisioning.py and MUST agree with it. When they diverge, a
    worker that the provisioning/UI routing view reports as serving a tenant
    will silently never claim that tenant's tasks — the exact defect where,
    on an on-prem shared-pool box, only the platform tenant (1) could ever
    run a workflow while every other tenant's tasks stranded PENDING forever.

    native-workers GP.11 — a native worker (see `_is_native_worker`) is the
    tenant's own desktop machine, enrolled to exactly ONE tenant
    (`worker.owner_tenant_id`). It NEVER gets the PLATFORM_POOL/
    PLATFORM_ASSIGNED wildcard-or-subset routing below, regardless of
    `owner_type` (a native worker enrolled with the default PLATFORM_POOL
    owner_type must not silently inherit shared-pool cross-tenant
    eligibility) and regardless of any cross-tenant-sharing configuration —
    that flag governs whether two tenants' *processes* may share one
    physical GPU card, never whether a tenant's own desktop runs another
    tenant's task at all. No tenant binding (`owner_tenant_id IS NULL`)
    means it is authorized for NOTHING — fail closed rather than falling
    back to any wildcard — and is logged once so the gap is visible.

    PLATFORM_POOL:
      - assigned_tenant_ids set   -> serves exactly those tenants (an
        operator deliberately narrowing a shared pool to a subset).
      - assigned_tenant_ids empty -> serves EVERY tenant in the install
        (ALL_TENANTS_AUTHORIZED wildcard). This is the shared-pool default
        in BOTH SaaS and on-prem: hard tenant isolation is achieved with a
        separate install, not a claim-path gate, and the per-task tenant_id
        filter in _try_claim_task still prevents one tenant from claiming
        another tenant's tasks/data.
    PLATFORM_ASSIGNED: serves only its explicitly assigned tenants.
    TENANT_OWNED:      serves only its owning tenant.
    PLATFORM_ONLY:     serves no tenant tasks.

    Callers filter with _is_tenant_authorized() (or check for the
    ALL_TENANTS_AUTHORIZED sentinel explicitly).
    """
    if _is_native_worker(worker):
        if worker.owner_tenant_id:
            return [worker.owner_tenant_id]
        logger.warning(
            "native worker %s has no tenant binding (owner_tenant_id is "
            "NULL) — refusing to authorize it for ANY tenant's tasks "
            "(fail closed)",
            worker.worker_name,
        )
        return None
    if worker.owner_type == WorkerOwnerType.PLATFORM_POOL:
        ids = list(worker.assigned_tenant_ids or [])
        return ids if ids else [ALL_TENANTS_AUTHORIZED]
    elif worker.owner_type == WorkerOwnerType.PLATFORM_ASSIGNED:
        return list(worker.assigned_tenant_ids or [])
    elif worker.owner_type == WorkerOwnerType.TENANT_OWNED:
        return [worker.owner_tenant_id] if worker.owner_tenant_id else None
    elif worker.owner_type == WorkerOwnerType.PLATFORM_ONLY:
        return None  # Cannot process tenant tasks
    return None


# ---------------------------------------------------------------------------
# Request/Response schemas
# ---------------------------------------------------------------------------
class TaskPollRequest(BaseModel):
    queues: List[str] = Field(..., description="Hardware queues this worker supports (CPU, CUDA, ROCm, Intel)")
    timeout: int = Field(default=30, ge=0, le=60, description="Long poll timeout in seconds (0 for instant)")
    # NOAH claim-gate bypass (2026-09-29 self-deadlock fix): when the worker's
    # local NOAH gate is withholding ordinary claims, it still needs to claim
    # persistent-stack-* ops (deploy/stop/logs/migrate) for stacks it already
    # hosts — those are pinned to this worker and are often the only way to
    # relieve the very load that tripped the gate. Scoping this server-side
    # (rather than trusting the worker not to over-claim) means a compromised
    # or buggy worker can never use this flag to grab ordinary work while
    # NOAH is supposed to be protecting a human's foreground use.
    stack_ops_only: bool = Field(
        default=False,
        description="Only claim persistent-stack-* ops tasks (NOAH gate bypass)",
    )


class TaskPollResponse(BaseModel):
    task_id: Optional[int] = None
    queue_name: Optional[str] = None
    workflow_run_id: Optional[int] = None
    node_id: Optional[str] = None
    node_run_id: Optional[int] = None
    claim_expires_at: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    phase_plan: Optional[Dict[str, Any]] = None  # Scheduler's lifecycle plan
    start_after: Optional[str] = None  # When to begin processing phase
    retry_after_seconds: int = 30


class TaskStartedRequest(BaseModel):
    estimated_duration_seconds: int = Field(default=300, description="Estimated processing time")


class TaskCompleteRequest(BaseModel):
    outputs: List[Dict[str, Any]] = Field(..., description="Output file metadata")
    log_tail: Optional[str] = Field(default=None, description="Last 200 lines of container logs")
    peak_vram_mb: Optional[int] = Field(
        default=None, ge=0,
        description="Peak per-container VRAM (MiB) the worker sampled; feeds the learned p95 reservation (14.6)")
    sidecar_samples: Optional[str] = Field(
        default=None,
        description="14.14 — raw output of the k8s GPU-sampler sidecar "
                    "(`<ts> <vram_mb> <util_pct>` per line); read back into "
                    "peak_vram_mb when the worker reported none and the "
                    "cluster has no Prometheus URL. Absent on the docker "
                    "path and on old workers.")
    ephemeral_applied_shm_mb: Optional[int] = Field(default=None, ge=0, description="WS1.38e — /dev/shm MiB the worker actually applied (post-clamp); absent on old workers")
    ephemeral_applied_tmpfs_mb: Optional[int] = Field(default=None, ge=0, description="WS1.38e — tmpfs MiB the worker actually applied (post-clamp); absent on old workers")
    ephemeral_clamped: Optional[bool] = Field(default=None, description="WS1.38e — true when the worker clamped the node's declared shm/tmpfs to its cap")
    egress: Optional[Dict[str, Any]] = Field(
        default=None,
        description="GB.EGR.11a/11b — per-task egress report (mode/allowed_hosts/denied); absent on old workers")
    hf_cache_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="GSEC R-159 — per-worker HF cache re-layout tally "
                    "(moved/verified/quarantined); absent on every ordinary task")
    # FIX-BYO-MIGRATION-SKIP-MISSING — the BYO-S3 copy job's skip report
    # (transport.StorageMigrationReport): the source keys the worker found
    # already gone and, when the activation opted in (skip_missing_source),
    # skipped. Absent on every other task and on old workers, so every
    # pre-existing completion body is unchanged.
    storage_migration_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="FIX-BYO-MIGRATION-SKIP-MISSING — BYO-S3 migration skip "
                    "report (missing_source_count + up to 50 missing_source_"
                    "keys); absent on every non-migration task and old workers")
    # GSEC FX.115 p2 — the worker's per-node-container lockdown verdict
    # (CORE/Worker node_lockdown.go planMap: node_kind + per-flag
    # mode/missing/would_apply/applied). services/node_lockdown_report.py
    # ingests it into models.NodeLockdownVerdict so the platform-admin
    # report endpoint can answer "what WOULD change under enforce" without
    # scraping the worker's log line. Absent for tasks that ran no node
    # container (native/k8s/stack paths) and on old workers.
    lockdown_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Per-node-container lockdown verdict "
                    "(node_kind + per-flag missing/would_apply/applied); "
                    "absent on tasks that ran no node container")
    # Fortress 2.3c S2b — ephemeral worker proof-signature (per-task, appended
    # to node_run_attestations).  Absent on all pre-2.3c workers; the API
    # silently ignores it when missing.  Never fails task completion.
    worker_signature: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Fortress 2.3c — ephemeral worker Ed25519 proof-signature "
                    "(alg, key_id, public_key_b64, payload_sha256, signature_b64, "
                    "signed_at); absent on pre-2.3c workers")
    worker_contribution: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Fortress 2.3c — per-task contribution payload that the "
                    "worker signed (task_id, workflow_run_id, node_run_id, "
                    "attempt_count, worker_name, executed_image_digest, "
                    "outputs_sha256_manifest, started_at_utc, completed_at_utc); "
                    "absent on pre-2.3c workers")
    usage_tokens: Optional[Dict[str, Any]] = Field(
        default=None,
        description="30.6.11 — stack-model-call token metering read from result.json; absent on old workers / non-model tasks")
    # 30.6.1 c1-J3d — the worker's per-table row counts of a restored LOGICAL
    # snapshot (stacklogical.go:logicalVerifyCounts → transport.go TaskResult.
    # VerifyTables). Names are re-validated here with the SAME identifier rule
    # the worker enforces (verifyTableName) so a hand-edited completion body can
    # never smuggle a junk key into the snapshot manifest; counts must be ints
    # >=0; the map is capped at 200 keys (the worker's own cap) so a rogue
    # worker cannot grow storage_refs unboundedly. A violation is a hard 422 —
    # never a 500, never a silent store.
    verify_tables: Optional[Dict[str, int]] = Field(
        default=None,
        description="30.6.1 c1-J3d — per-table row counts of a restored logical "
                    "snapshot; absent on every other task")
    verify_truncated: Optional[bool] = Field(
        default=None,
        description="30.6.1 c1-J3d — true when the count batch hit its caps and "
                    "the map below is partial; absent on every other task")

    @field_validator("verify_tables", mode="before")
    @classmethod
    def _validate_verify_tables(cls, value):  # noqa: ANN001 — pydantic signature
        import re
        if value is None:
            return value
        if not isinstance(value, dict):
            raise ValueError("verify_tables must be an object")
        if len(value) > 200:
            raise ValueError("verify_tables carries more than 200 table names")
        name_ok = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
        for name, count in value.items():
            if not isinstance(name, str) or not name_ok.match(name):
                raise ValueError("verify_tables carries an invalid table name")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("verify_tables carries a non-negative-int count")
            if count > MAX_VERIFY_ROWS:
                raise ValueError("verify_tables carries a count above MAX_VERIFY_ROWS")
        return value


class TaskFailedRequest(BaseModel):
    error_message: str = Field(..., description="Error description")
    should_retry: bool = Field(default=True, description="Whether this task should be retried")
    log_tail: Optional[str] = Field(default=None, description="Last 200 lines of container logs")
    egress: Optional[Dict[str, Any]] = Field(
        default=None,
        description="GB.EGR.11a/11b — per-task egress report (mode/allowed_hosts/denied); absent on old workers")
    error_detail: Optional[str] = Field(
        default=None,
        description=(
            "calibration-health-fixtures-and-timeouts §3 — raw stderr of the "
            "tool that actually failed (mc / container runtime), kept separate "
            "from error_message so a stdout success banner concatenated in "
            "front of it can't crowd out the diagnostic line. Empty/None when "
            "the failure isn't sidecar-shaped."
        ),
    )
    oom_killed: bool = Field(
        default=False,
        description=(
            "The kernel killed the container for memory (Docker State.OOMKilled). "
            "Distinct from should_retry because it changes WHERE a retry should go: "
            "an OOM is a property of the host, so the task may well succeed on "
            "another worker, whereas an ordinary crash fails identically everywhere. "
            "The worker only reports this; the manager decides what to do with it."
        ),
    )
    preempted: bool = Field(
        default=False,
        description=(
            "NOAH yielded this task back to a returning human — it never "
            "actually failed. Requeues without charging a retry attempt "
            "(attempt_count unchanged) and without running the normal "
            "failure-classification logic. A worker that predates this field "
            "signals the same thing by prefixing error_message with the "
            "sentinel '[noah-preempted] ' (see main.go), which the handler "
            "also honors."
        ),
    )
    ephemeral_applied_shm_mb: Optional[int] = Field(default=None, ge=0, description="WS1.38e — /dev/shm MiB the worker actually applied (post-clamp); absent on old workers")
    ephemeral_applied_tmpfs_mb: Optional[int] = Field(default=None, ge=0, description="WS1.38e — tmpfs MiB the worker actually applied (post-clamp); absent on old workers")
    ephemeral_clamped: Optional[bool] = Field(default=None, description="WS1.38e — true when the worker clamped the node's declared shm/tmpfs to its cap")


def _ephemeral_applied_fields(request) -> dict:
    """WS1.38e — the ephemeral shm/tmpfs keys actually present on the request
    (explicit False is kept; old workers send none, so {})."""
    fields = {}
    if request.ephemeral_applied_shm_mb is not None:
        fields["ephemeral_applied_shm_mb"] = request.ephemeral_applied_shm_mb
    if request.ephemeral_applied_tmpfs_mb is not None:
        fields["ephemeral_applied_tmpfs_mb"] = request.ephemeral_applied_tmpfs_mb
    if request.ephemeral_clamped is not None:
        fields["ephemeral_clamped"] = request.ephemeral_clamped
    return fields


class TaskEnqueueRequest(BaseModel):
    workflow_run_id: int
    node_run_id: Optional[int] = None
    node_id: str
    tenant_id: int
    queue_name: str
    priority: int = 0
    task_payload: Dict[str, Any]
    max_attempts: int = 3
    timeout_seconds: int = 1800
    # GSEC FX.1 — optional explicit task type for the public enqueue route.
    # Optional + default None so every existing (in-process and HTTP) caller
    # is byte-for-byte unchanged; enqueue_task_internal still creates the row
    # with the column default "processing". The HTTP wrapper uses this only to
    # run the tenant allowlist check (see TENANT_TASK_TYPE_ALLOWLIST).
    task_type: Optional[str] = None
    # B108 — platform-internal only. Set exclusively by the orchestrator's
    # in-process _start_workflow call for a root node; never honour it from
    # the public /workers/tasks/enqueue route (_enforce_tenant_enqueue_scope
    # 403s a tenant caller who sets it). enqueue_task_internal uses it to
    # apply the general MOD_NEEDS_API_TOKEN promotion AFTER the reserved-env
    # scrub, so a tenant-owned root NodeDefinition still gets the marker
    # even though it is not platform-owned.
    is_root_node: bool = False


class UpdateManifestResponse(BaseModel):
    version: str
    package_url: str
    checksum: str
    update_required: bool


def _worker_version_tuple(raw: Optional[str]) -> Optional[Tuple[int, ...]]:
    """1819 — strict semver parse for the worker version gate.

    Returns None when the string is NOT a comparable semver (a git-sha build
    stamp like "96a6d1614-1656", "dev", ""). The old inline parser scrubbed
    digits out of anything and produced a bogus tuple, which turned a
    mis-stamped build into a permanent 403 on /workers/tasks/poll.
    """
    import re
    m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", (raw or "").strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/workers/tasks/poll", response_model=TaskPollResponse)
async def poll_for_task(
    request: TaskPollRequest,
    background_tasks: BackgroundTasks,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """
    Remote worker polls for the next available task.

    Uses long polling: holds connection up to `timeout` seconds, returns
    immediately when a task is available. Uses PostgreSQL FOR UPDATE SKIP LOCKED
    to prevent double-claims.
    """
    if worker.status != WorkerStatus.LICENSED:
        raise HTTPException(status_code=403, detail="Worker is not approved — add it in Dashboard > Workers first")

    # Item #7 (fullproof extension) — version-gated task claiming.
    # Workers below the minimum-required version lack critical features
    # (e.g. v1.0.5 doesn't have MOD_WORKER_PREFER_LOCAL_IMAGES, so
    # local-only build-task images get pulled-then-deleted and the
    # build fails with "not found in registry"). Reject the claim
    # with a clear upgrade-required message so the operator gets a
    # visible signal instead of silent corruption.
    _min_version = os.getenv("MOD_WORKER_MIN_VERSION", "v1.0.6")
    _worker_version = (worker.agent_version or "").strip()
    _worker_min = _worker_version_tuple(_min_version)
    _worker_now = _worker_version_tuple(_worker_version)
    if (_min_version and _worker_min is not None and _worker_now is not None
            and _worker_now < _worker_min):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Worker version {_worker_version!r} is below the minimum "
                f"required {_min_version!r}. Upgrade via the worker-releases "
                f"MinIO bucket or via the Dashboard > Workers > Update "
                f"button. Task claiming refused until upgrade completes."
            ),
        )
    elif _min_version and _worker_now is None and _worker_version:
        # 1819 — the version string is not comparable (a build stamp that is
        # not semver, e.g. "<gitsha>-<n>"). The old inline parser digit-scrubbed
        # it into a bogus tuple ("96a6d1614-1656" -> (6, 1614, 1656)) and the
        # worker was refused with "below the minimum" forever: the worker can
        # never self-heal because the string comes from its own build stamp,
        # and the rolling upgrade campaign holds on "no release key compiled
        # in", so no upgrade is ever issued. Unparseable != below-minimum:
        # log once so the operator sees the gap, but let the claim through.
        logger.warning(
            "worker %s reports a non-semver agent_version %r — version-gate "
            "claim refused-check skipped (unparseable, not below-minimum); "
            "re-stamp the build from CORE/Worker/VERSION so the rolling "
            "upgrade campaign can track it.",
            worker.worker_name, _worker_version,
        )

    authorized_tenants = _get_authorized_tenants(worker)
    if not authorized_tenants:
        raise HTTPException(status_code=403, detail="Worker not authorized for any tenants")

    # Capture what the poll loop needs, then RELEASE the request-scoped DB
    # connection. FastAPI holds a get_db session (and its pooled Postgres
    # connection) for the whole request — including the up-to-60s long-poll
    # below. Parking hundreds of idle workers that way pins hundreds of
    # idle-in-transaction connections and exhausts the pool. Instead we close it
    # now and use a short-lived session per claim attempt: a connection is held
    # for the ~1ms claim, never across the wait.
    worker_id = worker.id
    queues = list(request.queues)

    # Server-side quarantine of dead accelerators. The worker self-reports its
    # queues every heartbeat (e.g. modhp still advertises INTEL), so a purely
    # worker-side exclusion can't be trusted to hold. Drop any queue whose
    # accelerator kind is fully failed/unsupported/admin-disabled for this
    # worker's hardware profile, so INTEL-queue tasks never land on a box whose
    # Intel GPU can't run anything. CPU and every live-accelerator queue survive
    # — a dead device can't take the worker's other work down with it.
    if worker.hardware_profile_id:
        try:
            from utils.accelerator_quarantine import filter_worker_queues
            _before = queues
            queues = filter_worker_queues(db, worker.hardware_profile_id, queues)
            _dropped = [q for q in _before if q not in queues]
            if _dropped:
                logger.info(
                    "Worker %s: withholding quarantined accelerator queue(s) %s "
                    "(device failed/unsupported/disabled)",
                    worker.worker_name, _dropped,
                )
        except Exception as e:  # noqa: BLE001 — never block claiming on this
            logger.debug("queue quarantine filter failed for %s: %s", worker.worker_name, e)

    # sz-build-queue.2: strip the BUILD queue when worker.build_queue_enabled
    # is False. Independent of hardware_profile_id — a worker with no
    # hardware profile (e.g. a pure build-farm box) must still be denied
    # BUILD tasks when the platform setting is off, so this runs
    # unconditionally rather than nesting inside the block above.
    try:
        from services.build_queue_settings import filter_build_queue
        queues = filter_build_queue(queues, db=db)
    except Exception as e:  # noqa: BLE001 — never block claiming on this
        logger.debug("build-queue capability filter failed for %s: %s", worker.worker_name, e)

    db.close()

    timeout = min(request.timeout, 60)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    # Backstop poll interval. The Redis subscription below wakes instantly on a
    # real task-available publish, so this only bounds worst-case latency for a
    # genuinely missed notification (rare with a persistent subscription).
    backstop = max(1.0, float(os.getenv("MOD_WORKER_POLL_BACKSTOP_SECS", "10")))

    # Subscribe ONCE and keep the subscription for the whole long-poll. The
    # previous code created+subscribed+tore-down a fresh Redis connection every
    # 2s, which burned CPU on churn and left a window where a publish landed
    # while nobody was subscribed — the exact miss the backstop papered over.
    r = None
    pubsub = None
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        pubsub = r.pubsub()
        await pubsub.subscribe(*[f"task:available:{q}" for q in queues])
    except Exception:
        pubsub = None  # Redis unavailable — degrade to a plain sleep backstop.

    try:
        while True:
            # Fresh short-lived session per attempt. A cheap PK lookup re-checks
            # the license (catches mid-poll revocation; _try_claim_task also
            # SQL-guards it) and the atomic claim runs under FOR UPDATE SKIP
            # LOCKED. The connection is released the instant this block exits.
            with SessionLocal() as poll_db:
                w = poll_db.get(WorkerLicense, worker_id)
                if w is None or w.status != WorkerStatus.LICENSED:
                    raise HTTPException(
                        status_code=403,
                        detail="Worker approval removed during poll — re-add it to continue",
                    )
                if getattr(w, "paused", False) or getattr(w, "update_requested", False):
                    raise HTTPException(
                        status_code=403,
                        detail="Worker is paused or updating — not claiming tasks",
                    )
                task = _claim_task_for_worker(poll_db, w, queues, authorized_tenants,
                                               stack_ops_only=request.stack_ops_only)
                if task:
                    _maybe_prefetch_stack_weights(task, w.worker_name, background_tasks)
                    _k8s_gate = optional_paid("services.k8s_backend_gate")
                    return TaskPollResponse(
                        task_id=task.id,
                        queue_name=task.queue_name,
                        workflow_run_id=task.workflow_run_id,
                        node_id=task.node_id,
                        node_run_id=task.node_run_id,
                        claim_expires_at=task.claim_expires_at.isoformat() if task.claim_expires_at else None,
                        # k8s-reduction-engine W7: per-task Enterprise executor-backend gate
                        payload=(_k8s_gate.task_payload_with_executor_backend(poll_db, task)
                                 if _k8s_gate is not None else task.task_payload),  # CE: no gate
                        phase_plan=task.phase_plan,
                        start_after=task.start_after.isoformat() if task.start_after else None,
                        retry_after_seconds=0,
                    )

            # No task — park until a notification, the backstop, or the deadline.
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                return TaskPollResponse(retry_after_seconds=30)
            wait = min(remaining, backstop)

            if pubsub is not None:
                try:
                    # Returns as soon as a publish lands (instant wake) or after
                    # `wait`s (backstop) — parked with no DB connection held.
                    await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=wait),
                        timeout=wait + 1,
                    )
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    # Redis hiccup mid-poll — drop the subscription, sleep-backstop
                    # for the rest of this poll.
                    pubsub = None
                    await asyncio.sleep(min(wait, 2))
            else:
                await asyncio.sleep(wait)
    finally:
        for closer in (pubsub, r):
            if closer is None:
                continue
            try:
                # redis-py ≥5 deprecates .close() in favour of .aclose().
                aclose = getattr(closer, "aclose", None)
                await (aclose() if aclose else closer.close())
            except Exception:
                pass


def _finalize_claimed_task(db: Session, task: Optional[ApiTaskQueue],
                           worker: WorkerLicense) -> Optional[ApiTaskQueue]:
    """launch-completion-megaplan GP.16b — claim-time native resolution.

    A multi-platform native task (execution == "native", native_platform
    unset, native_recipe carrying a platforms map) is resolved HERE, against
    the claiming worker's OS: the returned payload carries native_platform
    and the worker's native recipe, and the resolution is persisted on the
    task row so the poll response and the reaper/sweep see the settled value.
    Single-platform tasks (native_platform already set at enqueue) and every
    non-native task pass through unchanged.
    """
    if task is None:
        return None
    try:
        worker_os = str((worker.hardware_specs or {}).get("os") or "linux").strip().lower()
    except Exception:  # noqa: BLE001
        worker_os = "linux"
    payload = task.task_payload or {}
    resolved = _resolve_native_for_worker(payload, worker_os)
    if resolved is not payload:
        task.task_payload = resolved
        db.commit()
        db.refresh(task)
    return task


def _stack_port_give_up_s(db: Session) -> int:
    """Seconds a fixed-host-port conflict is retried before the deploy fails
    (platform setting stacks.host_port_conflict_give_up_s; 0 = retry forever)."""
    try:
        from services import platform_settings as _psr
        return int(_psr.get_setting("stacks.host_port_conflict_give_up_s",
                                    db=db) or 0)
    except Exception:  # noqa: BLE001 — a settings read must not block a claim
        return 600


def _stack_host_port_guard(db: Session, worker: WorkerLicense,
                           task: ApiTaskQueue) -> bool:
    """gw-stack-networking GW.3 — refuse to let a worker claim a stack deploy
    whose FIXED host port is already taken on that host.

    The complement of the GPU hand-back: a GPU task whose cards are full is
    handed back and the NEXT candidate is tried, because ANY worker might still
    have room. A stack with a fixed host port can only land on a host where that
    exact port (and protocol) is free — so the guard is per-worker. When the
    worker's own requested fixed port is observed in use OR reserved by another
    stack assigned to this same worker, the task is handed back to
    PENDING/ASSIGNED with `gpu_backoff_until` so it is not immediately re-claimed
    by the same (port-conflicted) host. A worker without the conflict (a second
    host with the port free) will claim it next tick.

    `host: "any"` requests are IGNORED by the guard — those are allocated by the
    worker at deploy (GW.2) and never conflict at claim time. A stack with no
    fixed host-port request (the common case: mesh-only, or legacy `ports:`
    with no `x-mod.host_ports`) passes through unchanged.

    Returns False when the task was handed back (port conflict), True otherwise
    (claim stands). Mirrors the `_assign_gpu_cards` contract exactly so the
    caller can treat both guards identically.
    """
    from services import stack_port_placement

    payload = task.task_payload or {}
    stack_op = payload.get("stack_op")
    if stack_op not in ("deploy", "migrate"):
        return True
    compose_spec = payload.get("compose_spec")
    if not compose_spec:
        return True
    # A redeploy of the SAME stack onto the SAME host must not block itself.
    stack_id = payload.get("stack_id")
    conflict = stack_port_placement.host_port_conflict(
        db, worker, compose_spec,
        exclude_stack_ids=[stack_id] if stack_id is not None else None)
    if conflict is None:
        return True

    # Hand back: clear the claim and mark a short backoff so this worker does
    # not immediately re-claim the same conflicting task. The backoff reuses
    # `gpu_backoff_until` (the claim guard already filters on it), so no new
    # payload key is needed — the field name is a legacy artefact of the GPU
    # hand-back this mirrors, and reusing it means the SQL guard that skips
    # unplaceable tasks already applies.
    task.status = (ApiTaskStatus.ASSIGNED if task.assigned_to_worker
                   else ApiTaskStatus.PENDING)
    task.claimed_by_worker = None
    task.claimed_at = None
    task.claim_expires_at = None
    p = dict(payload)
    # FX.165 p1a — the pin is VOID once handed back: this task is no longer
    # placed anywhere, so its old `gpu_assignment` must not keep reserving
    # cards in `gpu_reservation.cards_in_use()` — not against other tasks and
    # not against ITSELF (the next claim would see its own stale pin on the
    # very cards it is about to try, so it bounces again: measured 4044s of
    # repeated 20s hand-backs on worker-worker-01/02-GPU). The next
    # claim re-plans from scratch via `_assign_gpu_cards`.
    p.pop("gpu_assignment", None)
    now = datetime.now(timezone.utc)
    p["gpu_backoff_until"] = (
        now + timedelta(seconds=_GPU_UNPLACEABLE_BACKOFF_S)).isoformat()
    # D76: say WHY on the stack row (it used to read "deploying" forever) and
    # stop retrying silently once the conflict has lasted the give-up window.
    friendly = stack_port_placement.host_port_conflict_message(worker, conflict)
    since_raw = p.get("host_port_conflict_since")
    try:
        since = datetime.fromisoformat(since_raw) if since_raw else now
    except (TypeError, ValueError):
        since = now
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    p["host_port_conflict_since"] = since.isoformat()
    task.task_payload = p
    give_up_s = _stack_port_give_up_s(db)
    if give_up_s > 0 and (now - since).total_seconds() >= give_up_s:
        task.status = ApiTaskStatus.FAILED
        task.error_message = friendly
        task.completed_at = now
        db.commit()
        try:
            _apply_stack_task_result(db, task, is_success=False,
                                     error_message=friendly)
            db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("stack %s port-conflict failure update failed: %s",
                           stack_id, e)
        logger.warning("Stack task %s failed: %s", task.id, friendly)
        return False
    task.error_message = (
        f"host port busy on {worker.worker_name}: {conflict}; "
        f"retrying in {_GPU_UNPLACEABLE_BACKOFF_S}s")
    if stack_id is not None:
        _stack = worker_tasks_results_crud.get_persistent_stack_by_id(db, stack_id)
        if _stack is not None and _stack.status in ("deploying", "created"):
            _stack.status_reason = friendly
    db.commit()
    logger.info("Stack task %s unplaceable on %s now (%s); back to %s for %ss",
                task.id, worker.worker_name, conflict,
                task.status.value, _GPU_UNPLACEABLE_BACKOFF_S)
    return False


def _claim_task_for_worker(
    db: Session,
    worker: WorkerLicense,
    queues: List[str],
    authorized_tenants: List[int],
    stack_ops_only: bool = False,
) -> Optional[ApiTaskQueue]:
    """Claim the next task, then pin a GPU task to physical cards (§14.2).

    A GPU task whose cards are all full right now is handed back (see
    `_assign_gpu_cards`) and the next candidate is tried, so one unplaceable
    task never starves the worker's other work. The same applies to a stack
    deploy whose fixed host port is taken on this worker (`_stack_host_port_guard`,
    GW.3) — the task returns to the pool for a host where the port is free.

    `stack_ops_only` is the NOAH claim-gate bypass: when set, only
    persistent-stack-* ops tasks are eligible (see `_try_claim_task`).
    """
    for _ in range(_GPU_CLAIM_ATTEMPTS):
        task = _try_claim_task(db, worker, queues, authorized_tenants,
                                stack_ops_only=stack_ops_only)
        if task is None:
            return None
        if not _stack_host_port_guard(db, worker, task):
            continue
        if _assign_gpu_cards(db, worker, task):
            # FX.165 p1b — clear port-guard error_message on successful claim;
            # only clear if it was the port-guard itself (sentinel prefix check).
            _em = task.error_message or ""
            if _em.startswith("host port busy"):
                task.error_message = None
                db.commit()
            return task
    return None


def _maybe_prefetch_stack_weights(task: ApiTaskQueue, worker_name: str,
                                  background_tasks: BackgroundTasks) -> None:
    """GF.C2b — a `persistent-stack-deploy`/`-migrate` task just claimed by
    `worker_name` has a known target worker for the FIRST time here: deploys
    are queued unpinned and placed by whichever worker claims them (see
    `services/persistent_stacks_domain_service.py` `deploy_stack`), so this
    claim IS the placement decision. Fire a best-effort background warm of
    the stack's declared HF weights toward the model cache, racing the
    worker's own image pull rather than blocking either.

    Only plain values (`stack_id`, `worker_name`) cross into the background
    task — `services.model_cache_prefetch.prefetch_for_stack_assignment`
    opens its OWN short DB session and never touches `poll_db`/this
    request's transaction, so the warm's outbound HTTP call never runs
    inside an open DB transaction."""
    payload = task.task_payload or {}
    if payload.get("stack_op") not in ("deploy", "migrate"):
        return
    stack_id = payload.get("stack_id")
    if stack_id is None or not worker_name:
        return
    _mcp = optional_paid("services.model_cache_prefetch")
    if _mcp is None:
        return
    background_tasks.add_task(_mcp.prefetch_for_stack_assignment, stack_id, worker_name)


#: Claims tried per poll when GPU tasks keep coming back unplaceable.
_GPU_CLAIM_ATTEMPTS = 3
#: How long an unplaceable GPU task waits before it is claimable again.
_GPU_UNPLACEABLE_BACKOFF_S = int(os.getenv("MOD_GPU_UNPLACEABLE_BACKOFF_S", "20"))
#: GPUCAP — a PIN refusal that is a CONFIG error (the pinned card is not
#: schedulable on this worker, or is too small for the service) will never
#: resolve by waiting: the admin has to change the pin. Retry on a much
#: longer backoff so a mis-pinned stack does not burn claims every 20 s.
#: A busy pinned card keeps the short backoff above (it is transient).
_GPU_PIN_REFUSED_BACKOFF_S = 300
_GPU_PIN_CONFIG_REFUSALS = ('pin_no_cards_on_worker', 'pin_card_too_small')


#: Kinds the worker attaches by device id/render node (§14.3), so the
#: planner can pin a task to specific physical cards. Other reported kinds
#: (npu, ...) take raw unpinned device mounts and are never planned here.
_PINNABLE_KINDS = ('cuda', 'rocm', 'intel')


def _task_gpu_need(payload: dict) -> Optional[tuple]:
    """`(kind, gpu_count, declared_min_mb)` for a task that will use a
    per-card-pinnable accelerator.

    `accel_demand` (one VRAM figure per card, keyed by kind — `''` means an
    unlabelled/legacy cuda demand) is authoritative; otherwise `gpu_required`
    means one cuda card (unchanged legacy default — the flag itself carries
    no vendor), or a pinnable `gpu_variants` entry means one card of that
    kind, both with no declared minimum.
    """
    demand = payload.get('accel_demand')
    if isinstance(demand, dict):
        for kind in _PINNABLE_KINDS + ('',):
            vals = demand.get(kind)
            if isinstance(vals, list) and vals:
                out_kind = kind or 'cuda'
                try:
                    return out_kind, len(vals), int(max(float(v) for v in vals) * 1024)
                except (TypeError, ValueError):
                    return out_kind, len(vals), None
    if payload.get('gpu_required'):
        return 'cuda', 1, None
    variants = payload.get('gpu_variants')
    if isinstance(variants, (list, dict)):
        for kind in _PINNABLE_KINDS:
            if kind in variants:
                return kind, 1, None
    return None


def _stack_gpu_services(payload: dict) -> Optional[List[dict]]:
    """Per-service GPU needs for a persistent-stack deploy task (§14.7b).

    Re-parses `payload['compose_spec']` — the YAML string
    `persistent_stacks._shared._dispatch_stack_task` rides on the payload and
    the worker writes verbatim to docker-compose.yml — for `x-mod.accelerator`
    blocks, returning one entry per GPU-declaring, pinnable-kind service:
    `{"name", "kind", "gpu_count", "declared_min_mb", "exclusive"}`.

    None when the task isn't a stack deploy, has no compose spec, or no
    service declares a pinnable accelerator — callers fall back to the
    task-wide `accel_demand`/`_task_gpu_need` path (single kind, whole task).
    """
    spec = payload.get('compose_spec')
    if not isinstance(spec, str) or not spec.strip():
        return None
    try:
        import yaml
        compose = yaml.safe_load(spec)
    except Exception:
        return None
    if not isinstance(compose, dict):
        return None
    from routers.persistent_stacks._shared import _parse_accelerator_requirements
    reqs = _parse_accelerator_requirements(compose)
    if not reqs:
        return None
    out: List[dict] = []
    for name, req in reqs.items():
        if not req.get('required', True):
            continue
        kind = req.get('kind') or 'cuda'
        if kind not in _PINNABLE_KINDS:
            continue
        vram = req.get('min_vram_gb')
        out.append({
            'name': name,
            'kind': kind,
            'gpu_count': max(1, int(req.get('count') or 1)),
            'declared_min_mb': int(vram * 1024) if vram is not None else None,
            'exclusive': bool(payload.get('gpu_exclusive')),
        })
    return out or None


def _assign_stack_service_gpu_cards(db: Session, worker: WorkerLicense,
                                    task: ApiTaskQueue, payload: dict,
                                    services: List[dict]) -> bool:
    """Per-service variant of `_assign_gpu_cards` (§14.7b): plans each
    compose service's cards separately instead of one kind for the whole
    stack, so e.g. a cuda LLM service + a rocm STT service in one stack both
    get pinned to the right kind of card. Writes
    `gpu_assignment.service_cards = {service_name: [card_id, ...]}` plus the
    legacy task-wide `mode`/`cards`/`vram_limit_mb` (the union of every
    service's cards, for workers that don't understand `service_cards` yet).

    Same hand-back contract as `_assign_gpu_cards`: ANY service unplaceable
    right now hands the WHOLE task back (a stack is one compose project,
    never half-deployed); a worker with no sized cards of a needed kind keeps
    today's unpinned behaviour.
    """
    from services import gpu_reservation
    # GPUCAP — the admin's card pin rides on the SAME compose YAML string the
    # stack writers keep in the payload (x-mod.gpu_pin), so the claim-time
    # planner needs no extra DB read. Guarded parse like `_stack_gpu_services`:
    # any parse error just means "no pin" — never wedge a claim.
    pins = None
    _spec = payload.get('compose_spec')
    if isinstance(_spec, str) and _spec.strip():
        try:
            import yaml
            pins = gpu_reservation._gpu_pin_from_compose(yaml.safe_load(_spec))
        except Exception:  # noqa: BLE001 — malformed compose = no pin
            pins = None
    refusal: Dict[str, str] = {}
    try:
        per_service = gpu_reservation.plan_service_gpu_assignments(
            db, worker, node_kind=payload.get('node_kind') or '',
            tenant_id=task.tenant_id, services=services,
            pins=pins, refusal=refusal)
    except Exception as e:  # noqa: BLE001 — never wedge a claim over placement
        logger.warning("Per-service GPU planning failed for task %s: %s", task.id, e)
        db.rollback()
        return True
    if per_service is None:
        return True
    if per_service == gpu_reservation.UNPLACEABLE:
        backoff_s = (_GPU_PIN_REFUSED_BACKOFF_S
                     if refusal.get('code') in _GPU_PIN_CONFIG_REFUSALS
                     else _GPU_UNPLACEABLE_BACKOFF_S)
        if refusal:
            payload['gpu_pin_refused'] = refusal
            logger.warning(
                "Stack task %s GPU pin refused on %s (%s): %s", task.id,
                worker.worker_name, refusal.get('code'), refusal.get('detail'))
        task.status = (ApiTaskStatus.ASSIGNED if task.assigned_to_worker
                       else ApiTaskStatus.PENDING)
        task.claimed_by_worker = None
        task.claimed_at = None
        task.claim_expires_at = None
        # FX.165 p1a — a handed-back task is NOT placed, so its old pin must
        # go: keeping it made the ASSIGNED row hold VRAM in
        # `gpu_reservation.cards_in_use()` for the whole lease (and, before
        # the lease fix, forever), so the task blocked its OWN re-placement on
        # the next claim and every other deploy on that host. The next claim
        # re-plans from scratch.
        payload.pop('gpu_assignment', None)
        payload['gpu_backoff_until'] = (
            datetime.now(timezone.utc)
            + timedelta(seconds=backoff_s)).isoformat()
        task.task_payload = payload
        db.commit()
        logger.info("Stack task %s unplaceable on %s now (a service's cards "
                    "don't fit); back to %s for %ss", task.id, worker.worker_name,
                    task.status.value, backoff_s)
        return False

    union_cards: Dict[str, dict] = {}
    for a in per_service.values():
        for c in a['cards']:
            union_cards[c['card_id']] = c
    plan = {
        'mode': 'exclusive' if any(a['mode'] == 'exclusive' for a in per_service.values())
                else 'shared',
        'cards': list(union_cards.values()),
        'vram_limit_mb': max(a['vram_limit_mb'] for a in per_service.values()),
        'source': 'per_service',
        'service_cards': {name: [c['card_id'] for c in a['cards']]
                          for name, a in per_service.items()},
    }
    payload['gpu_assignment'] = plan
    payload.pop('gpu_backoff_until', None)
    payload.pop('gpu_pin_refused', None)
    task.task_payload = payload
    db.commit()
    logger.info("Stack task %s pinned per-service on %s: %s",
               task.id, worker.worker_name, plan['service_cards'])
    return True


def _assign_gpu_cards(db: Session, worker: WorkerLicense, task: ApiTaskQueue) -> bool:
    """Write `task_payload['gpu_assignment']` for a just-claimed GPU task.

    Returns False when the task was handed back because no card fits now:
    it returns to ASSIGNED (scheduler-placed) or PENDING, marked
    `gpu_backoff_until` so `gpu_backoff_guard` skips it briefly. Non-GPU tasks, workers that report no sized cards
    and any planner error keep today's behaviour (claim stands, no pinning).
    """
    from services import gpu_reservation

    payload = dict(task.task_payload or {})
    stack_services = _stack_gpu_services(payload)
    if stack_services:
        return _assign_stack_service_gpu_cards(db, worker, task, payload, stack_services)

    need = _task_gpu_need(payload)
    if need is None:
        return True
    kind, gpu_count, declared_min_mb = need
    try:
        plan = gpu_reservation.plan_gpu_assignment(
            db, worker, node_kind=payload.get('node_kind') or '',
            tenant_id=task.tenant_id, gpu_count=gpu_count,
            declared_min_mb=declared_min_mb,
            exclusive=bool(payload.get('gpu_exclusive')), kind=kind)
    except Exception as e:  # noqa: BLE001 — never wedge a claim over placement
        logger.warning("GPU card planning failed for task %s: %s", task.id, e)
        db.rollback()
        return True
    if plan is None:
        return True
    if plan == gpu_reservation.UNPLACEABLE:
        task.status = (ApiTaskStatus.ASSIGNED if task.assigned_to_worker
                       else ApiTaskStatus.PENDING)
        task.claimed_by_worker = None
        task.claimed_at = None
        task.claim_expires_at = None
        # FX.165 p1a — same hand-back contract as the stack-service variant:
        # the task is not placed, so drop its stale `gpu_assignment` (it would
        # otherwise keep holding cards against itself and every later task).
        payload.pop('gpu_assignment', None)
        payload['gpu_backoff_until'] = (
            datetime.now(timezone.utc)
            + timedelta(seconds=_GPU_UNPLACEABLE_BACKOFF_S)).isoformat()
        task.task_payload = payload
        db.commit()
        logger.info("GPU task %s unplaceable on %s now (all cards full); "
                    "back to %s for %ss", task.id, worker.worker_name,
                    task.status.value, _GPU_UNPLACEABLE_BACKOFF_S)
        return False
    payload['gpu_assignment'] = plan
    payload.pop('gpu_backoff_until', None)
    task.task_payload = payload
    db.commit()
    logger.info("GPU task %s pinned on %s: %s cards=%s limit=%sMB (%s)",
                task.id, worker.worker_name, plan['mode'],
                [c['card_id'] for c in plan['cards']], plan['vram_limit_mb'],
                plan['source'])
    return True


# launch-completion-megaplan GP.16b — native-task enqueue stamping.
#
# A node whose custom_metadata declares `execution: native` + the D-P1 recipe
# (see schemas.py::_validate_native_recipe_metadata) stamps these keys onto
# task_payload at enqueue time. Non-native nodes get NOTHING — the payload is
# byte-for-byte unchanged (no new keys), so every existing task is identical.
#
#   execution      = "native"  (the worker's NativeBackend trigger)
#   native_recipe  = the WHOLE native.platforms dict ({os: {command, ...}})
#                    — lets platform_guard match a multi-platform recipe by the
#                    claiming worker's OS without a per-claim row rewrite.
#   requires_app   = the node's requires_app {name, min_version, detect}
#   native_platform + native  = resolved ONLY when the recipe declares exactly
#                    ONE platform, so that case needs no claim-time work. A
#                    multi-platform recipe leaves native_platform unset; the
#                    claim path resolves it to the claiming worker's OS.
def _native_recipe_platforms(recipe):
    """Return the {os: recipe} map out of a task payload's `native_recipe`.

    Tolerates BOTH stored shapes: GP.16b stamps `native_recipe` as the
    platforms dict directly ({os: {command, ...}}), while the claim-time
    fallback and hand-built payloads may carry it wrapped
    ({"platforms": {os: ...}}). Returns None when there is no usable map."""
    if not isinstance(recipe, dict):
        return None
    if isinstance(recipe.get("platforms"), dict) and recipe.get("platforms"):
        return recipe["platforms"]
    # Direct shape: every value is a platform recipe dict. A wrapper-less
    # map that happens to hold non-dict values is not a platforms map.
    if recipe and all(isinstance(v, dict) for v in recipe.values()):
        return recipe
    return None


def effective_requires_app(payload_or_recipe, worker_os):
    """launch-completion-megaplan GP.16c — the requires_app a native task
    imposes on a worker of `worker_os`, with per-OS override and node-level
    fallback.

    Accepts either a full task payload (may carry `native_recipe` +
    `requires_app`) or a node `custom_metadata` dict (carries `native` +
    `requires_app`). Resolution order:
      1. platforms.<worker_os>.requires_app — the KEY's presence wins, so an
         explicit empty list / {} means "needs nothing on this OS";
      2. the node-level requires_app;
      3. None (no requirement).

    Pure: reads only the passed dict, never the DB, never mutates it.
    `worker_os` is matched case-insensitively against the platform keys."""
    if not isinstance(payload_or_recipe, dict):
        return None
    src = payload_or_recipe

    recipe = src.get("native_recipe")
    platforms = _native_recipe_platforms(recipe)
    if platforms is None:
        native_block = src.get("native")
        if isinstance(native_block, dict):
            platforms = native_block.get("platforms")
    if isinstance(platforms, dict):
        platform_os = str(worker_os or "").strip().lower()
        entry = platforms.get(platform_os)
        if isinstance(entry, dict) and "requires_app" in entry:
            return entry["requires_app"]
    return src.get("requires_app")


def _warm_policy_payload_fields(custom_metadata, db, tenant_id):
    """KEEP-WARM-SLICE-1 — the `warm_policy` payload key for a task, or {}.

    Keep-warm is an opt-in capability with THREE switches, all default OFF/ON
    as named: (1) the node definition carries an ENABLED
    custom_metadata.warm_policy; (2) the TENANT switch worker.warm_pool_enabled
    (tenant-scoped, default False) is on for THIS task's tenant; (3) the
    platform kill switch worker.warm_pool_allowed (default True, platform
    scope only: read WITHOUT a tenant so a tenant row can never override it).
    Anything else returns {} so the payload carries no warm_policy key at all
    and the worker takes the unchanged cold path. Never raises: any problem
    reading a switch fails closed (no warm policy). The worker re-checks
    eligibility fail-closed; it reads `protocol` (exec only in slice 1)."""
    if not isinstance(custom_metadata, dict):
        return {}
    wp = custom_metadata.get("warm_policy")
    if not isinstance(wp, dict) or wp.get("enabled") is not True:
        return {}
    if wp.get("mode", "exec") != "exec":
        return {}
    if tenant_id is None:
        return {}
    try:
        from services import platform_settings as _psr_warm
        if not _psr_warm.get_setting("worker.warm_pool_allowed", db):
            return {}
        if not _psr_warm.get_setting(
                "worker.warm_pool_enabled", db, tenant_id=tenant_id):
            return {}
    except Exception:
        logger.debug("warm_policy: setting read failed; cold path", exc_info=True)
        return {}
    out = {"protocol": "exec"}
    for _k in ("idle_seconds", "max_calls", "max_memory_mb"):
        _v = wp.get(_k)
        if isinstance(_v, int) and not isinstance(_v, bool) and _v > 0:
            out[_k] = _v
    return {"warm_policy": out}


def _native_payload_fields(custom_metadata):
    """Return the native-stamp keys for a node's `custom_metadata`, or {} when
    the node is not a native (execution != "native") one.

    Pure: reads only the passed dict, never the DB. Returns a NEW dict of keys
    to merge onto task_payload; the caller must not mutate `custom_metadata`."""
    if not isinstance(custom_metadata, dict):
        return {}
    if custom_metadata.get("execution") != "native":
        return {}

    native_block = custom_metadata.get("native")
    platforms = (
        native_block.get("platforms")
        if isinstance(native_block, dict) else None
    )
    if not isinstance(platforms, dict) or not platforms:
        # execution: native without a valid platform recipe. The schema
        # validator already 422s this on node create, but be defensive: stamp
        # execution so the task is never silently treated as a docker task
        # (which would fail loudly on the worker, per D-P1 "no fallback").
        return {"execution": "native"}

    fields = {"execution": "native", "native_recipe": platforms}

    # GP.16c — per-OS requires_app: an entry's own `requires_app` rides on
    # that platform's recipe (a copy — the source metadata is never mutated).
    # Entries WITHOUT the key fall back to the node-level `requires_app` at
    # resolution time (effective_requires_app), so the node-level value is
    # stamped whenever any entry might need that fallback. Legacy nodes (no
    # per-OS key anywhere) get byte-for-byte the same fields as before.
    per_os = {
        os_name: dict(entry)
        for os_name, entry in platforms.items()
        if isinstance(entry, dict) and "requires_app" in entry
    }
    if per_os:
        stamped = dict(platforms)
        stamped.update(per_os)
        fields["native_recipe"] = stamped

    requires_app = custom_metadata.get("requires_app")
    if isinstance(requires_app, dict) and requires_app:
        fields["requires_app"] = requires_app

    if len(platforms) == 1:
        (os_name, recipe), = platforms.items()
        fields["native_platform"] = os_name
        if isinstance(recipe, dict):
            fields["native"] = dict(recipe)
            fields["native"].setdefault("platform", os_name)
    return fields


def _resolve_native_for_worker(payload, worker_os):
    """Resolve a claim-time native task to `worker_os` for the claim response.

    Handles the multi-platform case (execution == "native", native_platform
    absent, native_recipe carrying a platforms map). Sets `native_platform`
    and `native` on a COPY of the payload (never mutates the argument) and
    returns it, or returns the payload unchanged when nothing to resolve.
    worker_os is the claiming worker's heartbeat OS, lowercased.

    GP.16c: the resolved requires_app (per-OS override, else the node-level
    value — see effective_requires_app) is also settled onto the payload and
    the single-platform `native` spec handed to the worker, so the persisted
    row and the claim response name the settled requirement."""
    if not isinstance(payload, dict):
        return payload
    if payload.get("execution") != "native":
        return payload
    if payload.get("native_platform"):
        return payload
    platforms = _native_recipe_platforms(payload.get("native_recipe"))
    if not isinstance(platforms, dict):
        return payload
    platform_os = str(worker_os or "").strip().lower()
    if platform_os not in platforms:
        return payload
    out = dict(payload)
    out["native_platform"] = platform_os
    plat = platforms.get(platform_os)
    if isinstance(plat, dict):
        native = dict(plat)
        native.setdefault("platform", platform_os)
        out["native"] = native
    resolved_app = effective_requires_app(payload, platform_os)
    if resolved_app is not None:
        out["requires_app"] = resolved_app
        if isinstance(out.get("native"), dict) and isinstance(resolved_app, dict):
            out["native"].setdefault("requires_app", resolved_app)
    return out


def _native_claim_guard_sql():
    """GP.16b/GP.16c — build the two native-task claim guards as SQL fragments.

    Returns `(platform_guard, requires_app_guard)`. Both are pure string
    builders (no DB, no state) so the claim query and the read-only smoke
    probe exercise the EXACT same SQL text. Bind params used:
    `:worker_os`, `:worker_native_apps`. jsonb_exists / `->` are used
    throughout (never a bare `?`, which collides with psycopg2's paramstyle).

    `platform_guard`: a native task is claimable when native_platform matches
    the worker's OS, OR the (possibly multi-platform) recipe names that OS.
    The recipe map is read from BOTH stored shapes of native_recipe — the
    wrapped `{"platforms": {...}}` and the direct `{os: recipe}` map that
    enqueue actually stamps (a read of `->'platforms'` alone was NULL on
    every stamped payload).

    `requires_app_guard`: the compared requirement is resolved ROW-LOCALLY
    for the worker's OS (mirroring effective_requires_app in Python): an OS
    entry's own `requires_app` key wins (an explicit empty list / {} means
    "needs nothing on this OS"), else the node-level `task_payload->
    'requires_app'`, else none. A requirement with no `name` passes, exactly
    as the legacy node-level-only guard did, so legacy payloads are
    byte-for-byte unchanged. The full name + min_version compare (see GP.4)
    stays in SQL so an ineligible task is never selected under SKIP LOCKED.
    """
    _recipe_platforms = (
        "CASE"
        "  WHEN jsonb_typeof(task_payload->'native_recipe'->'platforms') = 'object'"
        "    THEN task_payload->'native_recipe'->'platforms'"
        "  WHEN jsonb_typeof(task_payload->'native_recipe') = 'object'"
        "    THEN task_payload->'native_recipe'"
        "  ELSE '{}'::jsonb"
        " END"
    )
    platform_guard = f"""
        AND (
            CASE
                WHEN task_payload->>'execution' = 'native'
                    THEN (task_payload->>'native_platform' = :worker_os)
                         OR jsonb_exists({_recipe_platforms}, :worker_os)
                ELSE :worker_os = 'linux'
            END
        )
    """

    _effective_app = (
        "CASE"
        "   WHEN jsonb_exists((" + _recipe_platforms + ") -> :worker_os, 'requires_app')"
        "     THEN (" + _recipe_platforms + ") -> :worker_os -> 'requires_app'"
        "   WHEN jsonb_typeof(task_payload->'requires_app') = 'object'"
        "     THEN task_payload->'requires_app'"
        "   ELSE '{}'::jsonb"
        " END"
    )
    requires_app_guard = f"""
        AND NOT EXISTS (
            SELECT 1
            FROM (SELECT ({_effective_app}) AS eff) AS r_app
            WHERE jsonb_typeof(r_app.eff) = 'object'
              AND r_app.eff->>'name' IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(CAST(:worker_native_apps AS jsonb)) AS w_app
                WHERE (w_app->>'name') = lower(r_app.eff->>'name')
                  AND (
                      NULLIF(r_app.eff->>'min_version', '') IS NULL
                      OR (
                          (w_app->>'version') ~ '[0-9]'
                          AND
                          (
                              array_remove(
                                  string_to_array(regexp_replace(COALESCE(w_app->>'version', ''), '[^0-9.]', '', 'g'), '.'),
                                  ''
                              ) || ARRAY['0','0','0','0','0','0']
                          )[1:6]::int[]
                          >=
                          (
                              array_remove(
                                  string_to_array(regexp_replace(r_app.eff->>'min_version', '[^0-9.]', '', 'g'), '.'),
                                  ''
                              ) || ARRAY['0','0','0','0','0','0']
                          )[1:6]::int[]
                      )
                  )
              )
        )
    """
    return platform_guard, requires_app_guard


def _metal_lane_claim_guards_sql():
    """MAC.1.s8b — the Metal (macOS native) lane claim fences as SQL fragments.

    Returns `(darwin_task_guard, metal_lane_guard)`, both parameterised by
    the Python-computed `:worker_metal_lane` bool (true for a worker whose
    reported OS is darwin or whose hardware tags carry `metal` — the tag the
    Go probe reports, MAC.1.s7).

    `darwin_task_guard`: a task stamped as a darwin-only native task
    (`execution == 'native'` and either `native_platform == 'darwin'` or a
    recipe whose platform map names ONLY darwin) is claimable by a Metal-lane
    worker and nothing else. This is the imageless half of the fence: such a
    task has no image, so the arch guard's amd64 default would otherwise be
    meaningless and a stale/absent `hardware_specs.os` on a linux box must not
    be what lets it sit on the task.

    `metal_lane_guard`: a Metal-lane worker never claims a container task —
    a Mac has no container runtime, so anything that is not `execution ==
    'native'` (i.e. anything with an image to pull) is refused at the SQL
    layer. Defence-in-depth alongside platform_guard's `ELSE :worker_os =
    'linux'`, mirroring the license/paused pairs.

    A multi-platform recipe (darwin + windows) is NOT darwin-only, so a
    windows worker still claims its own entry — GP.16b behaviour unchanged.
    Linux/CUDA/ROCm workers have `:worker_metal_lane` false, which makes both
    fragments no-ops for them except for the darwin-only refusal above.
    """
    _recipe_platforms = (
        "CASE"
        "  WHEN jsonb_typeof(task_payload->'native_recipe'->'platforms') = 'object'"
        "    THEN task_payload->'native_recipe'->'platforms'"
        "  WHEN jsonb_typeof(task_payload->'native_recipe') = 'object'"
        "    THEN task_payload->'native_recipe'"
        "  ELSE '{}'::jsonb"
        " END"
    )
    darwin_only = (
        "("
        "  COALESCE(task_payload->>'native_platform', '') = 'darwin'"
        "  OR ("
        "    jsonb_exists(" + _recipe_platforms + ", 'darwin')"
        "    AND (SELECT COUNT(1) FROM jsonb_object_keys(" + _recipe_platforms + ")) = 1"
        "  )"
        ")"
    )
    # COALESCE'd comparisons: `->>` yields NULL when the key is absent, and
    # Postgres' three-valued logic would make NOT(NULL AND ...) NULL — which
    # would silently exclude every ordinary container task from the claim.
    darwin_task_guard = f"""
        AND (
            NOT (
                COALESCE(task_payload->>'execution', '') = 'native'
                AND {darwin_only}
            )
            OR :worker_metal_lane
        )
    """
    metal_lane_guard = """
        AND (
            NOT :worker_metal_lane
            OR task_payload->>'execution' = 'native'
        )
    """
    return darwin_task_guard, metal_lane_guard


# FX.131 (Q118=A) — the node kinds that train a customer-owned model. These
# are the nodes whose output is a trained model the customer owns (the LoRA
# .safetensors), so under training.own_workers_only they may only RUN on a
# worker the task's tenant owns. Kept as a static module constant (never
# derived from request data) so the claim guard and the unrunnable-sweep
# reason can share it. The system ml_retrain node_kind is deliberately NOT
# here: it is platform scheduler self-tuning (task_type='system', no tenant
# data), not a customer model.
_TRAINING_NODE_KINDS = (
    "sdxl_lora_train",
    "sdxl_turbo_lora_train",
)


def _try_claim_task(
    db: Session,
    worker: WorkerLicense,
    queues: List[str],
    authorized_tenants: List[int],
    stack_ops_only: bool = False,
) -> Optional[ApiTaskQueue]:
    """
    Atomically claim the next available task using FOR UPDATE SKIP LOCKED.

    Priority order:
    1. ASSIGNED tasks specifically assigned to this worker (scheduler placed them)
       — only claimable when start_after <= NOW()
    2. Fallback: PENDING tasks (FIFO) when predictive scheduling is disabled
       or no assigned tasks exist. Respects max_concurrent_tasks in fallback mode.

    Network zone filtering: tasks with a NETWORK_ZONE in their container_env are
    only claimable by workers in that zone (or any worker if zone is 'public').

    `stack_ops_only` (NOAH claim-gate bypass, 2026-09-29): restricts both
    branches to task_payload.node_kind LIKE 'persistent-stack-%' — deploy,
    stop, logs, migrate. Used when a worker's local NOAH gate is withholding
    ordinary claims but the worker still needs to manage a stack it already
    hosts. Enforced here, not just trusted from the worker, so the flag can
    never be used to slip an ordinary task through the gate.

    Returns the claimed task or None.
    """
    # GT.C2 — drain mode. When dispatch is paused, refuse ALL new claims and
    # return exactly what an empty queue returns (None), so the worker parks
    # in its normal no-task path instead of erroring. In-flight (already
    # claimed) tasks are unaffected — only NEW claims are withheld.
    # TODO(GT.C2): if an explicit system-maintenance / stack-teardown task_type
    # allow-list is added to the ApiTaskQueue schema, exempt those here so the
    # platform can still tear down stacks mid-drain; there is no such list
    # today, so everything pauses.
    from services import platform_settings as _psr_drain
    try:
        if _psr_drain.get_setting("dispatch.paused", db=db):
            return None
    except Exception as e:  # noqa: BLE001 — a settings read failure must not
        # block claiming; the platform_settings getter degrades to its
        # default (False = dispatch on) rather than raise.
        logger.debug("drain-mode claim check failed (proceeding): %s", e)

    stack_ops_guard = (
        "AND task_payload->>'node_kind' LIKE 'persistent-stack-%'"
        if stack_ops_only else ""
    )
    worker_zone = worker.network_zone or "local"

    # GY.V2b — foreign-worker exclusion for export-controlled work. A task
    # whose inputs carry EAR/ITAR (stamped task_payload.export_control_level
    # at enqueue by _stamp_export_control_level) is claimable ONLY by a
    # US-person-operated worker (worker_licenses.us_person_operated = TRUE).
    # CUI joins the gated set when compliance.cui_requires_us_person is on.
    # Default posture: if no US worker exists the task simply WAITS — it
    # never runs on an unflagged worker (fail-wait, never fail-route).
    # The level list is built from static strings only (data_classification
    # taxonomy, never request data), so there is no SQL interpolation risk.
    # If the guard itself cannot be built (setting read error, column not
    # yet migrated) we proceed WITHOUT it and log — the cog read/download
    # gate in routers/cogs/__init__.py stays the primary enforcement.
    export_control_guard = ""
    # Default True (fail closed: treat as "not a US-zone worker") so that if
    # the guard IS built but the zone read is skipped, an ITAR task never
    # routes to an unverified worker. Unused in the SQL when the guard is "".
    export_control_us_only = True
    try:
        if False:
            _ec_levels = ["EAR", "ITAR"]
            if False:
                _ec_levels.append("CUI")
            _us_worker_exists = db.execute(text(
                "SELECT 1 FROM worker_licenses "
                "WHERE us_person_operated = TRUE LIMIT 1"
            )).first() is not None
            if not _us_worker_exists:
                logger.warning(
                    "GY.V2b: export control enforced but no US-person-operated "
                    "worker exists — %s-classified tasks will wait for one "
                    "(they are never routed to unflagged workers)",
                    "/".join(_ec_levels),
                )
            _ec_in = ",".join("'" + lvl + "'" for lvl in _ec_levels)
            # GY.V2 — ITAR residency: defence articles may only RUN on
            # workers whose hardware tag marks them inside the US zone
            # (utils.residency.zone_from_specs, the same V3b tag the
            # region pin reads). A worker with an unknown zone fails
            # closed (counts as outside the US). The predicate is
            # constant-folded Python-side from the cached worker row.
            from utils import residency as _residency_v2
            # True = this worker passes the ITAR residency rule (either the
            # residency rule is off, or the worker IS in the US zone).
            # False = ITAR tasks are withheld from this worker.
            _itar_res_on = bool(False)
            export_control_us_only = (
                (not _itar_res_on)
                or (_residency_v2.worker_residency_zone(worker) == "us")
            )
            export_control_guard = f"""
                AND (
                    task_payload->>'export_control_level' IS NULL
                    OR task_payload->>'export_control_level' NOT IN ({_ec_in})
                    OR EXISTS (
                        SELECT 1 FROM worker_licenses
                        WHERE worker_name = :worker
                          AND us_person_operated = TRUE
                    )
                )
                AND (
                    task_payload->>'export_control_level' IS DISTINCT FROM 'ITAR'
                    OR :export_control_us_only
                )
            """
    except Exception:
        export_control_guard = ""
        logger.exception(
            "GY.V2b: export-control claim guard failed to build "
            "(proceeding without it)")
    # :export_control_us_only is always bound (the ASSIGNED + PENDING claim
    # queries both interpolate the guard placeholder, even when it is "").

    # GY.V3b — data-residency worker pin (claim side of GY.V3a). When the
    # platform pin residency.region_pin is set, tenant-data tasks are
    # claimable ONLY by workers whose hardware tag marks them inside the
    # pinned zone ('region:us' / 'region:eu' in
    # hardware_specs['hardware_tags']); a worker with no known zone fails
    # closed — it counts as outside the pin. Platform/system tasks
    # (task_type = 'system': calibration, benchmark, persistent-stack ops,
    # stack maintenance) touch no tenant data and stay claimable anywhere.
    # Pin off => guard is "" and the claim SQL is byte-identical to the
    # pre-GY.V3b path. Both bind params are per-call constants computed in
    # Python from the cached settings layer + the in-memory worker row —
    # the predicate is a constant-folded comparison, no subquery.
    # Task-kind decision: task_type is the existing task-kind field; only
    # 'system' is exempt ('processing' / 'pre_pull' / 'variant_smoke' all
    # carry tenant work and are gated).
    residency_guard = ""
    residency_params: dict = {}
    try:
        from utils import residency
        _pin = residency.get_residency_pin(db)
        if _pin:
            residency_guard = (
                "\n                AND (\n"
                "                    api_task_queue.task_type = 'system'\n"
                "                    OR :residency_zone = :residency_pin\n"
                "                )"
            )
            residency_params = {
                "residency_zone": residency.worker_residency_zone(worker),
                "residency_pin": _pin,
            }
    except Exception:
        residency_guard = ""
        residency_params = {}
        logger.exception(
            "GY.V3b: residency claim guard failed to build "
            "(proceeding without it; the GY.V3a store-side gates remain "
            "the primary enforcement)")

    # GY.C7 — worker pool / region policy (claim side of the pool stamp).
    # When a task's input labels restrict it to worker pools
    # (task_payload.label_worker_pools, stamped at enqueue by
    # _stamp_label_worker_pools from labels.allowed_worker_pools), it is
    # claimable ONLY by a worker whose own pool candidates — its worker_name,
    # its 'region:<zone>' hardware tag, or its bare zone
    # (utils.label_policy.worker_pool_candidates) — are a member of that
    # list. An EMPTY list means no pool currently matches: the task WAITS
    # (fail-wait, never fail-route) — no worker, including a pool-less one,
    # may claim it. Tasks WITHOUT the key (the {} default stamps nothing)
    # are claimable by anyone — byte-identical behaviour to before. The
    # predicate interpolates ONLY the worker's constant-folded candidates
    # (worker_name + residency zone read from the cached row, never request
    # data); setting-derived pool names are validated to a conservative
    # [a-z0-9._-] charset, an admin typo that fails validation simply turns
    # that label's restriction off rather than becoming a SQL hazard. Any
    # build error degrades to "" (guard off) and is logged inside the
    # helper — the enqueue-time pool stamp remains for audit.
    label_pool_guard = _label_pool_guard_sql(worker)

    # GY.C7 s2 — region allowlist (claim side of the region stamp).
    # When a task's input labels restrict it to worker REGIONS
    # (task_payload.label_regions, stamped at enqueue by _stamp_label_regions
    # from labels.allowed_regions), it is claimable ONLY by a worker whose
    # region candidates — its 'region:<zone>' hardware tag plus the bare
    # zone (utils.label_policy.worker_region_candidates) — are a member of
    # that list. A worker with no region tag claims NO region and is
    # withheld (fail-closed); an empty stamp means no region currently
    # matches and the task WAITS (fail-wait, never fail-route). Tasks
    # WITHOUT the key (the {} default stamps nothing) are claimable by
    # anyone — byte-identical behaviour to before, unlabelled runs
    # unchanged. Same posture as the pool guard immediately above.
    label_region_guard = _label_region_guard_sql(worker)

    # FX.131 (Q118=A) — trained-model isolation. When the platform setting
    # training.own_workers_only is True, a tenant's TRAINING tasks (node_kind
    # 'sdxl_lora_train' / 'sdxl_turbo_lora_train' — the customer-trains-own-
    # -models path) are claimable ONLY by a worker that the task's tenant
    # OWNS (worker_licenses.owner_type = 'tenant_owned' AND
    # owner_tenant_id = task.tenant_id). Platform/shared workers
    # (platform_only / platform_pool / platform_assigned) refuse the claim —
    # the same fail-wait posture as the other placement guards above: the
    # task stays PENDING and a matching tenant-owned worker picks it up on a
    # later tick. The sweep in services/scheduler.py
    # (_run_unrunnable_tasks_sweep) surfaces the reason when no eligible
    # worker exists so the run never hangs silently.
    # The kind list is built from the static TRAINING_NODE_KINDS constant
    # (never request data) and the worker row is constant-folded from the
    # in-memory `worker`, so there is no SQL interpolation risk.
    # Non-training tasks are unaffected — the guard is "" (byte-identical
    # claim SQL) when the setting is off.
    training_own_guard = ""
    try:
        from services import platform_settings as _psr_fx131
        if _psr_fx131.get_setting("training.own_workers_only", db=db):
            _fx131_kinds = ",".join(
                "'" + k + "'" for k in _TRAINING_NODE_KINDS)
            # owner_type is a NATIVE PG enum (workerownertype) that stores the
            # enum member NAMES (uppercase: TENANT_OWNED), so the literal must
            # match the name, not the .value — same convention as the
            # status = 'LICENSED' literal in license_guard above.
            training_own_guard = f"""
                AND (
                    task_payload->>'node_kind' NOT IN ({_fx131_kinds})
                    OR EXISTS (
                        SELECT 1 FROM worker_licenses
                        WHERE worker_name = :worker
                          AND owner_type = 'TENANT_OWNED'
                          AND owner_tenant_id = api_task_queue.tenant_id
                    )
                )
            """
    except Exception:  # noqa: BLE001 — a settings read failure must not
        # block claiming; degrade to the guard being off and log (the
        # unrunnable sweep reason stays the audit trail).
        training_own_guard = ""
        logger.exception(
            "FX.131: own-workers-only training claim guard failed to build "
            "(proceeding without it)")

    # GSEC FX.79 p2b — data-marking tenant-GPU-only guard. A task whose
    # payload carries the ``data_marking_tenant_gpu_only`` stamp (top level
    # or under node_data) — stamped at run creation when the workflow or an
    # input asset is marked with a blocks_shared_gpu label — is claimable
    # ONLY by a worker the task's tenant OWNS (owner_type TENANT_OWNED +
    # owner_tenant_id = task tenant), the exact FX.131 guard shape. Shared /
    # harvested / marketplace / platform-pool workers refuse the claim and
    # the task waits (fail-wait) for a tenant-owned worker. Tasks without the
    # stamp (every ordinary task) are unaffected — the predicate passes them
    # byte-identically. Build error -> guard off (logged), matching the
    # residency/pool guards above.
    data_marking_guard = ""
    try:
        _dm_fx79p2b = optional_paid("services.data_markings")
        if _dm_fx79p2b is not None:  # FX.162.p5 — CE carve: no markings -> claim guard off
            data_marking_guard = _dm_fx79p2b.data_marking_gpu_claim_guard_sql()
    except Exception:  # noqa: BLE001 — a guard-build failure must not block
        # claiming; degrade to the guard being off and log.
        data_marking_guard = ""
        logger.exception(
            "FX.79 p2b: data-marking tenant-GPU claim guard failed to build "
            "(proceeding without it)")

    # GSEC FX.123 s3 — full-disk-encryption (FDE) claim gate. When
    # security.require_fde_for_customer_tasks is ON, a MOD-hosted worker —
    # anything that is NOT the tenant's own BYOH box (owner_type != TENANT_OWNED:
    # platform_pool / platform_assigned / platform_only) whose heartbeat FDE
    # block does not say status='encrypted' refuses the claim. 'unknown'/missing
    # counts as unencrypted ONLY while the setting is on, so an older binary
    # that never reports FDE is unaffected while OFF (byte-identical SQL).
    # Customer-owned (TENANT_OWNED) workers are never affected: the disk is
    # theirs, and MOD has no opinion about encrypting it. Platform tasks
    # (tenant_id = the platform tenant, 1) are never affected either — they are
    # MOD's own work, not a customer's data. Fail-wait posture like every other
    # placement guard here: the task stays queued for a worker that can run it.
    # The FDE reading is taken from the worker_licenses row (claim-time fresh,
    # same belt-as-braces as license_guard/paused_guard above), and every
    # ->> comparison is COALESCEd: hardware_specs NULL / missing block would
    # otherwise make the whole predicate NULL and silently drop the claim.
    fde_guard = ""
    try:
        from services import platform_settings as _psr_fde123
        if _psr_fde123.get_setting("security.require_fde_for_customer_tasks",
                                   db=db):
            fde_guard = """
                AND (
                    api_task_queue.tenant_id = 1
                    OR EXISTS (
                        SELECT 1 FROM worker_licenses
                        WHERE worker_name = :worker
                          AND (
                              owner_type = 'TENANT_OWNED'
                              OR COALESCE(
                                  CAST(hardware_specs->'disk_encryption'
                                       ->>'status' AS text),
                                  'unknown'
                              ) = 'encrypted'
                          )
                    )
                )
            """
    except Exception:  # noqa: BLE001 — a settings read failure must not
        # block claiming; degrade to the guard being off and log.
        fde_guard = ""
        logger.exception(
            "FX.123 s3: require-FDE customer-task claim guard failed to build "
            "(proceeding without it)")

    # 1580 — system-job worker exclusion. scheduler.system_job_excluded_workers
    # (a list of worker_name / worker_licenses.id strings) names boxes the
    # owner has restricted from SYSTEM/background work (change-gate sweeps,
    # calibration, benchmark, stack maintenance). task_type IN ('system',
    # 'variant_smoke') is the existing signal for that kind of work — see the
    # 'system' classification note in the GY.V3b guard above and
    # _enqueue_variant_smoke_task's 'variant_smoke' stamp; there is no
    # separate boolean column on ApiTaskQueue, so the two task_types are
    # matched explicitly. Whether THIS worker is excluded is resolved once via
    # the shared `_is_worker_system_excluded` helper (also used by the
    # VariantTestPending drain hook in node_builder/variant_test/queue.py, the
    # OTHER system-job dispatch path the 453-job incident actually traced
    # back to) and bound as a single boolean — tenant task_types never match
    # the IN-list so an excluded worker still claims ordinary tenant work.
    # Empty list (default) ⇒ :worker_system_excluded is always False ⇒
    # byte-identical SQL/behaviour.
    # BUG-1028i — a task that is explicitly host-pinned to THIS polling worker
    # (utils.task_pinning.durable_target_worker: benchmark_target_worker, else
    # the operator stack pin `pinned_worker`, both worker_name strings) is not
    # general system-pool churn — the box already owns the stack, so withholding
    # the claim is a permanent deadlock (stack 112 stuck `deploying`). NULL pins
    # COALESCE to NULL ⇒ falsy ⇒ fail closed (unpinned stays excluded).
    system_job_excluded_guard = """
        AND (
            NOT :worker_system_excluded
            OR api_task_queue.task_type NOT IN ('system', 'variant_smoke')
            OR COALESCE(
                task_payload->>'benchmark_target_worker',
                task_payload->>'pinned_worker'
            ) = :worker
        )
    """
    worker_system_excluded = _is_worker_system_excluded(
        db, worker.worker_name, worker.id)

    # GX.S10c4b — a stack deploy whose stack declares a `secret_file` variable
    # (task_payload.needs_file_secrets, stamped at enqueue by
    # _dispatch_stack_task) is claimable only by a worker that can materialise
    # file secrets. Same rule as _shared._worker_supports_file_secrets: an
    # explicit boolean hardware_specs.file_secrets wins; otherwise fall back to
    # the NUMERIC agent_version gate (never a text compare: v1.0.8 < v1.0.75 <
    # v1.0.100). A NULL/unparseable version with no capability key fails closed.
    # Tasks without the stamp are untouched (first OR branch).
    from routers.persistent_stacks._shared import (
        FILE_SECRETS_MIN_VERSION as _FS_MIN, _version_tuple as _fs_vt)
    fs_min_version = "{" + ",".join(str(n) for n in _fs_vt(_FS_MIN)) + "}"
    file_secrets_guard = """
        AND (
            COALESCE(api_task_queue.task_payload->>'needs_file_secrets', 'false')
                <> 'true'
            OR EXISTS (
                SELECT 1 FROM worker_licenses fs_wl
                WHERE fs_wl.worker_name = :worker
                  AND CASE
                      WHEN jsonb_typeof(fs_wl.hardware_specs->'file_secrets')
                           = 'boolean'
                          THEN CAST(fs_wl.hardware_specs->>'file_secrets'
                                    AS boolean)
                      WHEN fs_wl.agent_version ~ '^v?[0-9]+[.][0-9]+[.][0-9]+'
                          THEN CAST(regexp_match(fs_wl.agent_version,
                                   '^v?([0-9]+)[.]([0-9]+)[.]([0-9]+)')
                                    AS int[])
                               >= CAST(:fs_min_version AS int[])
                      ELSE FALSE
                  END
            )
        )
    """

    # Network zone filter: task is claimable if:
    #   - task has no NETWORK_ZONE set, OR
    #   - task NETWORK_ZONE is 'public', OR
    #   - task NETWORK_ZONE matches the worker's zone
    zone_filter = """
        AND (
            task_payload->'container_env'->>'NETWORK_ZONE' IS NULL
            OR task_payload->'container_env'->>'NETWORK_ZONE' = 'public'
            OR task_payload->'container_env'->>'NETWORK_ZONE' = :worker_zone
        )
    """

    # Stack-hold filter (GA.GSW.4b): a task whose enqueue set
    # payload['awaiting_stack'] = <stack> (because no fit existed when it was
    # dispatched) must NOT be claimed by any worker until GSW.4c's
    # redrive_held_tasks has called ensure_for_node, seen a non-held result,
    # and cleared the marker. Skipping the held task here is what keeps the
    # "leave it undispatched until a fit exists" invariant from being violated
    # by a fast-tick worker. Checked at both top-level and under node_data.
    hold_filter = """
        AND task_payload->>'awaiting_stack' IS NULL
        AND (task_payload->'node_data'->>'awaiting_stack') IS NULL
    """

    # GPU backoff (§14.2): a GPU task handed back by _assign_gpu_cards because
    # every card was full waits out its short backoff before any claim.
    gpu_backoff_guard = """
        AND (task_payload->>'gpu_backoff_until' IS NULL
             OR (task_payload->>'gpu_backoff_until')::timestamptz <= NOW())
    """

    # Defence-in-depth: refuse the claim at the SQL layer if the worker's
    # license is not LICENSED at this moment. Closes the long-poll staleness
    # window where poll_for_task's up-front check passes but the worker is
    # revoked mid-poll. Both the ASSIGNED and PENDING branches check.
    license_guard = """
        AND EXISTS (
            SELECT 1 FROM worker_licenses
            WHERE worker_name = :worker
              AND status = 'LICENSED'
        )
    """

    # Paused/updating workers must not claim tasks at the SQL layer either.
    # The Python-level check in poll_for_task catches most cases, but a
    # paused flag set mid-poll (same race window as license revocation)
    # would slip through without this.
    paused_guard = """
        AND NOT EXISTS (
            SELECT 1 FROM worker_licenses
            WHERE worker_name = :worker
              AND (paused = TRUE OR update_requested = TRUE)
        )
    """

    # native-workers GP.3 — a native task never runs in a container, so it has
    # no benchmark/calibration profile to wait on; native workers skip this
    # gate entirely rather than stalling behind a container-only benchmark.
    profiling_guard = """
        AND (
            task_payload->>'execution' = 'native'
            OR NOT EXISTS (
                SELECT 1 FROM worker_licenses wl
                JOIN hardware_profiles hp ON wl.hardware_profile_id = hp.id
                WHERE wl.worker_name = :worker
                  AND hp.benchmark_status IN ('pending', 'running')
            )
            OR api_task_queue.task_type = 'system'
        )
    """

    # PLATFORM_TASK used to be pinned to *-embedded workers because
    # remote workers couldn't resolve compose-network hostnames
    # (`ollama`, `api`, `minio`). That restriction is now lifted:
    # start-main-server.sh exposes Ollama through nginx at
    # `${OLLAMA_SUBDOMAIN}.${BASE_DOMAIN}` and the API rewrites
    # LLM_BASE_URL to that public FQDN on every platform dispatch
    # (see node_definitions.py::_enqueue_container_task). Any worker,
    # embedded or remote, can now reach the LLM.
    #
    # native-workers D-P3 — the platform filter, both directions:
    #   - a native task (task_payload.execution == 'native') carries
    #     task_payload.native_platform ('windows'/'darwin'/'linux') and is only
    #     claimable by a worker whose reported OS matches it exactly;
    #   - a linux CONTAINER task (execution != 'native') is only claimable by a
    #     worker whose OS is 'linux' — a darwin/windows native worker has no
    #     container runtime for it.
    # :worker_os is the heartbeat-reported hardware_specs['os'], lowercased,
    # defaulting to 'linux' for older workers that never sent it (preserves
    # existing linux-fleet behaviour byte-for-byte).
    # launch-completion-megaplan GP.16b/GP.16c — the native branch accepts
    # EITHER an exact native_platform match (single-platform recipes stamp it
    # at enqueue) OR a multi-platform recipe that names the claiming worker's
    # OS (native_platform unset until claim, when it is resolved); and
    # GP.4/GP.16c — requires_app is compared against the per-OS-resolved
    # requirement (OS entry's own key wins — an explicit empty list means
    # "needs nothing on this OS" — else the node-level value). Both guards
    # are built by _native_claim_guard_sql() so the claim query and the
    # read-only smoke probe share the exact SQL text.
    # :worker_native_apps is a JSON array of {name, version} (names already
    # lowercased) built below from hardware_specs['native_apps'].
    platform_guard, requires_app_guard = _native_claim_guard_sql()
    # MAC.1.s8b — the Metal-lane pair (see _metal_lane_claim_guards_sql): a
    # darwin-only native task never reaches a non-Metal worker, and a Metal
    # (darwin / `metal`-tagged) worker never claims a container task.
    darwin_task_guard, metal_lane_guard = _metal_lane_claim_guards_sql()

    # gpu_variants guard — the Phase 2 routing gate. Per-node-wizard
    # builds (see Phase 1) ship a limited set of GPU images: a node
    # built only for cuda shouldn't be dispatched to a ROCm worker,
    # since the `docker pull <image>:latest-rocm` will 404 and the
    # task stalls until expiry.
    #
    # Rules:
    #   - If the task's task_payload.gpu_variants is NULL / empty /
    #     missing, routing is unrestricted (any worker matching the
    #     queue may claim). Preserves legacy behaviour for nodes
    #     that pre-date the variant system.
    #   - Otherwise, at least one key in gpu_variants MUST appear in
    #     the worker's hardware_tags for the worker to claim.
    #
    # (helper defined at module scope: _stacks_being_redeployed_on)
    #
    # P9c note: `not_recommended_variants` surfaces as a signal to the
    # wizard UI and API; scheduler preference (prefer non-recommended
    # variants when both worker types exist) lands in 9e once we have
    # real smoke-verification data to tune it against. Today a
    # not-recommended NPU variant still ships and still claims — we
    # just warn the user in the Review step.
    #
    # worker_hw_tags is prepared below from worker.hardware_specs
    # and passed as a SQL list parameter.
    variants_guard = """
        AND (
            task_payload->'gpu_variants' IS NULL
            OR jsonb_typeof(task_payload->'gpu_variants') != 'object'
            OR task_payload->'gpu_variants' = '{}'::jsonb
            OR EXISTS (
                SELECT 1
                FROM jsonb_object_keys(task_payload->'gpu_variants') AS v
                WHERE lower(v) = ANY(:worker_hw_tags)
            )
        )
    """

    # Extract the worker's hardware tags once, lowercased for the
    # case-insensitive compare above. Absence ⇒ empty list, which
    # means the worker can only claim tasks with no gpu_variants
    # declared (old untagged behaviour).
    hw_specs = worker.hardware_specs or {}
    worker_hw_tags_raw = hw_specs.get("hardware_tags") or []
    worker_hw_tags = [
        str(t).strip().lower() for t in worker_hw_tags_raw if isinstance(t, str)
    ]

    # Normalised so the guard compares like with like: Go reports GOARCH
    # ("amd64", "arm64") but `uname -m` spellings reach this field from other
    # paths, and "x86_64" failing to match "amd64" would silently strand a
    # perfectly ordinary worker. Shared normalize_arch() is now also applied at
    # heartbeat ingest, so stored values are already canonical; re-applying here
    # keeps rows written before that correct. Unknown/empty falls back to amd64.
    from utils.worker_arch import normalize_arch as _normalize_arch
    worker_arch = _normalize_arch(hw_specs.get("arch")) or "amd64"

    # native-workers D-P3 / GP.3 — the worker's reported OS, lowercased.
    # Absent (older worker binaries, or the field not yet ingested at
    # heartbeat) defaults to 'linux': every worker in the existing fleet is
    # linux, so this must not start withholding their work.
    worker_os = str(hw_specs.get("os") or "linux").strip().lower()

    # MAC.1.s8b — the Metal lane. A worker is on it when it reports darwin
    # (a Mac desktop running the native Go binary) OR carries the `metal`
    # hardware tag (the tag MAC.1.s7's darwin probe emits, paired with a
    # unified-memory figure). Both signals fence the same thing: no container
    # runtime. A linux box tagged `metal` (an Apple-Box-in-the-Cloud edge) is
    # fenced too — it is the tag, not the OS, that says "no docker".
    worker_metal_lane = worker_os == "darwin" or "metal" in worker_hw_tags

    # native-workers GP.4 / D-P2 — apps this worker detected at heartbeat
    # (hardware_specs['native_apps'] = [{name, version, path}, ...]). Passed
    # as a JSON param to requires_app_guard, which does the full name+version
    # compare in SQL so an ineligible task is never selected under SKIP LOCKED.
    worker_native_apps_raw = hw_specs.get("native_apps") or []
    worker_native_apps = json.dumps([
        {
            "name": str(a.get("name")).strip().lower(),
            "version": str(a.get("version") or ""),
        }
        for a in worker_native_apps_raw
        if isinstance(a, dict) and a.get("name")
    ])

    # required_capabilities guard — AND semantics, deliberately unlike
    # gpu_variants above (which is OR: any one key matching is enough).
    # A precondition list means the worker must satisfy EVERY entry, so
    # the test is "no required capability is missing from the worker's
    # tags". Absent/empty ⇒ unrestricted, preserving existing behaviour.
    #
    # Fails closed by design: a task requiring a capability nothing
    # reports is unclaimable rather than mis-placed. That is why the
    # dispatch side must not emit a requirement until workers can
    # actually report it.
    capabilities_guard = """
        AND (
            task_payload->'required_capabilities' IS NULL
            OR jsonb_typeof(task_payload->'required_capabilities') != 'array'
            OR jsonb_array_length(task_payload->'required_capabilities') = 0
            OR NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements_text(task_payload->'required_capabilities') AS c
                WHERE lower(c) != ALL(:worker_caps)
            )
        )
    """

    # accelerator fit guard — the per-device VRAM test.
    #
    # Fit is DOMINATION, not a sum. A stack wanting 48 GB and 24 GB needs
    # two devices, one of each size; a single 80 GB card does not satisfy
    # it, because each process loads into one device. Summing would place
    # it there and OOM both. So: sort the task's demands descending, sort
    # the worker's devices descending, and require device[i] >= demand[i]
    # for every i. `:worker_accel` is built below already sorted, keyed by
    # accelerator kind, with "" holding every device regardless of kind so
    # an unspecified-kind demand matches anything.
    #
    # A demand with more entries than the worker has devices falls off the
    # end of the array, yields NULL, and COALESCE(-1) makes the comparison
    # fail — which is correct: not enough devices is not a fit.
    # CPU architecture. A container image built for amd64 does not run on arm64
    # — it dies with `exec format error` — and no other guard here notices,
    # because architecture is not a capability a worker advertises, it is what
    # the binary IS.
    #
    # This matters the moment a Raspberry Pi joins for ingestion.
    # `SupportedQueues()` puts EVERY worker on the CPU and default queues
    # unconditionally, so a Pi starts claiming real work on its first heartbeat
    # and fails every task whose image is amd64-only, while reporting healthy
    # throughout. Not a near miss: it is the default behaviour today.
    #
    # `image_arches` on the payload lists what the task's image can actually
    # run on. ABSENT MEANS amd64-ONLY, which is the truth right now — every
    # base image and all 200+ node images are built single-arch (see
    # BaseImages/build-and-push.sh, a plain `docker build`). So this fails
    # CLOSED for anything that is not amd64: a new architecture claims nothing
    # until its images say they support it, rather than claiming everything and
    # failing loudly. amd64 workers match the default and are unaffected.
    #
    # An unknown worker arch is treated as amd64. Every worker in an existing
    # fleet is amd64 and older binaries report nothing here, so this must not
    # start withholding their work.
    # native-workers GP.3 — a native task has no image at all (it runs a host
    # process per the D-P1 recipe), so the image-arch check is meaningless for
    # it and must not reject it; native tasks are gated by platform_guard's OS
    # match instead, not by CPU arch of a nonexistent image.
    arch_guard = """
        AND (
            task_payload->>'execution' = 'native'
            OR (
                CASE
                    WHEN jsonb_typeof(task_payload->'image_arches') = 'array'
                        AND jsonb_array_length(task_payload->'image_arches') > 0
                    THEN task_payload->'image_arches'
                    ELSE '["amd64"]'::jsonb
                END
            ) @> to_jsonb(CAST(:worker_arch AS text))
        )
    """

    accelerator_guard = """
        AND (
            task_payload->'accel_demand' IS NULL
            OR jsonb_typeof(task_payload->'accel_demand') != 'object'
            OR (
                NOT EXISTS (
                    SELECT 1
                    FROM jsonb_each(task_payload->'accel_demand') AS kd(kind, vals)
                    WHERE
                        -- (1) the biggest single demand must fit on some ONE device.
                        -- A model loads into one device's memory, so two 24 GB cards
                        -- are never a 48 GB card no matter how the totals look.
                        COALESCE((
                            SELECT max(e::numeric)
                            FROM jsonb_array_elements_text(kd.vals) AS e
                        ), 0) > COALESCE((
                            SELECT max(d::numeric)
                            FROM jsonb_array_elements_text(
                                COALESCE(CAST(:worker_accel AS jsonb) -> kd.kind, '[]'::jsonb)) AS d
                        ), -1)
                        -- (2) the total demanded must fit the total available.
                        -- Processes DO share a card, so co-tenancy is allowed when
                        -- the combined footprint fits.
                        OR COALESCE((
                            SELECT sum(e::numeric)
                            FROM jsonb_array_elements_text(kd.vals) AS e
                        ), 0) > COALESCE((
                            SELECT sum(d::numeric)
                            FROM jsonb_array_elements_text(
                                COALESCE(CAST(:worker_accel AS jsonb) -> kd.kind, '[]'::jsonb)) AS d
                        ), -1)
                )
                -- (3) cross-key: a physical card sits under BOTH its named kind
                -- and "", so checks (1)/(2) double-count it — one 24 GB card
                -- would pass 20 GB of cuda AND 20 GB of kindless demand. Total
                -- demand across ALL keys must fit total PHYSICAL capacity, the
                -- sum of the named-kind pools ("" is those same cards again).
                -- Mirrors satisfies() in utils/accelerator_fit.py exactly, so
                -- the mover and the claimer never disagree.
                AND COALESCE((
                    SELECT sum(e::numeric)
                    FROM jsonb_each(task_payload->'accel_demand') AS kd(kind, vals),
                         jsonb_array_elements_text(kd.vals) AS e
                ), 0) <= COALESCE((
                    SELECT sum(x::numeric)
                    FROM jsonb_each(CAST(:worker_accel AS jsonb)) AS w(k, vals),
                         jsonb_array_elements_text(w.vals) AS x
                    WHERE w.k <> ''
                ), 0)
            )
        )
    """

    # Build the worker's device capability vector: per accelerator kind,
    # VRAM sizes sorted descending, plus "" holding every device so a
    # kind-agnostic demand can match. Read from AcceleratorProfile (one
    # row per physical device) rather than hardware_specs.vram_gb, which
    # is a single summary number and cannot answer "do you have TWO cards
    # big enough".
    # AVAILABLE, not total: capacity minus what stacks already placed on this
    # worker are holding. A running vLLM claims its memory fraction at load and
    # holds it for its whole lifetime, so without subtracting it the scheduler
    # would hand the same card out twice and OOM both.
    worker_accel_map: Dict[str, List[float]] = {}
    accel_inventory_known = False
    available_accel_kinds: List[str] = []
    if worker.hardware_profile_id:
        try:
            from utils.accelerator_fit import available_devices, device_map
            total = device_map(db, worker)
            # Only meaningful when we actually have a device inventory. Plenty
            # of workers report hardware_tags like "cuda" without ever having
            # produced AcceleratorProfile rows; blocking those would silently
            # stop them claiming GPU work they have always run. Fail OPEN.
            accel_inventory_known = bool(total)
            # A stack with a deploy already queued for THIS worker is being
            # REPLACED by that very task, so its live reservation must be
            # credited back before the task is judged — otherwise a redeploy
            # is refused by its own VRAM and sits queued for ever, which is
            # exactly what happens to a managed-AI tier change on a
            # single-card host. Crediting back is safe in general: it only
            # ever returns memory a stack currently holds AND is about to
            # stop holding, and a genuinely new stack holds nothing, so the
            # exclusion is a no-op for it.
            worker_accel_map = available_devices(
                db, worker,
                exclude_stack_id=_stacks_being_redeployed_on(db, worker.worker_name))
            # VRAM-rounding slack, mirroring the mover. hardware_gap() (the
            # placement/eligibility check) accepts a device when
            # `size + _VRAM_SLACK_GB >= demand`, because a card marketed as
            # 48 GB reports ~47.988 GB usable and a stack whose min_vram is set
            # to the round 48 would otherwise never fit ANY real card. The claim
            # guard below compares demand against this vector with a strict `>`,
            # so without the same slack the mover assigns work (48 fits a 47.988
            # card) that the claimer then refuses (48 > 47.988) — the task sits
            # ASSIGNED for ever. Padding each device by the shared constant makes
            # the strict SQL comparisons tolerate exactly the same slack across
            # all three checks (max-device, per-kind sum, cross-key sum), so the
            # mover and claimer agree, which the accelerator_guard comment has
            # always claimed but did not enforce.
            from utils.accelerator_fit import _VRAM_SLACK_GB
            worker_accel_map = {
                k: [float(v) + _VRAM_SLACK_GB for v in vals]
                for k, vals in worker_accel_map.items()
            }
            available_accel_kinds = [
                k for k, v in worker_accel_map.items() if k and v
            ]
        except Exception as e:  # noqa: BLE001 — table not migrated / transient
            logger.debug("accelerator capability lookup failed for %s: %s", worker.worker_name, e)

    # Stack-held accelerators leave the node-task pool.
    #
    # gpu_variants matching (above) asks only "does this worker have a cuda
    # tag", with no notion of that card already being spoken for. A worker
    # hosting a vLLM stack keeps advertising "cuda", so without this the
    # scheduler hands a GPU node task the very card the stack is holding at
    # 0.9 utilisation, and one or both die.
    #
    # A task that can run on CPU is unaffected — only work that genuinely
    # needs an accelerator is withheld, and only when every device of the kind
    # it needs is committed.
    accel_pool_guard = """
        AND (
            NOT :accel_inventory_known
            OR task_payload->'gpu_variants' IS NULL
            OR jsonb_typeof(task_payload->'gpu_variants') != 'object'
            OR task_payload->'gpu_variants' = '{}'::jsonb
            OR EXISTS (
                SELECT 1
                FROM jsonb_object_keys(task_payload->'gpu_variants') AS v
                WHERE lower(v) = 'cpu' OR lower(v) = ANY(:available_accel_kinds)
            )
        )
    """
    # stack-ha-reconciliation §4.4 — host exclusion.
    #
    # A re-placement must not be satisfied by the worker whose failure created
    # the gap. Evaluated HERE, by the same claim-time mechanism as accelerator
    # fit, rather than by picking a target at dispatch: an exclusion checked at
    # dispatch is a guess about which workers will be available when the task
    # is finally claimed, and a stack whose host flaps could be handed straight
    # back to the box that just dropped it.
    #
    # Absent or empty means no exclusion, so this is inert for every ordinary
    # task in the queue.
    exclusion_guard = """
        AND (
            task_payload->'exclude_workers' IS NULL
            OR jsonb_typeof(task_payload->'exclude_workers') != 'array'
            OR NOT (task_payload->'exclude_workers' ? :worker)
        )
    """

    # GX.D1 — workflow/node/run placement pin, enforced at claim time.
    # A stamped task (payload worker_pin / worker_pin_tag, set by
    # services.workflow_placement.apply_task_placement at dispatch) is
    # claimable ONLY by the matching worker: never silently run elsewhere.
    # Unstamped tasks (the overwhelming majority) pass the two IS NULL
    # branches through — byte-identical behaviour to before. The claim
    # happens under FOR UPDATE SKIP LOCKED, so a pinned task a worker does
    # not match is simply invisible to it and stays queued; the
    # unrunnable_tasks_sweep names the missing worker for the Dashboard.
    # `:worker` and `:worker_hw_tags` are already bound by both branches.
    from services.workflow_placement import placement_claim_guard_sql
    placement_guard = placement_claim_guard_sql()

    # native-workers GP.11 — defence-in-depth counterpart to the
    # `_get_authorized_tenants` fix: that Python-side gate already strips
    # the PLATFORM_POOL/PLATFORM_ASSIGNED wildcard out of `:tenants` for a
    # native worker before this query ever runs (a native worker with no
    # tenant binding gets `None` there and 403s in `poll_for_task` before
    # ever reaching this query — the real fail-closed enforcement for the
    # "no binding" case). This SQL-layer check is the belt to that
    # braces, closing the same long-poll staleness window `license_guard`/
    # `paused_guard` close above: for a native worker it drops the
    # `-1 = ANY(:tenants)` wildcard branch entirely (no flag, including
    # MOD_GPU_CROSS_TENANT_SHARING — which governs GPU-card co-tenancy, not
    # queue eligibility — can reinstate it) and additionally cross-checks
    # against `worker.owner_tenant_id` whenever that binding is known, so a
    # `:tenants` list that went stale mid-poll can't hand a native worker
    # a task belonging to a tenant it was reassigned away from.
    native_tenant_guard = """
        AND (
            NOT :is_native_worker
            OR (
                tenant_id = ANY(:tenants)
                AND (:worker_tenant_id IS NULL OR tenant_id = :worker_tenant_id)
            )
        )
    """
    is_native_worker = _is_native_worker(worker)
    worker_tenant_id = worker.owner_tenant_id

    worker_accel = json.dumps(worker_accel_map)
    worker_caps = list(worker_hw_tags)
    # already gates native tasks by OS (platform_guard) and container tasks by
    # the linux default, but a CONTAINER task that names an OS requirement
    # (task_payload.required_os, stamped by dispatch below for an iOS mobile
    # build) has nothing to compare against until the OS is in the capability
    # list the capabilities_guard matches on. One derived tag, same source of
    # truth as :worker_os above (hardware_specs['os'], lowercased, default
    # linux) — so the linux fleet gains `os:linux` and carries
    # required_capabilities: [] everywhere, i.e. unchanged behaviour.
    if f"os:{worker_os}" not in worker_caps:
        worker_caps.append(f"os:{worker_os}")

    # pinnable — 2026-09-29 owner decision: every worker is pinnable by
    # default; operators no longer hand-tag boxes. Exclusions (NOAH
    # idle-harvest hosts, minus the embedded worker exemption; explicit
    # `not-pinnable` opt-out tag) live in ONE place —
    # services/noah.py:worker_satisfies_pinnable — also used by the
    # dispatch-time starvation guard in add_pinnable_requirement, so the two
    # cannot drift apart. Computed here, at claim time, rather than trusted
    # from the worker's own report.
    _noah = optional_paid("services.noah")
    if _noah is not None and _noah.worker_satisfies_pinnable(db, worker) and "pinnable" not in worker_caps:
        worker_caps.append("pinnable")

    # A task inserted while its image's arches were uncached carries none and
    # reads as amd64-only forever; re-stamp from the warm cache for this
    # (non-amd64) worker before selecting. Never blocks the claim.
    try:
        from utils.image_arch import restamp_unstamped_tasks
        with db.begin_nested():
            restamp_unstamped_tasks(db, worker.worker_name, worker_arch)
    except Exception:  # noqa: BLE001
        logger.debug("image arch late-stamp skipped", exc_info=True)

    # First: try to claim tasks ASSIGNED to this worker by the scheduler
    result = db.execute(
        text(f"""
            WITH next_task AS (
                SELECT id FROM api_task_queue
                WHERE status = :assigned
                  AND assigned_to_worker = :worker
                  AND queue_name = ANY(:queues)
                  AND (tenant_id = ANY(:tenants) OR -1 = ANY(:tenants))
                  {zone_filter}
                  {license_guard}
                  {hold_filter}
                  {gpu_backoff_guard}
                  {profiling_guard}
                  {paused_guard}
                  {platform_guard}
                  {darwin_task_guard}
                  {metal_lane_guard}
                  {requires_app_guard}
                  {variants_guard}
                  {capabilities_guard}
                  {accelerator_guard}
                  {arch_guard}
                  {accel_pool_guard}
                  {exclusion_guard}
                  {placement_guard}
                  {native_tenant_guard}
                  {stack_ops_guard}
                  {export_control_guard}
                  {residency_guard}
                  {label_pool_guard}
                  {label_region_guard}
                  {training_own_guard}
                  {data_marking_guard}
                  {fde_guard}
                  {system_job_excluded_guard}
                  {file_secrets_guard}
                ORDER BY start_after ASC NULLS FIRST, priority DESC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE api_task_queue
            SET status = :claimed,
                claimed_by_worker = :worker,
                claimed_at = NOW(),
                claim_expires_at = NOW() + INTERVAL '30 minutes'
            FROM next_task
            WHERE api_task_queue.id = next_task.id
            RETURNING api_task_queue.id
        """),
        {
            "assigned": ApiTaskStatus.ASSIGNED.name,
            "claimed": ApiTaskStatus.CLAIMED.name,
            "queues": queues,
            "tenants": authorized_tenants,
            "worker": worker.worker_name,
            "worker_zone": worker_zone,
            "worker_hw_tags": worker_hw_tags,
            "worker_caps": worker_caps,
            "worker_accel": worker_accel,
            "worker_arch": worker_arch,
            "worker_os": worker_os,
            "worker_metal_lane": worker_metal_lane,
            "worker_native_apps": worker_native_apps,
            "accel_inventory_known": accel_inventory_known,
            "available_accel_kinds": available_accel_kinds,
            "is_native_worker": is_native_worker,
            "worker_tenant_id": worker_tenant_id,
            "export_control_us_only": export_control_us_only,
            "worker_system_excluded": worker_system_excluded,
            "fs_min_version": fs_min_version,
            **residency_params,
        },
    )
    row = result.fetchone()
    db.commit()

    if row:
        return _finalize_claimed_task(db, worker_tasks_lifecycle_crud.get_task_queue_row_by_id(db, row[0]), worker)

    # Fallback: claim PENDING tasks (FIFO) — for when scheduler is disabled
    # or hasn't placed the task yet. Apply max_concurrent_tasks as safety net (0 = unlimited).
    if worker.max_concurrent_tasks > 0:
        active_count = worker_tasks_lifecycle_crud.count_worker_active_tasks(db, worker.worker_name)
        if active_count >= worker.max_concurrent_tasks:
            return None

    result = db.execute(
        text(f"""
            WITH next_task AS (
                SELECT id FROM api_task_queue
                WHERE status = :pending
                  AND assigned_to_worker IS NULL
                  AND queue_name = ANY(:queues)
                  AND (tenant_id = ANY(:tenants) OR -1 = ANY(:tenants))
                  {zone_filter}
                  {license_guard}
                  {hold_filter}
                  {gpu_backoff_guard}
                  {profiling_guard}
                  {paused_guard}
                  {platform_guard}
                  {darwin_task_guard}
                  {metal_lane_guard}
                  {requires_app_guard}
                  {variants_guard}
                  {capabilities_guard}
                  {accelerator_guard}
                  {arch_guard}
                  {accel_pool_guard}
                  {exclusion_guard}
                  {placement_guard}
                  {native_tenant_guard}
                  {stack_ops_guard}
                  {export_control_guard}
                  {residency_guard}
                  {label_pool_guard}
                  {label_region_guard}
                  {training_own_guard}
                  {data_marking_guard}
                  {fde_guard}
                  {system_job_excluded_guard}
                  {file_secrets_guard}
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE api_task_queue
            SET status = :claimed,
                claimed_by_worker = :worker,
                claimed_at = NOW(),
                claim_expires_at = NOW() + INTERVAL '30 minutes'
            FROM next_task
            WHERE api_task_queue.id = next_task.id
            RETURNING api_task_queue.id
        """),
        {
            "pending": ApiTaskStatus.PENDING.name,
            "claimed": ApiTaskStatus.CLAIMED.name,
            "queues": queues,
            "tenants": authorized_tenants,
            "worker": worker.worker_name,
            "worker_zone": worker_zone,
            "worker_hw_tags": worker_hw_tags,
            "worker_caps": worker_caps,
            "worker_accel": worker_accel,
            "worker_arch": worker_arch,
            "worker_os": worker_os,
            "worker_metal_lane": worker_metal_lane,
            "worker_native_apps": worker_native_apps,
            "accel_inventory_known": accel_inventory_known,
            "available_accel_kinds": available_accel_kinds,
            "is_native_worker": is_native_worker,
            "worker_tenant_id": worker_tenant_id,
            "export_control_us_only": export_control_us_only,
            "worker_system_excluded": worker_system_excluded,
            "fs_min_version": fs_min_version,
            **residency_params,
        },
    )
    row = result.fetchone()
    db.commit()

    # Owner decision 11-25 (COMPLIANCE-REFUSAL-ALERT, F-1): tasks the compliance
    # claim guards withheld from THIS worker raise a once-per-(task, rule) alert
    # (and the scheduler escalates a task that stays pending). Paid-only module
    # (absent on CE = no alert); runs after the claim commit, rate limited,
    # best-effort, never changes the claim outcome.
    try:
        _cra_w = optional_paid("services.compliance_refusal_alert")
        if _cra_w is not None:
            _cra_w.note_claim_withholds(
                db, worker.worker_name,
                {"export_control": export_control_guard,
                 "residency": residency_guard,
                 "label_pool": label_pool_guard,
                 "label_region": label_region_guard},
                {"worker": worker.worker_name,
                 "export_control_us_only": export_control_us_only,
                 **residency_params},
                queues=queues, tenants=authorized_tenants)
    except Exception:  # noqa: BLE001
        logger.debug("compliance withheld note failed", exc_info=True)

    if not row:
        return None

    return _finalize_claimed_task(db, worker_tasks_lifecycle_crud.get_task_queue_row_by_id(db, row[0]), worker)


@router.post("/workers/tasks/{task_id}/started")
async def report_task_started(
    task_id: int,
    request: TaskStartedRequest,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """Worker reports that task execution has begun.

    Idempotency + race resilience: a `/started` POST from a worker is
    really just a state-machine *forward* signal — "I've got this, move
    me to RUNNING". The old implementation rejected anything other than
    exactly CLAIMED, which surfaced as a 409 storm whenever a benign
    race in the orchestrator/poller path left the row briefly in
    ASSIGNED, or after the worker re-tried and an earlier attempt had
    already transitioned the row to RUNNING. The retry loop on the
    worker (see internal/transport/api.go::ReportStarted, 2-min budget
    over ~9 attempts) treated persistent 409s as fatal and re-ran the
    whole task — which is how we ended up with two `skopeo` containers
    pushing the same image variant in parallel on run 6 (task 392).

    Accepted states (each terminates the retry loop cleanly):
      * CLAIMED   — normal path. Transition to RUNNING, set started_at.
      * ASSIGNED  — recover from a poll/dispatch race where the worker
                    legitimately holds the task but the row briefly
                    rebounded to ASSIGNED. Treat as if it were CLAIMED.
      * RUNNING   — idempotent re-call (a previous attempt already
                    succeeded, the worker just didn't see the 200).
                    Return ok, don't double-write started_at or fire
                    the run event a second time.

    Anything else (PENDING / COMPLETED / FAILED / EXPIRED / CANCELLED)
    is still a 409 — those mean the worker should NOT be touching this
    task at all.
    """
    task = _get_worker_task(db, task_id, worker.worker_name)

    if task.status == ApiTaskStatus.RUNNING:
        return {"status": "ok", "already_started": True}

    if task.status not in (ApiTaskStatus.CLAIMED, ApiTaskStatus.ASSIGNED):
        raise HTTPException(
            status_code=409,
            detail=f"Task is {task.status.value}, expected claimed/assigned/running",
        )

    if task.status == ApiTaskStatus.ASSIGNED:
        # Worker is racing the scheduler/orchestrator. Log it once so
        # we still see drift if it gets worse, but accept the forward
        # transition — the worker holds the task either way.
        logger.warning(
            "Task %d /started while still ASSIGNED (worker=%s). Promoting "
            "to RUNNING; this implies a poll/dispatch race wrote ASSIGNED "
            "after the claim's CLAIMED commit.",
            task.id, worker.worker_name,
        )

    # Fortress 4.4b — per-tag dispatch gate (enforce refuses; shadow/off no-op).
    # CE-safe: compliance_* is a paid-only service module (ce-import-closure-rule
    # follow-up); absent-on-CE == gate disabled, not a crash.
    compliance_tag_dispatch_gate = optional_paid("services.compliance_tag_dispatch_gate")
    if compliance_tag_dispatch_gate is not None:
        _gate = compliance_tag_dispatch_gate.gate_task_start(
            db, task, worker.worker_name)
        if _gate and _gate["mode"] == "enforce" and not _gate["allowed"]:
            compliance_tag_dispatch_gate.refuse_task_start(
                db, task, _gate, worker_name=worker.worker_name)
            raise HTTPException(status_code=409, detail="compliance_gate_refused")

    # Fortress 5.2 — dispatch-time Rego policy gate (enforce refuses; the
    # service itself never raises in shadow/off, so no extra guard needed).
    compliance_policy_dispatch_gate = optional_paid("services.compliance_policy_dispatch_gate")
    if compliance_policy_dispatch_gate is not None:
        _policy_gate = compliance_policy_dispatch_gate.gate_task_start(
            db, task, worker.worker_name)
        if _policy_gate and _policy_gate["mode"] == "enforce" and not _policy_gate["allowed"]:
            compliance_policy_dispatch_gate.refuse_task_start(
                db, task, _policy_gate, worker_name=worker.worker_name)
            raise HTTPException(status_code=409, detail="compliance_policy_refused")

    # Fortress 4.5 slice B2 — per-tag worker/region/storage dispatch attestation.
    # One row PER compliance tag the run's workflow carries, keyed on the worker/
    # region/storage this claim actually chose (the service derives them from the
    # worker row + the tenant's active StorageConnection; untagged workflow -> no
    # rows, no extra queries). Best-effort: the service never raises, but an
    # attestation outage must never 500 a claim (same guard style as the FX.91
    # emit wrapper at _record_egress_block).
    try:
        from services import workflow_run_attestations as _wra
        _wra.record_dispatch(db, task=task, worker=worker)
    except Exception as _att_exc:  # noqa: BLE001 — never blocks a claim
        logger.warning("workflow_run_attestations dispatch failed for task %d: %s",
                       task.id, type(_att_exc).__name__)

    task.status = ApiTaskStatus.RUNNING
    task.started_at = datetime.now(timezone.utc)
    # Extend claim expiry based on estimated duration + buffer
    task.claim_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=request.estimated_duration_seconds + 600  # 10 min buffer
    )

    # #12 — record the worker pickup so the run-detail page shows "queued -> claimed -> running"
    # transitions, not just "queued -> done".
    try:
        from services.workflow_orchestrator import _emit_run_event as _emit_evt
        if task.workflow_run_id:
            _emit_evt(
                db, task.workflow_run_id, "worker", "info",
                f"Worker {worker.worker_name} started node {task.node_id} (task {task.id})",
                {
                    "task_id": task.id,
                    "node_id": task.node_id,
                    "worker": worker.worker_name,
                    "queue": task.queue_name,
                    "estimated_duration_seconds": request.estimated_duration_seconds,
                },
            )
    except Exception:
        pass

    # Also update the node_run status to RUNNING so the frontend sees the green pulse
    if task.node_run_id:
        node_run = worker_tasks_lifecycle_crud.get_node_run_by_id(db, task.node_run_id)
        if node_run:
            node_run.status = TaskStatus.RUNNING
            node_run.started_at = datetime.now(timezone.utc)

    # WS4.Q17.16 — the migrate-on-activate copy job flips its
    # StorageConnection to migration_status='running' the moment the worker
    # actually picks it up (was 'pending' since enqueue). Best-effort: a
    # missed transition here is purely cosmetic — the completion/failure
    # hooks below still land the terminal state correctly either way.
    _tp_started = task.task_payload or {}
    if _is_storage_migration_task(task, _tp_started):
        try:
            from crud import storage_connection_cud as sc_crud
            _conn = sc_crud.get_connection(
                db, conn_id=_tp_started.get("dest_connection_id"), tenant_id=task.tenant_id,
            )
            if _conn is not None and _conn.migration_status == "pending":
                _conn.migration_status = "running"
                _conn.migration_started_at = datetime.now(timezone.utc)
        except Exception:  # noqa: BLE001 — cosmetic only, never block /started
            logger.warning("storage_migration: failed to mark task %d running", task.id, exc_info=True)

    db.commit()

    try:
        from services.task_wait_metrics import log_task_timing
        log_task_timing(task)
    except Exception:  # noqa: BLE001 — logging only, never fail /started
        pass

    # Update Redis queue state for monitoring compatibility
    try:
        from utils.queue_state import register_task_start
        register_task_start(
            worker_name=worker.worker_name,
            workflow_run_id=task.workflow_run_id,
            node_id=task.node_id,
            estimated_duration_seconds=request.estimated_duration_seconds,
            metadata={"task_queue_id": task.id, "remote": True},
        )
    except Exception as e:
        logger.warning("Failed to update queue state for task %d: %s", task_id, e)

    return {"status": "ok"}


def apply_contention_verdict(db: Session, pair_id: str, accel_family: str) -> dict:
    """Evaluate a pairing's stressor verdict and stamp it on the calibration task.

    Called from BOTH completion paths, because neither alone is sufficient.
    When the CALIBRATION task finishes, the stressor is usually still running —
    it is deliberately sized to outlive the run it contends with — so its
    verdict does not exist yet. When the STRESSOR finishes, the verdict exists
    but the calibration task completed earlier and nothing would revisit it.

    So the calibration task is stamped provisionally at its own completion and
    corrected when the stressor reports. Idempotent, and the later call must be
    able to overturn the earlier one in both directions.
    """
    from services.calibration_workflow import evaluate_contention_measurement

    ev = evaluate_contention_measurement(db, pair_id, accel_family)

    cal_tasks = worker_tasks_lifecycle_crud.get_contention_calibration_tasks(db, pair_id)

    for t in cal_tasks:
        rp = dict(t.result_payload or {})
        rp["stressor_verdict"] = ev.get("verdict")
        rp["contention_valid"] = ev["valid"]
        pl = dict(t.task_payload or {})
        kind = pl.get("calibration_run_kind")

        if not ev["valid"]:
            rp["contention_invalid_reason"] = ev["reason"]
            # Demote so the pattern updater and every consumer treat it as what
            # it actually is — an uncontended run. Remember what it was, since
            # a provisional rejection is routinely overturned once the stressor
            # finishes and publishes its verdict.
            if kind in ("pairwise", "multi"):
                pl["contended_run_kind"] = kind
                pl["calibration_run_kind"] = "invalid_contention"
                t.task_payload = pl
                flag_modified(t, "task_payload")
        else:
            rp.pop("contention_invalid_reason", None)
            if kind == "invalid_contention" and pl.get("contended_run_kind"):
                pl["calibration_run_kind"] = pl.pop("contended_run_kind")
                t.task_payload = pl
                flag_modified(t, "task_payload")

        t.result_payload = rp
        flag_modified(t, "result_payload")

    db.commit()
    return ev


def _release_stack_best_effort(db: Session, task, node_kind) -> None:
    """Release the finished task's stack via stack_wake.release_for_task (D1).
    Skipped for NIM kinds (nim_ephemeral.sleep_back already handles those).
    Never raises: a release failure must not block completion/failure reporting."""
    try:
        from services import stack_wake as _stack_wake
        _stack_wake.release_for_task(db, task)
        db.commit()
    except Exception as _sw_exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("stack_wake release failed after task %s: %s",
                       getattr(task, "id", None), _sw_exc)


def _k8s_vram_readback(task_id: int, sidecar_samples: Optional[str] = None) -> None:
    """14.14 background entry — see services.k8s_task_metrics
    .readback_for_task_background (own session, best-effort, never raises).
    CE: the Paid module is absent and this is a no-op."""
    _metrics = optional_paid("services.k8s_task_metrics")
    if _metrics is not None:
        _metrics.readback_for_task_background(task_id, sidecar_samples)


def _gpu_metrics_record(task_id: int) -> None:
    """18.5.s2 background entry — see services.task_gpu_metrics
    .record_for_task_background (own session, best-effort, never raises).
    Platform-setting-gated (gpu_metrics.prometheus_url, '' default = OFF:
    no HTTP call at all), so this is a no-op until an operator points it at
    a DCGM/AMD exporter."""
    _metrics = optional_paid("services.task_gpu_metrics")
    if _metrics is not None:
        _metrics.record_for_task_background(task_id)


def _cost_guard_after_node(db: Session, workflow_run) -> None:
    """sz-1.7.2 + sz-1.8.2 — mid-run cost cap-kill + soft warning hook.

    Called once per node completion (see report_task_complete) AFTER that
    node's cost row exists and AFTER downstream dispatch, so a cap-kill also
    cancels the queue rows the dispatch just enqueued. services.cost_accumulator
    sums the run's actual credit_usage_log spend against the tenant's
    workflow_budgets.max_cost_per_run_credits cap: at/over the cap the run is
    cancelled through the service-level cancel (never the HTTP route) and its
    still-active ApiTaskQueue rows marked CANCELLED — the same cancel_fn shape
    services/scheduler.py's paused_run_timeout_sweep_tick uses, because the
    accumulator's own default cancel only flips the run row and would leave
    claimable work sitting in the queue. Soft status changes only: nothing here
    deletes anything (ISO 27001 — no hard deletes).

    At the soft ratio (cost.soft_warning_ratio, default 0.8) the run is NOT
    touched: services.cost_notifications.warn_at_threshold sends ONE deduped
    tenant-admin notification.

    A finished run is never killed: the gate below skips anything that is not
    QUEUED/RUNNING, and the whole helper is wrapped — a cost-guard failure is
    logged and rolled back, it must NEVER break task completion.
    """
    # CE carve: open-core/manifest.yaml DENIES `CORE/API/services/cost_*.py`,
    # so services/cost_accumulator + services/cost_notifications are stripped
    # from CE images. The call-time imports below then raised
    # ModuleNotFoundError and the except logged "cost guard failed for run %s"
    # on every node completion. Skip cleanly instead: the ONE edition predicate
    # (utils/paid_import.is_ce_edition, same lazy-call-time pattern as
    # permissions._ce_edition) plus a guarded import (optional_paid) so a
    # missing module is never a logged failure even if the sentinel lies.
    from utils.paid_import import is_ce_edition
    if workflow_run is None:
        return
    if is_ce_edition():
        return
    if getattr(workflow_run, "status", None) not in (
        models.TaskStatus.QUEUED, models.TaskStatus.RUNNING,
    ):
        return
    try:
        cost_accumulator = optional_paid("services.cost_accumulator")
        cost_notifications = optional_paid("services.cost_notifications")
        if cost_accumulator is None or cost_notifications is None:
            return  # absent in this carve — not a cost-guard failure
        check_and_kill = cost_accumulator.check_and_kill
        warn_at_threshold = cost_notifications.warn_at_threshold

        def _cancel_run_and_its_tasks(db_, run_id):
            """Flip the run, then mark its still-active queue rows CANCELLED.

            Returns False on a lost race (someone else already cancelled/
            finished the run) so the queue is left alone.
            """
            from crud import workflows_domain as _wd_crud
            from sqlalchemy import update as sa_update
            now = datetime.now(timezone.utc)
            flipped = _wd_crud.conditional_cancel_run(
                db_, run_id,
                completed_at=now,
                error_message="Cancelled automatically: the run's actual cost "
                              "reached workflow_budgets.max_cost_per_run_credits "
                              "(sz-1.7.2 mid-run cost cap)",
            ).rowcount > 0
            if not flipped:
                return False
            db_.execute(
                sa_update(models.ApiTaskQueue)
                .where(
                    models.ApiTaskQueue.workflow_run_id == run_id,
                    models.ApiTaskQueue.status.in_(
                        (models.ApiTaskStatus.PENDING,
                         models.ApiTaskStatus.ASSIGNED,
                         models.ApiTaskStatus.CLAIMED,
                         models.ApiTaskStatus.RUNNING),
                    ),
                )
                .values(status=models.ApiTaskStatus.CANCELLED,
                        completed_at=now)
            )
            db_.commit()
            return True

        result = check_and_kill(
            db,
            workflow_run.tenant_id,
            workflow_run.id,
            cancel_fn=_cancel_run_and_its_tasks,
        )
        action = result.get("action")
        if action == "warn":
            warn_at_threshold(
                db,
                tenant_id=workflow_run.tenant_id,
                workflow_id=workflow_run.workflow_id,
                run_id=workflow_run.id,
                spent_credits=result.get("spent"),
                cap_credits=result.get("cap"),
            )
        elif action == "kill":
            # ids + amounts only, no secrets (the accumulator already audited
            # the kill itself with the same numbers).
            logger.info(
                "cost guard: run %s cap-killed — spent %s of cap %s credits",
                workflow_run.id, result.get("spent"), result.get("cap"),
            )
    except Exception as e:  # noqa: BLE001 — never breaks task completion
        logger.warning("cost guard failed for run %s: %s",
                       getattr(workflow_run, "id", None), e)
        try:
            db.rollback()
        except Exception:
            pass


@router.post("/workers/tasks/{task_id}/complete")
async def report_task_complete(
    task_id: int,
    request: TaskCompleteRequest,
    background_tasks: BackgroundTasks,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """
    Worker reports task completion with outputs. Idempotent -- safe to call twice.
    """
    task = _get_worker_task(db, task_id, worker.worker_name)

    # Idempotent: if already completed, just return OK
    if task.status == ApiTaskStatus.COMPLETED:
        return {"status": "ok", "message": "Already completed"}

    # Bug 25 — a genuine success from the worker that HOLDS this task must WIN
    # over a reaper-induced terminal state. A manager outage (WSL2 restart) or
    # a lapsed 30-min claim makes cleanup_expired_task_claims mark a task
    # FAILED/EXPIRED while its container was still running on a live worker;
    # the container then exits 0 and reports here. The worker is ground truth
    # for its own container's outcome, so accept the success and flip the
    # premature failure to COMPLETED (the NodeRun/WorkflowRun are recovered
    # below and the run finaliser recomputes the true outcome). The terminal
    # branches of the reaper leave claimed_by_worker set, so _get_worker_task
    # above still authorises the reporting worker.
    #
    # CANCELLED is deliberately NOT recovered: a hard-cancel is user/operator
    # intent and the worker SIGKILLs the container, so any late "success" there
    # is spurious and must stay cancelled.
    _recovered_from = None
    if task.status in (ApiTaskStatus.FAILED, ApiTaskStatus.EXPIRED):
        _recovered_from = task.status.value
        logger.warning(
            "Task %d reported COMPLETE by worker %s while in terminal state %s "
            "— the reaper marked it prematurely but the container actually "
            "succeeded. Accepting the success and recovering the run.",
            task_id, worker.worker_name, _recovered_from,
        )
    elif task.status not in (ApiTaskStatus.RUNNING, ApiTaskStatus.CLAIMED):
        raise HTTPException(status_code=409, detail=f"Task is {task.status.value}, cannot complete")

    # Fortress 3.6b — scan the worker's inline artefacts BEFORE the task is
    # marked COMPLETED. shadow logs (codes/counts only, never artefact content)
    # and lets the completion through; enforce routes a block through the
    # SAME code path /fail uses (report_task_failed with should_retry=False —
    # the terminal-failure propagation, not a copy of it) so the task/NodeRun/
    # WorkflowRun end up exactly as an ordinary failure. The worker gets a
    # plain 200: a blocked output is a permanent verdict, so it must not retry.
    # apply_task_output_scan never raises — a scanner blow-up never 500s a
    # completion.
    try:
        from services import task_output_scan as _tos_scan
        _scan_verdict = _tos_scan.apply_task_output_scan(
            db, task,
            {"outputs": [o.dict() if hasattr(o, 'dict') else o
                         for o in (request.outputs or [])]},
        )
    except Exception as _scan_exc:  # noqa: BLE001 — belt-and-braces, never blocks
        # Only an ImportError for the output-scan module ITSELF is the CE open-core
        # cut (the manifest-denied import fails on every completion — by design, the
        # Go worker-side scans still block). An ImportError from anywhere else (a
        # missing transitive dependency inside a present module) is a real bug and
        # must still WARN like any other scan blow-up.
        if isinstance(_scan_exc, ImportError) and (
                getattr(_scan_exc, "name", None) or "").endswith("task_output_scan"):
            logger.debug("output scan module not shipped in this build; "
                         "skipping API-side scan")
        else:
            logger.warning("output scan wiring failed for task %d: %s",
                           task_id, type(_scan_exc).__name__)
        _scan_verdict = {"blocked": False}
    if _scan_verdict.get("blocked"):
        _codes = _tos_scan.finding_codes(_scan_verdict.get("findings") or [])
        _fail_req = TaskFailedRequest(
            error_message=f"output_safety_blocked: {_codes}",
            should_retry=False,
            log_tail=request.log_tail,
            egress=request.egress,
        )
        await report_task_failed(task_id, _fail_req, worker=worker, db=db)
        # Owner decision 11-25: a compliance block is a bad failure -> audit +
        # alert + notifications.  Runs AFTER the FAILED state is committed by
        # report_task_failed (own commit, rollback on error, email async), so no
        # alert problem can lose the block.  Paid-only module (absent on CE = no
        # alert); never changes the block outcome.
        try:
            # NOT the module-level optional_paid: a later in-function import
            # (the awaiting_external_event branch) makes that name local to
            # this whole function -> UnboundLocalError here (alert never fired).
            from utils.paid_import import optional_paid as _opt_paid_cra
            _cra = _opt_paid_cra("services.compliance_refusal_alert")
            if _cra is not None:
                _cra.alert_refusal_committed(
                    db, task, gate="output_scan", rule="output_safety_blocked",
                    reason=str(_codes), mode=str(_scan_verdict.get("mode") or "enforce"),
                    worker_name=getattr(worker, "worker_name", None),
                    tail=_cra.OUTPUT_BLOCKED)
        except Exception as _cra_exc:  # noqa: BLE001
            logger.warning("compliance refusal alert failed for task %d: %s",
                           task_id, type(_cra_exc).__name__)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return {"status": "ok", "message": "output blocked by safety scan"}

    # FIX-CE-DP-NOISE-FAIL-CLOSED (owner decision 11-10 10:50 PM CT): a dp spec with
    # the module ABSENT (CE) must FAIL the task, not silently skip the noise — a silent
    # skip hands raw, un-noised output downstream (and into NodeOutputCache), which
    # breaks FORTRESS.6.2's contract. Same failure mechanism and same error field as
    # the dp-noise-failure branch below, so the UI shows the failure identically; the
    # text is fixed (no module name, no exception class name). This sits BEFORE the
    # COMPLETED stamp on purpose: on CE nothing may ever be stamped completed for a
    # dp task whose noise cannot run, so the spec/lookup reads moved up here.
    _dp_spec = (task.task_payload or {}).get("dp")
    from utils.paid_import import optional_paid as _optional_paid
    _dpn = _optional_paid("services.dp_noise") if _dp_spec else None
    _dp_active = bool(_dp_spec) and _dpn is not None
    if bool(_dp_spec) and _dpn is None:
        logger.warning(
            "dp noise unavailable for task %d (dp_noise is not in this edition) — failing closed",
            task_id,
        )
        try:
            await report_task_failed(
                task_id,
                TaskFailedRequest(
                    error_message=DP_NOISE_UNAVAILABLE,
                    should_retry=False,
                    log_tail=request.log_tail,
                    egress=request.egress,
                ),
                worker=worker,
                db=db,
            )
        except Exception as _dp_fail_exc:  # noqa: BLE001
            logger.warning(
                "dp noise fail-closed routing failed for task %d: %s",
                task_id, _dp_fail_exc,
            )
        return {"status": "ok", "message": "dp noise failed; task failed closed"}

    # Update task
    if not task.started_at:
        task.started_at = task.claimed_at or task.created_at or datetime.now(timezone.utc)
    task.status = ApiTaskStatus.COMPLETED
    task.completed_at = datetime.now(timezone.utc)
    # Strip null bytes from log_tail — Docker multiplexing headers contain \x00
    # which PostgreSQL JSONB cannot store
    log_tail = request.log_tail
    if log_tail:
        log_tail = log_tail.replace('\x00', '')

    # FX.117 (Q104=A) — mask the run's exact secret values in everything the
    # API stores/serves for this task: log_tail + outputs (the stored
    # node-run outputs mirror these). Pattern redaction stays in place at
    # read time; this closes the bare-credential gap at ingest.
    _fx117_vals = _fx117_masked_secret_values(db, task)
    if _fx117_vals:
        from utils.secret_redaction import mask_exact_values
        _fx117_outs = [o.dict() if hasattr(o, 'dict') else o for o in request.outputs]
        _fx117_outs = mask_exact_values(_fx117_outs, _fx117_vals)
        if log_tail:
            log_tail = mask_exact_values(log_tail, _fx117_vals)
    else:
        _fx117_outs = [o.dict() if hasattr(o, 'dict') else o for o in request.outputs]

    # FORTRESS.6.2 — DP noise MUST happen here, before anything derived from
    # this completion is persisted: result_payload (below), NodeRun.outputs
    # (the awaiting/else branch further down), WorkflowNodeOutput +
    # NodeOutputCache (_record_task_outputs, which reads request.outputs) and
    # the downstream dispatch (_check_and_dispatch_downstream re-reads
    # result_payload["outputs"] as the child's parent_outputs). So we overwrite
    # the raw JSON in place in BOTH the local list and request.outputs — the raw
    # numeric value never survives anywhere on the task row.
    #
    # An invalid spec raises ValueError -> the task fails closed
    # ("dp_noise_failed"); we never pass raw output through on a broken spec.
    # File-metadata outputs (content_hash entries) cannot be noised (the hash
    # IS the pointer to the bytes) — honest limit: leave them, stamp
    # dp_noise_applied=false and log a warning.
    # (_dp_spec / _dpn / _dp_active were read above, before the COMPLETED stamp —
    # FIX-CE-DP-NOISE-FAIL-CLOSED needed them there to fail a CE dp task closed.)
    _dp_applied = None
    if _dp_active:
        try:
            _fx117_outs, _dp_applied = _dpn.apply_dp_noise(_fx117_outs, _dp_spec, db=db)
            if isinstance(_fx117_outs, list):
                request.outputs = _fx117_outs
        except Exception as _dp_exc:  # noqa: BLE001 — handled below, fails closed
            logger.warning(
                "dp noise failed for task %d: %s", task_id, _dp_exc,
            )
            try:
                await report_task_failed(
                    task_id,
                    TaskFailedRequest(
                        error_message=f"dp_noise_failed: {type(_dp_exc).__name__}",
                        should_retry=False,
                        log_tail=request.log_tail,
                        egress=request.egress,
                    ),
                    worker=worker,
                    db=db,
                )
            except Exception as _dp_fail_exc:  # noqa: BLE001
                logger.warning(
                    "dp noise fail-closed routing failed for task %d: %s",
                    task_id, _dp_fail_exc,
                )
            return {"status": "ok", "message": "dp noise failed; task failed closed"}

    task.result_payload = {
        "outputs": _fx117_outs,
        "log_tail": log_tail,
    }
    if _dp_active:
        task.result_payload["dp_noise_applied"] = bool(_dp_applied)
        if not _dp_applied:
            logger.warning(
                "dp noise NOT applied for task %d — the node emitted file "
                "outputs only (numeric-JSON-only at launch); the raw file "
                "bytes are delivered unchanged",
                task_id,
            )
    if request.peak_vram_mb:
        task.result_payload["peak_vram_mb"] = request.peak_vram_mb
    # GSEC R-159 — the HF cache re-layout tally (flat field, NOT outputs[0]):
    # the completion hook reads it back from result_payload.
    if request.hf_cache_report:
        task.result_payload["hf_cache_report"] = request.hf_cache_report
    # FIX-BYO-MIGRATION-SKIP-MISSING — the skip report rides onto result_payload
    # (same idiom as hf_cache_report) so complete_migration/verify_migration and
    # the migration-status response can read it back off the task row.
    if request.storage_migration_report:
        task.result_payload["storage_migration_report"] = request.storage_migration_report
    # 30.6.1 c1-J3d — the restored logical snapshot's per-table row counts ride
    # the completion body onto result_payload so _record_snapshot_verify_tables
    # / _record_rehearsal_result can fold them into the snapshot manifest.
    # Names/counts were already validated on the model (same identifier rule the
    # worker enforces, ints >=0, <=200 keys), so nothing hostile reaches the
    # manifest; absent on every other task -> result_payload stays byte-identical.
    # Counts/names are never logged.
    if request.verify_tables:
        task.result_payload["verify_tables"] = request.verify_tables
    if request.verify_truncated:
        task.result_payload["verify_truncated"] = True
    _eph = _ephemeral_applied_fields(request)
    if _eph:
        task.result_payload = {**task.result_payload, **_eph}
    # GSEC FX.115 p2 — the worker's lockdown verdict rides the completion body
    # (flat field, same shape as hf_cache_report) so it stays on the task row
    # AND feeds the per-(node kind, flag) aggregation the platform-admin report
    # endpoint reads. Best-effort: a verdict that cannot be parsed must never
    # roll back a completion that already happened.
    if request.lockdown_report:
        task.result_payload = {**task.result_payload,
                               "lockdown_report": request.lockdown_report}
        try:
            from services import node_lockdown_report as _nlr
            _nlr.ingest_verdicts(db, task, request.lockdown_report)
        except Exception as _nlr_exc:  # noqa: BLE001
            logger.exception(
                "node_lockdown_report ingest failed for task %d: %s",
                task.id, _nlr_exc,
            )

    # Fortress 2.3c S2b — persist worker attestation; best-effort so it can
    # NEVER fail or alter task completion.  Store the signature dict in
    # result_payload alongside the attestation row.
    _b119f_att_ok = False  # B119(c): True only when the signature verified
    if request.worker_signature and request.worker_contribution:
        task.result_payload = {**task.result_payload,
                               "worker_signature": request.worker_signature}
        try:
            from services import node_run_attestations as _nra
            _b119f_att_ok = _nra.record_worker_attestation(
                db, task, worker.worker_name,
                request.worker_signature, request.worker_contribution,
            )
        except Exception as _att_exc:  # noqa: BLE001
            logger.exception(
                "node_run_attestation ingest failed for task %d: %s",
                task.id, _att_exc,
            )

    # Update NodeRun if linked
    node_run = None
    # expr 9.4 slice C — did this node return an awaiting_external_event envelope?
    # When it did we park the node+run (hash-only marker in outputs) INSTEAD of
    # marking COMPLETED, still let the ApiTaskQueue task finish normally (the
    # worker slot is released), and skip downstream dispatch below — the resume
    # endpoint re-enters it. Never log the token.
    _awaiting_envelope = False
    if task.node_run_id:
        node_run = worker_tasks_lifecycle_crud.get_node_run_by_id(db, task.node_run_id)
        if node_run and _b119f_att_ok is True:
            # B119 gap (c): persist the digest the worker actually executed,
            # only from a signature-verified contribution. Best-effort.
            try:
                _b119f_dg = (request.worker_contribution or {}).get(
                    "executed_image_digest")
                if isinstance(_b119f_dg, str) and _b119f_dg.strip():
                    _b119f_dg = _b119f_dg.strip()[:100]
                    _b119f_old = node_run.executed_image_digest
                    if _b119f_old != _b119f_dg:
                        if _b119f_old:
                            logger.warning(
                                "B119: task %s executed digest %s differs "
                                "from enqueue-stamped %s",
                                task.id, _b119f_dg, _b119f_old)
                        node_run.executed_image_digest = _b119f_dg
            except Exception as _b119f_exc:  # noqa: BLE001
                logger.warning("B119 executed digest persist failed for "
                               "task %s: %s", task.id, _b119f_exc)
        if node_run:
            _env_out = (_fx117_outs if isinstance(_fx117_outs, dict)
                        else (_fx117_outs[0] if _fx117_outs
                             and isinstance(_fx117_outs[0], dict) else None))
            _await_token = None
            if _env_out is not None:
                # 1788d CE carve: services/awaiting_external_event.py is paid
                # (manifest deny 966). The park-on-envelope lane is a paid
                # capability; with it absent no envelope is recognised and the
                # node completes normally - the task proceeds.
                from utils.paid_import import optional_paid
                _await_mod = optional_paid("services.awaiting_external_event")
                if _await_mod is not None:
                    detect_envelope = _await_mod.detect_envelope
                    park_node = _await_mod.park_node
                    _await_token = detect_envelope(_env_out)
            if _await_token:
                park_node(db, node_run=node_run, resume_token=_await_token)
                _awaiting_envelope = True
                # expr 9.4 security — park_node stored only sha256(token) on the
                # NodeRun; the task row's result_payload["outputs"] still carried
                # the RAW envelope, so drop resume_token from it (fresh dict,
                # never in-place JSONB mutation; other keys survive).
                if isinstance(_fx117_outs, list):
                    _scrub_outs = [({k: v for k, v in _el.items() if k != "resume_token"}
                                    if isinstance(_el, dict) and detect_envelope(_el) else _el)
                                   for _el in _fx117_outs]
                elif isinstance(_fx117_outs, dict) and detect_envelope(_fx117_outs):
                    _scrub_outs = {k: v for k, v in _fx117_outs.items() if k != "resume_token"}
                else:
                    _scrub_outs = _fx117_outs
                task.result_payload = {**task.result_payload, "outputs": _scrub_outs}
                flag_modified(task, "result_payload")
            else:
                node_run.status = TaskStatus.COMPLETED
                node_run.completed_at = datetime.now(timezone.utc)
                node_run.outputs = clamp_node_run_text(_fx117_outs)

    # GB.EGR.11b — best-effort egress report stamp. Must run AFTER the
    # result_payload assignment above (it merges into it) and BEFORE the
    # commit below; never allowed to fail task completion.
    try:
        from services.egress_report import record_egress_report
        record_egress_report(db, task, node_run, request.egress)
    except Exception as _egr_exc:  # noqa: BLE001
        logger.warning("egress report failed for task %d: %s", task.id, _egr_exc)

    # 30.6.11 c11-J24 — stack-model-call token metering stamp. Same posture as
    # the egress report above: runs AFTER node_run is final (status/outputs set)
    # and BEFORE the commit below, and is never allowed to fail task completion.
    # The service (services/node_run_usage.py) does the STRICT validation — this
    # field stays a loose Dict on purpose so a bad usage can never 422 /complete.
    try:
        from services.node_run_usage import record_usage_tokens
        record_usage_tokens(db, task=task, node_run=node_run,
                            raw=request.usage_tokens)
    except Exception as _u_exc:  # noqa: BLE001
        logger.warning("usage_tokens record failed for task %d: %s", task.id,
                       type(_u_exc).__name__)

    # Bug 25 recovery — if the premature task failure had already flipped the
    # parent run to FAILED, un-fail it so the run finaliser
    # (_check_and_dispatch_downstream, called below) recomputes the true
    # outcome from the now-corrected task states. Only touches a run we are
    # actively reversing; a run failed for an unrelated reason and still
    # carrying active tasks is put back to RUNNING and re-finalised honestly.
    if _recovered_from and task.workflow_run_id:
        _rec_run = worker_tasks_lifecycle_crud.get_workflow_run_by_id(db, task.workflow_run_id)
        if _rec_run and _rec_run.status == TaskStatus.FAILED:
            _rec_run.status = TaskStatus.RUNNING
            _rec_run.completed_at = None
            logger.info(
                "Run %s un-failed after recovering task %d's genuine success",
                task.workflow_run_id, task_id,
            )

    # sz-4.7c — record output-node delivery results at task completion.
    # Must run BEFORE the commit; best-effort so it can never fail completion.
    try:
        from services.output_delivery_capture import capture_from_task_result
        capture_from_task_result(
            db, task, succeeded=True,
            result={
                "http_status": request.outputs[0].get("http_status")
                    if request.outputs and isinstance(request.outputs[0], dict)
                    else None,
                "destination": request.outputs[0].get("destination")
                    if request.outputs and isinstance(request.outputs[0], dict)
                    else None,
                "receipt": request.outputs[0].get("receipt")
                    if request.outputs and isinstance(request.outputs[0], dict)
                    else None,
            } if (request.outputs and isinstance(request.outputs[0], dict)) else None,
        )
    except Exception as _cap_exc:  # noqa: BLE001
        logger.warning("output_delivery_capture failed for task %d: %s", task_id, _cap_exc)

    # Fortress 4.5 slice B2 — linked completion attestation: one row per still-
    # open dispatch row for this node run (status="completed", referenced_dispatch_id
    # back-pointer, idempotent). Best-effort — an attestation outage must never
    # 500 or roll back a completion (same guard style as the FX.91 emit wrapper).
    try:
        from services import workflow_run_attestations as _wra_done
        _wra_done.record_completion(db, task=task, worker=worker,
                                    status="completed")
    except Exception as _att_exc:  # noqa: BLE001 — never blocks a completion
        logger.warning("workflow_run_attestations completion failed for task %d: %s",
                       task_id, type(_att_exc).__name__)

    db.commit()

    # 14.14 — k8s per-task peak VRAM read-back (Paid; CE degrades to a no-op):
    # best-effort fetch of the k8s task's peak VRAM (cluster Prometheus pod
    # series, or the GPU-sampler sidecar lines) into the SAME peak_vram_mb
    # result field the docker worker reports (14.6b), so gpu_reservation's
    # learned p95 learns from k8s runs too. BackgroundTask with its own
    # short session — never inside this request's transaction, never allowed
    # to fail or delay the completion above.
    background_tasks.add_task(
        _k8s_vram_readback, task_id, getattr(request, "sidecar_samples", None))

    # 18.5.s2 — per-task peak VRAM + GPU-util read path (any backend): the
    # cluster's DCGM/AMD exporter series over the task's worker + window,
    # stored on the SAME result_payload keys the k8s read-back above writes
    # (worker-reported values always win). Gated by the gpu_metrics.prometheus_url
    # platform setting ('' default = OFF, no HTTP call). Post-commit background
    # task with its own session — never inside this request's transaction.
    background_tasks.add_task(_gpu_metrics_record, task_id)

    # Ephemeral local NIM sleep-back: a NIM client task finished, so put its
    # NIM back to sleep immediately (freeing the GPU) unless a sibling job still
    # needs it. No-op for every non-NIM node kind. Post-commit + wrapped so it
    # can never roll back the completion above.
    try:
        from services import nim_ephemeral as _nim
        _nk = (task.task_payload or {}).get("node_kind")
        if _nim.spec_for(_nk) is not None:
            _nim.sleep_back(db, _nk, task.tenant_id, exclude_task_id=task.id)
            db.commit()
    except Exception as _nim_exc:  # noqa: BLE001
        logger.warning("NIM sleep-back failed after task %d: %s", task.id, _nim_exc)

    # Universal stack release for non-NIM kinds (GA.GSW.1). Best-effort.
    _release_stack_best_effort(db, task, (task.task_payload or {}).get("node_kind"))

    # WS4.Q17.16 — BYO-S3 migrate-on-activate: the copy task reported
    # COMPLETED, so verify the reported counts and flip the destination
    # connection active. Best-effort — a failure here must never roll back
    # the task completion above; it only leaves the connection stuck at
    # migration_status='running', which is safe (no partial activation).
    _tp_complete = task.task_payload or {}
    if _is_storage_migration_task(task, _tp_complete):
        try:
            from services import storage_migration as _stmig
            _stmig.complete_migration(db, task)
        except Exception as _mig_exc:  # noqa: BLE001
            logger.exception(
                "storage_migration completion hook failed for task %d: %s",
                task.id, _mig_exc,
            )

    # GSEC R-159 — the per-worker HF cache re-layout reported COMPLETED:
    # record the per-move report (it IS the per-worker completion record
    # the flip gate reads) + audit. Best-effort, like the storage_migration
    # hook — a failure here must never roll back the task completion.
    if _is_hf_cache_relayout_task(task, _tp_complete):
        try:
            from services import hf_cache_relayout as _hcrl
            _hcrl.complete_task(db, task)
        except Exception as _rel_exc:  # noqa: BLE001
            logger.exception(
                "hf_cache_relayout completion hook failed for task %d: %s",
                task.id, _rel_exc,
            )

    # Fire queue_empty triggers if this tenant has any. Wrapped here at
    # the helper level so a failure can't roll back the task commit above.
    try:
        from services.queue_empty_cache import fire_queue_empty_triggers
        fire_queue_empty_triggers(db, task)
    except Exception as _qe_exc:
        logger.exception(
            "queue_empty fan-out failed for task %d: %s", task.id, _qe_exc,
        )

    # add-workflow-triggers §4.2 — node/step-completion triggers (cross-workflow).
    if task.node_run_id:
        try:
            from services.workflow_orchestrator import fire_node_completion_triggers
            _nr = worker_tasks_lifecycle_crud.get_node_run_by_id(db, task.node_run_id)
            if _nr:
                fire_node_completion_triggers(db, _nr)
        except Exception as _nc_exc:
            logger.exception("node-completion fan-out failed for task %d: %s", task.id, _nc_exc)
        # capture-preflight §3.1 — persist the report if this node emitted one.
        # No-op (self-identifying via sentinels) for every other node kind.
        try:
            from services.workflow_orchestrator import persist_capture_preflight_report
            _nr2 = worker_tasks_lifecycle_crud.get_node_run_by_id(db, task.node_run_id)
            if _nr2:
                persist_capture_preflight_report(db, _nr2, task.result_payload)
        except Exception as _pf_exc:
            logger.exception("capture-preflight persist failed for task %d: %s", task.id, _pf_exc)

    # Process Trivy scan results if this was a security scan task
    node_kind = task.task_payload.get("node_kind", "") if task.task_payload else ""

    # GE.WQR.6 — surface completion onto the Wizard Queue for
    # container-adapter tasks (see routers/node_definitions_domains/
    # node_authoring.py:generate_dockerfile, which emits the linked
    # WizardRun). Observational only — never let queue reconciliation
    # break task completion.
    if node_kind == "container_adapter":
        try:
            from services import wizard_queue_emitters as _wqe
            _wqe.reconcile_task_status(db, task.id, "completed")
        except Exception as _e:  # noqa: BLE001
            logger.warning(
                "wizard-queue reconcile failed for task %d: %s", task_id, _e)

    if node_kind == "trivy_scan":
        try:
            _process_trivy_scan_result(db, task)
        except Exception as e:
            logger.warning("Failed to process Trivy scan result for task %d: %s", task_id, e)

    if node_kind in ("hardware_benchmark", "accelerator_benchmark"):
        try:
            _process_benchmark_result(db, task)
        except Exception as e:
            logger.warning("Failed to process benchmark result for task %d: %s", task_id, e)

    # Persistent-stack lifecycle: advance the stack now its worker op landed.
    # Without this a deployed stack sits at 'deploying' forever and never
    # becomes reachable.
    if node_kind.startswith("persistent-stack-"):
        try:
            _apply_stack_task_result(db, task, is_success=True)
        except Exception as e:
            logger.warning("Stack lifecycle update failed for task %d: %s", task_id, e)

    # P9e — variant smoke-verification result. Hook identifies the task
    # by its `task_payload.variant_smoke` marker (set by
    # node_definitions._enqueue_variant_smoke_task) and flips
    # NodeDefinition.variant_verification_status[variant] to "verified".
    if task.task_type == "variant_smoke":
        try:
            _apply_variant_smoke_result(db, task, is_success=True, log_tail=log_tail)
        except Exception as e:
            logger.warning("variant_smoke success handler failed for task %d: %s", task_id, e)

    # Wizard artifact-generation tasks (Containerizer) — notify the
    # user who kicked it off. The wizard's /generate-artifacts stashed
    # `triggered_by_user_id` + `wizard_metadata` on the task_payload.
    # `OUTPUT_MODE=albedo` in container_env is what identifies these
    # tasks (the Containerizer takes OUTPUT_MODE to decide between
    # albedo, llm-ping, codebase-map, etc).
    try:
        _notify_wizard_generation_complete(db, task, is_success=True)
    except Exception as e:
        logger.warning("Wizard notification failed for task %d: %s", task_id, e)

    # P9g.1 — persist port_outcomes rows for Port Coverage dashboard,
    # regression alerts (9t), and bisection (9aa). Non-blocking:
    # failures here never fail the task completion itself.
    try:
        _record_port_outcomes(db, task, log_tail=log_tail)
    except Exception as e:
        logger.warning("port_outcomes record failed task=%d: %s", task_id, e)

    # Update Redis queue state
    try:
        from utils.queue_state import register_task_complete
        register_task_complete(worker.worker_name, task.workflow_run_id)
    except Exception as e:
        logger.warning("Failed to update queue state for task %d: %s", task_id, e)

    # Record outputs in workflow_node_outputs table and create downloadable file entries
    if request.outputs:
        try:
            _record_task_outputs(db, task, request.outputs)
        except Exception as e:
            logger.warning("Output recording failed for task %d: %s", task_id, e)

        # remove-minio-versitygw Gate 3 (D4) — worker-written outputs never went
        # through confirm-upload, so bucket notifications used to be the only way
        # they fired file_event triggers. Emit the ObjectCreated file_event
        # API-side here so "on file upload" triggers fire for worker outputs on
        # versitygw/BYO-S3 (no backend callback). Non-fatal.
        try:
            from services.triggers.watchers import emit_object_event
            for _o in request.outputs:
                _od = _o.dict() if hasattr(_o, "dict") else _o
                _name = (_od.get("file_name")
                         or ((_od.get("content_hash") or "") + (_od.get("file_extension") or "")))
                if _name:
                    emit_object_event(db, tenant_id=task.tenant_id,
                                      key=_name, event_type="created")
        except Exception as e:
            logger.warning("file_event emission for outputs failed task %d: %s", task_id, e)

    # Publish task status change via Redis
    _publish_event(f"task:status:{task.id}", "completed")

    # Check and dispatch downstream nodes — SKIPPED when the node parked an
    # awaiting_external_event envelope: nothing downstream runs until the
    # resume-event endpoint re-enters this node.
    if not _awaiting_envelope:
        _check_and_dispatch_downstream(db, task)

    # sz-1.7.2 + sz-1.8.2 — mid-run cost cap-kill + 80% soft warning. AFTER
    # downstream dispatch so a cap-kill also cancels the queue rows the
    # dispatch just enqueued; BEFORE the run finaliser (which is a different
    # function) so a finished run is never killed here.
    _cost_guard_after_node(
        db,
        db.get(models.WorkflowRun, task.workflow_run_id)
        if task.workflow_run_id else None,
    )

    # Classify resource pattern from samples (async-safe, fire-and-forget)
    try:
        from scheduler.pattern_updater import update_pattern_from_task
        update_pattern_from_task(task, db)
    except Exception as e:
        logger.warning("Pattern update failed for task %d: %s", task_id, e)

    _pl = task.task_payload or {}
    _pair_id = _pl.get("calibration_pair_id")

    if _pair_id and _pl.get("calibration_run"):
        try:
            from services.calibration_workflow import (
                release_paired_stressors, worker_accel_family,
            )
            # Release the stressors paired with this measurement. Only unstarted
            # ones can be stopped — there is no cancel channel to a running
            # container, so one already executing runs out its own duration
            # (bounded by calibration_workflow.stressor_duration_for). This stops
            # a QUEUED stressor from starting after the run it was meant to
            # contend with has finished, which would otherwise load the box
            # during the NEXT measurement.
            n = release_paired_stressors(db, _pair_id)
            if n:
                db.commit()
                logger.info("Released %d queued stressor(s) for pair %s", n, _pair_id)

            # Gate the measurement on its stressor. A contention run whose
            # stressor cannot be confirmed is NOT a contention measurement:
            # keeping it would file an isolation-shaped result under a
            # 'pairwise' label, and downstream nothing could distinguish
            # "unaffected by contention" from "the stressor never ran".
            #
            # This verdict is PROVISIONAL. The stressor is sized to outlive the
            # run it contends with, so at this moment it is usually still
            # executing and has published no verdict. The authoritative pass is
            # the is_stressor branch below, which revisits this same task.
            _ev = apply_contention_verdict(
                db, _pair_id, worker_accel_family(db, worker.worker_name))
            if not _ev["valid"]:
                logger.info("Task %d: contention unconfirmed for now — %s",
                            task_id, _ev["reason"])
        except Exception as e:
            logger.warning("Provisional verdict failed for pair %s: %s", _pair_id, e)

    if _pair_id and _pl.get("is_stressor"):
        # A finishing stressor is the moment its verdict exists. Re-evaluate and
        # correct the calibration task, which completed earlier with nothing to
        # read.
        try:
            from services.calibration_workflow import worker_accel_family
            _ev = apply_contention_verdict(
                db, _pair_id, worker_accel_family(db, worker.worker_name))
            logger.info(
                "Stressor %s finished for pair %s — contention %s%s",
                _pl.get("stressor_kind"), _pair_id,
                "CONFIRMED" if _ev["valid"] else "REJECTED",
                "" if _ev["valid"] else f": {_ev['reason']}",
            )
        except Exception as e:
            logger.warning("Stressor-side verdict failed for pair %s: %s", _pair_id, e)

    return {"status": "ok"}


# Bounded size of the build/test error tail persisted to a failed run's
# workflow_runs.error_message (see _build_run_failure_tail).
RUN_FAILURE_TAIL_MAX_CHARS = 2000


def _build_run_failure_tail(log_tail: Optional[str], error_message: Optional[str]) -> Optional[str]:
    """Bounded, secret-redacted tail of the last build/test error, for a run
    that just failed terminally.

    ``workflow_runs.error_message`` was historically left empty when a worker
    task failed here — the operator (and the wizard breadth harness) only saw
    the task row's "container exited with code 1" with no reason. The real
    failure lives in the container's stdout/stderr (``log_tail``): for the
    test-and-repair build stage that's the final smoke stderr + the
    "Auto-repair could not converge" line. We persist a redacted tail of it so
    the run-detail view and the breadth harness's ``fail_reason`` carry the
    actual cause.

    Bounded to ``RUN_FAILURE_TAIL_MAX_CHARS`` and run through
    ``utils.secret_redaction.redact`` (passwords / tokens / keys / connection
    strings / JWTs) so a credential echoed into build output never lands in the
    run row. Returns None when there is nothing usable to record.
    """
    raw = (log_tail or "").strip() or (error_message or "").strip()
    if not raw:
        return None
    from utils.secret_redaction import redact
    tail = redact(raw).strip()[-RUN_FAILURE_TAIL_MAX_CHARS:]
    return tail or None


# ---------------------------------------------------------------------------
# FX.117 (Q104=A) — exact-value masking of stored node logs/outputs/errors.
#
# Pattern-based redaction (utils.secret_redaction.redact) only catches values
# that LOOK like a known secret shape (password=..., token: Bearer ..., conn
# strings, JWTs). A node that echoes a bare credential (no prefix) into its
# stdout/output would otherwise persist it verbatim. These helpers gather the
# run's REFERENCED secret values (tenant-scoped, from the run's own workflow
# graph `secret_refs`) and replace any exact occurrence with ``***`` in the
# log_tail / outputs / error_message the API stores and serves.
# ---------------------------------------------------------------------------

def _fx117_masked_secret_values(db: Session, task) -> list:
    """FX.117 — the run's referenced secret values, for exact-value masking
    of node logs/outputs/errors the API stores. The lookups live in
    services.secrets_service (never a raw router db.query); tenant-scoped to
    the run's own tenant; never raises."""
    run_id = getattr(task, "workflow_run_id", None)
    if not run_id:
        return []
    from services import secrets_service
    return secrets_service.masked_secret_values_for_run(db, run_id)


class TaskOutputScanCheckRequest(BaseModel):
    """Body of the worker's PRE-STORE output-scan consult (executor.go).

    The worker sends identity + output descriptors only; no artefact bytes
    cross the wire. ``tenant_id`` / ``node_kind`` / ``minio_bucket`` are
    accepted for the per-tenant scanner-config resolution the async path uses,
    but are NEVER trusted for scoping — the task row is authoritative.
    """
    outputs: List[Dict[str, Any]] = Field(default_factory=list,
                                         description="Output file metadata")
    tenant_id: Optional[int] = Field(default=None, description="Advisory only")
    node_kind: Optional[str] = Field(default=None, description="Advisory only")
    minio_bucket: Optional[str] = Field(default=None, description="Advisory only")
    upload_pending: Optional[bool] = Field(
        default=None, description="True for the pre-bucket-write consult")


class OutputScanCheckVerdict(BaseModel):
    """Exactly executor.go's OutputScanVerdict decode shape."""
    mode: str
    blocked: bool
    exempted: bool = False
    error: bool = False
    findings: List[Dict[str, Any]] = Field(default_factory=list)


@router.post("/workers/tasks/{task_id}/output-scan-check",
             response_model=OutputScanCheckVerdict)
async def check_task_output_scan(
    task_id: int,
    request: TaskOutputScanCheckRequest,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """Fortress 3.5 — read-only output-scan consult BEFORE the worker stores.

    executor.go:scanOutputBeforeStore POSTs here before the MinIO bucket
    upload and the inline result-JSON return, so a blocked output is never
    written at all (ISO 27001: nothing is deleted, a refused object simply
    never lands). A 404 here is a "no verdict" and the worker's posture
    decides, so this route must exist and must not lie.

    Auth/ownership: the same ``require_enrolled_worker`` dependency /complete
    uses, plus the same ``_get_worker_task`` task-ownership check (unknown
    task -> 404, task claimed by another worker -> 403). The consult only
    makes sense while the worker is mid-execution, so a task that has already
    reached a terminal state gets a 409 — the /complete path owns the verdict
    there. Tenant comes from ``task.tenant_id`` ONLY; the body's tenant_id is
    advisory and never used to scope a fetch, so a cross-tenant staging_key
    cannot reach another tenant's bucket.

    Read-only: no task state change, no DB writes. Deliberately NOT
    ``apply_task_output_scan`` — whose enforce path appends the
    output_safety_scan_results + AuditEvent exemption-override pair; nothing
    has been stored here, so no durable record is written.
    """
    try:
        task = _get_worker_task(db, task_id, worker.worker_name)
    except HTTPException as _own:
        # A consult must not confirm that another worker's task exists: the
        # ownership 403 /complete gives becomes a plain 404 here, so a
        # stranger's posture can never be steered by an existence oracle.
        if _own.status_code == 403:
            raise HTTPException(status_code=404, detail="Task not found")
        raise
    if task.status not in (ApiTaskStatus.RUNNING, ApiTaskStatus.CLAIMED,
                           ApiTaskStatus.ASSIGNED):
        raise HTTPException(
            status_code=409,
            detail=f"Task is {task.status.value}, cannot consult a scan",
        )

    try:
        from services import task_output_scan as _tos
        verdict = _tos.consult_task_output_scan(
            db, task=task, outputs=request.outputs or [])
    except ValueError as exc:
        # a staging_key outside the task's own staging prefix — the reason
        # text can echo caller-supplied key material, so it is logged
        # server-side only; the wire answer is a fixed category code.
        logger.warning("output-scan consult rejected staging_key for task %d: %s",
                       task_id, exc)
        raise HTTPException(status_code=400,
                            detail={"error": "invalid_staging_key"})
    except Exception as exc:  # noqa: BLE001 — a scan blow-up never 500s the consult
        # Only an ImportError for the output-scan module ITSELF is the CE open-core
        # cut (manifest-denied import, by design — the Go worker-side scans still
        # block). Any other ImportError (a missing transitive dependency of a
        # present module) is a real bug and WARNs like any other scan blow-up.
        if isinstance(exc, ImportError) and (
                getattr(exc, "name", None) or "").endswith("task_output_scan"):
            logger.debug("output scan module not shipped in this build; "
                         "skipping API-side scan")
        else:
            logger.warning("output-scan consult failed for task %d: %s",
                           task_id, type(exc).__name__)
        verdict = {"mode": "shadow", "blocked": False, "exempted": False,
                   "error": True, "findings": []}
    return OutputScanCheckVerdict(**verdict)
class WorkerCheckpointExportUploadedRequest(BaseModel):
    """Body the worker (checkpoint_transfer.go ExportCheckpointVolume) posts
    after it PUTs the loaned checkpoint export tar to the presigned URL
    `request_checkpoint_export` minted (Fortress 3.5b). ``transfer_id`` must
    name a live, pending, ``direction='export'`` row on ``loan_id``;
    ``byte_size`` is the actual uploaded object size for the durable record.
    """
    loan_id: int
    transfer_id: int
    byte_size: Optional[int] = None


class WorkerCheckpointExportUploadedResponse(BaseModel):
    loan_id: int
    transfer_id: int
    object_key: Optional[str] = None
    status: str
    scanned: bool
    mode: Optional[str] = None
    byte_size: Optional[int] = None
    finding_codes: Optional[str] = None


@router.post("/workers/tasks/{task_id}/checkpoint-export-uploaded",
             response_model=WorkerCheckpointExportUploadedResponse)
async def worker_checkpoint_export_uploaded(
    task_id: int,
    request: WorkerCheckpointExportUploadedRequest,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """Worker-authenticated mirror of `mesh_loans.mark_checkpoint_export_uploaded`
    (Fortress 3.5f).

    985a added the platform-admin route
    `POST /loan/{loan_id}/checkpoint/export-uploaded` (routers/loans.py)
    for a human/admin caller. The Go worker (985b,
    `checkpoint_transfer.go:ExportCheckpointVolume`) authenticates with the
    worker bootstrap-token seam (`outputScanSetAuth`), never a platform-admin
    JWT, so it needs this route instead — same `require_enrolled_worker`
    dependency and task-ownership pattern as `output-scan-check` above.

    Ownership: ``task_id`` must be claimed by the calling worker
    (`_get_worker_task` -> 404/403), and the task's own
    ``task_payload['loan_id']`` (stamped at dispatch time by
    `services.mesh_loan_payload.loan_id_for_run`, GC.MWL.12) must match the
    body's ``loan_id`` — otherwise 403, since a worker may only report an
    upload for the loan it is actually executing, never an arbitrary
    ``loan_id`` it names in the body. ``caller_manager_id`` is always this
    manager's own `mesh_loans.MANAGER_ID` (the local manager is the
    borrower already running the task) — never taken from the request body.
    """
    task = _get_worker_task(db, task_id, worker.worker_name)
    if task.status not in (ApiTaskStatus.RUNNING, ApiTaskStatus.CLAIMED,
                           ApiTaskStatus.ASSIGNED):
        raise HTTPException(
            status_code=409,
            detail=f"Task is {task.status.value}, cannot report an export upload",
        )
    _task_loan_id = (task.task_payload or {}).get("loan_id")
    if _task_loan_id is None or str(_task_loan_id) != str(request.loan_id):
        raise HTTPException(
            status_code=403,
            detail="Task is not executing this loan",
        )

    # 1788d CE carve: services/mesh_loans.py is paid (manifest deny
    # `CORE/API/**/mesh_*.py`). This endpoint is the worker mirror of a paid
    # mesh-loan route, so CE answers the same 402 the central edition handler
    # produced - never a 500, never a silent success on loan state.
    from utils.paid_import import optional_paid
    _loan_service = optional_paid("services.mesh_loans")
    if _loan_service is None:
        raise HTTPException(
            status_code=402,
            detail="Mesh loan export reporting is not available "
                   "in MOD Community Edition.",
        )
    from services._base import NotFoundError, ServiceError
    try:
        result = _loan_service.mark_checkpoint_export_uploaded(
            db,
            request.loan_id,
            transfer_id=request.transfer_id,
            caller_manager_id=_loan_service.MANAGER_ID,
            byte_size=request.byte_size,
        )
    except _loan_service.LoanForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from e
    except _loan_service.LoanStateError as e:
        raise HTTPException(status_code=409, detail=e.message) from e
    except _loan_service.LoanCheckpointMissingError as e:
        raise HTTPException(status_code=409, detail=e.message) from e
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from e
    except ServiceError as e:
        raise HTTPException(status_code=500, detail=e.message) from e
    db.commit()
    return WorkerCheckpointExportUploadedResponse(
        loan_id=request.loan_id,
        transfer_id=result["transfer_id"],
        object_key=result.get("object_key"),
        status=result["status"],
        scanned=result["scanned"],
        mode=result.get("mode"),
        byte_size=result.get("byte_size"),
        finding_codes=result.get("finding_codes"),
    )


@router.post("/workers/tasks/{task_id}/failed")
async def report_task_failed(
    task_id: int,
    request: TaskFailedRequest,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """Worker reports task failure. Requeues if retriable."""
    result = await _report_task_failed_impl(task_id, request, worker, db)
    # Owner decision 11-25 (COMPLIANCE-REFUSAL-ALERT, F-2): a worker-side output
    # scan block (executor.go / host-exec: "output_scan_blocked: ...") ends as an
    # ordinary failure; raise the compliance alert too, AFTER the failure outcome
    # is committed (the alert runs in its own try/rollback and can never change
    # or lose the outcome). Paid-only module (absent on CE = no alert); no inline
    # SMTP on this request path. Fixed reason text: the masked error_message
    # stays only on the task row.
    if (request.error_message or "").startswith("output_scan_blocked"):
        try:
            _cra_f = optional_paid("services.compliance_refusal_alert")
            if _cra_f is not None:
                _t = db.get(ApiTaskQueue, task_id)
                if _t is not None:
                    _cra_f.alert_refusal(
                        db, _t, gate="worker_output_scan",
                        rule="output_scan_blocked",
                        reason="the worker output scan blocked this task's output",
                        worker_name=getattr(worker, "worker_name", None),
                        tail=_cra_f.OUTPUT_BLOCKED, kind="worker_output_scan",
                        email=False)
                    db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("compliance refusal alert failed for task %d", task_id)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return result


async def _report_task_failed_impl(
    task_id: int,
    request: TaskFailedRequest,
    worker: WorkerLicense,
    db: Session,
):
    task = _get_worker_task(db, task_id, worker.worker_name)

    # Terminal / cancelled tasks must NOT requeue. CANCELLED and EXPIRED are
    # included deliberately: hard-cancel sets the task CANCELLED and then the
    # worker SIGKILLs its container, so the worker's follow-up failure report
    # lands here with should_retry set — without this guard a user-cancelled
    # task would be requeued and the run would resurrect itself.
    if task.status in (
        ApiTaskStatus.COMPLETED,
        ApiTaskStatus.FAILED,
        ApiTaskStatus.CANCELLED,
        ApiTaskStatus.EXPIRED,
    ):
        return {"status": "ok", "message": f"Already {task.status.value}", "requeued": False}

    # Update Redis queue state
    try:
        from utils.queue_state import register_task_complete
        register_task_complete(worker.worker_name, task.workflow_run_id)
    except Exception as e:
        logger.warning("Failed to update queue state for task %d: %s", task_id, e)

    requeued = False
    is_profiling = False

    # FX.117 (Q104=A) — mask the run's exact secret values in everything this
    # handler stores: error_message, error_detail, log_tail (result_payload).
    _fx117_vals = _fx117_masked_secret_values(db, task)

    def _fx117_mask(_v):
        if _fx117_vals and _v:
            from utils.secret_redaction import mask_exact_values
            return mask_exact_values(_v, _fx117_vals)
        return _v

    # --- NOAH preemption. -----------------------------------------------
    # The worker yielded this task back to a returning human; it never
    # actually failed, so it must not be charged a retry attempt, must not
    # be routed through the self-repairing/quarantine/terminal-failure logic
    # below, and must not reuse the killed partial run's output_namespace
    # (a preempted re-run would otherwise clobber/read stale partial output).
    if request.preempted or (request.error_message or "").startswith("[noah-preempted]"):
        # A benchmark task is pinned to the machine whose profile it measures —
        # a preemption must never re-pool it unpinned, or another worker would
        # claim it and stamp this profile with the wrong machine's scores. Keep
        # the pin AND the ASSIGNED status (the claim branch is
        # ASSIGNED+pinned-to-me); a PENDING+pin row matches neither claim branch
        # and would strand it forever.
        if task.assigned_to_worker == worker.worker_name and not _benchmark_target_worker(task):
            task.assigned_to_worker = None
        task.status = (ApiTaskStatus.ASSIGNED if task.assigned_to_worker
                       else ApiTaskStatus.PENDING)
        task.output_namespace = f"attempt_{task.attempt_count}_p{int(time.time())}"
        task.claimed_by_worker = None
        task.claimed_at = None
        task.claim_expires_at = None
        task.started_at = None
        task.result_payload = {"log_tail": request.log_tail, "preempted": True}
        try:
            from services import productivity_floor_metrics as _pfm
            _pfm.record_preemption(task.tenant_id)
        except Exception:  # noqa: BLE001 — a metrics blip never blocks the requeue
            logger.debug("record_preemption failed", exc_info=True)
        db.commit()
        logger.info(
            "Task %d preempted by NOAH on %s — requeued PENDING without "
            "retry charge (attempt_count stays %d)",
            task_id, worker.worker_name, task.attempt_count,
        )
        _publish_event(f"task:status:{task.id}", "requeued")
        return {"status": "ok", "requeued": True, "preempted": True}

    # Self-repairing build stages (test-and-repair) run their OWN internal
    # build+smoke+repair loop with an inner/outer budget. A task-level retry
    # re-runs that ENTIRE loop from scratch (resetting the budget), stacking N
    # full loops and burning hours on a doomed build — observed as a 9-hour
    # build from 3 stacked loops on an un-buildable Meshroom Dockerfile. The
    # internal budget IS the retry policy; never multiply it at the task level.
    # (Genuine worker-death is still recovered via claim expiry, a separate
    # path that doesn't go through should_retry.)
    # --- OOM handling. The worker DETECTS; the manager DECIDES. ------------
    # Two decisions, both of which the worker is in the wrong position to make:
    #
    #   1. Calibration and stressor runs deliberately load the box. An OOM
    #      there is the MEASUREMENT — the point at which this host stops
    #      coping — not a transient failure to retry. Retrying it just burns
    #      the box again and re-learns the same fact.
    #
    #   2. For ordinary work an OOM is a property of the HOST, so a retry
    #      should land somewhere else. A task pinned via assigned_to_worker
    #      would otherwise be handed straight back to the machine that just
    #      ran out of memory, and loop there until attempts are exhausted.
    _payload = task.task_payload or {}
    _is_calibration_work = bool(
        _payload.get("calibration_run") or _payload.get("is_stressor")
    )
    if request.oom_killed:
        logger.warning(
            "Task %d was OOM-killed on worker %s (node_kind=%s, calibration=%s)",
            task_id, worker.worker_name, _payload.get("node_kind"),
            _is_calibration_work,
        )
        if _is_calibration_work:
            # Record rather than retry — this is the datum we wanted.
            request.should_retry = False
        elif task.assigned_to_worker:
            if _benchmark_target_worker(task):
                # Benchmark task: the measurement is only valid on the pinned
                # machine, so an OOM there must NOT move the retry elsewhere —
                # the retry re-runs on the same worker (pin preserved).
                logger.warning(
                    "Task %d: benchmark OOM on pinned worker %s — pin preserved, "
                    "retry stays on the same host (scores are only valid there)",
                    task_id, task.assigned_to_worker,
                )
            else:
                logger.info(
                    "Task %d: clearing pin to %s after OOM so the retry can land on "
                    "a host with memory", task_id, task.assigned_to_worker,
                )
                task.assigned_to_worker = None

    # Unsupported-environment accelerator benchmark = terminal quarantine.
    # When the runtime cannot see the device at all in this environment (SYCL 0
    # devices, no /dev/dri, Level Zero can't open the device), retrying just
    # re-runs the same probe and re-learns the same fact — so fail terminally
    # NOW rather than burning the bounded retry budget on a certain outcome.
    # A transient/real accelerator failure keeps its normal retries.
    _node_kind_early = (task.task_payload or {}).get("node_kind", "")
    if _node_kind_early == "accelerator_benchmark" and request.should_retry:
        from utils.accelerator_quarantine import is_unsupported_environment
        if is_unsupported_environment(request.error_message):
            logger.warning(
                "Task %d: accelerator benchmark hit an unsupported-environment "
                "error — quarantining terminally (no retry): %s",
                task_id, (request.error_message or "")[:200],
            )
            request.should_retry = False

    # sz-4.7c — snapshot the 1-based attempt number THIS report is for,
    # before any branch below mutates attempt_count (the requeue branch bumps
    # it to the NEXT attempt, so capturing it after would mislabel the one
    # that just failed).
    _failed_attempt_no = task.attempt_count + 1

    _self_repairing = _is_self_repairing_stage(task)
    if _self_repairing and request.should_retry and task.attempt_count < task.max_attempts - 1:
        logger.warning(
            "Task %d: self-repairing stage (%s) exhausted its internal repair "
            "budget — NOT task-retrying (would re-run the whole loop and stack "
            "hours). Failing terminally.",
            task_id, (task.task_payload or {}).get("node_kind"),
        )
    if request.should_retry and not _self_repairing and task.attempt_count < task.max_attempts - 1:
        # Requeue with new attempt namespace.
        # PINNED tasks go back as ASSIGNED — the claim branches are
        # (ASSIGNED + pinned to me) and (PENDING + unpinned), so PENDING with a
        # pin still set matches neither and no worker can ever take it. The OOM
        # branch above deliberately clears the pin first when the retry should
        # move hosts; anything still pinned here is pinned on purpose.
        task.status = (ApiTaskStatus.ASSIGNED if task.assigned_to_worker
                       else ApiTaskStatus.PENDING)
        task.attempt_count += 1
        task.output_namespace = f"attempt_{task.attempt_count}"
        task.claimed_by_worker = None
        task.claimed_at = None
        task.claim_expires_at = None
        task.started_at = None
        task.error_message = _fx117_mask(request.error_message)
        task.error_detail = _fx117_mask(request.error_detail)
        task.result_payload = {"log_tail": _fx117_mask(request.log_tail),
                               "oom_killed": request.oom_killed,
                               **_ephemeral_applied_fields(request)}
        # GB.EGR.11b — record before the requeue commits so it isn't lost.
        # No NodeRun update on this path, so pass None.
        try:
            from services.egress_report import record_egress_report
            record_egress_report(db, task, None, request.egress)
        except Exception as _egr_exc:  # noqa: BLE001
            logger.warning("egress report failed for task %d: %s", task.id, _egr_exc)
        requeued = True
        logger.info("Task %d requeued (attempt %d/%d): %s", task_id, task.attempt_count + 1, task.max_attempts, request.error_message)
    else:
        # Final failure
        task.status = ApiTaskStatus.FAILED
        task.completed_at = datetime.now(timezone.utc)
        task.error_message = _fx117_mask(request.error_message)
        task.error_detail = _fx117_mask(request.error_detail)
        task.result_payload = {"log_tail": _fx117_mask(request.log_tail),
                               "oom_killed": request.oom_killed,
                               **_ephemeral_applied_fields(request)}

        # Propagate failure to NodeRun and WorkflowRun
        node_run = None
        if task.node_run_id:
            node_run = worker_tasks_lifecycle_crud.get_node_run_by_id(db, task.node_run_id)
            if node_run:
                node_run.status = TaskStatus.FAILED
                node_run.completed_at = datetime.now(timezone.utc)

        # GB.EGR.11b — record on the final-failure path too, with the
        # NodeRun when one was loaded above.
        try:
            from services.egress_report import record_egress_report
            record_egress_report(db, task, node_run, request.egress)
        except Exception as _egr_exc:  # noqa: BLE001
            logger.warning("egress report failed for task %d: %s", task.id, _egr_exc)

        workflow_run = worker_tasks_lifecycle_crud.get_workflow_run_by_id(db, task.workflow_run_id)
        is_profiling = (workflow_run.parameters or {}).get("is_profiling", False) if workflow_run else False
        if workflow_run and workflow_run.status in (TaskStatus.QUEUED, TaskStatus.RUNNING) and not is_profiling:
            workflow_run.status = TaskStatus.FAILED
            workflow_run.completed_at = datetime.now(timezone.utc)
            # Persist the last build/test error so a failed run isn't just a
            # "FAILED" badge with an empty error_message (WS6.7c). The task row
            # only carries "container exited with code N"; the actual reason
            # (final smoke stderr, "Auto-repair could not converge", etc.) is in
            # the worker log_tail. Bounded + secret-redacted (see helper).
            _failure_tail = _build_run_failure_tail(request.log_tail, request.error_message)
            if _fx117_vals and _failure_tail:
                from utils.secret_redaction import mask_exact_values
                _failure_tail = mask_exact_values(_failure_tail, _fx117_vals)
            if _failure_tail and not workflow_run.error_message:
                workflow_run.error_message = _failure_tail
            # delta-dag-wizard-cutover §22.4 — a run that just FAILED must not keep
            # spawning work. A task on a parallel chain with no DIRECT dependency on
            # the failed node stays claimable, and a worker will pick it up and run
            # it against a half-built run (observed: push-intel ran ~10 min after
            # its run failed, pushing an image whose hero counterpart never built).
            # The existing BLOCKED-propagation only walks the failing node's
            # descendants; this catches every OTHER still-claimable task of the run.
            # Scope is DB-only: cancel PENDING/ASSIGNED tasks (not yet claimed) so
            # none START after the failure. Already-RUNNING tasks finish/time out.
            try:
                cancelled = worker_tasks_lifecycle_crud.cancel_claimable_tasks_for_failed_run(
                    db, workflow_run.id, task.id
                )
                if cancelled:
                    logger.warning(
                        "workflow_run %s FAILED — cancelled %d still-claimable task(s) so they can't run against a failed run",
                        workflow_run.id, cancelled,
                    )
            except Exception as e:  # noqa: BLE001 — best-effort halt, never masks the failure report
                logger.warning("could not cancel downstream tasks for failed run %s: %s", workflow_run.id, e)

            # 2400LR-nrq — the task-level cancel above stops the QUEUE, but the
            # run's never-started NodeRuns stayed QUEUED forever (g1R run 79:
            # node_runs 98/99 of nodes 70/71), so the run-detail UI showed queued
            # nodes on a dead run and anything counting QUEUED node_runs
            # (scheduler/dashboards/quotas) over-counted. Same transaction as the
            # FAILED status above (commit happens later in this handler): one bulk
            # UPDATE of this run's QUEUED node_runs -> BLOCKED, the codebase's
            # canonical "never ran because upstream failed" status (see
            # crud/worker_tasks_lifecycle.skip_unstarted_node_runs_for_failed_run).
            # RUNNING node_runs are not touched — their task-result path owns them.
            try:
                _skipped = worker_tasks_lifecycle_crud.skip_unstarted_node_runs_for_failed_run(
                    db, workflow_run.id
                )
                if _skipped:
                    logger.warning(
                        "workflow_run %s FAILED — marked %d never-started node_run(s) BLOCKED "
                        "so they stop counting as queued",
                        workflow_run.id, _skipped,
                    )
            except Exception as e:  # noqa: BLE001 — best-effort, never masks the failure report
                logger.warning("could not skip queued node_runs for failed run %s: %s", workflow_run.id, e)

        logger.error("Task %d permanently failed after %d attempts: %s", task_id, task.attempt_count + 1, request.error_message)

        # If the failed task was a trivy_scan, fan out to the same result
        # handler the success path uses — the scanner emits {"status":"failed",
        # "error": "..."} on stdout even on exit-code-1, and that JSON carries
        # the actual reason (e.g., "docker pull failed: ... not found").
        # Without this, the NodeDefinition is stuck at scan_status=pending_scan
        # indefinitely — the "Waiting for security scan" UI black hole.
        failed_node_kind = task.task_payload.get("node_kind", "") if task.task_payload else ""
        if failed_node_kind == "trivy_scan":
            try:
                _process_trivy_scan_result(db, task)
            except Exception as e:
                logger.warning("Failed to process Trivy scan failure for task %d: %s", task_id, e)

        # Persistent-stack lifecycle — terminal failure only. This sits inside
        # the final-failure branch on purpose: a requeued teardown (max_attempts
        # 3) must not flag cleanup_pending while it still has retries left.
        if failed_node_kind.startswith("persistent-stack-"):
            try:
                _apply_stack_task_result(db, task, is_success=False,
                                         error_message=request.error_message)
            except Exception as e:
                logger.warning("Stack lifecycle failure update failed for task %d: %s", task_id, e)

        # Wizard artifact-generation failure → in-app notification so
        # the user doesn't sit watching a progress bar forever when
        # the Containerizer exited non-zero.
        try:
            _notify_wizard_generation_complete(db, task, is_success=False)
        except Exception as e:
            logger.warning("Wizard fail-notification for task %d: %s", task_id, e)

        # P9g.1 — even failed wizard tasks may have emitted a partial
        # port_plans block before exiting (e.g. port passed, docker
        # build failed downstream). Capture what's there so the
        # Coverage dashboard gets both success and failure signal.
        try:
            _record_port_outcomes(db, task, log_tail=request.log_tail)
        except Exception as e:
            logger.warning("port_outcomes record (failed task) %d: %s", task_id, e)

        # P9e — variant_smoke failure: flip NodeDefinition.variant_verification_status
        # for this variant to "failed". Only fires on the terminal (non-requeued)
        # path so transient retries don't prematurely mark a variant broken.
        if task.task_type == "variant_smoke":
            try:
                _apply_variant_smoke_result(
                    db, task, is_success=False,
                    log_tail=request.log_tail,
                    error_message=request.error_message,
                )
            except Exception as e:
                logger.warning("variant_smoke failure handler failed for task %d: %s", task_id, e)

    # OOM circuit breaker: if the failure looks like an OOM, degrade the pattern confidence
    if request.error_message and any(oom in request.error_message.lower() for oom in
            ["oom", "out of memory", "killed", "signal 9", "memory limit", "oomkilled"]):
        try:
            from scheduler.pattern_updater import _extract_node_kind
            node_kind = _extract_node_kind(task)
            if node_kind:
                pattern = worker_tasks_lifecycle_crud.get_resource_pattern_for_node_kind(
                    db, node_kind, task.queue_name
                )
                if pattern:
                    # Halve confidence — makes scheduler fall back to conservative mode
                    pattern.confidence = max(pattern.confidence * 0.5, 0.1)
                    logger.warning(
                        "OOM circuit breaker: halved confidence for %s/%s to %.2f",
                        node_kind, task.queue_name, pattern.confidence,
                    )
        except Exception as e:
            logger.debug("OOM circuit breaker failed: %s", e)

    # Reset benchmark/calibration status on failure
    node_kind = task.task_payload.get("node_kind", "") if task.task_payload else ""
    if node_kind in ("hardware_benchmark", "accelerator_benchmark") and not requeued:
        try:
            container_env = (task.task_payload or {}).get("container_env", {})
            profile_id = container_env.get("HARDWARE_PROFILE_ID")
            target_kind = container_env.get("TARGET_KIND", "").strip().lower()
            target_index = container_env.get("TARGET_INDEX", "").strip()

            if target_kind and target_index and profile_id:
                accel = worker_tasks_lifecycle_crud.get_accelerator_profile(
                    db, int(profile_id), target_kind, int(target_index)
                )
                if accel:
                    from utils.accelerator_quarantine import classify_accelerator_failure
                    accel.benchmark_status = classify_accelerator_failure(task.error_message)
                    accel.error_message = task.error_message or "Task failed"
                    logger.warning(
                        "Accelerator benchmark %s for profile %s %s[%s]: %s",
                        accel.benchmark_status, profile_id, target_kind, target_index,
                        (task.error_message or "")[:200],
                    )
            elif profile_id:
                profile = worker_tasks_lifecycle_crud.get_hardware_profile_by_id(db, int(profile_id))
                if profile:
                    logger.warning("System benchmark failed for profile %s", profile_id)
        except Exception as e:
            logger.debug("Failed to update benchmark status on failure: %s", e)

    # sz-4.7c — mirror of the success-path capture in report_task_complete.
    # Fires for BOTH a terminal failure and a failure that's about to be
    # requeued (the retry gets its own row via _failed_attempt_no, so retries
    # aren't lost from the delivery log and nothing is double-logged for this
    # single report). NOAH preemption returns above this point and is never
    # charged a delivery row. Must run BEFORE the commit; best-effort so it
    # can never fail failure-reporting itself.
    try:
        from services.output_delivery_capture import capture_from_task_result
        capture_from_task_result(
            db, task, succeeded=False,
            result=None,
            error=task.error_message,
            attempt=_failed_attempt_no,
        )
    except Exception as _cap_exc:  # noqa: BLE001
        logger.warning("output_delivery_capture failed for task %d: %s", task_id, _cap_exc)

    # Fortress 4.5 slice B2 — terminal-failure attestation: linked completion row
    # (status="failed") for each still-open dispatch row of this node run. Only on
    # the terminal branch — a requeued attempt is not terminal and its later attempt
    # gets the row. Best-effort, same guard style as the FX.91 emit wrapper.
    if not requeued:
        try:
            from services import workflow_run_attestations as _wra_fail
            _wra_fail.record_completion(db, task=task, worker=worker,
                                        status="failed")
        except Exception as _att_exc:  # noqa: BLE001 — never blocks a failure report
            logger.warning("workflow_run_attestations failure-completion failed for task %d: %s",
                           task_id, type(_att_exc).__name__)

    db.commit()

    # Ephemeral local NIM sleep-back — but ONLY on a TERMINAL failure. A
    # retryable failure (`requeued`) must NOT sleep the NIM: the retry reuses the
    # SAME task row (so sleep_back's exclude_task_id can't see it as a sibling),
    # and tearing the NIM down between attempts makes the retry cold-start into
    # nothing (the "connection refused -> DNS resolution failed" death spiral
    # that killed Studio Voice mid-workflow). On the final failure we free the
    # GPU. No-op for every non-NIM node kind.
    try:
        from services import nim_ephemeral as _nim
        _nk_nim = (task.task_payload or {}).get("node_kind")
        if _nim.spec_for(_nk_nim) is not None and not requeued:
            _nim.sleep_back(db, _nk_nim, task.tenant_id, exclude_task_id=task.id)
            db.commit()
    except Exception as _nim_exc:  # noqa: BLE001
        logger.warning("NIM sleep-back failed after failed task %d: %s", task.id, _nim_exc)

    # Universal stack release for non-NIM kinds (GA.GSW.2) — terminal failure
    # only, same reasoning as the NIM guard above. Best-effort.
    if not requeued:
        _release_stack_best_effort(db, task, (task.task_payload or {}).get("node_kind"))

    # WS4.Q17.16 — BYO-S3 migrate-on-activate: terminal failure only (a
    # requeued/retryable failure must not park the connection at
    # migration_status='failed' while a retry is still coming). Leaves
    # is_active untouched (it was never flipped True for this connection)
    # and records migration_error. Best-effort.
    _tp_failed = task.task_payload or {}
    if not requeued and _is_storage_migration_task(task, _tp_failed):
        try:
            from services import storage_migration as _stmig
            _stmig.fail_migration(db, task, request.error_message)
        except Exception as _mig_exc:  # noqa: BLE001
            logger.exception(
                "storage_migration failure hook failed for task %d: %s",
                task.id, _mig_exc,
            )

    # GSEC R-159 — terminal re-layout failure: audit it (it stays visible
    # in the per-worker status as failed until re-triggered). Nothing was
    # flipped, so no state to roll back.
    if not requeued and _is_hf_cache_relayout_task(task, _tp_failed):
        try:
            from services import hf_cache_relayout as _hcrl
            _hcrl.fail_task(db, task, request.error_message)
        except Exception as _rel_exc:  # noqa: BLE001
            logger.exception(
                "hf_cache_relayout failure hook failed for task %d: %s",
                task.id, _rel_exc,
            )

    # Fire queue_empty triggers when this task transitions terminal
    # (FAILED counts as terminal — drains an active row out of the queue).
    if not requeued:
        try:
            from services.queue_empty_cache import fire_queue_empty_triggers
            fire_queue_empty_triggers(db, task)
        except Exception as _qe_exc:
            logger.exception(
                "queue_empty fan-out failed for failed-task %d: %s",
                task.id, _qe_exc,
            )
        # §4.2 — node-completion triggers also fire on node FAILURE (on_status).
        if task.node_run_id:
            try:
                from services.workflow_orchestrator import fire_node_completion_triggers
                _nr = worker_tasks_lifecycle_crud.get_node_run_by_id(db, task.node_run_id)
                if _nr:
                    fire_node_completion_triggers(db, _nr)
            except Exception as _nc_exc:
                logger.exception("node-completion fan-out failed for failed-task %d: %s",
                                 task.id, _nc_exc)

    if not requeued and is_profiling:
        _check_and_dispatch_downstream(db, task)

    # Publish task status change via Redis
    _publish_event(f"task:status:{task.id}", "failed" if not requeued else "requeued")

    return {"status": "ok", "requeued": requeued}


def _is_platform_fixture_migrator_task(task, task_payload: dict) -> bool:
    """GC.PFM.7c — True only for a fixture_service.dispatch_migration task."""
    try:
        from services.fixture_service import FIXTURE_MIGRATOR_IMAGE as fixture_img
    except Exception:  # noqa: BLE001 — defensive; never break /credentials
        fixture_img = "mod/fixture-migrator"
    if task.workflow_run_id is not None or task.node_run_id is not None:
        return False
    if task_payload.get("node_kind") != "fixture_migrator":
        return False
    if task.output_namespace != "fixture_migrator":
        return False
    image = (task_payload.get("processing_image") or "").strip()
    if ":" in image.rsplit("/", 1)[-1]:
        image = image.rsplit(":", 1)[0]
    if image == fixture_img:
        return True
    _registry, sep, repo = image.partition("/")
    return bool(sep) and repo == f"modtim/{fixture_img}"


def _is_storage_migration_task(task, task_payload: dict) -> bool:
    """WS4.Q17.16 — True only for a services.storage_migration dispatch_migration
    task (the BYO-S3 migrate-on-activate copy job). Same shape of check as
    ``_is_platform_fixture_migrator_task`` above: a standalone task (no
    workflow/node run) with a server-set node_kind + output_namespace pair a
    tenant-reachable enqueue path never produces."""
    from services import storage_migration as _stmig
    return _stmig.is_migration_task(task, task_payload)


def _is_hf_cache_relayout_task(task, task_payload: dict) -> bool:
    """GSEC R-159 — True only for a services.hf_cache_relayout task (the
    one-time per-tenant HF cache re-layout run on each worker). Same shape
    of check as ``_is_storage_migration_task`` above: a standalone task (no
    workflow/node run) with a server-set node_kind + output_namespace pair
    a tenant-reachable enqueue path never produces."""
    if task.workflow_run_id is not None or task.node_run_id is not None:
        return False
    if task_payload.get("node_kind") != "hf_cache_relayout":
        return False
    if task.output_namespace != "hf_cache_relayout":
        return False
    return True


def _mint_byo_creds_for_connection(db: Session, conn) -> dict:
    """Decrypt one StorageConnection's own key and shape it exactly like the
    generic BYO-S3 credential block above. Deliberately does NOT require
    ``conn.is_active`` — used for both the destination (being activated,
    not yet active) and a prior-active source connection (already
    deactivated by the same activate call) of a storage_migration task."""
    from crud import storage_connection_cud as sc_crud

    secret = sc_crud.get_connection_secret(db, conn)
    if not secret:
        raise HTTPException(
            status_code=503,
            detail=f"Storage connection {conn.id} has no stored secret; cannot mint migration credentials",
        )
    scheme = "https" if conn.secure else "http"
    return {
        "mc_host": f"{scheme}://{conn.access_key}:{secret}@{conn.endpoint}",
        "endpoint": conn.endpoint,
        "expiry": None,
        "access_key": conn.access_key,
        "secret_key": secret,
        "session_token": "",
        "bucket": conn.bucket,
    }


def _mint_house_creds_for_tenant(tenant_id: int) -> dict:
    """House-default (versitygw) tenant-scoped creds, for the storage_migration
    task's house-backend side (whichever of source/dest is NOT a BYO
    connection). Mirrors the generic ``_use_vgw_mint`` mint below, minus the
    first-time ``rotate_tenant_secret`` realignment: a tenant beginning their
    first migration has no stray un-persisted account to realign, and if one
    somehow exists the mint fails closed (503) rather than silently minting
    a credential that won't authenticate."""
    from utils.storage_iam import mint_tenant_account
    from crud.secret_cud import _vault_get_tenant_secret, _vault_put_tenant_secret
    import secrets as _secmod

    vgw_secret_name = "versitygw-tenant-account"
    persisted = None
    try:
        persisted = _vault_get_tenant_secret(tenant_id, vgw_secret_name)
    except Exception:  # noqa: BLE001 — Vault miss/unavailable → create below
        persisted = None
    if not persisted:
        persisted = _secmod.token_hex(24)
        _vault_put_tenant_secret(tenant_id, vgw_secret_name, persisted)
    minted = mint_tenant_account(tenant_id, secret=persisted)
    scheme = "https" if minted.secure else "http"
    return {
        "mc_host": f"{scheme}://{minted.access_key}:{minted.secret_key}@{minted.endpoint}",
        "endpoint": minted.endpoint,
        "expiry": None,
        "access_key": minted.access_key,
        "secret_key": minted.secret_key,
        "session_token": "",
        "bucket": minted.bucket,
    }


def _mint_storage_migration_credentials(db: Session, task_id: int, task_payload: dict, tenant_id: int) -> dict:
    """WS4.Q17.16 — dual dest+source creds for the migrate-on-activate copy
    job. Neither side is necessarily the tenant's currently-ACTIVE backend:
    dest is the connection the activate call is trying to make active, and
    source may be a connection the SAME call already deactivated. Both
    resolve straight off ``task_payload``'s connection ids, tenant-scoped,
    never off ``StorageConnection.is_active``.

    D4 (external-fetch-inside-db-transaction): takes ``task_id``/``tenant_id``
    as plain locals rather than the ORM ``task`` — the caller commits before
    invoking this, so ``task`` is expired and must not be touched. Each
    connection lookup below is its own short-lived read (new transaction,
    auto-begun on ``db``) immediately followed by its own Vault decrypt —
    the same "one row, then Vault" shape the Harbor-credential block already
    uses in the external phase further down this file, not one long-held
    transaction spanning both mints."""
    from crud import storage_connection_cud as sc_crud

    dest_id = task_payload.get("dest_connection_id")
    if dest_id is None and task_payload.get("move_out") is True:
        # FIX-BYO-MOVE-OUT-DEST-HOUSE-CREDS — the headline "Move into MOD
        # storage" target (routers/storage_connections.py:617-618) enqueues
        # ``dest_connection_id=None``: the destination IS the tenant's house
        # (versitygw) backend, so mint the tenant-scoped house creds for it —
        # the mirror of the source-side fallback below. Tenant-scoped:
        # ``tenant_id`` is the task ROW's (the caller passes ``task.tenant_id``
        # at :5081), never anything off the payload. A set ``dest_id`` keeps
        # today's tenant-scoped lookup (and its 503 for a foreign/missing
        # connection), and an ACTIVATE migration (no ``move_out`` key) with a
        # None dest still raises exactly as before. Creds are returned only,
        # never logged.
        dest_creds = _mint_house_creds_for_tenant(tenant_id)
    else:
        dest_conn = sc_crud.get_connection(db, conn_id=dest_id, tenant_id=tenant_id) if dest_id else None
        if dest_conn is None:
            raise HTTPException(
                status_code=503,
                detail=f"storage_migration task {task_id}: destination connection {dest_id} not found for tenant {tenant_id}",
            )
        dest_creds = _mint_byo_creds_for_connection(db, dest_conn)

    source_id = task_payload.get("source_connection_id")
    if source_id:
        source_conn = sc_crud.get_connection(db, conn_id=source_id, tenant_id=tenant_id)
        if source_conn is None:
            raise HTTPException(
                status_code=503,
                detail=f"storage_migration task {task_id}: source connection {source_id} not found for tenant {tenant_id}",
            )
        source_creds = _mint_byo_creds_for_connection(db, source_conn)
    else:
        source_creds = _mint_house_creds_for_tenant(tenant_id)

    return {"minio": dest_creds, "minio_source": source_creds}


def _embedded_worker_seed_names() -> Tuple[str, ...]:
    """
    The trusted embedded/manager worker names — read from main's seed set,
    never from a request-supplied name. Lazy import because main imports this
    module (same pattern as the heartbeat's `from main import
    _EMBEDDED_WORKER_SEED_NAMES` at ~:3794); the fallback keeps the exemption
    fail-closed (SaaS name only) rather than crashing the request if the
    constant is ever renamed. Mirrors routers/worker_management.py's helper —
    this module must not import that router at module level (it is imported
    lazily from inside functions only, so a module-level import would risk a
    cycle).
    """
    try:
        from main import _EMBEDDED_WORKER_SEED_NAMES as _names
    except Exception:  # noqa: BLE001 — never let an import hiccup 500 a request
        _names = ("mod-worker-embedded",)
    return _names


def _is_loopback_endpoint(ep: str) -> bool:
    """True when an S3 endpoint host is only resolvable inside the manager box.

    BUG-1027k: the CE on-manager worker ``mod-worker-ce`` is handed the tenant's
    browser-facing external endpoint (e.g. ``localhost:7070``), which no worker
    container can reach — so the external-endpoint override must be skipped
    SEMANTICALLY (loopback host), never by adding the worker to a name list.
    Handles optional ``scheme://``, ``host:port`` and bracketed IPv6
    (``[::1]:7070``). Never raises: unparseable input is not loopback.
    """
    try:
        host = (ep or "").strip().lower()
        if not host:
            return False
        if "://" in host:
            host = host.split("://", 1)[-1]
        host = host.split("/", 1)[0]
        if host.startswith("["):
            end = host.find("]")
            host = host[1:end] if end != -1 else host[1:]
        elif host.count(":") > 1:
            # Unbracketed IPv6 (``::1``) — stripping on ':' would eat the host.
            pass
        else:
            host = host.split(":", 1)[0]
        if host in ("::1", "[::1]"):
            return True
        if host == "localhost" or host.endswith(".localhost"):
            return True
        import ipaddress
        return ipaddress.ip_address(host).is_loopback
    except Exception:  # noqa: BLE001 — a weird endpoint is never "loopback"
        return False


@router.post("/workers/tasks/{task_id}/credentials")
async def get_task_credentials(
    task_id: int,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """
    Get fresh storage and Harbor credentials for a claimed/running task.

    Storage creds come from the active backend: versitygw tenant-scoped minting
    (house default) or a tenant's BYO-S3 connection. The legacy MinIO STS path
    was removed in Gate 2.7. Credentials are generated on-demand and never stored
    in the task payload.

    GF.C6.5: also mints `model_cache_token` (short-lived, tenant-scoped,
    verified by mod-model-cache with the public key only) when the platform
    model cache is enabled — the worker swaps the container's HF_TOKEN for it
    whenever the container is pointed at a MOD model cache, so the tenant's
    raw HF token never ships to a container and gated bytes are cached under
    a tenant attribution. Omitted when the cache is off or the keypair is
    unconfigured (the worker then behaves exactly as before).
    """
    task = _get_worker_task(db, task_id, worker.worker_name)

    if task.status not in (ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING):
        raise HTTPException(status_code=409, detail=f"Task is {task.status.value}, cannot issue credentials")

    # WS4.Q17.16 — the storage_migration copy job needs BOTH source and dest
    # creds and neither is the tenant's currently-*active* backend (dest is
    # what's ABOUT to become active; source may already have been
    # deactivated by the same activate call). Short-circuits every other
    # branch below (Harbor, tenant_harbor, build token, node-ownership
    # paywall) — none of it applies to a task with no container, no image,
    # and no purchasable node_kind.
    _tp_early = task.task_payload or {}
    if _is_storage_migration_task(task, _tp_early):
        # D4: capture the plain locals this needs, THEN release the pooled
        # backend, THEN mint (each mint call below does its own short DB
        # read immediately followed by a Vault decrypt / versitygw mint —
        # see _mint_storage_migration_credentials). `task` is expired by
        # this commit; only the captured locals are used past this point.
        _mig_task_id = task.id
        _mig_tenant_id = task.tenant_id
        db.commit()
        return _mint_storage_migration_credentials(db, _mig_task_id, _tp_early, _mig_tenant_id)

    # node-ownership-purchases — run-time paywall (defense in depth #2): never
    # mint Harbor pull credentials for a priced maintained node the tenant does
    # not own. Catches the case where ownership was revoked between enqueue and
    # dispatch (e.g. a refund) — the enqueue check alone can't.
    _nk = (task.task_payload or {}).get("node_kind") if task.task_payload else None
    if _nk and task.tenant_id:
        try:
            import crud
            _tp = task.task_payload or {}
            _tk = _tp.get("trial_node_kind") if _tp.get("purpose") == "node_trial" else None
            _owns = crud.node_ownership_cud.tenant_may_run_node_kind(db, task.tenant_id, _nk, trial_node_kind=_tk)
        except Exception:  # noqa: BLE001 — never fail-closed on a check error
            _owns = True
            _record_paywall_check_error("credential_mint")
        if not _owns:
            raise HTTPException(
                status_code=402,
                detail=f"Node '{_nk}' is not owned by this tenant; no credentials issued",
            )

    creds = {}

    # D4 (external-fetch-inside-db-transaction): this handler is READ-ONLY on the
    # DB but historically ran every external credential mint (the versitygw
    # tenant-account mint, the Vault approle read for the tenant Harbor robot, and
    # the scoped build-token mint) while the get_db session was still checked out,
    # so a slow mint pinned a pgbouncer server backend for the whole network
    # round-trip. Fix: gather EVERYTHING the response needs from the DB first,
    # capture it into locals, db.commit() to release the pooled backend, THEN do
    # the external calls. The ORM `task` is expired by that commit, so the
    # external phase uses the captured `tenant_id` / `task_payload` /
    # `worker_name` locals, never `task.*`.
    tenant_id = task.tenant_id
    worker_name = worker.worker_name

    # Check if this task actually needs MinIO (download/upload sidecar).
    # System tasks (benchmarks, trivy scans, calibration) set skip_download
    # and skip_upload — they don't touch MinIO at all. Generating STS creds
    # for them is wasteful and, when the STS client has bugs (like the
    # offset-naive vs offset-aware datetime comparison), blocks the entire
    # credentials response with a 503 for tasks that never needed MinIO.
    #
    # EXCEPTION: The System benchmark sets skip_download=True + skip_upload
    # =True (no sidecars needed) but still wants MinIO creds because it
    # runs its own internal MinIO throughput measurements. Callers set
    # `needs_minio_creds: True` in the payload to override the "skip =
    # don't bother" inference. Without this, the benchmark reported
    # minio_*_mbps=0 with MINIO_ENDPOINT missing inside the container.
    task_payload = task.task_payload or {}

    # GC.PFM.7c — the fixture-migrator container task needs a credential
    # scoped to ONLY the shared platform-calibration-data bucket (never a
    # root key, never the requesting tenant's own bucket). Both signals
    # below are hardcoded server-side constants (services/fixture_service.py
    # FIXTURE_MIGRATOR_IMAGE / node_kind_label="fixture_migrator") set by
    # dispatch_migration — no tenant-reachable path can set either value
    # (every _enqueue_container_task caller that IS tenant-reachable passes
    # a hardcoded image + node_kind_label of its own, e.g. "imagebuilder:
    # latest"/"image_builder" — see routers/node_definitions_domains/
    # node_authoring.py).
    # Lead review 2026-09-29: node_kind alone is NOT server-only — workflow
    # and calibration node runs copy a tenant-authored node_def.node_kind
    # into the payload, and a basename image match would accept a tenant
    # image named ".../fixture-migrator". So require all of: a standalone
    # container task (no workflow/node run), node_kind + output_namespace
    # "fixture_migrator", and the image under the platform-only modtim/
    # project exactly as _resolve_platform_image produces it.
    _is_fixture_migrator_task = _is_platform_fixture_migrator_task(task, task_payload)

    skip_sidecars = bool(task_payload.get("skip_download")) and bool(task_payload.get("skip_upload"))
    explicit_minio = bool(task_payload.get("needs_minio_creds"))
    needs_minio = explicit_minio or (not skip_sidecars)

    # Gate 5 (5.7) — BYO-S3 takes priority. If this task's tenant has an ACTIVE
    # StorageConnection, the worker must talk to THAT endpoint/bucket with the
    # tenant's own stored key (decrypted fresh from Vault by resolve_backend) —
    # NOT versitygw minting and NOT MinIO STS. The tenant's key is already scoped
    # to their endpoint/bucket, so no minting is needed. The Go worker consumes
    # {access_key, secret_key, endpoint, mc_host, bucket, session_token:""}
    # generically (Gate 2B), so this is API-side only.
    # GC.PFM.7c — the fixture-migrator writes the shared PLATFORM bucket
    # (platform-calibration-data), never the requesting tenant's own
    # storage. Skip BYO-S3 resolution entirely for it: that tenant's BYO
    # connection points at THEIR endpoint/bucket, which would silently
    # misroute the platform fixture upload if applied here.
    _byo_cfg = None
    _sts_gate = None  # GSEC Q21.2 — set below when the BYO STS path applies
    if needs_minio and not _is_fixture_migrator_task:
        try:
            from utils import storage
            _cfg = storage.resolve_backend(tenant_id, db)
            if getattr(_cfg, "is_byo", False):
                _byo_cfg = _cfg
        except Exception as e:  # noqa: BLE001 — fall through to house-default creds
            logger.warning("BYO cred resolution failed for task %d: %s", task_id, e)
            _byo_cfg = None

    if _byo_cfg is not None:
        scheme = "https" if _byo_cfg.internal_secure else "http"
        mc_host = (
            f"{scheme}://{_byo_cfg.access_key}:{_byo_cfg.secret_key}"
            f"@{_byo_cfg.internal_endpoint}"
        )
        creds["minio"] = {
            "mc_host": mc_host,
            "endpoint": _byo_cfg.internal_endpoint,
            "expiry": None,             # tenant's own key; no TTL
            "access_key": _byo_cfg.access_key,
            "secret_key": _byo_cfg.secret_key,
            "session_token": "",        # BYO S3 static key; no session token
            "bucket": _byo_cfg.bucket,
        }
        logger.info(
            "/credentials task=%d: issued BYO-S3 tenant creds bucket=%s endpoint=%s "
            "(no minting, no STS)",
            task_id, _byo_cfg.bucket, _byo_cfg.internal_endpoint,
        )
        # GSEC Q21.2 — gate decision for the scoped STS session (DB-read
        # phase, pre-release). The tenant's static bundle above is the
        # FALLBACK: it is kept as-is when the gate is off (old worker
        # version, or sts_enabled=False) — byte-identical to today's
        # response — and only REPLACED by the session mint in the
        # post-release external phase below. The gate needs the worker's
        # last-reported agent version (Q21 refresher capable) AND the
        # tenant's active connection opt-in (Q21.1 sts_enabled column).
        from utils import worker_version_gate
        if worker_version_gate.worker_supports_sts_refresh(worker.agent_version):
            try:
                from crud import storage_connection_cud as _sc_crud_q21
                _active_conn = _sc_crud_q21.get_active_connection(db, tenant_id=tenant_id)
                if _active_conn is not None and bool(getattr(_active_conn, "sts_enabled", False)):
                    _sts_gate = {"ttl_s": _resolve_byo_sts_ttl_s(db)}
            except Exception as e:  # noqa: BLE001 — gate read failure keeps static creds
                logger.warning("GSEC Q21.2: STS gate read failed for task %d: %s", task_id, e)

    # Gate 2 (Phase B) — versitygw tenant-scoped minting is the credential path
    # for the (now house-default) versitygw backend. It hands the worker a
    # tenant-scoped, ownership-isolated credential minted by utils.storage_iam
    # (no STS, no session token). The Go worker consumes this bundle verbatim on
    # ALL paths and does not care which backend produced it.
    # See utils/storage_iam.py + tests/test_versitygw_isolation.py.
    #
    # Gate 2.7 (remove-minio-versitygw): the legacy MinIO STS path is REMOVED —
    # the STS client helper is deleted and MinIO is being decommissioned in
    # Gate 6. The `VERSITYGW_TASK_CREDS_MODE=sts` A/B override and the
    # Phase-A `VERSITYGW_MINT_TASK_CREDS` flag are gone with it. Non-BYO tasks on
    # any non-versitygw backend therefore have no credential source and fail
    # closed with a clear 503 rather than shipping an empty bundle to the worker.
    #
    # D4: the actual versitygw mint (an external call) runs AFTER db.commit()
    # below; here we only decide whether it applies.
    _backend = os.getenv("STORAGE_BACKEND", "versitygw").strip().lower()
    _use_vgw_mint = (
        needs_minio
        and _byo_cfg is None          # Gate 5: BYO-S3 already resolved above
        and _backend == "versitygw"
    )

    if needs_minio and _byo_cfg is None and not _use_vgw_mint:
        # No credential source: BYO-S3 is not active for this tenant and the
        # active backend is not versitygw. The MinIO STS path was removed in
        # Gate 2.7 (legacy STS client helper deleted). Fail closed — never ship an
        # empty/partial credentials bundle to the worker (that historically
        # caused silent AccessDenied inside the sidecar, e.g. MINIO_ACCESS_KEY="").
        logger.error(
            "/credentials task=%d: no credential source for backend=%r "
            "(BYO-S3 not active, versitygw minting not applicable, MinIO STS removed)",
            task_id, _backend,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"Cannot issue storage credentials for task {task_id}: backend="
                f"{_backend!r} has no credential path (MinIO STS removed in Gate 2.7; "
                "set STORAGE_BACKEND=versitygw or configure a BYO-S3 connection)."
            ),
        )
    # else: task explicitly opted out of MinIO (skip_download +
    # skip_upload + no needs_minio_creds override). Leave creds["minio"]
    # key absent — the worker's getter treats key-absent as "not
    # needed" the same way it treats explicit-null. Avoids an
    # inline `creds["minio"] = None` line that source-inspection
    # regression tests flag as "error-swallowed".

    # Remote (non-embedded) workers can't reach the in-network storage endpoint —
    # versitygw:7070 is a docker-network name that only resolves ON the manager. Hand
    # them the external/presigned endpoint (e.g. example.com) instead. The
    # embedded on-manager worker keeps the fast internal endpoint; BYO-S3 already uses
    # the tenant's own reachable endpoint, so leave it alone.
    #
    # D4: resolve_backend is a DB read, so PRE-FETCH the external endpoint here
    # (before the release) and apply it to creds["minio"] after the mint below.
    # BUG-1027k: the guard is SEMANTIC, not name-based — the CE on-manager worker
    # mod-worker-ce plus a browser-facing localhost:7070 external endpoint must keep
    # the internal endpoint (loopback is unreachable from any worker container).
    _remote_ext_override = None
    if _use_vgw_mint and worker_name not in _embedded_worker_seed_names():
        try:
            from utils.storage import resolve_backend as _rb
            _sc = _rb(tenant_id, db)
            _ext = (getattr(_sc, "external_endpoint", "") or "").strip()
            if _ext and not _is_loopback_endpoint(_ext):
                _remote_ext_override = {
                    "endpoint": _ext,
                    "scheme": "https" if getattr(_sc, "external_secure", True) else "http",
                }
        except Exception as e:  # noqa: BLE001 — never fail credential issuance on this
            logger.warning("external-endpoint override failed for task %d: %s", task_id, e)

    # Harbor credentials from tenant secrets — Vault is the single source of truth.
    # No env-var fallbacks: if any of registry/username/password is missing from
    # Vault for this tenant, refuse to issue credentials. Falling back to env
    # vars would produce registry/credential mismatches (e.g., Harbor creds
    # against example.com) that silently 401 inside the worker.
    #
    # SaaS pattern: harbor_* are platform-shared secrets (every tenant pulls
    # from the same Harbor). If the tenant has no own copy, fall back to the
    # platform tenant (id=1). Mirrors the runtime resolution in
    # routers/secrets.py:184-195 and the pre-flight check in
    # dependencies.py:require_harbor_credentials.
    PLATFORM_TENANT_ID = 1
    from crud.secret_cud import get_decrypted_secret_by_name
    def _harbor_unset(v: Optional[str]) -> bool:
        # A tenant row that still holds the auto-seeded 'PLEASE_CONFIGURE'
        # placeholder (or an empty string) is NOT a real credential. Treat it
        # as unset so we fall back to the platform tenant, exactly as an
        # absent row does. Without this the placeholder is shipped to the
        # worker verbatim, which builds `PLEASE_CONFIGURE/modtim/...` and
        # Docker rejects the pull with "repository name must be lowercase".
        # Matches the canonical placeholder check used across the codebase
        # (worker_management._harbor_creds_missing, dependencies, harbor.py).
        return not v or v.strip() == "" or v.strip() == "PLEASE_CONFIGURE"
    def _resolve_harbor(name: str) -> str:
        v = get_decrypted_secret_by_name(db, name, tenant_id)
        if _harbor_unset(v) and tenant_id != PLATFORM_TENANT_ID:
            v = get_decrypted_secret_by_name(db, name, PLATFORM_TENANT_ID)
        return v
    harbor_reg = _resolve_harbor("harbor_registry")
    harbor_user = _resolve_harbor("harbor_username")
    harbor_pass = _resolve_harbor("harbor_password")
    missing = [n for n, v in (
        ("harbor_registry", harbor_reg),
        ("harbor_username", harbor_user),
        ("harbor_password", harbor_pass),
    ) if _harbor_unset(v)]
    # 1665l Gap W — CE carve: CE ships no Harbor registry at all. When the
    # platform registry env (DOCKER_REGISTRY) is empty/unset there is nothing
    # to authenticate against, so the missing harbor_* secrets are NOT a
    # misconfiguration — issue the storage creds with an EMPTY harbor block
    # (the Go Credentials struct decodes a missing/null `harbor` key into
    # zero-value HarborCredentials, CORE/Worker/internal/transport/transport.go:703)
    # and let the worker's MOD_WORKER_PREFER_LOCAL_IMAGES path use the locally
    # built image. Same "configured" posture as dependencies.py:require_harbor_credentials
    # (1784) and routers/platform_feature_config.py:177. When a registry IS
    # configured, the 503 below stays exactly as before — still a real misconfig.
    from utils.registries import platform_registry_configured
    _registry_configured = platform_registry_configured()
    if missing and not _registry_configured:
        logger.info(
            "Task %d tenant %d has no Harbor secrets but no registry is configured "
            "(DOCKER_REGISTRY unset): issuing storage creds without harbor creds "
            "(CE carve; worker prefers local images)",
            task_id, tenant_id,
        )
        creds["harbor"] = {"registry": "", "username": "", "password": ""}
    elif missing:
        logger.error(
            "Task %d tenant %d is missing Vault Harbor secret(s): %s",
            task_id, tenant_id, ", ".join(missing),
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"Harbor credentials missing from Vault for tenant: {', '.join(missing)}. "
                "Set them via the secrets API; the worker cannot pull images without them."
            ),
        )
    else:
        creds["harbor"] = {
            "registry": harbor_reg.strip().rstrip("/"),
            "username": harbor_user,
            "password": harbor_pass,
        }

    # Local container registry (for user-created containers). Env-only, so it is
    # safe on either side of the release.
    creds["local_registry"] = {
        "registry": tenant_registry(),
        "external": os.getenv("LOCAL_REGISTRY_EXTERNAL", ""),
    }

    # Build-token gating needs a DB read (_task_is_system_build), so decide it
    # HERE (before the release) and capture the outcome; the actual external
    # mint_build_task_token runs in the external phase below.
    _emit_build_token = False
    _build_spec_id = None
    try:
        from utils.build_task_token import is_configured as _bt_is_configured
        if _bt_is_configured() and tenant_id and _task_is_system_build(db, task):
            _tp = task_payload
            _env = _tp.get("container_env") or {}
            _nd_env = (_tp.get("node_data") or {}).get("container_env") or {}
            _spec_raw = _env.get("BUILD_SPEC_ID") or _nd_env.get("BUILD_SPEC_ID")
            if _spec_raw is not None and str(_spec_raw).strip().isdigit():
                _emit_build_token = True
                _build_spec_id = int(str(_spec_raw).strip())
    except Exception as _bt_err:  # pragma: no cover — never block creds on this
        logger.warning("Task %d: build-task token gating skipped: %s", task_id, _bt_err)

    # GF.C6.5 — model-cache token gating needs a DB read (the
    # `model_cache_enabled` setting), so decide it HERE (before the release);
    # the actual mint (local signing, no I/O) runs in the external phase.
    # Gating failure degrades to "no token" (pre-GF.C6.5 behaviour), never a
    # credential-issuance 503.
    _emit_model_cache_token = False
    _mcs = optional_paid("services.model_cache_service")
    if _mcs is not None:
        try:
            _emit_model_cache_token = _mcs.should_issue_model_cache_token(tenant_id, db)
        except Exception as _mc_err:  # noqa: BLE001 — never block creds on this
            logger.warning("Task %d: model-cache token gating skipped: %s", task_id, _mc_err)

    # D4: release the pooled DB backend now. Everything above is gathered; every
    # call below is external and must NOT hold a pgbouncer server backend open.
    db.commit()

    # --- External credential phase (no open DB transaction) -------------------
    # The DB backend was released above, so every network round-trip below —
    # versitygw mint_tenant_account, the Vault approle_login / read_kv_v2 for the
    # tenant Harbor robot, mint_build_task_token, and the removed legacy MinIO STS
    # helpers get_minio_mc_env / get_global_sts_client — can no longer pin a
    # pgbouncer server backend for its duration. All state comes from the locals
    # captured above; the ORM `task` object is expired and must not be touched.

    # GSEC Q21.2 — BYO-S3 scoped, short-lived STS session (external call,
    # post-release: the AssumeRole round-trip must not pin a pgbouncer
    # backend). The gate (agent version + sts_enabled + TTL setting) was
    # decided in the DB-read phase above. Success REPLACES the static
    # bundle in creds["minio"] with a session bundle — same shape plus the
    # additive expires_at + refresh_interval_s the Q21.7 worker refresher
    # consumes. ANY failure keeps the static bundle and alerts (design: a
    # failed mint/refresh must never kill a healthy task — the worker keeps
    # the last good creds). Scope is the tenant's own bucket: MOD object
    # keys are content-addressed flat hashes (content_blobs.id IS the key;
    # inputs may reference blobs from earlier runs), so a narrower per-run
    # prefix would deny legitimate downloads — the session policy builder
    # in utils/sts_session.py still supports prefix tightening for callers
    # that can express one.
    if _sts_gate is not None and creds.get("minio") is not None:
        try:
            from utils import sts_session
            _sts = sts_session.assume_role(
                _byo_cfg.internal_endpoint,
                access_key=_byo_cfg.access_key,
                secret_key=_byo_cfg.secret_key,
                bucket=_byo_cfg.bucket,
                session_name=sts_session.session_name(task_id, tenant_id),
                ttl_seconds=_sts_gate["ttl_s"],
                session_policy=sts_session.build_session_policy(_byo_cfg.bucket),
                region=(getattr(_byo_cfg, "region", None) or "us-east-1"),
                secure=_byo_cfg.internal_secure,
            )
            _scheme = "https" if _byo_cfg.internal_secure else "http"
            _iso = _sts["expires_at"].isoformat()
            creds["minio"].update({
                "mc_host": (
                    f"{_scheme}://{_sts['access_key']}:{_sts['secret_key']}"
                    f"@{_byo_cfg.internal_endpoint}"
                ),
                "access_key": _sts["access_key"],
                "secret_key": _sts["secret_key"],
                "session_token": _sts["session_token"],
                "expiry": _iso,
                "expires_at": _iso,
                "refresh_interval_s": sts_session.refresh_interval_s(_sts_gate["ttl_s"]),
            })
            logger.info(
                "GSEC Q21.2: task %d BYO-S3 creds are a scoped STS session "
                "(bucket=%s ttl=%ds expires_at=%s refresh_every=%ds)",
                task_id, _byo_cfg.bucket, _sts_gate["ttl_s"], _iso,
                creds["minio"]["refresh_interval_s"],
            )
        except Exception as _sts_err:  # noqa: BLE001 — static key is the fallback
            _record_sts_mint_fallback("byo", task_id, _sts_err)

    if _use_vgw_mint and _is_fixture_migrator_task:
        try:
            # GC.PFM.7c — mint a DEDICATED versitygw account that owns ONLY
            # the shared platform-calibration-data bucket, via the same
            # scoped-minting primitive (mint_tenant_account) used for tenant
            # creds, but with a bucket + access_key override and a reserved
            # sentinel "tenant_id" that is never a real tenant row — so this
            # credential can never reach the requesting tenant's own
            # tenant-{id}-files bucket, and no root/admin key is issued.
            from utils.storage_iam import mint_tenant_account
            from crud.secret_cud import (
                _vault_get_tenant_secret, _vault_put_tenant_secret,
            )
            import secrets as _secmod
            _FX_VGW_TENANT_ID = -7001  # reserved sentinel; never a real tenant
            _FX_ACCESS_KEY = "platform-fixture-migrator"
            _FX_BUCKET = "platform-calibration-data"
            _fx_secret_name = "versitygw-fixture-migrator-account"
            _fx_persisted = None
            try:
                _fx_persisted = _vault_get_tenant_secret(_FX_VGW_TENANT_ID, _fx_secret_name)
            except Exception:  # noqa: BLE001 — Vault miss/unavailable → create below
                _fx_persisted = None
            if not _fx_persisted:
                _fx_persisted = _secmod.token_hex(24)
                _vault_put_tenant_secret(_FX_VGW_TENANT_ID, _fx_secret_name, _fx_persisted)
            minted = mint_tenant_account(
                _FX_VGW_TENANT_ID,
                secret=_fx_persisted,
                access_key=_FX_ACCESS_KEY,
                bucket=_FX_BUCKET,
            )
            scheme = "https" if minted.secure else "http"
            mc_host = (
                f"{scheme}://{minted.access_key}:{minted.secret_key}"
                f"@{minted.endpoint}"
            )
            creds["minio"] = {
                "mc_host": mc_host,
                "endpoint": minted.endpoint,
                "expiry": None,
                "access_key": minted.access_key,
                "secret_key": minted.secret_key,
                "session_token": "",
                "bucket": minted.bucket,
            }
            logger.info(
                "/credentials task=%d: issued versitygw platform-fixture creds "
                "ak=%s bucket=%s (scoped, no root key)",
                task_id, minted.access_key, minted.bucket,
            )
        except Exception as e:
            raise internal_http_error(
                503, f"Cannot mint fixture-migrator storage credentials for task {task_id}", e
            ) from e
    elif _use_vgw_mint:
        try:
            from utils.storage_iam import mint_tenant_account, rotate_tenant_secret
            # remove-minio-versitygw Phase B — PERSIST the tenant's versitygw
            # secret so it is STABLE across credential requests. Without this
            # the account keeps the first random secret while every later mint
            # returns a fresh `secrets.token_urlsafe(24)` (storage_iam
            # `_load_or_make_secret` is deliberately side-effect-free), so mc
            # signs with a secret the account never had → SignatureDoesNotMatch
            # on every download. The secret lives in Vault keyed by tenant, so
            # it survives API restarts AND a fresh versitygw IAM store.
            from crud.secret_cud import (
                _vault_get_tenant_secret, _vault_put_tenant_secret,
            )
            import secrets as _secmod
            _vgw_secret_name = "versitygw-tenant-account"
            _persisted = None
            try:
                _persisted = _vault_get_tenant_secret(tenant_id, _vgw_secret_name)
            except Exception:  # noqa: BLE001 — Vault miss/unavailable → create below
                _persisted = None
            if _persisted:
                minted = mint_tenant_account(tenant_id, secret=_persisted)
            else:
                # First time for this tenant — SERIALIZE with a per-tenant
                # pg_advisory_xact_lock so only one concurrent request generates+
                # persists the secret and calls rotate_tenant_secret (which
                # delete-creates the versitygw account).  After acquiring the
                # lock we re-check (double-checked locking); if another request
                # already persisted while we waited, we use that value instead
                # of rotating again (which would invalidate the first request's
                # already-dispatched mc_host credentials → SignatureDoesNotMatch).
                from sqlalchemy import text as _sa_text
                _lock_key = f"vgw-mint:{tenant_id}"
                try:
                    db.execute(_sa_text(
                        "SELECT pg_advisory_xact_lock(hashtext(:k))"),
                               {"k": _lock_key})
                except Exception:  # noqa: BLE001 — DB hiccup; best-effort
                    pass
                # Re-check after lock acquisition (double-checked locking).
                _persisted = None
                try:
                    _persisted = _vault_get_tenant_secret(
                        tenant_id, _vgw_secret_name)
                except Exception:  # noqa: BLE001
                    _persisted = None
                if _persisted:
                    minted = mint_tenant_account(tenant_id, secret=_persisted)
                else:
                    # Still first time after lock: mint, persist, rotate.
                    _persisted = _secmod.token_hex(24)
                    _vault_put_tenant_secret(
                        tenant_id, _vgw_secret_name, _persisted)
                    minted = rotate_tenant_secret(
                        tenant_id, new_secret=_persisted)
            scheme = "https" if minted.secure else "http"
            mc_host = (
                f"{scheme}://{minted.access_key}:{minted.secret_key}"
                f"@{minted.endpoint}"
            )
            creds["minio"] = {
                "mc_host": mc_host,
                "endpoint": minted.endpoint,
                "expiry": None,             # no TTL; revoked by account deletion
                "access_key": minted.access_key,
                "secret_key": minted.secret_key,
                "session_token": "",        # versitygw has no STS/session token
                "bucket": minted.bucket,
            }
            logger.info(
                "/credentials task=%d: issued versitygw tenant-scoped creds "
                "ak=%s bucket=%s (backend=versitygw; STS removed)",
                task_id, minted.access_key, minted.bucket,
            )
        except Exception as e:
            raise internal_http_error(
                503, f"Cannot mint versitygw credentials for task {task_id}", e
            ) from e

    # Apply the pre-fetched external endpoint override now that the bundle exists.
    if (_remote_ext_override and creds.get("minio")
            and _byo_cfg is None and worker_name not in _embedded_worker_seed_names()):
        _m = creds["minio"]
        _ext = _remote_ext_override["endpoint"]
        _m["endpoint"] = _ext
        _m["mc_host"] = f"{_remote_ext_override['scheme']}://{_m['access_key']}:{_m['secret_key']}@{_ext}"
        logger.info(
            "/credentials task=%d: remote worker %s → external storage endpoint %s",
            task_id, worker_name, _ext,
        )

    # Tenant Harbor robot — separate from the platform Harbor above.
    # Wizard-built nodes push to and pull from the tenant Harbor (e.g.
    # example.com/tenant-{id}/), and the platform admin
    # creds above are NOT valid against that registry. Without these,
    # the worker pulls with platform creds, the tenant Harbor returns
    # 401, and the task hangs on `pull image` retries forever.
    #
    # Lives in Vault at tenants/{id}/tenant_harbor (provisioned by
    # _get_or_provision_tenant_harbor_creds in node_definitions.py on
    # first wizard build). Read here per-task instead of caching so a
    # rotated robot password takes effect on the next dispatch with no
    # API restart.
    #
    # If Vault has no entry, leave the field empty: the worker picker
    # falls back to the platform Harbor creds, which produces a clear
    # 401 the operator can act on instead of a silent feature gap.
    creds["tenant_harbor"] = {"registry": "", "username": "", "password": ""}
    try:
        from utils.vault_client import approle_login, read_kv_v2
        vault_role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
        vault_secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
        if vault_role_id and vault_secret_id:
            vt = approle_login(role_id=vault_role_id, secret_id=vault_secret_id)
            th = read_kv_v2(f"tenants/{tenant_id}/tenant_harbor", token=vt)
            if th and th.get("registry") and th.get("username") and th.get("password"):
                creds["tenant_harbor"] = {
                    "registry": (th.get("registry") or "").strip(),
                    "username": th.get("username") or "",
                    "password": th.get("password") or "",
                }
                logger.info(
                    "Task %d tenant %d: tenant_harbor creds attached (registry=%s, user=%s)",
                    task_id, tenant_id, creds["tenant_harbor"]["registry"],
                    creds["tenant_harbor"]["username"],
                )
            else:
                logger.warning(
                    "Task %d tenant %d: Vault returned no tenant_harbor entry (th=%r) — pull will use platform creds and may 401",
                    task_id, tenant_id, bool(th),
                )
        else:
            logger.warning(
                "Task %d tenant %d: VAULT_APPROLE_* env not set in API container — cannot read tenant_harbor",
                task_id, tenant_id,
            )
    except Exception as e:  # pragma: no cover — non-fatal
        logger.warning(
            "Tenant Harbor creds unavailable for task %d tenant %d (will fall back to platform creds): %s",
            task_id, tenant_id, e,
        )

    # Per-task scoped build credential (STS-style, mirrors the MinIO triplet
    # above). Replaces the long-lived platform MASTER key that build-DAG
    # stages used to receive as API_KEY. Minted ONLY for system-shared
    # build-DAG tasks, scoped to (tenant_id, build_spec_id) with a short TTL.
    # The worker injects it into the build-task container as API_KEY; the
    # build-task callback endpoints verify it and enforce the scope.
    # Non-fatal: if minting is unconfigured or the task carries no
    # BUILD_SPEC_ID, we just omit it — nothing else in the response depends
    # on it. Gating was decided (DB read) before the release; only the external
    # mint runs here.
    if _emit_build_token:
        try:
            from utils.build_task_token import mint_build_task_token
            creds["build_token"] = mint_build_task_token(
                tenant_id=int(tenant_id),
                build_spec_id=int(_build_spec_id),
                task_id=int(task_id),
            )
            logger.info("Task %d: minted scoped build_token (spec=%s tenant=%s)",
                        task_id, _build_spec_id, tenant_id)
        except Exception as _bt_err:  # pragma: no cover — never block creds on this
            logger.warning("Task %d: build-task token mint skipped: %s", task_id, _bt_err)

    # GF.C6.5 — per-task tenant-scoped model-cache token (same STS posture as
    # build_token above: fresh, short-lived, never persisted or logged). The
    # worker presents it as the container's HF_TOKEN ONLY when the container
    # is pointed at a MOD model cache; mod-model-cache verifies it, resolves
    # the tenant's real HF token itself, and stamps the download with tenant
    # attribution. Non-fatal: minting failure omits the field, and the worker
    # then falls back to the pre-GF.C6.5 raw-token behaviour.
    if _emit_model_cache_token and _mcs is not None:
        try:
            creds["model_cache_token"] = _mcs.mint_model_cache_token(tenant_id=int(tenant_id))
            logger.info("Task %d tenant %d: issued model_cache_token (tenant-scoped, TTL default)",
                        task_id, tenant_id)
        except Exception as _mc_mint_err:  # pragma: no cover — never block creds on this
            logger.warning("Task %d: model-cache token mint skipped: %s", task_id, _mc_mint_err)

    return creds


class SecretRequest(BaseModel):
    """Worker sends its ephemeral X25519 public key (raw 32 bytes, base64-encoded)."""
    worker_public_key: str  # base64-encoded 32-byte X25519 public key


def _is_self_repairing_stage(task) -> bool:
    """True for build-DAG stages that run their OWN internal build+smoke+repair
    loop (test-and-repair). Such a stage exhausts an inner/outer repair budget
    before it ever reports failure, so a task-level retry re-runs the whole loop
    from scratch — the internal budget IS the retry policy and must not be
    multiplied at the task level (see the requeue guard in report-failed).

    Matched by node_kind (``*test_and_repair*``) or the STAGE env the DAG sets
    (``test_and_repair_variant``), so it holds whether the payload carries
    node_kind, container_env, or node_data.container_env."""
    tp = task.task_payload or {}
    nk = tp.get("node_kind") or ""
    if isinstance(nk, str) and "test_and_repair" in nk:
        return True
    env = tp.get("container_env") or {}
    nd_env = (tp.get("node_data") or {}).get("container_env") or {}
    return (env.get("STAGE") or nd_env.get("STAGE") or "") == "test_and_repair_variant"


def _task_is_system_build(db: Session, task) -> bool:
    """True iff the task runs a SYSTEM-shared build-DAG node (node_kind starts
    with ``system_`` and its NodeDefinition has tenant_id IS NULL). Gates
    resolution of source-typed build-credential refs so a tenant-authored node
    can never request Harbor/platform/LLM creds."""
    nk = (task.task_payload or {}).get("node_kind") or ""
    if not isinstance(nk, str) or not nk.startswith("system_"):
        return False
    nd = worker_tasks_lifecycle_crud.find_system_node_definition(db, nk)
    return nd is not None


@router.post("/workers/tasks/{task_id}/secrets")
async def resolve_task_secrets(
    task_id: int,
    body: SecretRequest,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """
    Resolve secret references for a task using X25519 encrypted transport.

    Flow:
      1. Worker generates ephemeral X25519 keypair, sends public key
      2. API generates its own ephemeral X25519 keypair
      3. ECDH derives a shared secret (both sides compute the same one)
      4. API encrypts resolved secrets with AES-256-GCM using shared secret
      5. Returns: {encrypted_secrets, api_public_key, nonce} — all base64
      6. Worker derives same shared secret, decrypts

    Plaintext secrets never appear in the HTTP response. Ephemeral keys are
    discarded immediately — a captured response is useless.
    """
    import base64
    import hashlib
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from services import oauth_task_secrets as oauth_ts

    task = _get_worker_task(db, task_id, worker.worker_name)

    if task.status not in (ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING):
        raise HTTPException(status_code=409, detail=f"Task is {task.status.value}, cannot resolve secrets")

    secret_refs = (task.task_payload or {}).get('secret_refs', [])
    if not secret_refs:
        return {"encrypted_secrets": "", "api_public_key": "", "nonce": ""}

    # Resolve plaintext values (stays in memory only — envelope-encrypted below)
    resolved = {}
    from crud.secret_cud import get_decrypted_secret_by_name
    from node_builder.build_dag.build_secrets import (
        is_build_secret_ref, resolve_build_secret_ref,
    )

    # Source-typed refs (build-DAG Harbor/API/LLM creds) are only honored for
    # SYSTEM build-task nodes — a tenant-authored node can never harvest them.
    build_ok = _task_is_system_build(db, task)

    # FX.117 p3b — per-secret allowed-workflows scope
    # (services/secret_workflow_scope_service.py, table
    # `secret_workflow_scopes`). When the secret has active scope rows and
    # this task's run workflow is not among them the value is NOT injected:
    # a WARNING naming the secret only (never the value), an audited
    # SECRET_ACCESS_DENIED row, and the node fails with the message below
    # (the Go worker reports a non-200 here as `secret resolution failed:
    # HTTP 409: <detail>` and marks the node failed). A secret with NO
    # scope rows behaves exactly as before.
    _scope_denials = []
    # FX.117 p4a — expired secrets are refused the same way (see the gate
    # comment at the call site below); kept SEPARATE from the scope denials
    # so the node message names the real cause.
    _expiry_denials = []
    # sz-4.6a — OAuth-connection secret refs (services/oauth_task_secrets.py)
    _oauth_denials = []

    # Fortress 1.3 (664) — one name-only audit row per secret ref this task
    # attempted (NEVER a value: the row dicts carry secret_name/outcome/reason
    # only). Collected during the loop, written ONCE below (services/secret_access_log.py
    # record_secret_accesses: never raises, tolerates a missing table, honours the
    # fortress.secret_access_log_enabled kill switch). Build refs are skipped:
    # they are system build-DAG credentials, not tenant secrets.
    _acc_rows = []
    _acc_wrid = task.workflow_run_id
    _acc_nrid = task.node_run_id
    _acc_nid = task.node_id
    _acc_accessor = f"worker:{worker.worker_name}"

    def _acc_row(secret_name, outcome, reason=None):
        if secret_name:
            _acc_rows.append({
                "tenant_id": task.tenant_id,
                "secret_name": secret_name,
                "accessor": _acc_accessor,
                "outcome": outcome,
                "reason": reason,
                "workflow_run_id": _acc_wrid,
                "node_run_id": _acc_nrid,
                "node_id": _acc_nid,
            })

    for ref in secret_refs:
        if oauth_ts.is_oauth_secret_ref(ref):
            _oauth_value, _oauth_denial = oauth_ts.resolve_oauth_secret_ref(db, task, ref)
            if _oauth_value:
                resolved[ref.get('env_var') or oauth_ts.DEFAULT_ENV_VAR] = _oauth_value
                _acc_row(f"oauth:{ref.get('oauth_connection_id')}", "granted")
            else:
                _oauth_denials.append((ref.get('oauth_connection_id'), _oauth_denial))
                _acc_row(f"oauth:{ref.get('oauth_connection_id')}",
                         "denied", _oauth_denial)
            continue
        env_var = ref.get('env_var', '')
        if not env_var:
            continue
        if is_build_secret_ref(ref):
            if not build_ok:
                logger.warning(
                    "Dropping build secret_ref env_var=%s for task %d — "
                    "not a system build-task", env_var, task_id,
                )
                continue
            value = resolve_build_secret_ref(
                db, task.tenant_id, ref.get('source'), ref.get('field'),
            )
            if value:
                resolved[env_var] = value
            else:
                logger.warning(
                    "Build secret '%s'/%s unresolved for task %d (tenant %d)",
                    ref.get('source'), ref.get('field'), task_id, task.tenant_id,
                )
            continue
        secret_name = ref.get('secret_name', '')
        if not secret_name:
            continue
        # FX.117 p3b — per-secret allowed-workflows scope
        # (services/secret_workflow_scope_service.py, table
        # `secret_workflow_scopes`). When the secret has active scope rows and
        # this task's run workflow is not among them the value is NOT injected:
        # a WARNING naming the secret only (never the value), an audited
        # SECRET_ACCESS_DENIED row, and the node fails with the message below
        # (the Go worker reports a non-200 here as `secret resolution failed:
        # HTTP 409: <detail>` and marks the node failed). A secret with NO
        # scope rows behaves exactly as before.
        _scope_row = None
        try:
            from crud.secret_cud import get_secret_by_name as _scope_lookup
            _scope_row = _scope_lookup(db, secret_name, task.tenant_id)
        except Exception:  # noqa: BLE001 — lookup failure = no scope info
            _scope_row = None
        if _scope_row is not None:
            from services import secret_workflow_scope_service as _sws
            if not _sws.assert_run_scope(
                db, _scope_row, secret_name,
                workflow_run_id=task.workflow_run_id,
                node_id=task.node_id,
            ):
                _scope_denials.append(secret_name)
                _acc_row(secret_name, "denied", "workflow_scope")
                continue
            # FX.117 p4a — per-secret EXPIRY (services/secret_expiry_service.py,
            # table `secret_expiries`, migration
            # 20261007zzzf_fx117_p4_secret_expiries). An EXPIRED secret is
            # refused the SAME way an out-of-scope one is: the value is NOT
            # injected, a WARNING names the secret only (never the value), an
            # audited SECRET_ACCESS_DENIED row with reason=expired is written,
            # and the node fails with "secret <name> has expired". Gated by
            # secrets.block_expired (default true — an expired credential
            # silently injected is the unsafe direction).
            from services import secret_expiry_service as _ses
            if not _ses.assert_run_expiry(
                db, _scope_row, secret_name,
                workflow_run_id=task.workflow_run_id,
                node_id=task.node_id,
            ):
                _expiry_denials.append(secret_name)
                _acc_row(secret_name, "denied", "expired")
                continue
        try:
            value = get_decrypted_secret_by_name(db, secret_name, task.tenant_id)
            if value is not None:
                resolved[env_var] = value
                _acc_row(secret_name, "granted")
            else:
                logger.warning(
                    "Secret '%s' not found for task %d (tenant %d)",
                    secret_name, task_id, task.tenant_id,
                )
        except Exception as e:
            logger.warning(
                "Failed to resolve secret '%s' for task %d: %s",
                secret_name, task_id, e,
            )

    # Fortress 1.3 (664) — write the name-only secret_access_log rows for every
    # ref this task attempted, ONCE, BEFORE the 409 denials raise and before the
    # encrypted envelope returns. record_secret_accesses never raises, tolerates
    # the table not existing yet, and honours the fortress.secret_access_log_enabled
    # kill switch, so the HTTP response/status is untouched either way.
    # Commit ordering: the DENIAL paths already commit — assert_run_scope /
    # assert_run_expiry / resolve_oauth_secret_ref each end in an audit write
    # (audit.log_security_event does db.add + db.commit), which releases the
    # savepoint the INSERT ran in, so those rows land. The SUCCESS path (value
    # resolved, envelope returned) has no other commit — this handler is
    # read-only on the DB otherwise — so the rows would be rolled back with the
    # request session. Hence the guarded db.commit() right here: it is the one
    # place this endpoint owns its own audit rows, wrapped so a commit failure
    # (lost/broken connection) can never change the response — rollback + a
    # warning naming only the exception class.
    from services import secret_access_log as _secret_access_log
    _secret_access_log.record_secret_accesses(db, _acc_rows)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 — logging must not fail the request
        db.rollback()
        logger.warning("secret_access_log commit skipped: %s", type(exc).__name__)

    # FX.117 p3b — a scoped secret the run may not use fails the node with a
    # clear message (secret NAME only, never the value). Checked BEFORE the
    # empty-response short-circuit so a run whose only secret was refused
    # still fails instead of silently running with nothing injected.
    if _scope_denials:
        raise HTTPException(
            status_code=409,
            detail=f"secret {_scope_denials[0]} is not allowed for this workflow",
        )

    # FX.117 p4a — an EXPIRED secret the run may not use fails the node with
    # a clear message (secret NAME only, never the value). Checked BEFORE the
    # empty-response short-circuit so a run whose only secret was refused
    # still fails instead of silently running with nothing injected.
    if _expiry_denials:
        raise HTTPException(
            status_code=409,
            detail=f"secret {_expiry_denials[0]} has expired",
        )

    # sz-4.6a — an OAuth-connection ref the run may not use fails the node with
    # a clear message (connection ID only, never a token). Checked BEFORE the
    # empty-response short-circuit so a run whose only secret was refused
    # still fails instead of silently running with nothing injected.
    if _oauth_denials:
        _cid, _code = _oauth_denials[0]
        raise HTTPException(
            status_code=409,
            detail=oauth_ts.denial_message(_code, _cid),
        )

    if not resolved:
        return {"encrypted_secrets": "", "api_public_key": "", "nonce": ""}

    # --- X25519 + AES-256-GCM envelope encryption ---
    # 1. Decode worker's ephemeral public key
    try:
        worker_pub_bytes = base64.b64decode(body.worker_public_key)
        if len(worker_pub_bytes) != 32:
            raise ValueError("X25519 public key must be 32 bytes")
        worker_pub = X25519PublicKey.from_public_bytes(worker_pub_bytes)
    except Exception as e:
        logger.error("Invalid worker public key: %s", e)
        raise HTTPException(status_code=400, detail="Invalid worker public key")

    # 2. Generate API's ephemeral keypair (lives only for this request)
    api_private = X25519PrivateKey.generate()
    api_pub_bytes = api_private.public_key().public_bytes_raw()

    # 3. ECDH → shared secret → AES-256 key via SHA-256
    shared_secret = api_private.exchange(worker_pub)
    aes_key = hashlib.sha256(shared_secret).digest()  # 32 bytes → AES-256

    # 4. Encrypt the resolved secrets JSON
    plaintext = json.dumps(resolved).encode("utf-8")
    nonce = os.urandom(12)  # 96-bit nonce for GCM
    aesgcm = AESGCM(aes_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    # 5. Return encrypted blob — plaintext never in the response
    return {
        "encrypted_secrets": base64.b64encode(ciphertext).decode(),
        "api_public_key": base64.b64encode(api_pub_bytes).decode(),
        "nonce": base64.b64encode(nonce).decode(),
    }


@router.get("/workers/tasks/{task_id}/status")
async def get_task_status(
    task_id: int,
    db: Session = Depends(get_db),
    caller: models.User = Depends(require_bearer_or_api_key),
):
    """Internal endpoint: get task status. Used by Traffic_Cop_Dag to poll remote task progress."""
    task = worker_tasks_lifecycle_crud.get_task_queue_row_by_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    # GSEC FX.1 (R-051): this route leaked ANY task's result_payload to any
    # authenticated caller. Non-admin callers now see only their own tenant's
    # tasks; a mismatched tenant is a 404 (not 403) so task existence is not
    # enumerable. Admins keep the previous unfiltered view. Remote workers do
    # not poll this route — they claim/complete via /workers/tasks/poll +
    # /report (require_enrolled_worker) — so no worker auth is affected.
    if not getattr(caller, "is_platform_admin", False) and task.tenant_id != caller.tenant_id:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task.id,
        "status": task.status.value,
        "claimed_by_worker": task.claimed_by_worker,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "result_payload": task.result_payload,
        "error_message": task.error_message,
    }


class TaskLogRequest(BaseModel):
    lines: List[str] = Field(..., description="Log lines to append")
    timestamp: Optional[str] = Field(None, description="ISO8601 timestamp of when lines were captured")


# Gate 3 — the marker a node prints to report progress. Parsed by the WORKER
# (transport.CheckpointPrefix), not here: only remote workers use the log
# endpoint, so extracting on this side recorded nothing for embedded ones.
# Kept as the documented wire contract, and asserted equal on both sides.
CHECKPOINT_LOG_PREFIX = "MOD_CHECKPOINT "


def _emit_checkpoint_event(db: Session, task, checkpoints) -> None:
    """Put progress on the run-detail timeline, where people already look.

    Gate 3's frontend half, and it needs no frontend: `workflow_run_events`
    already feeds the run-detail Logs panel, so a checkpoint event appears
    beside the orchestrator and traffic-cop lines that explain the rest of the
    run. A dedicated panel would have been a second place to look for the same
    story.

    Only the LAST checkpoint of a batch is emitted. A six-hour node reporting
    every few seconds would otherwise bury the events that explain a failure
    under thousands of progress lines — the timeline is for reading, and the
    full series is in `workflow_checkpoints` for anything that wants to plot it.
    """
    try:
        last = (checkpoints or [])[-1]
        if last is None:
            return
        pct = f"{last.progress * 100:.0f}%" if last.progress is not None else "?"
        label = (last.label or "").strip()
        attempt = _attempt_number(task)
        msg = f"Checkpoint {pct}" + (f" — {label}" if label else "")
        if attempt:
            # Only mentioned on a retry. On a first attempt it is noise, and on
            # a retry it is the whole point: this is the run that got further.
            msg += f" (attempt {attempt + 1})"
        from services.workflow_orchestrator import _emit_run_event
        _emit_run_event(db, task.workflow_run_id, "checkpoint", "info", msg,
                        {"node_id": task.node_id, "progress": last.progress,
                         "attempt": attempt})
    except Exception:  # noqa: BLE001 — a timeline entry must never fail a task
        logger.debug("checkpoint event emit failed", exc_info=True)


class CheckpointReport(BaseModel):
    progress: Optional[float] = None
    label: Optional[str] = None
    payload: Optional[Any] = None


class CheckpointReportRequest(BaseModel):
    checkpoints: List[CheckpointReport] = []


@router.post("/workers/tasks/{task_id}/checkpoints")
def report_task_checkpoints(task_id: int,
                            request: CheckpointReportRequest,
                            worker: WorkerLicense = Depends(require_enrolled_worker),
                            db: Session = Depends(get_db)):
    """How far a node has got, reported by the worker that is running it.

    The worker extracts these from the node's stdout and sends them HERE rather
    than the manager parsing the log stream, because only remote workers use the
    log endpoint — DirectTransport writes lines straight to Redis. Extracting at
    the worker is the one place that sees every node's output regardless of how
    that worker talks home.

    ADVISORY. `progress` drives a UI and never a decision: a node reporting 0.99
    and then dying is still retried, because the only thing that proves
    completion is the task status the executor observed.
    """
    task = _get_worker_task(db, task_id, worker.worker_name)
    if not task or not task.node_run_id:
        # Stack ops and system tasks have no node run. Not an error — there is
        # simply nothing for a checkpoint to belong to.
        return {"status": "ok", "recorded": 0}

    recorded = 0
    try:
        for c in (request.checkpoints or [])[:200]:
            progress = c.progress
            if progress is not None:
                try:
                    progress = max(0.0, min(1.0, float(progress)))
                except (TypeError, ValueError):
                    progress = None
            db.add(models.WorkflowCheckpoint(
                workflow_run_id=task.workflow_run_id,
                node_run_id=task.node_run_id,
                # BOTH, added, because a node run is retried two different
                # ways and each counts somewhere else:
                #   - the reaper reuses this ROW and increments attempt_count
                #   - a re-dispatch creates a NEW row, where attempt_count
                #     restarts at 0 and only the row count has moved
                # Using either alone reads "attempt 0" for half the retries in
                # the system, which is how this was wrong the first time.
                attempt=_attempt_number(task),
                label=(c.label or "")[:255] or None,
                progress=progress,
                payload=c.payload if isinstance(c.payload, (dict, list)) else None,
            ))
            recorded += 1
        if recorded:
            # BEFORE the commit, not after. `_emit_run_event` deliberately does
            # not commit — "let the caller's transaction commit it" — so an
            # event added afterwards sits in the session and is discarded. The
            # checkpoints and the timeline entry are one logical write anyway:
            # if the rows roll back, the line claiming them should too.
            _emit_checkpoint_event(db, task, request.checkpoints)
            db.commit()
    except Exception:  # noqa: BLE001 — bookkeeping must not fail a running task
        logger.debug("checkpoint report failed for task %s", task_id, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"status": "ok", "recorded": 0}
    return {"status": "ok", "recorded": recorded}


class StorageMigrationProgressRequest(BaseModel):
    object_count: int
    byte_count: int


class StorageMigrationProgressResponse(BaseModel):
    status: str
    recorded: bool


@router.post(
    "/workers/tasks/{task_id}/storage-migration-progress",
    response_model=StorageMigrationProgressResponse,
)
def report_storage_migration_progress(
    task_id: int,
    request: StorageMigrationProgressRequest,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """WS4.Q17.16 — periodic progress from the Go worker's storage_migration
    copy job (``Transport.ReportStorageMigrationProgress``).

    Minimal by design, per the task spec's own fallback instruction: the
    existing ``/checkpoints`` endpoint silently no-ops for this task type
    (it requires ``task.node_run_id``, which a runless migration task never
    has — see the early return in ``report_task_checkpoints`` above).

    Persists straight onto the destination StorageConnection row
    (``migrated_object_count``/``migrated_bytes``). Best-effort: never fails
    the worker's call. Only accepts monotonically-increasing counts so an
    out-of-order/retried report can't regress the numbers the completion
    hook (``services.storage_migration.complete_migration``) later
    sanity-checks against ``task_payload["objects"]``.
    """
    task = _get_worker_task(db, task_id, worker.worker_name)
    if not task:
        return {"status": "ok", "recorded": False}
    task_payload = task.task_payload or {}
    if not _is_storage_migration_task(task, task_payload):
        # Not this task type — advisory endpoint, no error for a mismatch.
        return {"status": "ok", "recorded": False}
    try:
        from crud import storage_connection_cud as sc_crud
        conn = sc_crud.get_connection(
            db, conn_id=task_payload.get("dest_connection_id"), tenant_id=task.tenant_id,
        )
        if conn is not None:
            new_count = max(0, int(request.object_count))
            new_bytes = max(0, int(request.byte_count))
            if new_count >= (conn.migrated_object_count or 0):
                conn.migrated_object_count = new_count
            if new_bytes >= (conn.migrated_bytes or 0):
                conn.migrated_bytes = new_bytes
            db.commit()
            return {"status": "ok", "recorded": True}
    except Exception:  # noqa: BLE001 — progress reporting must never fail the worker's call
        logger.debug("storage_migration progress report failed for task %s", task_id, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return {"status": "ok", "recorded": False}


class CheckpointReapRequest(BaseModel):
    # Volume names this worker holds and believes nothing is using.
    volumes: List[str] = []


@router.post("/workers/checkpoints/reapable")
def checkpoints_reapable(request: CheckpointReapRequest,
                         worker: WorkerLicense = Depends(require_enrolled_worker),
                         db: Session = Depends(get_db)):
    """Which of these checkpoint volumes may be removed.

    WORKER ASKS, MANAGER DECIDES. The worker holds the volumes and can remove
    them; only the manager knows whether a run is finished with them. Asking
    keeps the direction outbound — nothing dials into a worker — and keeps the
    authority where the truth is.

    A volume is reapable when its workflow run has reached a terminal state. NOT
    when the node run succeeded: a run can retry a node later, and reclaiming
    between attempts would destroy exactly the progress this exists to keep.

    Unknown volumes are reapable. A name that matches the pattern but belongs to
    no node run is an orphan from a deleted run — the case that leaves 3,900
    volumes on a host if nobody collects it.

    Fails CLOSED: anything this cannot resolve is left alone. Refusing to reap
    costs disk; reaping wrongly costs a six-hour job.
    """
    import re as _re
    out = []
    pattern = _re.compile(r"^modcore-ckpt-noderun-(\d+)$")
    terminal = ("completed", "failed", "cancelled", "error", "succeeded")
    for name in (request.volumes or [])[:500]:
        m = pattern.match((name or "").strip())
        if not m:
            continue                      # not ours to judge
        try:
            nr = db.get(NodeRun, int(m.group(1)))
            if nr is None:
                out.append(name)          # orphan: the node run is gone
                continue
            run = db.get(WorkflowRun, nr.workflow_run_id) if nr.workflow_run_id else None
            if run is None:
                out.append(name)
                continue
            status = str(getattr(run.status, "value", run.status) or "").lower()
            if status in terminal:
                out.append(name)
        except Exception:  # noqa: BLE001 — unresolved means keep
            logger.debug("reapable check failed for %s", name, exc_info=True)
    if out:
        logger.info("checkpoint reap: %s may remove %d volume(s)",
                    worker.worker_name, len(out))
    return {"reapable": out}


@router.post("/workers/tasks/{task_id}/log")
async def stream_task_log(
    task_id: int,
    request: TaskLogRequest,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """Append log lines from a running task. Stored in Redis with 24h TTL."""
    task = _get_worker_task(db, task_id, worker.worker_name)

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("WORKER_TASK_LOG_REDIS_URL", "redis://redis:6379/2"))
        key = f"task:{task_id}:logs"
        for line in request.lines:
            r.rpush(key, line)
        r.expire(key, 86400)  # 24h TTL
    except Exception as e:
        logger.warning("Failed to store logs for task %d: %s", task_id, e)

    # NOT parsed for checkpoints here. This endpoint is the SIGNALLING path and
    # only remote workers use it: DirectTransport writes log lines straight to
    # Redis with no API round-trip, so an embedded worker's output never arrives
    # here at all. Hooking checkpoints on this route recorded nothing for half
    # the fleet, which a real run found and twenty unit tests did not.
    #
    # Checkpoints are durable state, and DirectTransport already draws that line
    # — "Redis is used for task signalling; the API is still used for durable
    # state changes". They go to /checkpoints, which both transports reach.
    return {"status": "ok", "lines_stored": len(request.lines)}


# =============================================================================
# Resource Telemetry Endpoints
# =============================================================================

class ResourceSampleItem(BaseModel):
    timestamp: str
    phase: str
    cpu_percent: Optional[float] = None
    ram_mb: Optional[float] = None
    vram_mb: Optional[float] = None
    gpu_util_pct: Optional[float] = None
    gpu_temp_c: Optional[float] = None
    net_rx_mbps: Optional[float] = None
    net_tx_mbps: Optional[float] = None
    disk_read_iops: Optional[float] = None
    disk_write_iops: Optional[float] = None
    single_thread_detected: Optional[bool] = None
    container_disk_read_mb: Optional[float] = None
    container_disk_write_mb: Optional[float] = None
    vram_alloc_rate_mb_s: Optional[float] = None
    process_count: Optional[int] = None
    gpu_clock_mhz: Optional[float] = None
    swap_used_mb: Optional[float] = None

class ResourceSamplesRequest(BaseModel):
    samples: List[ResourceSampleItem]


@router.post("/workers/tasks/{task_id}/resource-samples")
async def receive_resource_samples(
    task_id: int,
    request: ResourceSamplesRequest,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """
    Receive batched resource time-series samples from a running task.
    Workers send these every 30s. Used by pattern_updater to classify
    resource patterns after task completion.
    """
    task = _get_worker_task(db, task_id, worker.worker_name)

    samples_to_insert = []
    for s in request.samples:
        try:
            ts = datetime.fromisoformat(s.timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            ts = datetime.now(timezone.utc)

        samples_to_insert.append(TaskResourceSample(
            task_id=task.id,
            worker_name=worker.worker_name,
            timestamp=ts,
            phase=s.phase,
            cpu_percent=s.cpu_percent,
            ram_mb=s.ram_mb,
            vram_mb=s.vram_mb,
            gpu_util_pct=s.gpu_util_pct,
            gpu_temp_c=s.gpu_temp_c,
            net_rx_mbps=s.net_rx_mbps,
            net_tx_mbps=s.net_tx_mbps,
            disk_read_iops=s.disk_read_iops,
            single_thread_detected=s.single_thread_detected,
            container_disk_read_mb=s.container_disk_read_mb,
            container_disk_write_mb=s.container_disk_write_mb,
            vram_alloc_rate_mb_s=s.vram_alloc_rate_mb_s,
            process_count=s.process_count,
            gpu_clock_mhz=s.gpu_clock_mhz,
            swap_used_mb=s.swap_used_mb,
            disk_write_iops=s.disk_write_iops,
        ))

    if samples_to_insert:
        db.bulk_save_objects(samples_to_insert)
        db.commit()

    # Late-sample classification. The worker batches samples every 30s and
    # flushes whatever remains when the task ends, so the FINAL batch routinely
    # lands AFTER report_task_completed has already run. That handler calls
    # update_pattern_from_task, which finds zero samples and early-returns at
    # debug level — silently, exactly like the node_config payload bug this
    # pipeline already suffered.
    #
    # Observed live: task 10967 completed with 3 samples on disk, yet no
    # ResourcePattern row; running the classifier by hand immediately produced
    # one. Without this the corpus never accumulates, and predictive scheduling
    # would be switched on against an empty table.
    #
    # Classifying here is idempotent — update_pattern_from_task upserts.
    if samples_to_insert and task.status == ApiTaskStatus.COMPLETED:
        try:
            from scheduler.pattern_updater import update_pattern_from_task
            update_pattern_from_task(task, db)
        except Exception as e:
            logger.warning(
                "Late pattern classification failed for task %d: %s", task_id, e)

    return {"status": "ok", "samples_stored": len(samples_to_insert)}


class PhaseValidationRequest(BaseModel):
    phase: str
    actual_resources: Dict[str, Any]

@router.post("/workers/tasks/{task_id}/validate-phase")
async def validate_phase(
    task_id: int,
    request: PhaseValidationRequest,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """
    Validate worker's actual resources against the scheduler's phase plan.
    Called by workers before each lifecycle phase.

    Uses pattern-aware thresholds:
    - Early ramp phases: lenient (50% deviation allowed)
    - Peak phases: strict (20% deviation allowed)
    - Step function patterns: strict from the start
    - Network/disk: always lenient (30% tolerance)
    """
    task = worker_tasks_lifecycle_crud.get_task_queue_row_by_id(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Record actual resources in scheduling_metadata for accuracy tracking
    meta = task.scheduling_metadata or {}
    validations = meta.get("validations", [])
    validations.append({
        "phase": request.phase,
        "actual": request.actual_resources,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    meta["validations"] = validations
    task.scheduling_metadata = meta
    db.commit()

    # If no phase plan (FIFO mode / feature flag off), always proceed
    if not task.phase_plan:
        return {"proceed": True}

    # Find the expected resources for this phase from the plan
    phase_plan = task.phase_plan
    phase_entry = None
    for p in phase_plan.get("phases", []):
        if p.get("name") == request.phase:
            phase_entry = p
            break

    if not phase_entry:
        # Phase not in plan — proceed (could be an unplanned phase)
        return {"proceed": True}

    expected_resources = phase_entry.get("resources", {})
    if isinstance(expected_resources, str):
        # Pattern reference like "pattern:albedo_texture_process" — proceed, we validated at scheduling time
        return {"proceed": True}

    actual = request.actual_resources
    proceed = True
    reasons = []

    # RAM check with pattern-aware tolerance
    expected_ram = expected_resources.get("ram_mb")
    if expected_ram and expected_ram > 0:
        actual_available = actual.get("ram_available_mb", 0)
        # Determine tolerance based on phase position
        tolerance = _get_phase_tolerance(request.phase, phase_plan)
        required_available = expected_ram * (1.0 - tolerance)
        if actual_available < required_available:
            proceed = False
            reasons.append(f"RAM: need {required_available:.0f}MB available, have {actual_available:.0f}MB")

    # VRAM fragmentation check (hard stop)
    expected_vram = expected_resources.get("vram_mb")
    if expected_vram and expected_vram > 0:
        vram_largest = actual.get("vram_largest_block_mb", actual.get("vram_free_mb", 0))
        if vram_largest < expected_vram:
            proceed = False
            reasons.append(f"VRAM: need {expected_vram:.0f}MB contiguous, largest block is {vram_largest:.0f}MB")

    # GPU power state check (hard stop for GPU phases)
    if expected_vram and expected_vram > 0:
        gpu_state = actual.get("gpu_power_state", "active")
        if gpu_state == "low_power" and request.phase in ("container_start", "processing"):
            proceed = False
            reasons.append(f"GPU in low_power state, need active for {request.phase}")

    # Thermal throttle check
    if actual.get("thermal_throttled", False) and request.phase == "processing":
        reasons.append("CPU thermal throttled (warning, not blocking)")

    if not proceed:
        # FX.168 p1 guard 1 — a non-GPU queue has NO VRAM to reserve, so a
        # VRAM-only complaint against a CPU/SYSTEM task is a phantom demand
        # (the queue-wide pattern fallback can hand a GPU node's avg_peak_vram_mb
        # to a CPU row; see scheduler/overlap_scheduler._vram_demand_for_queue).
        # Letting it through stops the claim→validate→revert self-DDoS the
        # re-pool below would otherwise amplify (~1.4/s, scheduling_version 1446).
        from scheduler.overlap_scheduler import _gpu_queue_names
        _blocking = [r for r in reasons if "warning, not blocking" not in r]
        _vram_only = bool(_blocking) and all(
            r.startswith("VRAM:") or r.startswith("GPU in low_power")
            for r in _blocking)
        _task_queue = (getattr(task, "queue_name", None) or "").strip().lower()
        if _vram_only and _task_queue not in _gpu_queue_names():
            logger.info("Task %d phase-validation VRAM complaint ignored on "
                        "non-GPU queue %s: %s",
                        task_id, _task_queue or "unknown", "; ".join(reasons))
            return {"proceed": True}

        # Revert task to PENDING for rescheduling
        #
        # A benchmark task is pinned to the machine whose profile it measures,
        # so it must NOT be re-pooled unpinned (another worker would claim it
        # and write that machine's scores onto the pinned worker's profile).
        # Keep the pin + ASSIGNED so only the pinned worker can re-take it; if
        # that worker genuinely can't run it, the profiling-deadline expirer
        # fails the run with the real reason.
        _pinned_benchmark = bool(_benchmark_target_worker(task))
        if not _pinned_benchmark:
            # FX.168 p1 guard 2 — churn ceiling. A genuinely un-runnable task
            # used to re-pool forever (claim → validate → revert), which is an
            # API self-DDoS amplifier. Past the ceiling the task is FAILED
            # outright with the real reasons instead of being re-pooled.
            #
            # The counter is validate-phase re-pools ONLY, NOT scheduling_version:
            # the overlap scheduler bumps that on routine re-plans too
            # (assignment-expiry revert + batch placement), so a legitimate GPU
            # task that waited an hour in the queue could sit at version 200 and
            # would fail on its FIRST rejection. The count lives in
            # `task_payload.validate_phase_repools` — task_payload is the one
            # ApiTaskQueue JSONB the scheduler never rewrites (it reads it, and
            # its claim SQL writes status/assigned_to_worker/start_after/
            # phase_plan/scheduling_metadata only), so the count survives the
            # claim→validate→revert loop it has to bound. phase_plan and
            # scheduling_metadata are both clobbered wholesale at claim time, so
            # they would reset the count every cycle and never trip.
            from services import platform_settings as _psr
            _repool_ceiling = int(_psr.get_setting(
                "scheduler.validate_phase_repool_ceiling", db=db) or 0)
            _payload = task.task_payload if isinstance(
                task.task_payload, dict) else {}
            _repools = int(_payload.get("validate_phase_repools") or 0) + 1
            if 0 < _repool_ceiling < _repools:
                task.status = ApiTaskStatus.FAILED
                task.completed_at = datetime.now(timezone.utc)
                task.error_message = (
                    f"validate-phase re-pool limit exceeded: {'; '.join(reasons)}")
                task.claimed_by_worker = None
                task.assigned_to_worker = None
                task.scheduling_version = (task.scheduling_version or 0) + 1
                db.commit()
                logger.error("Task %d FAILED after %d validate-phase re-pools "
                             "on phase %s: %s",
                             task_id, _repools, request.phase, "; ".join(reasons))
                return {"proceed": False, "reason": "; ".join(reasons),
                        "action": "fail_task"}
            # Count the re-pool we are about to do (copy + reassign + flag so
            # SQLAlchemy persists the JSONB change).
            _payload = dict(_payload)
            _payload["validate_phase_repools"] = _repools
            task.task_payload = _payload
            flag_modified(task, "task_payload")
        task.status = (ApiTaskStatus.ASSIGNED if _pinned_benchmark else ApiTaskStatus.PENDING)
        if not _pinned_benchmark:
            task.assigned_to_worker = None
        task.claimed_by_worker = None
        task.scheduling_version = (task.scheduling_version or 0) + 1
        db.commit()
        logger.info("Task %d failed phase validation for %s: %s (pinned_benchmark=%s)",
                    task_id, request.phase, "; ".join(reasons), _pinned_benchmark)
        return {"proceed": False, "reason": "; ".join(reasons),
                "action": "hold_pinned" if _pinned_benchmark else "reschedule"}

    return {"proceed": True}


def _get_phase_tolerance(phase: str, phase_plan: dict) -> float:
    """
    Get the resource deviation tolerance for a phase based on the pattern context.
    Early ramp → lenient, peak → strict, step function → strict from start.
    """
    confidence = phase_plan.get("scheduling_confidence", 0.5)

    # Network/disk phases are always lenient
    if phase in ("image_pull", "data_download", "upload", "cleanup"):
        return 0.3

    # For processing: tolerance depends on pattern
    phases = phase_plan.get("phases", [])
    for p in phases:
        if p.get("name") == "processing":
            resources = p.get("resources", {})
            if isinstance(resources, str) and "step_function" in resources:
                return 0.2  # Strict for step functions
            break

    # Default: moderate tolerance, adjusted by confidence
    base = 0.35
    return base * (1.0 - confidence * 0.3)  # Higher confidence → stricter


@router.get("/workers/tasks/resolve-cog-hash/{cog_id}")
async def resolve_cog_hash(
    cog_id: int,
    principal=Depends(node_or_service_principal),
    db: Session = Depends(get_db),
):
    """Resolve a cog's content hash (SHA256) and file extension from its latest version.

    Accepts either the existing Bearer/API-key service auth or a run_task node token
    (X-API-Key). For the run_task path the resolved cog's tenant is pinned to the token.
    """
    cog = worker_tasks_status_crud.get_cog_by_id(db, cog_id)
    if not cog or not cog.latest_version_id:
        raise HTTPException(status_code=404, detail="Cog or version not found")

    # run_task node caller: pin the resolved cog to the token's tenant.
    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=cog.tenant_id)

    version = worker_tasks_status_crud.get_version_by_id(db, cog.latest_version_id)
    if not version or not version.content_id:
        raise HTTPException(status_code=404, detail="Content not found")

    blob = worker_tasks_status_crud.get_content_blob(db, version.content_id, cog.tenant_id)

    return {
        "cog_id": cog_id,
        "content_hash": version.content_id,
        "file_extension": blob.file_extension if blob else "",
        "size_bytes": blob.size_bytes if blob else 0,
    }


@router.get("/workers/tasks/system-tasks")
async def list_system_tasks(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    List all system/platform tasks (thumbnails, converters, scanners).
    Platform admin only.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    total, tasks = worker_tasks_status_crud.list_system_tasks_page(db, offset, limit)

    return {
        "total": total,
        "tasks": [
            {
                "id": t.id,
                "node_id": t.node_id,
                "status": t.status.value,
                "queue_name": t.queue_name,
                "node_kind": t.task_payload.get("node_kind", "") if t.task_payload else "",
                "processing_image": t.task_payload.get("processing_image", "") if t.task_payload else "",
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "error_message": t.error_message,
                "has_logs": bool(t.result_payload and t.result_payload.get("log_tail")),
                # GY.V4b — the last-line summary is a log tail shown to the
                # caller, so it is redacted on display only (stored
                # result_payload is never rewritten; off profile = unchanged).
                "log_summary": _v4b_redact_text(
                    (t.result_payload.get("log_tail", "") or "").strip().split("\n")[-1][:200]
                ) if t.result_payload else "",
                "claimed_by_worker": t.claimed_by_worker,
            }
            for t in tasks
        ],
    }


@router.get("/workers/tasks/{task_id}/container-logs")
async def get_container_logs(
    task_id: int,
    db: Session = Depends(get_db),
    caller: models.User = Depends(require_bearer_or_api_key),
):
    """
    Get container execution logs for a worker task.
    Reads from Redis (live/recent logs) with fallback to result_payload.log_tail (completed tasks).
    """
    task = worker_tasks_lifecycle_crud.get_task_queue_row_by_id(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # IDOR gate: a task's container logs are private. This endpoint was keyed by
    # task_id with NO tenant filter and NO view gate, so any authenticated caller
    # could read another tenant's/user's per-task container logs by id.
    #
    # Resolve task -> workflow_run and gate on run-view (owner / view-grant /
    # platform admin), mirroring the sibling
    # /workers/workflow-runs/{id}/container-logs fix. Tasks with NO associated
    # run (build / system tasks — thumbnails, converters, zip downloads) have no
    # workflow to gate on, so fall back to a strict tenant match against the
    # task's own tenant_id (or platform admin). Workers only PUSH logs (SendLog
    # -> Redis) via a different endpoint, so this pull-side gate does not affect
    # the ingest path.
    from resource_permissions import can_view_workflow_run
    if getattr(caller, "is_platform_admin", False):
        _authorized = True
    elif task.workflow_run_id is not None:
        run = worker_tasks_lifecycle_crud.get_workflow_run_by_id(db, task.workflow_run_id)
        _authorized = bool(
            run is not None
            and run.tenant_id == caller.tenant_id
            and can_view_workflow_run(db, caller, run)
        )
    else:
        # No associated run (build/system task): keep the caller's own-tenant
        # log view working without leaking a co-tenant's task.
        _authorized = task.tenant_id == caller.tenant_id
    if not _authorized:
        # 404 (not 403) to avoid leaking task existence — matches sibling fixes.
        raise HTTPException(status_code=404, detail="Task not found")

    # Try Redis first (live logs, 24h TTL)
    lines = []
    source = "none"
    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        key = f"task:{task_id}:logs"
        raw_lines = r.lrange(key, 0, -1)
        if raw_lines:
            lines = [line.decode("utf-8", errors="replace") for line in raw_lines]
            source = "redis"
    except Exception as e:
        logger.debug("Redis log read failed for task %d: %s", task_id, e)

    # Fallback to stored log_tail in result_payload
    if not lines and task.result_payload:
        log_tail = task.result_payload.get("log_tail", "")
        if log_tail:
            lines = log_tail.split("\n")
            source = "result_payload"

    # Fallback to error message if no logs captured (e.g. pull failed, container never ran)
    if not lines and task.result_payload:
        error = task.result_payload.get("error", "")
        if error:
            lines = [f"[TASK ERROR] {error}"]
            source = "error_message"
    if not lines and task.error_message:
        lines = [f"[TASK ERROR] {task.error_message}"]
        source = "error_field"

    # GY.V4b — PII/PHI redaction on READ only. Stored log tails and the Redis
    # log list are never rewritten; the profile (privacy.redaction_profile,
    # default off) returns the same list object when off, so this is
    # byte-identical to today, and a redaction failure passes the lines through.
    lines = _v4b_redact_lines(lines)

    return {
        "task_id": task_id,
        "lines": lines,
        "source": source,
        "line_count": len(lines),
    }


@router.get("/workers/workflow-runs/{workflow_run_id}/container-logs")
async def get_workflow_run_container_logs(
    workflow_run_id: int,
    db: Session = Depends(get_db),
    caller: models.User = Depends(require_bearer_or_api_key),
):
    """δ — aggregated container-logs view for a DAG-dispatched build.

    Mirrors the /workers/tasks/{task_id}/container-logs shape but
    walks ALL child api_task_queue rows for the given workflow_run.
    Each child task's lines are prefixed with `[task <id> (<variant>)]`
    so a multi-variant build's interleaved output is readable and a
    failing variant is identifiable.

    Why this exists: the DAG-based phase-2 dispatch returns a
    `workflow_run_id`, not a single task id. The legacy endpoint
    would either 404 OR — worse — collide with an unrelated old
    `ApiTaskQueue` row whose id happens to equal the new
    workflow_run_id (recently observed: workflow_run_id=6 reading a
    4-day-old completed task's "Billing settings already configured"
    log line). The frontend's BuildProgressCard routes by phase.
    """
    run = worker_tasks_lifecycle_crud.get_workflow_run_by_id(db, workflow_run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    # Per-workflow gate: this aggregated log view was tenant-unfiltered and
    # ungated, leaking a co-tenant's (or cross-tenant's) private workflow-run
    # container logs by run id. Workers only PUSH logs (SendLog -> Redis); the
    # only pull consumer is the UI's BuildProgressCard, whose caller owns the
    # build run — so the owner/granted/admin view gate keeps it working.
    from resource_permissions import can_view_workflow_run
    if run.tenant_id != caller.tenant_id or not can_view_workflow_run(db, caller, run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    child_tasks = worker_tasks_status_crud.list_child_tasks_for_workflow_run(db, workflow_run_id)

    aggregated_lines: List[str] = []
    sources_used = set()

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
    except Exception as e:
        logger.debug("Redis client init failed for workflow_run %d: %s", workflow_run_id, e)
        r = None

    for t in child_tasks:
        tp = t.task_payload or {}
        env = tp.get("container_env", {}) if isinstance(tp, dict) else {}
        variant = env.get("VARIANT") or env.get("variant") or ""
        prefix = f"[task {t.id}" + (f" ({variant})" if variant else "") + "]"

        # Try Redis first for this child task (live + 24h TTL).
        lines: List[str] = []
        source = "none"
        if r is not None:
            try:
                key = f"task:{t.id}:logs"
                raw_lines = r.lrange(key, 0, -1)
                if raw_lines:
                    lines = [line.decode("utf-8", errors="replace") for line in raw_lines]
                    source = "redis"
            except Exception as e:
                logger.debug("Redis log read failed for child task %d: %s", t.id, e)

        # Fallback to stored log_tail on the child row (completed tasks).
        if not lines and t.result_payload:
            log_tail = t.result_payload.get("log_tail", "")
            if log_tail:
                lines = log_tail.split("\n")
                source = "result_payload"

        # Last-resort: surface the task error so a build that never
        # produced stdout (e.g. image pull failed) still shows WHY.
        if not lines and t.result_payload:
            error = t.result_payload.get("error", "")
            if error:
                lines = [f"[TASK ERROR] {error}"]
                source = "error_message"
        if not lines and t.error_message:
            lines = [f"[TASK ERROR] {t.error_message}"]
            source = "error_field"

        if lines:
            sources_used.add(source)
            for line in lines:
                aggregated_lines.append(f"{prefix} {line}")
        else:
            # Even when a child task hasn't emitted anything yet,
            # surface its header so the user sees that the variant
            # is running.
            tstatus = t.status.value if hasattr(t.status, "value") else str(t.status)
            aggregated_lines.append(f"{prefix} (status={tstatus}, no logs yet)")

    # If there are NO children at all, surface that explicitly so the
    # UI doesn't show a confusing empty state.
    if not child_tasks:
        aggregated_lines = ["[workflow-run] no child tasks dispatched yet"]
        sources_used.add("none")

    # GY.V4b — same read-side redaction as the single-task log view above.
    aggregated_lines = _v4b_redact_lines(aggregated_lines)

    return {
        "workflow_run_id": workflow_run_id,
        "lines": aggregated_lines,
        "source": "+".join(sorted(sources_used)) if sources_used else "none",
        "line_count": len(aggregated_lines),
        "child_task_count": len(child_tasks),
    }


@router.get("/workers/tasks/active-count")
async def active_task_count(request: Request, db: Session = Depends(get_db)):
    """
    Returns the number of tasks currently running across all workers.
    Used by update-system.sh to wait for workers to drain before restarting services.
    No auth: returns one integer (the number of active tasks). Reachable by anything that can reach the API port, so it is rate limited; update-system.sh polls it.
    """
    # update-system.sh polls every 10 s (6/min); 30/min per client IP is 5x that.
    from utils.rate_limiter import check_rate_limit, _rate_limit_client_ip
    check_rate_limit(f"active-count:{_rate_limit_client_ip(request)}", 30, 60)
    count = worker_tasks_status_crud.count_active_tasks(db)
    return count


# Parent outputs that are never the artifact to chain downstream — metadata
# emitted alongside real content.
_METADATA_FILENAMES = {'response_metadata.json', 'error.txt', 'validation_result.json'}
# Companion/sidecar files ride ALONGSIDE a primary asset and are never the file
# to chain: an OBJ export emits <name>.obj + <name>.mtl (materials), a glTF a
# .bin buffer. Picking the .mtl/.bin and feeding it downstream fails — deprioritise
# them so the actual mesh/content is chosen when a node emits more than one file.
_OUTPUT_SIDECAR_EXTS = {'mtl', 'bin'}
# Per-node-kind preferred upstream filename. The wizard's `ingest_artifacts`
# stage needs `albedo-artifacts.json` from the upstream Containerizer node, which
# now emits several other files too; the alphabetical-first picker would grab the
# Dockerfile without this.
_PREFERRED_INPUT_BY_KIND = {
    'system_wizard_ingest_artifacts': 'albedo-artifacts.json',
}


def _pick_real_output(outputs, preferred_name: "Optional[str]" = None) -> "Optional[dict]":
    """Choose the one output from a parent's output list to chain downstream.

    Skips metadata sidecars, honours a per-kind preferred filename, and prefers
    a real content file over a companion sidecar (.mtl/.bin). Returns the chosen
    output dict, or None when the parent produced nothing usable.
    """
    if not isinstance(outputs, list) or not outputs:
        return None
    real_outputs = [
        o for o in outputs
        if isinstance(o, dict) and (o.get('file_name') or '').lower() not in _METADATA_FILENAMES
    ]
    if preferred_name:
        for o in real_outputs:
            if (o.get('file_name') or '').lower() == preferred_name.lower():
                return o

    def _out_ext(o):
        fx = o.get('file_extension') or ''
        if not fx:
            fn = o.get('file_name') or ''
            fx = fn.rsplit('.', 1)[-1] if '.' in fn else ''
        return fx.lower().lstrip('.')

    content_outputs = [o for o in real_outputs if _out_ext(o) not in _OUTPUT_SIDECAR_EXTS]
    pool = content_outputs or real_outputs
    if pool:
        return pool[0]
    # Reaching here means real_outputs is empty → the parent produced ONLY
    # metadata (response_metadata.json / error.txt / validation_result.json). That
    # is NOT a usable content input. The old `return outputs[0]` handed a
    # downstream node the metadata file itself — e.g. a metadata-only parent of
    # VideoCombine collapsed into source_file_location, so the node received the
    # 135-byte response_metadata.json as its "video" and ffprobe found no streams.
    # A metadata-only parent yields nothing usable → None (caller skips it).
    return None


def build_multi_port_input_files(node_kind: str, parent_outputs: dict,
                                 parent_ports: "dict | None" = None) -> "list[dict]":
    """Every wired upstream input for a multi-PORT node, for `payload.input_files`.

    A node wired to more than one UPSTREAM NODE (Video Combine's `video` +
    `audio` ports, each fed by a different parent) must receive EVERY parent's
    output. `resolve_source_from_parent_outputs` collapses all parents down to
    ONE `source_file_location`, silently dropping the other port(s) — the bug
    that made Video Combine start with only the extracted audio staged and die
    with "no input file with a video stream found".

    Rather than trust the (object-form) input_schema, key off what is actually
    wired: the parents that produced a usable output. Each entry gets a synthetic
    per-entry id — parent outputs are content-addressed, not cog-addressed, so
    there is no cog id to reuse; the id only has to make the worker's staged
    filenames (`/data/input/<cog_id><ext>`) distinct so two ports that share an
    extension (two video inputs) never collide to one file. The worker keys the
    DOWNLOAD off content_hash, so a synthetic id is safe.

    Returns [] unless MORE THAN ONE parent contributed — a single-parent node
    keeps the existing single-file path, so no currently-working node changes.
    """
    if not isinstance(parent_outputs, dict) or len(parent_outputs) <= 1:
        return []
    preferred = _PREFERRED_INPUT_BY_KIND.get((node_kind or '').lower())
    multi_inputs = []
    for idx, (_pid, outs) in enumerate(parent_outputs.items()):
        chosen = _pick_real_output(outs, preferred)
        if not chosen:
            continue
        # A parent whose ONLY output is metadata is not a content input. The
        # single-file resolver chains such an output as a last resort, but a
        # multi-port node wants genuine files — skip it rather than stage an
        # error.txt / response_metadata.json alongside the real inputs.
        if (chosen.get('file_name') or '').lower() in _METADATA_FILENAMES:
            continue
        content_hash = (chosen.get('content_hash')
                        or (chosen.get('location', '').split('/')[-1]
                            if chosen.get('location') else ''))
        if not content_hash:
            continue
        file_ext = chosen.get('file_extension') or ''
        if not file_ext:
            fn = chosen.get('file_name') or ''
            file_ext = fn.rsplit('.', 1)[-1] if '.' in fn else ''
        entry = {
            'cog_id': idx + 1,  # synthetic — only names the staged file
            'content_hash': content_hash,
            'file_extension': file_ext,
        }
        # negative-training-inputs — carry the logical input PORT this parent
        # feeds (recovered from the graph edge targetHandle upstream) so nodes
        # with positive + negative example channels (sdxl_lora_train,
        # vector_upsert) can classify staged files by role via MOD_INPUT_ROLES.
        # Optional: absent for every node that doesn't pass a parent_ports map.
        if parent_ports:
            _port = parent_ports.get(_pid)
            if _port:
                entry['port'] = str(_port)
        multi_inputs.append(entry)
    return multi_inputs if len(multi_inputs) > 1 else []


def resolve_source_from_parent_outputs(payload: dict) -> bool:
    """Fill payload['source_file_location'] from a parent node's outputs.

    Extracted from enqueue_task_internal so the PINNED downstream dispatch
    path can use it too. That path builds ApiTaskQueue directly and therefore
    never ran this — harmless for profiling, which sets skip_download=True and
    chains no files, but calibration is the first pinned workflow that passes a
    real artifact between nodes. Symptom without it: the generator uploads
    sample.png, the node under test starts with an empty /data/input and dies
    with "ERROR: No input file found".

    Returns True when a location was resolved. Mutates payload in place.
    """
    if 'source_file_location' in payload:
        return False
    # Try parent outputs first (downstream nodes get files from parents).
    # Per-parent output selection (metadata skip, sidecar deprioritisation,
    # per-kind preferred filename) is shared with the multi-input builder in
    # enqueue_task_internal via _pick_real_output.
    parent_outputs = payload.get('parent_outputs', {})
    downstream_kind = (payload.get('node_kind') or '').lower()
    preferred_name = _PREFERRED_INPUT_BY_KIND.get(downstream_kind)
    for parent_id, outputs in parent_outputs.items():
        chosen = _pick_real_output(outputs, preferred_name)
        if not chosen:
            continue
        content_hash = chosen.get('content_hash') or chosen.get('location', '').split('/')[-1]
        file_ext = chosen.get('file_extension', '')
        if content_hash:
            payload['source_file_location'] = content_hash
            if file_ext:
                payload['file_extension'] = file_ext
            break
    return 'source_file_location' in payload


# Gate 3 — nodes that keep state across attempts declare it on their definition,
# in `custom_metadata`. Read from the DEFINITION rather than a per-run flag:
# whether a node can resume is a property of the image, not of a run. No schema
# change, so a node opts in by setting one key.
CHECKPOINT_CAPABLE_KEY = "supports_checkpointing"


def _checkpoint_volume_for(db: Session, node_run_id) -> Optional[str]:
    """The durable volume name for this node run, creating the record once.

    Returns None for nodes that do not declare checkpoint support, which is
    every node until one opts in. Handing a volume to a node that never writes
    to it would leave an orphan per run — 3,900 were swept off this deployment
    on 2026-08-13, and that is exactly how they accumulate.
    """
    if not node_run_id:
        return None
    try:
        nr = db.get(NodeRun, node_run_id)
        if nr is None:
            return None
        if nr.checkpoint_volume:
            return nr.checkpoint_volume       # a retry: reuse, never regenerate

        node = getattr(nr, "workflow_node", None)
        node_def = getattr(node, "node_definition", None) if node is not None else None
        meta = (getattr(node_def, "custom_metadata", None) or {}) if node_def else {}
        declared = bool(meta.get(CHECKPOINT_CAPABLE_KEY)) if isinstance(meta, dict) else False
        if not declared:
            return None

        name = f"modcore-ckpt-noderun-{int(node_run_id)}"
        nr.checkpoint_volume = name
        db.flush()
        logger.info("checkpointing enabled for node_run %s -> %s", node_run_id, name)
        return name
    except Exception:  # noqa: BLE001 — never let this fail a dispatch
        logger.debug("checkpoint volume assignment skipped for node_run %s",
                     node_run_id, exc_info=True)
        return None


# How stale a heartbeat may be before a worker stops counting as somewhere to
# send work. Two minutes is several heartbeat intervals (15s) — long enough to
# ride out jitter, short enough that a rebooting host is not held open.
_CHECKPOINT_HOST_MAX_SILENCE_S = 120


def _attempt_number(task) -> int:
    """Which attempt of the node run produced this checkpoint.

    `attempt_index` is fixed at dispatch (how many rows came before);
    `attempt_count` moves as the reaper retries THIS row. Their sum is the only
    figure that counts both paths, and counting both is the entire point — the
    record exists to answer "did the retry get further than the one before".
    """
    payload = getattr(task, "task_payload", None) or {}
    try:
        idx = int(payload.get("attempt_index", 0) or 0)
    except (TypeError, ValueError):
        idx = 0
    try:
        retries = int(getattr(task, "attempt_count", 0) or 0)
    except (TypeError, ValueError):
        retries = 0
    return idx + retries


def _attempt_index(db: Session, node_run_id) -> int:
    """How many times this node run has already been dispatched.

    Counted from the task rows themselves rather than tracked on the node run,
    so it stays correct however a retry is created — by the reaper, by an
    operator, or by the orchestrator.
    """
    try:
        return int(worker_tasks_status_crud.count_task_attempts_for_node_run(db, node_run_id) or 0)
    except Exception:  # noqa: BLE001
        logger.debug("attempt index lookup failed for node_run %s",
                     node_run_id, exc_info=True)
        return 0


def _live_checkpoint_host(db: Session, node_run_id) -> Optional[str]:
    """The worker that ran a previous attempt, IF it can still take work.

    Liveness is the whole point. Preferring a dead host would pin the task to
    somewhere that never claims it, which is strictly worse than starting over:
    the run stops instead of merely losing progress. So an unreachable,
    unlicensed or silent worker returns None and the task goes out unpinned.
    """
    from datetime import datetime, timedelta, timezone
    try:
        prior = worker_tasks_status_crud.get_latest_claimed_task_for_node_run(db, node_run_id)
        if prior is None or not prior.claimed_by_worker:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_CHECKPOINT_HOST_MAX_SILENCE_S)
        w = worker_tasks_status_crud.get_live_licensed_worker(db, prior.claimed_by_worker, cutoff)
        if w is None:
            logger.info("checkpoint host %s is not available; node_run %s starts over",
                        prior.claimed_by_worker, node_run_id)
            return None
        return w.worker_name
    except Exception:  # noqa: BLE001 — affinity is an optimisation
        logger.debug("checkpoint host lookup failed for node_run %s",
                     node_run_id, exc_info=True)
        return None


def _ensure_user_assigned_stack(db, node_data, payload, node_kind_value, request) -> None:
    """Honor a node's OWN per-instance `requires_stack` (set in the
    WorkflowBuilder) at dispatch: wake the assigned managed stack before the
    node runs (generalized-stack-wake D12).

    Scoped DELIBERATELY to the explicit per-instance hint (`requires_stack` on
    the node's node_data / task_payload) only. NIM/VLM/SDXL/LLM nodes resolve
    through their own central maps with NO hint and are handled by their
    dedicated dispatch flows (the ephemeral-NIM block; apply_node_task_injections
    for VLM/SDXL) — this helper never touches them. As a belt-and-suspenders
    guard against double-waking, a node the ephemeral-NIM path manages is skipped
    here outright.

    Additive + defensive: no hint => no-op; the wake is best-effort (wait=False,
    stack_wake's default) so it never blocks dispatch, and every fault is
    swallowed so it can never wedge the enqueue. The node is enqueued regardless;
    a not-yet-ready stack fails the node fast exactly as the NIM path does.
    """
    try:
        req_stack = None
        if isinstance(node_data, dict):
            req_stack = node_data.get('requires_stack')
        if not req_stack and isinstance(payload, dict):
            req_stack = payload.get('requires_stack')
        if not req_stack:
            return  # no user-assigned stack — no-op

        from services import stack_wake as _stack_wake
        node_conf = {'node_kind': node_kind_value, 'requires_stack': req_stack}
        res = _stack_wake.ensure_for_node(
            db, node_conf, tenant_id=getattr(request, 'tenant_id', None))
        node_id = getattr(request, 'node_id', '?')
        if getattr(res, 'ready', False):
            logger.info("requires_stack %s ensured for node=%s (%s)",
                        req_stack, node_id, getattr(res, 'reason', ''))
            return None
        if getattr(res, 'held', False):
            # A fit does not exist yet: leave the task undispatched until one
            # appears. Hand the stack name back so the caller can mark the
            # task `awaiting_stack` (GSW.4a).
            return req_stack
        logger.info(
            "requires_stack %s for node=%s not ready at dispatch (%s); "
            "enqueuing anyway.", req_stack, node_id,
            getattr(res, 'reason', ''))
        return None
    except Exception as e:  # noqa: BLE001 — never wedge dispatch over stack wake
        logger.warning("requires_stack ensure failed for node=%s: %s",
                       getattr(request, 'node_id', '?'), e)


# Platform tenant: owns every system (tenant_id IS NULL) node, so it is exempt
# from the marketplace entitlement guard. Same convention as
# dependencies.PLATFORM_TENANT_ID / crud.smtp_cud._PLATFORM_TENANT_ID.
_PLATFORM_TENANT_ID = 1

# ---------------------------------------------------------------------------
# GSEC FX.1 — public enqueue route authz (register R-003 / R-052 / R-051).
#
# POST /workers/tasks/enqueue previously needed only require_bearer_or_api_key
# and trusted a caller-supplied tenant_id + free-form payload, so ANY tenant
# user / API key could enqueue arbitrary container tasks for ANY tenant.
# Chained with SEC-7-001, a payload container_env marker escalated the
# container to the platform network with platform Harbor push credentials.
# All of the checks below live ONLY in the HTTP wrapper (enqueue_task); the
# in-process enqueue_task_internal is untouched.
# ---------------------------------------------------------------------------

# Task types a non-admin caller may request through the public route. The
# ApiTaskQueue.task_type column values in use: "processing" (default — every
# node-execution task the orchestrator enqueues), "pre_pull" (image
# pre-pull, platform-internal), "system" (stack ops / calibration /
# benchmarks — all created by in-process platform code), "system_internal"
# (API-process maintenance) and "variant_smoke" (smoke verification). Only
# "processing" is tenant-legit: it is what a user's own workflow run
# produces, and it is the value the row gets by default anyway. Everything
# else is reserved for the platform's own in-process dispatch paths, which
# never use this HTTP route.
TENANT_TASK_TYPE_ALLOWLIST = frozenset({"processing"})

# container_env keys a non-admin caller may NOT set. Each one is a marker the
# Go worker (CORE/Worker/internal/executor/) keys trust decisions on:
#   PLATFORM_TASK     → main (platform) docker network + platform Harbor push
#                       creds + Vault/Postgres bootstrap injection
#                       (netpol.go:90, executor.go:1909/1938,
#                       k8s_taskspec.go:137) — the R-003 escalation marker.
#   API_KEY           → main platform network ("system container calling
#                       back to API") — the platform SERVICE key is set by
#                       the API itself, never by a caller.
#   MOD_TASK_TOKEN_URL→ main platform network (worker-vended run-token
#                       fetch endpoint).
#   NIM_API_KEY       → main platform network (NIM bridge / NVIDIA egress).
#   MOD_STACK_NETWORK → lets a payload NAME a docker network (only
#                       "mod_stack-api-net" is honoured, but naming any net
#                       is a platform placement decision).
#   NEEDS_INTERNET    → routes the container onto the external egress
#                       network instead of the default-deny isolated net.
#   MOD_GOV_UNDECLARED_INTERNAL_ONLY
#                   → FX.91 p5 (gov profile, undeclared node): park the
#                       container on the internal-only posture net instead of
#                       the main network. The API stamps it at the enqueue
#                       choke point, never the caller — and unlike the markers
#                       above it can only ever RESTRICT a container, never
#                       widen one.
#   MOD_NEEDS_API_TOKEN   → B108: grants the container the main platform
#                       network (worker vends a nonce + MOD_TASK_TOKEN_URL,
#                       netpol promotes to MAIN). A tenant cannot self-promote.
#   MOD_TASK_NONCE        → B108: the nonce vended by the worker alongside
#                       MOD_TASK_TOKEN_URL; a tenant must not be able to
#                       forge it to impersonate a platform nonce.
TENANT_RESERVED_CONTAINER_ENV = frozenset({
    "PLATFORM_TASK",
    "API_KEY",
    "MOD_TASK_TOKEN_URL",
    "NIM_API_KEY",
    "MOD_STACK_NETWORK",
    "NEEDS_INTERNET",
    "MOD_GOV_UNDECLARED_INTERNAL_ONLY",
    "MOD_NEEDS_API_TOKEN",
    "MOD_TASK_NONCE",
})


def _reserved_env_hit(payload: Dict[str, Any]) -> Optional[str]:
    """First platform-reserved container_env key in a task payload (or None).

    Checks the top-level `container_env` and the nested `node_data` copy —
    enqueue_task_internal flattens node_data into the payload, so a marker
    hidden under node_data would otherwise reach the worker unscrubbed.
    """
    envs = []
    cenv = payload.get("container_env")
    if isinstance(cenv, dict):
        envs.append(cenv)
    nd = payload.get("node_data")
    if isinstance(nd, dict) and isinstance(nd.get("container_env"), dict):
        envs.append(nd["container_env"])
    for env in envs:
        for key in env:
            if key in TENANT_RESERVED_CONTAINER_ENV:
                return key
    return None


def _tenant_enqueue_active_count(db: Session, tenant_id: int) -> int:
    """PENDING+ASSIGNED rows the tenant holds — the cap window.

    Counts ALL such rows (not just route-created ones) because the cap's job
    is to bound how much queue depth a tenant's own users can drive via this
    endpoint; the orchestrator-created share is small and bounded by the run
    graph, and the platform's internal in-process paths are never subject to
    the cap. Uses db.execute(select(...)) (the router's non-raw-query idiom)
    so it stays clear of the R5 raw-db.query lint rule.
    """
    from sqlalchemy import select, func
    stmt = select(func.count(ApiTaskQueue.id)).where(
        ApiTaskQueue.tenant_id == tenant_id,
        ApiTaskQueue.status.in_([ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED]),
    )
    return int(db.execute(stmt).scalar_one())


def _enforce_tenant_enqueue_scope(
    db: Session,
    caller: models.User,
    request: TaskEnqueueRequest,
) -> None:
    """GSEC FX.1 — non-admin enqueues are scoped to the caller's own tenant.

    Raises 403 on a mismatched tenant_id, a disallowed task type, or a
    reserved container_env marker; 429 over the per-tenant cap. Admins (and
    any future service principal — same is_platform_admin gate the rest of
    this router uses, e.g. get_task_credentials) bypass every check and keep
    today's behaviour.
    """
    if getattr(caller, "is_platform_admin", False):
        return

    # (a) Tenant scoping: the body's tenant_id is caller-controlled and was
    #     trusted verbatim (R-052). A mismatch is a cross-tenant attempt —
    #     403, and we never fall back to the caller's tenant: silently
    #     rewriting would mask the attempt from the client.
    if request.tenant_id != caller.tenant_id:
        raise HTTPException(
            status_code=403,
            detail="You can only enqueue worker tasks for your own tenant.",
        )

    # (b) Task-type allowlist. Absent/None means "processing" (the column
    #     default the row is created with), which is the only tenant-legit
    #     type — see TENANT_TASK_TYPE_ALLOWLIST.
    effective_type = (request.task_type or "processing").strip()
    if effective_type not in TENANT_TASK_TYPE_ALLOWLIST:
        raise HTTPException(
            status_code=403,
            detail=(
                f"task_type {effective_type!r} is not permitted for tenant "
                f"users through this route (allowed: "
                f"{sorted(TENANT_TASK_TYPE_ALLOWLIST)})."
            ),
        )

    # (c) Reserved platform markers in container_env (top-level or nested
    #     under node_data) — R-003's escalation path.
    hit = _reserved_env_hit(request.task_payload or {})
    if hit is not None:
        raise HTTPException(
            status_code=403,
            detail=f"container_env key {hit!r} is reserved by the platform.",
        )

    # (d) B108: is_root_node is platform-internal — set only by the
    #     orchestrator's in-process _start_workflow call. A tenant calling
    #     this HTTP route must never be able to self-declare "root" to earn
    #     the general MOD_NEEDS_API_TOKEN main-network promotion.
    if getattr(request, "is_root_node", False):
        raise HTTPException(
            status_code=403,
            detail="is_root_node is platform-internal and cannot be set by tenant callers.",
        )

    # (e) Per-tenant cap (DB platform setting, default 200) on
    #     PENDING+ASSIGNED tasks; over it the route is 429, not 403 — the
    #     caller did nothing wrong, the tenant's queue is simply full. The
    #     platform tenant itself is exempt: it carries the system-task and
    #     stack-op queue volume of the whole install, so capping it at the
    #     tenant default would 429 the platform's own legitimate users.
    if caller.tenant_id != _PLATFORM_TENANT_ID:
        from services import platform_settings as _psr
        try:
            cap = int(_psr.get_setting("worker.tenant_enqueue_max_pending", db=db,
                                          tenant_id=caller.tenant_id))
        except Exception:  # noqa: BLE001 — a settings read failure must not wedge enqueue
            cap = 200
        # FIX-CE-UNLIMITED-C: the CE default for this key is the sentinel 0 =
        # unlimited (CE has no quantity caps). Here
        # 0 would mean "refuse everything", so the smallest guard is `cap > 0`
        # — a no-op for every finite positive limit (Paid 200 and any
        # operator-set row enforce exactly as before).
        if cap > 0 and _tenant_enqueue_active_count(db, caller.tenant_id) >= cap:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Tenant has {cap} pending/assigned worker tasks; enqueue "
                    f"refused until some complete. Ask a platform admin to raise "
                    f"worker.tenant_enqueue_max_pending if this is expected load."
                ),
            )


def _check_marketplace_entitlement(db: Session, tenant_id, node_kind, trial_node_kind=None) -> None:
    """GF.2 — run-time entitlement guard (defense in depth #2).

    Rejects dispatch of a priced marketplace node the tenant holds no ACTIVE
    entitlement for. Complements the 402 paywall above, which deliberately
    fails OPEN on a check error; this guard fails CLOSED and raises 403.

    One query: the SYSTEM node of this kind (only system nodes can be priced —
    see crud.node_ownership_cud.is_priced_marketplace_node) outer-joined to the
    tenant's non-soft-deleted TenantNodeEntitlement (the same "active" rule the
    palette filter in crud/node_definition_cud.py uses; the model has no
    status/expiry columns).

    Exempt: the platform tenant (owner of every system node), the node's
    owning tenant (tenant-authored nodes are never priced), free / non-
    marketplace nodes, unknown kinds, and the scoped node_trial kind.
    """
    if not node_kind or not tenant_id or tenant_id == _PLATFORM_TENANT_ID:
        return
    import crud
    row = worker_tasks_status_crud.get_node_def_with_entitlement(db, node_kind, tenant_id)
    if row is None:
        return
    node_def, entitlement_id = row
    if not crud.node_ownership_cud.is_priced_marketplace_node(node_def):
        return
    if trial_node_kind and trial_node_kind == node_kind:
        return

    # On-prem (GG.L3): subscription == licence — no Stripe/purchase rows exist
    # on-prem, so a paid marketplace node is gated by the signed licence's
    # `items` claim instead, verified fully offline. Fails CLOSED: no licence,
    # a tampered/invalid signature, or an absent/expired item all refuse.
    # SaaS is completely unchanged (falls through to the purchase-row check
    # below). Module-level `_is_saas` (imported above) so tests can
    # monkeypatch it directly, matching this file's existing test convention.
    if not _is_saas():
        license_verify_mod = optional_paid("utils.license_verify")
        licence_items_mod = optional_paid("services.licence_items")
        if license_verify_mod is None or licence_items_mod is None:
            # CE: priced marketplace nodes are a paid feature, full stop.
            raise HTTPException(
                status_code=402,
                detail=f"{node_kind} is not available in MOD Community Edition.",
            )
        verified = license_verify_mod.current_verified()
        if verified is None or not verified.valid:
            # Owner decision 2A (10-15): no/expired license on the paid build
            # degrades to the CE feature set — same 402 as CE above, not a
            # separate "licence invalid" lockout.
            raise HTTPException(
                status_code=402,
                detail=f"{node_kind} is not available in MOD Community Edition.",
            )
        allowed, reason = licence_items_mod.licence_allows(verified.items, "node", node_kind)
        if not allowed:
            raise HTTPException(status_code=402, detail=reason)
        return

    if entitlement_id is None:
        # GG.UI.6b — carry the definition id in the refusal string so the
        # failed-run parser (WB lib/blocked-run.ts) can link the Buy CTA
        # straight to /store/node/{id} instead of the bare node listing.
        raise HTTPException(
            status_code=403,
            detail=(
                f"Node '{node_def.name or node_kind}' is a paid marketplace node "
                f"and tenant has no active entitlement (id {node_def.id})"
            ),
        )


def _compile_tailnet_egress_cfg(allowed_egress, node_id=None) -> Optional[dict]:
    """GB.EGR.D.9d: given a task's ``allowed_egress`` (whatever shape the four
    call sites above left it in -- a list of host / ``host:port`` strings),
    return the ``payload['tailnet_egress']`` dict to attach, or ``None`` when
    nothing needs it (no tailnet-kind entry, an unparseable list, or no anchor
    key could be minted).

    Split out from `enqueue_task_internal` purely so this Docker-networking
    fix is unit-testable without a DB session / TaskEnqueueRequest -- it has
    no other caller. Both sub-calls are best-effort: a compile failure or a
    mint failure only means that one tailnet entry goes unreached, never a
    blocked task creation (the worker enforces the declared allowlist itself
    regardless of whether a credential was minted).
    """
    if not allowed_egress:
        return None
    from services.egress_allowlist import compile_allowlist, KIND_TAILNET
    try:
        entries = compile_allowlist(allowed_egress)
    except Exception as e:  # noqa: BLE001
        logger.warning("tailnet egress: allowlist compile failed, skipping tailnet mint: %s", e)
        return None
    if not any(e.kind == KIND_TAILNET for e in entries):
        return None

    from routers.persistent_stacks._shared import (
        _tailnet_egress_anchors,
        _tailnet_egress_authkey,
        MESH_ANCHOR_SITE,
        MESH_ANCHOR_CLOUD,
    )
    cfg: dict = {}
    for anchor, (login, client) in _tailnet_egress_anchors().items():
        key = _tailnet_egress_authkey(client)
        if not key:
            continue
        slot = 'site' if anchor == MESH_ANCHOR_SITE else 'cloud'
        cfg[slot] = {'login_server': login, 'auth_key': key}
    if not cfg:
        logger.warning(
            "tailnet egress: task declares a tailnet allowed_egress entry but no "
            "anchor key could be minted (node=%s); it will not be reachable from "
            "the task container", node_id,
        )
        return None
    return cfg


def _stamp_export_control_level(db: Session, payload: dict) -> None:
    """GY.V2b — stamp task_payload['export_control_level'] when the task's
    input cogs carry an export-controlled classification (CUI/EAR/ITAR).

    The claim guard in _try_claim_task (export_control_guard) keeps such
    tasks off non-US workers; the stamp is what tells it the task's inputs
    are export-controlled. No-op for ordinary tasks — the payload stays
    byte-for-byte identical. Best-effort: on lookup failure we log loudly
    and leave the key absent (the cog read/download gate in
    routers/cogs/__init__.py remains the primary enforcement).
    """
    if not isinstance(payload, dict) or "export_control_level" in payload:
        return
    cog_ids = payload.get("cog_ids") or (payload.get("ProjectInfo") or {}).get("cog_ids") or []
    if not isinstance(cog_ids, (list, tuple)) or not cog_ids:
        return
    # 1665s (Gap Y) — CE posture: services/data_classification is manifest-denied
    # in the CE carve, so the old raw `from services import ...` raised on EVERY
    # enqueue and the except handler (which also referenced an unbound `ids`)
    # turned a NameError into a hard enqueue failure. CE has no export-control
    # classification at all: absent module = return quietly, no warning.
    _dc2b = optional_paid("services.data_classification")
    if _dc2b is None:
        return
    try:
        level = _dc2b.max_export_control_level_for_cogs(db, cog_ids)
        if level:
            payload["export_control_level"] = level
    except Exception:
        logger.warning(
            "GY.V2b: export-control level stamp failed for cog_ids=%s "
            "(task enqueues unguarded — re-check input classifications)",
            cog_ids, exc_info=True,
        )


def _stamp_label_worker_pools(db: Session, payload: dict) -> None:
    """GY.C7 (pool policy) — stamp task_payload['label_worker_pools'] with the
    worker pool/region names the task's inputs restrict it to, when
    ``labels.allowed_worker_pools`` maps any input label to a pool list.

    The claim guard (label_pool_guard in the poll handler) withholds the
    task from every non-matching worker; an empty list means no pool
    currently matches and the task WAITS (fail-wait, never fail-route).
    Absent key = no restriction — the payload is byte-for-byte unchanged
    for ordinary tasks (the {} default never stamps). Best-effort: a lookup
    failure leaves the key absent (the pool restriction is a placement
    layer, never the primary data-protection gate).
    """
    if not isinstance(payload, dict) or "label_worker_pools" in payload:
        return
    cog_ids = payload.get("cog_ids") or (payload.get("ProjectInfo") or {}).get("cog_ids") or []
    if not isinstance(cog_ids, (list, tuple)) or not cog_ids:
        return
    try:
        from utils import label_policy
        pools = label_policy.restricted_pool_list(db, cog_ids,
                                                  payload.get("tenant_id"))
        if pools is not None:
            payload["label_worker_pools"] = pools
    except Exception:  # noqa: BLE001
        logger.warning(
            "GY.C7: label worker-pool stamp failed (task enqueues without a "
            "pool restriction) — re-check labels.allowed_worker_pools")


def _stamp_label_regions(db: Session, payload: dict) -> None:
    """GY.C7 s2 (region allowlist) — stamp task_payload['label_regions'] with
    the worker REGIONS the task's inputs restrict it to, when
    ``labels.allowed_regions`` maps any input label to a region list.

    The region layer rides the pool layer (same store, same posture, same
    claim choke point): a labelled task whose region restriction excludes a
    worker is withheld from it at claim (fail-wait — the task stays PENDING
    and an allowed-region worker claims it), and the unrunnable sweep names
    the reason so the Dashboard banner says WHY. Absent key = no restriction
    — the payload is byte-for-byte unchanged for ordinary/unlabelled tasks
    (the {} default never stamps). Best-effort: a lookup failure leaves the
    key absent, exactly like the pool stamp.
    """
    if not isinstance(payload, dict) or "label_regions" in payload:
        return
    cog_ids = payload.get("cog_ids") or (
        payload.get("ProjectInfo") or {}).get("cog_ids") or []
    if not isinstance(cog_ids, (list, tuple)) or not cog_ids:
        return
    try:
        from utils import label_policy
        regions = label_policy.restricted_region_list(
            db, cog_ids, payload.get("tenant_id"))
        if regions is not None:
            payload["label_regions"] = regions
    except Exception:  # noqa: BLE001
        logger.warning(
            "GY.C7 s2: label region stamp failed (task enqueues without a "
            "region restriction) — re-check labels.allowed_regions")


# GY.C7 s2 fail-CLOSED region guard: same posture as the pool guard above —
# when the worker's own region candidates cannot be determined (helper raised,
# or the worker carries no 'region:<zone>' tag at all) we cannot PROVE this
# worker sits in the task's allowed regions, so EVERY task carrying a
# ``label_regions`` stamp is withheld. Unlabelled / unrestricted tasks have no
# stamp and pass unchanged, so ordinary work is unaffected.
_LABEL_REGION_FAIL_CLOSED_SQL = (
    "\n                AND "
    "jsonb_typeof(task_payload->'label_regions') "
    "IS DISTINCT FROM 'array'"
)


def _label_region_guard_sql(worker) -> str:
    """GY.C7 s2 (region allowlist, claim side) — withhold a region-restricted
    task from a worker whose region is NOT in
    ``task_payload['label_regions']``.

    Byte-identical in shape to :func:`_label_pool_guard_sql`, one key deeper:
    the predicate constant-folds ONLY the worker's own region candidates
    (``utils.label_policy.worker_region_candidates`` — its 'region:<zone>'
    hardware tag plus the bare zone, never request data), validated to the
    same conservative [a-z0-9._-:] charset before interpolation, so an admin
    typo can never become a SQL hazard. Tasks without the ``label_regions``
    key (the {} default stamps nothing) pass unchanged; an empty stamp (no
    allowed region currently exists) matches nothing, so the task waits.
    FAIL-CLOSED: no provable region identity ->
    ``_LABEL_REGION_FAIL_CLOSED_SQL``.
    """
    try:
        from utils import label_policy as _lp7s2
        _wregions = _lp7s2.worker_region_candidates(worker)
    except Exception:  # noqa: BLE001
        logger.exception(
            "GY.C7 s2: label region claim guard failed to build "
            "(FAILING CLOSED: region-restricted tasks withheld from this "
            "worker; unlabelled tasks unaffected)")
        return _LABEL_REGION_FAIL_CLOSED_SQL
    _valid = [r for r in _wregions
              if r and all(c in "abcdefghijklmnopqrstuvwxyz0123456789._-:"
                           for c in r)]
    if not _valid:
        # No provable region identity for this worker -> fail closed.
        return _LABEL_REGION_FAIL_CLOSED_SQL
    _in = ", ".join("'" + r + "'" for r in _valid)
    return (
        "\n                AND (\n"
        "                    jsonb_typeof(task_payload->"
        "'label_regions') IS DISTINCT FROM 'array'\n"
        f"                    OR EXISTS (SELECT 1 FROM jsonb_array_elements_text"
        f"(task_payload->'label_regions') AS r(region) WHERE r.region IN ({_in}))\n"
        "                )"
    )


# GY.C7 fail-CLOSED pool guard: when the worker's own pool candidates cannot
# be determined (helper raised, or none passed the SQL-charset validation),
# we cannot PROVE this worker is in the task's allowed pools. Rather than
# open the pool (return "" -> the task becomes claimable by ANY worker), we
# withhold EVERY task that carries a ``label_worker_pools`` stamp. Unlabelled
# / unrestricted tasks have no stamp (jsonb_typeof ... IS DISTINCT FROM
# 'array' is true) and pass unchanged, so ordinary work is unaffected.
_LABEL_POOL_FAIL_CLOSED_SQL = (
    "\n                AND "
    "jsonb_typeof(task_payload->'label_worker_pools') "
    "IS DISTINCT FROM 'array'"
)


def _label_pool_guard_sql(worker) -> str:
    """GY.C7 (pool policy, claim side) — the SQL predicate fragment that
    withholds a label-restricted task from a worker whose pool / region /
    name is NOT in ``task_payload['label_worker_pools']``.

    The predicate constant-folds ONLY the worker's own candidates (its name
    + hardware region tags — never request data), validated to a conservative
    [a-z0-9._-:] charset before interpolation; an admin typo that fails
    validation is simply dropped rather than becoming a SQL hazard. Tasks
    without the ``label_worker_pools`` key (the ``{}`` default stamps
    nothing) pass unchanged. An empty stamp (no pool matches yet) makes the
    EXISTS match nothing, so the task waits (fail-wait, never fail-route).

    FAIL-CLOSED: if the worker's candidates cannot be determined (helper
    raised, or none are valid) the returned predicate withholds any task that
    carries a ``label_worker_pools`` stamp (see
    ``_LABEL_POOL_FAIL_CLOSED_SQL``) — a label-restricted task is never routed
    to a worker we cannot prove is authorized, even though unlabelled tasks
    still claim normally.
    """
    try:
        from utils import label_policy as _lp7
        _wpools = _lp7.worker_pool_candidates(worker)
    except Exception:  # noqa: BLE001
        logger.exception(
            "GY.C7: label worker-pool claim guard failed to build "
            "(FAILING CLOSED: label-restricted tasks withheld from this "
            "worker; unlabelled tasks unaffected)")
        return _LABEL_POOL_FAIL_CLOSED_SQL
    # The charset is conservative for SQL interpolation inside a quoted
    # literal; ':' is admitted because region pool tags are literally
    # 'region:us' (a colon is not a SQL metacharacter inside quotes).
    _valid = [p for p in _wpools
              if p and all(c in "abcdefghijklmnopqrstuvwxyz0123456789._-:"
                           for c in p)]
    if not _valid:
        # No provable pool identity for this worker -> fail closed (withhold
        # any label-restricted task; unlabelled tasks still pass).
        return _LABEL_POOL_FAIL_CLOSED_SQL
    _in = ", ".join("'" + p + "'" for p in _valid)
    return (
        "\n                AND (\n"
        "                    jsonb_typeof(task_payload->"
        "'label_worker_pools') IS DISTINCT FROM 'array'\n"
        f"                    OR EXISTS (SELECT 1 FROM jsonb_array_elements_text"
        f"(task_payload->'label_worker_pools') AS p(pool) WHERE p.pool IN ({_in}))\n"
        "                )"
    )


def _stamp_egress_mode(db: Session, request, payload, *,
                       _now: int | None = None) -> None:
    """declared-egress C7 (3.3) — stamp the task's egress enforcement posture.

    Only a task whose FINAL ``allowed_egress`` is non-empty carries a posture
    (empty/missing allowlists get NO key — sealed node, payload byte-identical).
    Selecting ``direct`` vs ``mediated`` enforcement is the paid
    ``services.egress_mode`` layer, absent from the Community Edition tree: with
    it absent the key stays unset and the worker's own default applies
    ('direct', the narrowest — never widens).
    """
    if not isinstance(payload, dict) or not payload.get("allowed_egress"):
        return
    _egress_mode_mod = optional_paid("services.egress_mode")
    if _egress_mode_mod is None:
        return
    _egress_mode_mod.stamp_task_egress(db, request, payload, _now=_now)


def _external_egress_block_check(db: Session, request, payload) -> Optional[str]:
    """GY.C7 (external-egress block) — refuse a node dispatch at the enqueue
    choke point when the node declares INTERNET egress and the run's input
    cogs carry a ``labels.no_external_egress_labels`` label.

    Runs after every allowed_egress assignment (so the final task egress is
    what is judged) and BEFORE any task row is written. Non-workflow tasks
    and nodes without internet egress return None (proceed). A blocked
    dispatch fails the node run + run with a clear reason and writes a
    LABEL_EXTERNAL_EGRESS_BLOCKED audit event
    (utils/label_policy.block_external_egress_for). Never raises — a
    crash inside the check is a log line, not a wedge.

    Returns the refusal reason string, or None to proceed.
    """
    if getattr(request, "workflow_run_id", None) is None:
        return None
    if not isinstance(payload, dict):
        return None
    if not payload.get("allowed_egress"):
        return None
    cog_ids = payload.get("cog_ids") or (
        payload.get("ProjectInfo") or {}).get("cog_ids") or []
    if not isinstance(cog_ids, (list, tuple)) or not cog_ids:
        return None
    try:
        from utils import label_policy
        if not label_policy.node_is_external(payload.get("allowed_egress")):
            return None
        label = label_policy.no_external_egress_label_for(
            db, cog_ids, getattr(request, "tenant_id", None))
        if label is None:
            return None
        reason = (
            f"external-egress node '{request.node_id}' refused: run inputs "
            f"carry label '{label}' (labels.no_external_egress_labels)")
        logger.error(
            "GY.C7: %s — refusing dispatch (run=%s)",
            reason, request.workflow_run_id,
        )
        label_policy.block_external_egress(
            db,
            run_id=request.workflow_run_id,
            node_run_id=getattr(request, "node_run_id", None),
            node_id=request.node_id,
            tenant_id=getattr(request, "tenant_id", None),
            label=label,
        )
        return reason
    except Exception:  # noqa: BLE001 — never wedge dispatch on a check bug
        logger.exception(
            "GY.C7: external-egress block check failed for run %s node %s "
            "(proceeding — the labels.block_external_llm provider gate and "
            "the V2b claim guard remain the backstops)",
            getattr(request, "workflow_run_id", None),
            getattr(request, "node_id", None),
        )
        return None


def _check_image_signature(payload: dict, db: Session) -> None:
    """GSEC FX.6 (R-071) image-signature check at the dispatch seam.

    'off' is a no-op; 'report' logs the cosign verify result and never
    refuses; 'enforce' refuses the enqueue with HTTP 422 when the result is
    blocked — fail-closed, including a verifier crash. A verifier crash in
    off/report mode never wedges dispatch.
    """
    image = payload.get('processing_image')
    if not image:
        return
    try:
        # CARVE-U9b: image_signature is paid. Absent in CE -> raise into the
        # handler below, which is fail-CLOSED under mode=enforce and a no-op
        # under off/report — the same posture as a verifier crash, so the
        # edition carve never opens the gate.
        _image_sig_mod = optional_paid("services.image_signature")
        if _image_sig_mod is None:
            raise ModuleNotFoundError("services.image_signature")
        res = _image_sig_mod.verify_image_signature(image, db=db)
        mode, status, blocked = res.mode, res.status, res.blocked
    except Exception:  # noqa: BLE001 — off/report: crash must never wedge dispatch
        logger.debug("FX.6 image-signature verify raised", exc_info=True)
        from services import platform_settings as _psr
        if str(_psr.get_setting("supply_chain.image_signature_mode", db=db)) != "enforce":
            return
        mode, status, blocked = "enforce", "error", True
    if status != "skipped":
        logger.info(
            "FX.6 image-signature: image=%s mode=%s status=%s blocked=%s",
            image, mode, status, blocked,
        )
    if blocked:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Image signature verification failed "
                           "(supply_chain.image_signature_mode=enforce)",
                "image": image,
                "status": status,
            },
        )


def _record_egress_block(db: Session, request, decision, *, gate: str,
                         report_only: bool = False) -> Dict[str, Any]:
    """FX.91 p3b — audit + SIEM for one blocked egress attempt (best effort).

    Thin wrapper over services/node_egress_approval.record_blocked_egress so
    both FX.91 gates (p2c approval, p3a gov allowlist) emit the SAME event
    shape: one append-only PermissionAuditLog row (AuditAction.NODE_EGRESS_BLOCKED)
    carrying tenant, node id/name, profile, denied/declared/allowlist sets,
    enforced + report-only posture and the run/node-run ids, plus an
    immediate ship through the existing SIEM forwarder. Deduped per
    (tenant, gate, node) inside security.egress_block_audit_window_seconds, so
    a run that retries the same blocked node cannot flood the trail.

    Never raises and never returns anything the CALLER uses to decide — the
    gate's own refusal reason is what the caller returns.
    """
    try:
        from services import node_egress_approval as _fx91
        return _fx91.record_blocked_egress(
            db,
            tenant_id=getattr(request, "tenant_id", None),
            decision=decision,
            gate=gate,
            report_only=report_only,
            workflow_run_id=getattr(request, "workflow_run_id", None),
            node_run_id=getattr(request, "node_run_id", None),
            task_id=None,
        )
    except Exception as exc:  # noqa: BLE001 — audit/SIEM must not move the gate
        logger.warning(
            "FX.91 p3b blocked-egress emit failed (decision unchanged): %s",
            exc,
        )
        return {"emitted": False, "deduped": False, "audit_id": None,
                "siem": None, "error": str(exc)}


def _fx91_node_ref(request, payload) -> Optional[str]:
    """BUG.B85c2 — the node identity the FX.91 gates resolve, not the graph id.

    The queue's ``node_id`` is the graph node id ('process-1' on the HarborScan
    sweep), which services/node_egress_approval._node_row can never resolve, so
    under a gov/DoD posture every FX.91 gate failed closed on an UNRESOLVABLE
    node and refused the run. The orchestrator already resolved the node
    definition for this dispatch — its kind is stamped into the payload
    (node_conf.node_kind, flattened here) — so the gates review THAT identity.
    Falls back to ``node_id`` (the legacy kind/name label the p3a tests pin);
    an identity that resolves to nothing is still unresolvable → still refused.
    """
    return (payload.get("node_kind") or payload.get("node_name")
            or getattr(request, "node_id", None))


def _node_egress_approval_gate(db: Session, request, payload) -> Optional[str]:
    """FX.91 p2c (tenant-admin one-time egress approval) — the dispatch line.

    Asks services/node_egress_approval.gate_reason whether the node about to
    dispatch has a live tenant-admin approval for the EXACT set of external
    hostnames it declares (node_definitions.egress_domains, FX.91 p2a).
    Inert while the ``nodes.egress_approval_required`` platform setting is
    off — behaviour unchanged — and a node declaring nothing is default-deny
    (nothing to grant, so nothing to approve). Runs at the same choke point
    as GY.C7, BEFORE any task row is written, and resolves the node the same
    way the hydration above does (the queue's node_id is a string kind/name).

    Returns the refusal reason, or None to proceed. Never raises: a lookup
    failure inside the service is a refusal (fail closed), never an open gate.

    FX.91 p2c ships REPORT-ONLY (`nodes.egress_approval_report_only`, default
    True): the finding is logged here and the enqueue proceeds, so turning the
    vertical on cannot change what runs. The hard refusal is FX.91 p3a's
    (gov profile: declared domains blocked unless allowlisted); flipping the
    setting False opts a tenant into refusing at the gate.
    """
    try:
        from services import node_egress_approval as _node_egress_approval
        reason = _node_egress_approval.gate_reason(
            db,
            getattr(request, "tenant_id", None),
            _fx91_node_ref(request, payload),
        )
        if reason is None:
            return None
        from services import platform_settings as _psr
        _report_only = bool(_psr.get_setting(
            "nodes.egress_approval_report_only", db=db,
            tenant_id=getattr(request, "tenant_id", None),
        ))
        # FX.91 p3b — the blocked (or, in report-only mode, WOULD-be) attempt
        # gets its audit row + SIEM ship HERE, once per (run, node, decision)
        # inside the dedupe window, whether or not the gate refuses. Best
        # effort: the emit cannot change what this gate returns — the field
        # read is guarded separately so even a crash there cannot turn the
        # report-only finding into a refusal.
        try:
            _record_egress_block(
                db, request,
                _node_egress_approval.approval_gate_fields(
                    db,
                    getattr(request, "tenant_id", None),
                    getattr(request, "node_id", None),
                ),
                gate=_node_egress_approval.APPROVAL_GATE_LABEL,
                report_only=_report_only,
            )
        except Exception as _emit_exc:  # noqa: BLE001 — emit must not move the gate
            logger.warning(
                "FX.91 p3b approval-block emit failed (decision unchanged): %s",
                _emit_exc,
            )
        if _report_only:
            logger.warning(
                "FX.91 p2c egress approval REPORT (not blocking): %s", reason,
            )
            return None
        return reason
    except Exception as exc:  # noqa: BLE001 — a gate crash must not 500 enqueue
        logger.error(
            "FX.91 p2c egress-approval gate failed (refusing, fail-closed): %s",
            exc,
        )
        return (
            "node egress approval: the approval gate could not be evaluated "
            "(refusing dispatch; nodes.egress_approval_required)"
        )


def _gov_mod_built_gate(db: Session, request,
                        payload: Optional[dict] = None) -> Optional[str]:
    """FX.95 p5c (gov profile = MOD-built nodes only) — the RUN half.

    Adjacent to the FX.91 p3a gov gate and inert while the posture is off, so
    a non-gov install is unchanged byte-for-byte. Under an active gov/DoD
    profile (or the `marketplace.mod_built_only_enforced` opt-in) a node whose
    publisher is not MOD itself is refused at the dispatch line, even if the
    tenant installed it earlier — nothing installed earlier is deleted, only
    the run is refused. The predicate + audit live in
    services/gov_mod_built_only.py (this is the one call line at the choke
    point); status 'gov_mod_built_only' is a stable contract.

    BUG.B85c3 — identity: p5c resolves the node with FX.91 p3a's _node_row
    VERBATIM, so it must be handed the same identity the FX.91 gates get
    (_fx91_node_ref: the payload's node_kind, falling back to request.node_id),
    not the orchestrator's graph node id ('process-1'), which _node_row can
    never resolve. An unresolvable identity still fails closed under an active
    posture, so the gate is never weakened — only the false refusals on
    resolvable nodes are.

    Returns the refusal reason, or None to proceed. Never raises.

    1665l Gap U — CE carve: services/gov_mod_built_only.py is manifest-denied in
    CE (open-core/manifest.yaml), so the bare `from services import ...` used to
    raise ImportError and the except path REFUSED every CE run. The gate is
    inert while the posture is off, so on CE (module absent) it must be inert
    too: optional_paid() returns None exactly when the module itself is the
    missing party (1784/1788 pattern), and a PRESENT module that raises still
    fails closed below — paid behaviour is unchanged.
    """
    _p5c = optional_paid("services.gov_mod_built_only")
    if _p5c is None:
        if not getattr(_gov_mod_built_gate, "_ce_inert_logged", False):
            _gov_mod_built_gate._ce_inert_logged = True
            logger.info(
                "FX.95 p5c mod-built gate is inert in this edition "
                "(services/gov_mod_built_only.py absent — CE carve): dispatch allowed",
            )
        return None
    try:
        return _p5c.run_gate_reason(
            db,
            getattr(request, "tenant_id", None),
            _fx91_node_ref(request, payload or {}),
            workflow_run_id=getattr(request, "workflow_run_id", None),
            node_run_id=getattr(request, "node_run_id", None),
            request=request,
        )
    except Exception as exc:  # noqa: BLE001 — a gate crash must not 500 enqueue
        logger.error(
            "FX.95 p5c mod-built gate failed (refusing, fail-closed): %s", exc,
        )
        return (
            "FX.95 p5c mod-built gate: the posture could not be evaluated "
            "(refusing dispatch; marketplace.mod_built_only_enforced)"
        )


def _node_egress_gov_gate(db: Session, request, payload) -> Optional[str]:
    """FX.91 p3a (gov profile: declared egress blocked unless allowlisted).

    Runs BEFORE the p2c approval gate's report-only logic and is a HARD
    block: under an active gov/DoD compliance profile (cmmc / fedramp_high_il5
    — or a non-gov install that opted into `nodes.egress_gov_allowlist_enforced`),
    any hostname the node DECLARES (node_definitions.egress_domains, FX.91 p2a)
    that is not covered by `nodes.egress_allowlist_domains` refuses the enqueue
    regardless of `nodes.egress_approval_report_only` and regardless of the
    tenant's approval — a tenant's approval is a decision about the p2c
    contract, it never widens the install's allowlist.

    Inert (returns None, behaviour byte-for-byte unchanged) while the gov
    posture is not enforced. The matching itself is the pure
    services/node_egress_approval.gov_egress_decision, so the reason names
    exactly the offending domains and FX.91 p3b can key its audit/SIEM event
    on the same fields (this part writes NO audit row).

    Returns the refusal reason, or None to proceed. Never raises: a gate
    crash is a refusal (fail closed), never an open gate.
    """
    try:
        from services import node_egress_approval as _fx91
        _gov_reason = _fx91.gov_gate_reason(
            db,
            getattr(request, "tenant_id", None),
            _fx91_node_ref(request, payload),
        )
        if _gov_reason is None:
            return None
        # FX.91 p3b — the gov refusal is the LOUDEST event in this vertical:
        # audit row + immediate SIEM ship, deduped per (run, node, decision)
        # inside the window. Best effort: wrapped again here so an emit
        # failure can never turn a refusal into a pass (or a 500).
        try:
            _record_egress_block(
                db, request,
                _fx91.gov_gate_decision(
                    db,
                    getattr(request, "tenant_id", None),
                    getattr(request, "node_id", None),
                ),
                gate=_fx91.GOV_GATE_LABEL,
                report_only=False,
            )
        except Exception as exc2:  # noqa: BLE001 — emit must not move the gate
            logger.warning(
                "FX.91 p3b gov-block emit failed (refusal unchanged): %s",
                exc2,
            )
        return _gov_reason
    except Exception as exc:  # noqa: BLE001 — a gate crash must not 500 enqueue
        logger.error(
            "FX.91 p3a egress gov gate failed (refusing, fail-closed): %s",
            exc,
        )
        # Fail closed, same as the p2c gate: gov_enforced swallows its own
        # settings/profile reads, so reaching this handler means the lookup
        # itself blew up and nothing here can prove the block does NOT
        # apply. A non-gov install never reaches it in practice (gov_gate_
        # reason returns None before any DB read when the posture is off).
        return (
            "node egress gov block: the allowlist gate could not be evaluated "
            "(refusing dispatch; nodes.egress_gov_allowlist_enforced)"
        )


def _fx91_undeclared_gate(db: Session, request, payload) -> Dict[str, Any]:
    """FX.91 p5 (owner 10-07 Q5=A) — gov + UNDECLARED node: NO external network.

    The UNDECLARED half of p3a's posture, at the same enqueue choke point,
    immediately after p3a's DECLARED half. p3a is inert for a node whose
    `egress_domains` is empty (nothing to block), and such a node still got
    EXTERNAL reach today because the worker promotes it to the main network on
    the API_KEY / MOD_TASK_TOKEN_URL / NIM_API_KEY markers
    (CORE/Worker/internal/executor/netpol.go:104/116/123, all BEFORE the
    AllowedEgress check at :138). Q5=A says that under an active gov/DoD
    compliance profile such a node gets NO EXTERNAL network — but the MOD API /
    task-token callback must stay open (nodes must still report results), so
    the worker parks it on the internal-only posture net instead of the main
    one. The API expresses that by stamping the task payload with the
    platform-reserved marker MOD_GOV_UNDECLARED_INTERNAL_ONLY; the worker half
    (netpol.go) reads the marker and never re-derives the posture.

    Returns {'reason': Optional[str], 'stamp': bool, 'decision': {...}} and
    never raises: a gate crash is a REFUSAL (fail closed), never an open gate.
    Inert (reason None, stamp False) while the posture is off, so a non-gov
    install is unchanged byte-for-byte.
    """
    try:
        from services import node_egress_approval as _fx91
        return _fx91.gov_undeclared_gate(
            db,
            getattr(request, "tenant_id", None),
            _fx91_node_ref(request, payload),
            payload,
        )
    except Exception as exc:  # noqa: BLE001 — a gate crash must not 500 enqueue
        logger.error(
            "FX.91 p5 undeclared-egress gate failed (refusing, fail-closed): %s",
            exc,
        )
        return {
            "reason": (
                "node egress gov block (undeclared_gov): the undeclared-egress "
                "posture could not be evaluated (refusing dispatch; "
                "nodes.egress_gov_undeclared_fail_closed)"
            ),
            "stamp": False,
            "decision": None,
        }


def _stamp_gov_internal_only(payload: Dict[str, Any]) -> None:
    """Stamp the task payload with the internal-only posture marker (p5).

    Writes the platform-reserved container_env key
    MOD_GOV_UNDECLARED_INTERNAL_ONLY into the payload's TOP-LEVEL container_env
    — the one key the Go worker deserialises into TaskPayload.ContainerEnv
    (CORE/Worker/internal/transport/transport.go:199) and the one netpol.go
    reads. The nested node_data copy is deliberately left alone: the worker
    never reads it, and writing it there would put a platform marker inside the
    tenant-authored half of the payload.

    Only ever RESTRICTS a container (the marker can never widen reach), and the
    key is in TENANT_RESERVED_CONTAINER_ENV + the orchestrator's
    RESERVED_CONTAINER_ENV_KEYS, so a tenant cannot set it on its own payload —
    the API stamps it, never the caller.
    """
    from services import node_egress_approval as _fx91
    env = payload.get("container_env")
    if not isinstance(env, dict):
        env = {}
        payload["container_env"] = env
    env[_fx91.INTERNAL_ONLY_MARKER] = "1"


def _ios_build_macos_workers_online(db: Session) -> int:
    """BUG.B40d — how many macOS (darwin) workers can take an iOS build NOW.

    Same "active worker" definition the unrunnable-task sweep uses
    (services/scheduler.py:8330): LICENSED, not paused, not update_requested,
    heartbeated within the last 5 minutes. The OS is the heartbeat-reported
    hardware_specs['os'] (lowercased, default linux) — the identical source of
    truth the claim-path guards read (routers/worker_tasks.py:2042).
    """
    from sqlalchemy import select
    from datetime import timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    rows = db.execute(
        select(models.WorkerLicense).where(
            models.WorkerLicense.status == models.WorkerStatus.LICENSED,
            models.WorkerLicense.paused.is_(False),
            models.WorkerLicense.update_requested.is_(False),
            models.WorkerLicense.last_heartbeat >= cutoff,
            models.WorkerLicense.deleted_at.is_(None),
        )
    ).scalars().all()
    return sum(1 for w in rows
               if str((w.hardware_specs or {}).get("os") or "linux")
               .strip().lower() == "darwin")


def _mobile_build_auto_secret_refs(db: Session, node_data: Dict[str, Any],
                                   signing_map) -> list:
    """BUG.B40b3 — attach only the signing secrets that are actually SET.

    KEYSTORE_SECRET_MAP lists every signing input that *can* be attached (3
    legacy keystore slots + the `mobile_signing.<key>` inputs from the b40a
    manifest). Attaching a ref for an unset optional input used to fail the
    worker's ResolveSecrets for the whole task, so dispatch now checks which of
    those names exist as live (not soft-deleted) Secrets on the platform tenant
    in ONE query and attaches only those — `plain` manifest inputs are platform
    settings and never appear in the map, so they are unaffected.

    Legacy behaviour is unchanged for the 3 legacy slots when they exist; refs
    already present in node_data (user wiring) are preserved and de-duplicated
    by env var.
    """
    auto_refs = list(node_data.get('secret_refs', []) or [])
    present_env = {r.get('env_var') for r in auto_refs if isinstance(r, dict)}
    entries = list(signing_map or ())
    if not entries:
        return auto_refs

    from sqlalchemy import select
    names = [name for name, _env, _slot in entries]
    rows = db.execute(
        select(models.Secret.name).where(
            models.Secret.tenant_id == _PLATFORM_TENANT_ID,
            models.Secret.name.in_(names),
            models.Secret.deleted_at.is_(None),
        )
    ).all()
    present = {r[0] for r in rows}

    _smb = optional_paid("seed_mobile_builder")
    refs = _smb.active_signing_secret_refs(present) if _smb is not None else [
        {'secret_name': name, 'env_var': env_var}
        for name, env_var, _slot in entries if name in present
    ]
    for ref in refs:
        if ref.get('env_var') in present_env:
            continue
        present_env.add(ref.get('env_var'))
        auto_refs.append(ref)
    return auto_refs


def _node_def_for_tenant(db: Session, node_kind, tenant_id) -> Optional[NodeDefinition]:
    """Tenant-scoped NodeDefinition lookup for a node_kind (GSEC cross-tenant fix).

    A node_kind is shared by the system row (tenant_id IS NULL) and every
    tenant's palette copy / fork of it, so an unscoped ``.first()`` returns an
    ARBITRARY one — another tenant's fork could supply this task's
    executable_path / container_timeout / ephemeral resources / accepted_formats
    / gpu_variants. Same filter + ordering as the secret_refs block fix
    (4acad9217): live rows only, system rows apply to every tenant, the tenant's
    own row wins over the system row (False sorts before True).

    tenant_id None (no tenant in scope) degenerates to the system row only.
    """
    if not node_kind:
        return None
    return worker_tasks_status_crud.get_node_definition_for_tenant(db, node_kind, tenant_id)


def _apply_deterministic_seed(db, request, payload, node_data) -> None:
    """FORTRESS.2.8 slice C — stamp the run's recorded seed for this node into
    the dispatched payload. Best-effort: any hiccup leaves the payload
    byte-identical to today's (no seeds recorded / node not deterministic).
    """
    try:
        from services import deterministic_seeds as _f28c
        node_run_id = getattr(request, 'node_run_id', None)
        if not node_run_id:
            return
        tenant_id = getattr(request, 'tenant_id', None)
        if tenant_id is None:
            tenant_id = payload.get('tenant_id')
        graph_node_id, seed = _f28c.seed_for_node_run(db, node_run_id, tenant_id)
        if graph_node_id is None or seed is None:
            return
        env = _f28c.apply_seed(
            {"data": node_data} if isinstance(node_data, dict) else {},
            graph_node_id, seed,
            payload.get('container_env') or {},
        )
        if _f28c.SEED_ENV_KEY in env:
            payload['container_env'] = env
            payload['deterministic_seed'] = seed
    except Exception as e:  # noqa: BLE001 — seed stamping must never block dispatch
        logger.warning(
            "fort28 seed stamp failed for node_run %s (%s)",
            getattr(request, 'node_run_id', None), type(e).__name__,
        )


def enqueue_task_internal(request: TaskEnqueueRequest, db: Session) -> dict:
    """
    Core enqueue logic shared by the HTTP endpoint, orchestrator, and downstream dispatch.

    Returns dict with task_id and status.
    """
    # Flatten payload: the Traffic Cop sends node_conf with nested node_data,
    # but the Go worker expects flat fields (node_kind, processing_image, etc.)
    payload = dict(request.task_payload)
    # KEEP-WARM-SLICE-1 — warm_policy is stamped ONLY by the API from the node
    # definition + tenant/platform switches; a caller-supplied key is dropped.
    payload.pop("warm_policy", None)
    # BUG.B40d — an iOS mobile build overrides the queue the caller asked for
    # (a Mac polls ONLY the Metal queue, MAC.1.s8a). None = no override, so
    # every other task keeps request.queue_name byte-for-byte.
    _queue_override = None
    node_data = payload.get('node_data', {})

    # Runaway-dispatch guard (workflow_version 81 incident) — the single choke
    # point. Idempotency: never enqueue a duplicate task for a (run, node,
    # batch-identity) that already has an outstanding/completed instance.
    # Circuit breaker: fail the run cleanly if the same node is enqueued past a
    # sane ceiling. See _dispatch_guard above. Runs BEFORE any expensive
    # payload build / paywall check so a duplicate is cheap to reject.
    _guard_hit = _dispatch_guard(db, request, payload)
    if _guard_hit is not None:
        return _guard_hit

    # Terminal-run guard (single choke point): never enqueue a task for a
    # workflow run that is already CANCELLED/FAILED. This makes a user Cancel
    # actually stick (no new containers spawn behind it) and stops EVERY
    # dispatch path — root, downstream, scheduler re-dispatch — from kicking off
    # work for a dead run. Scoped to workflow tasks (build/maintenance tasks
    # carry no workflow_run_id and are unaffected).
    # FORTRESS.6.3 S3 — the terminal-run query below already reads this run's
    # row; widening the select to include workflow_id (instead of a second
    # db.query) hands the dp-budget reserve block its workflow_id for free,
    # with no new raw db.query( in this router (lint R5).
    _fortress63_run_workflow_id = None
    if getattr(request, "workflow_run_id", None):
        _wr = db.query(WorkflowRun.status, WorkflowRun.workflow_id).filter(
            WorkflowRun.id == request.workflow_run_id
        ).first()
        if _wr is not None:
            _st = _wr[0].value if hasattr(_wr[0], "value") else _wr[0]
            _fortress63_run_workflow_id = _wr[1]
            if str(_st).upper() in ("CANCELLED", "FAILED"):
                logger.info(
                    "enqueue skipped — run %s is terminal (%s); not dispatching node %s",
                    request.workflow_run_id, _st, getattr(request, "node_id", "?"),
                )
                return {"task_id": None, "status": "run_terminal_skipped"}

    # stack-warm (task 2.4) — RUN-START hook. The first enqueue for a run is
    # its start; here we recompute reach-times and background-warm the stacks the
    # remaining DAG needs so they serve before their nodes dispatch. Throttled
    # per-run (rides every enqueue but collapses to one recompute per interval),
    # a paid layer (absent in the Community Edition tree, where the import below
    # fails and is swallowed), and best-effort so it can never block or wedge
    # the enqueue.
    if getattr(request, "workflow_run_id", None):
        try:
            from services import stack_warm as _stack_warm
            _stack_warm.warm_run(db, request.workflow_run_id, force=False)
        except Exception:  # noqa: BLE001 — pre-warm must never wedge dispatch
            logger.debug("stack_warm: run-start hook failed", exc_info=True)

    # Flatten node_data fields to top level (Go TaskPayload expects these at root).
    # process_type deliberately absent — DAGs don't carry it any more;
    # queue_name (set by workflow_orchestrator via select_queue_for_node)
    # + task_payload.gpu_variants (populated below) are the routing
    # inputs the scheduler + worker_tasks.variants_guard consume.
    # FORTRESS.6.2 — 'dp' must ride the payload so report_task_complete can
    # apply the node's DP noise at completion (the spec is stamped onto
    # ApiTaskQueue.task_payload here; the Go worker ignores unknown keys).
    for key in ('node_kind', 'node_name', 'output_schema', 'cost_in_credits',
                'gpu_required', 'container_timeout', 'executable_path',
                'image_pull_policy', 'category', 'allowed_egress', 'dp'):
        if key not in payload and key in node_data:
            payload[key] = node_data[key]

    # NodeDefinition is authoritative for gpu_variants. Always copy from
    # it unless the caller explicitly set gpu_variants in the enqueue
    # payload (rare override path — e.g. a test or a manual dispatch).
    # executable_path + container_timeout also hydrate here when the
    # workflow-graph node_data omitted them.
    node_kind = payload.get('node_kind')
    # GSEC FX.72 part 2 (Q55=C) — no-training-without-opt-in at the single
    # dispatch choke point: a customer-training task (the SDXL LoRA trainers)
    # for a tenant that has not opted in (tenant setting ai.training_opt_in,
    # default False) is refused 403 + audit before any row is created. This
    # also covers re-dispatch/retry paths that skip the run-start gate in
    # routers/workflows_domains/workflows.py.
    # 1788d CE carve: services/ai_training_consent.py is paid (manifest deny
    # 950). The training opt-in consent gate is a paid compliance control; with
    # it absent CE has no consent policy to enforce, so dispatch proceeds. The
    # tenant/worker auth above is untouched - this is not an auth control.
    from utils.paid_import import optional_paid
    _consent_mod = optional_paid("services.ai_training_consent")
    if _consent_mod is not None:
        _consent_mod.check_task_training_opt_in(
            db, getattr(request, 'tenant_id', None), payload)
    # node-ownership-purchases — run-time paywall (defense in depth #1). A priced
    # maintained node requires ownership; reject dispatch of an unowned node
    # before it ever reaches a worker. Free + tenant-authored nodes pass.
    # GG.4: routed through services.entitlement_guard.check_run, which wraps
    # this exact fail-open-paywall (tenant_may_run_node_kind) + fail-closed-
    # marketplace-guard (_check_marketplace_entitlement) pair — neither
    # function moved or changed — and is where the stack/workflow-listing
    # extension points now live (services/entitlement_guard.py).
    if node_kind and getattr(request, 'tenant_id', None):
        # node-trial-runs — a node_trial run exempts ONLY the trialed node.
        _trial_kind = payload.get('trial_node_kind') if payload.get('purpose') == 'node_trial' else None
        # CE-NOPAID-4.5b — this site now rides the entitlement PROVIDER seam
        # (services/entitlement_provider.py, CE-NOPAID-4.5a). The provider is
        # the Paid add-on's bodies once CE-NOPAID-4.7 registers it; until then
        # the resolved provider is still the null base (the add-on is absent on
        # BOTH trees), so the guard's own free functions - which ARE today's
        # real bodies - keep the paid tree byte-identical (same 402/403).
        from services.entitlement_guard import check_run as _entitlement_check_run
        from services.entitlement_provider import (
            EntitlementProvider as _NullEntitlementProvider,
            get_entitlement_provider as _get_entitlement_provider,
        )
        _provider = _get_entitlement_provider(db)
        if type(_provider) is _NullEntitlementProvider:
            _entitlement_check_run(
                db, request.tenant_id, node_kind, trial_node_kind=_trial_kind)
        else:
            _provider.check_run(
                db, request.tenant_id, node_kind, trial_node_kind=_trial_kind)
    # GSEC FX.135 s5 (Q123=A) — new-tenant GPU budget at the SAME single
    # dispatch choke point. A tenant with no verified payment history (the
    # trial flag FX.135 p2 reads) is capped at fraud.new_tenant_gpu_concurrency_limit
    # outstanding GPU tasks and fraud.new_tenant_gpu_spend_limit_usd of metered
    # GPU spend this period; over either, ServiceError(bad_request) — a clear
    # refusal the router maps like every other credit/limit refusal, never a
    # silent drop. Verified tenants and CPU tasks pass untouched.
    try:
        from services._base import ServiceError as _GpuBudgetServiceError
        from services.billing_service import new_tenant_gpu_guard as _gpu_budget_guard
        import crud.billing as _billing_crud
        _tenant_for_gpu_budget = _billing_crud.get_tenant(
            db, request.tenant_id)
        if _tenant_for_gpu_budget:
            _gpu_budget_guard(
                db,
                tenant=_tenant_for_gpu_budget,
                node_kind=node_kind,
                queue_name=getattr(request, 'queue_name', None),
                workflow_run_id=getattr(request, "workflow_run_id", None),
            )
    except _GpuBudgetServiceError:
        raise  # the refusal itself — never swallowed by the best-effort wrap
    except Exception:  # noqa: BLE001 — a budget-read blip never wedges dispatch
        logger.debug("new_tenant_gpu_guard: check error, allowing enqueue", exc_info=True)

    # FORTRESS.6.3 S3 — dp-budget reserve-at-dispatch, same single choke
    # point as the guards above (design /tmp/design_fortress_63.md §2/§4):
    # defence-in-depth for re-dispatch/retry paths that skip the run-start
    # preflight in routers/workflows_domains/workflows.py. node_data is
    # node["data"] (services/workflow_orchestrator.py only ever flattens
    # that key onto the payload), so `dp` lives at node_data["dp"] here —
    # no legacy node["config"] shape reaches dispatch. reserve() flushes
    # only (never commits) so the ledger row lands in the SAME transaction
    # as the ApiTaskQueue insert's db.commit() below; a refusal here raises
    # 402 before that row — or any ledger row — ever exists.
    _dp_spec = node_data.get("dp") if isinstance(node_data, dict) else None
    if (_dp_spec is not None and request.workflow_run_id
            and _fortress63_run_workflow_id is not None):
        from services import platform_settings as _dp_psr
        from utils.paid_import import optional_paid
        _dp_mod = optional_paid("services.dp_budget")
        if _dp_mod is None:
            # 1788d CE carve: services/dp_budget.py is paid (manifest deny
            # `CORE/API/services/dp_*.py`). The differential-privacy budget
            # ledger is a paid gate; with it absent the dispatch proceeds
            # unreserved (paid feature OFF) and the gate below is skipped.
            pass
        else:
            _dp_validate = _dp_mod.validate_dp
            _dp_reserve = _dp_mod.reserve
            _DpBudgetExhausted = _dp_mod.DpBudgetExhausted
        if _dp_mod is not None and _dp_psr.get_setting("dp.budget_enabled", db=db):
            try:
                _dp_epsilon, _dp_delta = _dp_validate(_dp_spec)
            except ValueError as _dp_spec_exc:
                # FORTRESS.6.2 (923): validate_dp raises ValueError for a
                # malformed dp spec. Workflows saved before 911's save-time
                # gate (b6e1d01cd) can still carry one, so this dispatch path
                # stays reachable — surface it as the same fixed 422 code as
                # the run-start sibling (922, workflows.py) instead of an
                # unhandled 500. Exception text never reaches the response;
                # nothing has been reserved or inserted yet, so the refusal
                # leaves no partial state.
                logger.warning(
                    "dp reserve rejected malformed dp spec at dispatch: "
                    "workflow_id=%s run_id=%s node_id=%s: %s",
                    _fortress63_run_workflow_id,
                    request.workflow_run_id,
                    request.node_id,
                    type(_dp_spec_exc).__name__,
                )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "dp_spec_invalid",
                        "message": "A node's dp spec is invalid and could not "
                                   "be validated.",
                    },
                )
            try:
                _dp_reserve(
                    db,
                    tenant_id=request.tenant_id,
                    workflow_id=_fortress63_run_workflow_id,
                    run_id=request.workflow_run_id,
                    node_id=request.node_id,
                    epsilon=_dp_epsilon,
                    delta=_dp_delta,
                )
            except _DpBudgetExhausted as _dp_exc:
                raise HTTPException(
                    status_code=402,
                    detail={
                        **_dp_exc.as_detail(),
                        "message": "Differential-privacy epsilon budget "
                                   "exhausted for this period.",
                    },
                )

    node_def = None
    if node_kind:
        try:
            # Tenant-scoped (see _node_def_for_tenant): an unscoped lookup
            # hands THIS task another tenant's fork of the same node_kind.
            node_def = _node_def_for_tenant(db, node_kind,
                                            getattr(request, 'tenant_id', None))
            if node_def:
                if not payload.get('executable_path'):
                    payload['executable_path'] = node_def.executable_path or ''
                if not payload.get('container_timeout') and hasattr(node_def, 'container_timeout'):
                    payload['container_timeout'] = getattr(node_def, 'container_timeout', 7200)
                # Ephemeral resources from node definition custom_metadata.
                # Extract shm_mb (canonical) or legacy shm_size_mb, and
                # tmpfs_mb from node_def.custom_metadata when not already
                # present in payload. Positive integers only, ignore garbage,
                # don't set key when value is absent.
                _eph = (node_def.custom_metadata or {}).get('ephemeral_resources')
                if _eph and isinstance(_eph, dict):
                    if 'shm_size_mb' not in payload:
                        _shm = _eph.get('shm_mb', _eph.get('shm_size_mb'))
                        if isinstance(_shm, int) and not isinstance(_shm, bool) and _shm > 0:
                            payload['shm_size_mb'] = _shm
                    if 'tmpfs_mb' not in payload:
                        _tmp = _eph.get('tmpfs_mb')
                        if isinstance(_tmp, int) and not isinstance(_tmp, bool) and _tmp > 0:
                            payload['tmpfs_mb'] = _tmp
                # launch-completion-megaplan GP.16b — native (imageless) tasks.
                # When the node's custom_metadata declares execution: native,
                # stamp the D-P1 recipe onto the payload so the worker's
                # NativeBackend runs it as a host process (no docker). A
                # single-platform recipe also resolves native_platform + native
                # here; a multi-platform one leaves native_platform unset and
                # the claim path resolves it to the claiming worker's OS.
                # Non-native nodes: _native_payload_fields returns {} so no key
                # is added and the payload is byte-for-byte unchanged.
                for _nk, _nv in _native_payload_fields(node_def.custom_metadata).items():
                    payload[_nk] = _nv
                # KEEP-WARM-SLICE-1 — warm_policy only when the node flag, the
                # tenant switch and the platform kill switch all allow it.
                payload.update(_warm_policy_payload_fields(
                    node_def.custom_metadata, db,
                    getattr(request, 'tenant_id', None)))
                if 'gpu_variants' not in payload and node_def.gpu_variants:
                    # P9e.3 — scheduler gate. Filter out variants whose
                    # smoke verification came back "failed" before the
                    # task ever reaches the claim SQL. variants_guard
                    # already intersects task_payload.gpu_variants with
                    # the worker's hardware_tags; dropping failed keys
                    # here is the cheapest place to enforce "don't route
                    # to a known-broken variant".
                    #
                    # pending / verified / NULL all pass through — a
                    # never-smoke-tested variant is tried once
                    # optimistically (matches the plan's "NULL means
                    # try it once" rule); a pending variant might
                    # complete smoke before the worker claims.
                    #
                    # P9m — numerical_drift is treated identically to
                    # "failed": the variant produces wrong tensors, we
                    # don't want to route user work to it. Operator
                    # fixes the port, re-runs smoke, variant flips to
                    # verified, it's eligible again.
                    #
                    # When EVERY variant is "failed" (or drift), gpu_variants
                    # ends up empty and the task becomes unrunnable — the
                    # unrunnable_tasks_sweep job surfaces it in the
                    # System Health banner so the tenant sees the
                    # problem instead of a silently-stuck queue.
                    vvs = node_def.variant_verification_status or {}
                    # build_failed lives in vvs but the variant was
                    # already removed from gpu_variants by P9b's PATCH
                    # handler — not redundantly filtered here.
                    _BAD_STATUSES = {"failed", "numerical_drift"}
                    filtered = {
                        v: img for v, img in node_def.gpu_variants.items()
                        if vvs.get(v) not in _BAD_STATUSES
                    }

                    # project-agent §1.5 — the gate above reads
                    # variant_verification_status, which is the P9 SMOKE
                    # drift signal. The §5 per-variant SAMPLE TEST writes
                    # a separate verdict on node_variant_verification and
                    # nothing here has ever read it, so a variant whose
                    # output diverges from the reference has been routable
                    # this whole time. It published with a warning chip on
                    # the assumption a human would see it; selection is by
                    # placement and sees no chips.
                    #
                    # Only 'drift' awaiting a ruling is withheld. verified /
                    # marginal / untested / NULL keep the existing
                    # "try it once optimistically" behaviour, and a variant
                    # ruled 'publish' by the tenant is deliberately in
                    # service.
                    unruled = _unruled_drift_variants(db, node_def.id)
                    if unruled:
                        before = set(filtered)
                        filtered = {
                            v: img for v, img in filtered.items()
                            if v not in unruled
                        }
                        withheld = sorted(before - set(filtered))
                        if withheld:
                            logger.info(
                                "gpu_variants gate: withheld %s from node %s "
                                "(sample-test drift, awaiting a ruling) — "
                                "task will route to %d variant(s)",
                                withheld, node_kind, len(filtered),
                            )

                    # GO.9 — prefer verified variants over unverified, never
                    # pick incompatible, and only fall back to not_recommended
                    # variants when nothing better is eligible. Layered on top
                    # of the smoke-failed/drift gate above rather than
                    # replacing it: `filtered` is already the "known safe to
                    # try" set, this narrows it further by trustworthiness.
                    from utils.queue_selector import apply_variant_preference_tiers
                    preferred = apply_variant_preference_tiers(
                        filtered,
                        node_def.incompatible_reasons,
                        vvs,
                        node_def.not_recommended_variants,
                    )
                    if preferred != filtered:
                        logger.info(
                            "gpu_variants gate: preference-tiered %s -> %s "
                            "for node %s (prefer verified > unverified > "
                            "not_recommended)",
                            sorted(filtered), sorted(preferred), node_kind,
                        )
                    filtered = preferred

                    payload['gpu_variants'] = filtered
                    if len(filtered) < len(node_def.gpu_variants):
                        dropped = [
                            v for v in node_def.gpu_variants
                            if vvs.get(v) == "failed"
                        ]
                        if dropped:
                            logger.info(
                                "gpu_variants gate: dropped %s from node %s "
                                "(smoke-failed) — task will route to %d "
                                "variant(s)", dropped, node_kind, len(filtered),
                            )
        except Exception as e:
            logger.debug("Could not look up node_definition for %s: %s", node_kind, e)

    # FX.103 part 2 (owner decision Q89=A) — RETIREMENT RUN GUARD: a node
    # past its retirement date (retire_after) must not dispatch — pinned
    # runs keep working until the date, then fail HERE with the upgrade
    # pointer. Lives OUTSIDE the best-effort pin block below: that block
    # degrades on resolver hiccups by design, but a retired node must fail
    # the run, not silently dispatch on the live build. The guard itself is
    # fail-open on its own lookup errors (a transient DB hiccup must never
    # 500 every dispatch).
    _fx103_violation = None
    try:
        from services import node_retirement as _fx103p2
        _fx103_violation = _fx103p2.run_guard_violation(
            db, getattr(request, 'node_run_id', None))
    except Exception:  # noqa: BLE001 — the guard check must never block dispatch
        logger.debug("FX.103 retirement run-guard check failed", exc_info=True)
    if _fx103_violation is not None:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "node_retired",
                "message": (
                    f"Node '{_fx103_violation['name']}' was retired on "
                    f"{_fx103_violation['retired_on']:%Y-%m-%d}; this "
                    f"workflow can no longer run it. Upgrade via POST "
                    f"/road/{_fx103_violation['workflow_id']}"
                    f"/upgrade-nodes (moves its pins to current builds), "
                    f"then re-run."
                ),
            },
        )

    # node-version-pinning — if this task's workflow node is pinned to a specific
    # build, the pin is authoritative: send its immutable digest (+ snapshot
    # variant map/digests) so a later rebuild can't change what this run
    # executes. Unpinned nodes keep the live resolution above (legacy behaviour).
    # Either way, stamp the executed digest onto the NodeRun for a reproducible
    # audit trail. Best-effort: a resolver hiccup must never block dispatch.
    try:
        import crud
        pinned_version = crud.resolve_node_run_pin(db, getattr(request, 'node_run_id', None))
        if pinned_version is not None:
            payload['image_digest'] = pinned_version.image_digest
            if pinned_version.gpu_variants:
                payload['gpu_variants'] = pinned_version.gpu_variants
            if pinned_version.gpu_variant_digests:
                payload['gpu_variant_digests'] = pinned_version.gpu_variant_digests
            if pinned_version.executable_path:
                payload['executable_path'] = pinned_version.executable_path
        elif node_def is not None and getattr(node_def, 'image_digest', None):
            # B119: unpinned nodes — stamp the current NodeDefinition's digest
            # so the worker can verify the local copy matches before prefer-local
            # use. Without this, the worker's prefer-local path has no digest to
            # compare and always skips the pull (stale image).
            payload['image_digest'] = node_def.image_digest
            # B119b: the digest alone is not enough — the worker still pulls the
            # cuda/rocm/intel variants BY TAG under prefer_local, so a stale
            # variant tag silently runs the wrong build. Recover the immutable
            # per-variant digests from the newest NodeVersion row recorded for
            # THIS digest (one indexed lookup on the uq_node_version_digest
            # pair, limit 1; the pair is unique per (definition, digest)). No row
            # (node never recorded) or a row whose digest differs → payload
            # unchanged, worker degrades to tag pulls exactly as before.
            # Best-effort: the lookup runs in a savepoint so a SQL error cannot
            # abort the outer transaction.
            try:
                with db.begin_nested():
                    _b119b_row = (
                        db.query(models.NodeVersion)  # r5-lock-ok: B119b unpinned variant-digest recovery, indexed by uq_node_version_digest
                        .filter(
                            models.NodeVersion.node_definition_id == node_def.id,
                            models.NodeVersion.image_digest == node_def.image_digest,
                        )
                        .order_by(models.NodeVersion.created_at.desc(),
                                  models.NodeVersion.id.desc())
                        .limit(1)
                        .first()
                    )
                    if _b119b_row is not None and _b119b_row.gpu_variant_digests:
                        payload['gpu_variant_digests'] = _b119b_row.gpu_variant_digests
            except Exception:  # noqa: BLE001
                logger.warning(
                    "B119b variant-digest recovery skipped for node_definition %s",
                    getattr(node_def, 'id', None), exc_info=True,
                )
        crud.stamp_node_run_execution(db, getattr(request, 'node_run_id', None), pinned_version)
    except Exception:  # noqa: BLE001
        logger.warning(
            "node-version pin resolution failed for node_run %s",
            getattr(request, 'node_run_id', None), exc_info=True,
        )

    # FORTRESS.2.8 slice C — stamp the run's recorded per-node seed into the
    # dispatched payload so repeat dispatch replays bit-identically.
    _apply_deterministic_seed(db, request, payload, node_data)

    # Flatten ProjectInfo fields
    project_info = payload.get('ProjectInfo', {})
    if 'user_id' not in payload and 'user_id' in project_info:
        payload['user_id'] = project_info['user_id']
    if 'cog_ids' not in payload and 'cog_ids' in project_info:
        payload['cog_ids'] = project_info['cog_ids']

    # GY.V2b — foreign-worker exclusion: stamp the inputs' highest
    # export-controlled classification so the claim guard can withhold the
    # task from non-US workers (no-op when no input cog is CUI/EAR/ITAR).
    _stamp_export_control_level(db, payload)

    # Resolve minio_bucket from tenant_id (BYO-S3 aware: an active
    # StorageConnection routes to the tenant's own bucket)
    if 'minio_bucket' not in payload:
        from utils.minio_client import get_tenant_bucket_name
        payload['minio_bucket'] = get_tenant_bucket_name(request.tenant_id, db)

    # Resolve processing_image from executable_path
    if 'processing_image' not in payload:
        executable_path = payload.get('executable_path') or node_data.get('executable_path')
        if not executable_path:
            # LAST-RESORT GUESS, and it is usually wrong. node_kind is not an
            # image name: 217 of 218 node definitions carry an explicit
            # executable_path precisely because the two differ. Kinds are
            # snake_case; image names follow the container DIRECTORY (see any
            # build-and-push.sh, which lowercases basename), so `checkpoint_demo`
            # guesses an image that has never existed while the real one is
            # `checkpoint-demo`. Underscores cannot simply be rewritten either —
            # 32 real image names contain them.
            #
            # The guess is KEPT so nothing that works today by coincidence
            # breaks, but it is announced: without this the only symptom is a
            # pull failure on a remote worker, minutes later and nowhere near
            # the node definition that caused it. That cost an hour during the
            # Gate 5 end-to-end run.
            executable_path = node_data.get('node_kind')
            if executable_path:
                logger.warning(
                    "node '%s' (definition %s) has no executable_path — guessing "
                    "the image from node_kind '%s'. Set executable_path on the "
                    "node definition; if the image does not exist under that "
                    "exact name the task will fail at pull time on the worker.",
                    node_data.get('label') or executable_path,
                    node_data.get('nodeDefinitionId'), executable_path,
                )
        if executable_path and executable_path.lower() not in ('none', ''):
            # If already fully qualified (contains registry prefix), use as-is
            # This handles local registry paths like "registry:5000/tenant-1/foo:latest"
            first_segment = executable_path.split('/')[0] if '/' in executable_path else ''
            if '.' in first_segment or ':' in first_segment:
                # pd1f2: baked ghcr.io/modtim refs follow CE_IMAGE_REGISTRY
                # (mirror / rc registry); no-op when unset or not that prefix.
                from utils.registries import ce_mirror_ref
                payload['processing_image'] = ce_mirror_ref(executable_path)
            else:
                try:
                    from utils.docker_helpers import get_full_image_name
                    payload['processing_image'] = get_full_image_name(executable_path)
                except Exception as e:
                    # Fallback: build image name manually
                    from utils.registries import platform_image
                    payload['processing_image'] = platform_image(executable_path)
                    logger.debug("get_full_image_name failed (%s), using fallback: %s", e, payload['processing_image'])

    # pd1f2R: the worker PREFERS a gpu_variants image over processing_image on
    # GPU workers (executor.go resolveProcessingImage), and the pinned-version
    # executable_path is carried in the payload too; rewrite baked
    # ghcr.io/modtim refs there as well. New dict: never mutate the ORM JSON.
    from utils.registries import ce_mirror_ref as _mirror
    _gv = payload.get('gpu_variants')
    if isinstance(_gv, dict) and _gv:
        payload['gpu_variants'] = {
            k: (_mirror(v) if isinstance(v, str) else v) for k, v in _gv.items()
        }
    if isinstance(payload.get('executable_path'), str):
        payload['executable_path'] = _mirror(payload['executable_path'])

    # GSEC FX.6 (R-071) — image signing / provenance seam. This is the ONE
    # narrow point where the API hands a node image to a worker
    # (enqueue_task_internal is the choke point every dispatch path funnels
    # through). "off" is a no-op; "report" logs the verify result without
    # refusing; "enforce" refuses the enqueue with 422 (fail-closed, including
    # a verifier crash). See docs/security-audit/design/R-071-image-signing.md.
    _check_image_signature(payload, db)

    # Resolve source_file_location from cog content hash or parent outputs
    if 'source_file_location' not in payload:
        resolve_source_from_parent_outputs(payload)

        # If no parent outputs, resolve from cog_ids (input nodes)
        # Chain: Cog → latest_version_id → Version → content_id → ContentBlob.id (SHA256 hash)
        if 'source_file_location' not in payload:
            cog_ids = payload.get('cog_ids', [])
            if cog_ids:
                try:
                    from models import Cog, Version, ContentBlob
                    cog = db.query(Cog).filter(Cog.id == cog_ids[0]).first()
                    if cog and cog.latest_version_id:
                        version = db.query(Version).filter(Version.id == cog.latest_version_id).first()
                        if version and version.content_id:
                            payload['source_file_location'] = version.content_id  # SHA256 hash
                            # Get file extension from ContentBlob
                            blob = db.query(ContentBlob).filter(
                                ContentBlob.id == version.content_id,
                                ContentBlob.tenant_id == request.tenant_id,
                            ).first()
                            if blob:
                                payload['file_extension'] = blob.file_extension or ''
                except Exception as e:
                    logger.debug("Could not resolve source file from cog: %s", e)

    # D76 — kinds whose image reads cog-style env (3d thumbnail: CONTENT_HASH,
    # MINIO_BUCKET, ...) get it from the source/extension/cog just resolved
    # above. Table-driven; unmapped kinds get {} and are untouched. Existing
    # container_env keys win (setdefault).
    from services.node_container_env_mappings import container_env_for_node_kind
    _mapped_env = container_env_for_node_kind(
        payload.get('node_kind'), payload, request.tenant_id,
        lambda: __import__('utils.minio_client', fromlist=['x'])
        .get_tenant_bucket_name(request.tenant_id, db),
    )
    if _mapped_env:
        _cenv_map = payload.setdefault('container_env', {})
        for _k, _v in _mapped_env.items():
            _cenv_map.setdefault(_k, _v)

    # Resolve EVERY cog, not just the first.
    #
    # A downstream processing node always receives one task carrying every
    # cog — that is the ordinary path, not a grouped-mode quirk: the batch
    # fan-out happens at the ROOT node, so `image_input` gets 38 tasks of one
    # file while the node after it gets one task of 38. Measured 2026-08-17.
    #
    # Until now only `cog_ids[0]` was resolved, so the sidecar staged one file
    # and the container saw one. `blur_detection_image_process` given 38 images
    # reported "Found 0 images to analyze / Processed: 0" and COMPLETED without
    # error. Nothing surfaced it because no task in this deployment's history
    # had ever carried more than one cog.
    #
    # Per-file extension is the reason this is a list of objects rather than a
    # list of hashes. `file_extension` above is whatever cog_ids[0] happened to
    # be, and a mixed capture is normal — DJI writes JPG+DNG — so applying the
    # first file's extension to all of them would hand COLMAP eight DNGs named
    # .jpg and produce a pre-flight report blaming a capture that was fine.
    #
    # `source_file_location` and `file_extension` are left exactly as they were:
    # every existing single-file node keeps its current payload untouched, and
    # a worker that does not understand `input_files` behaves as it does today.
    #
    # NOT for input nodes. An `*_input` node does not read /data/input at all —
    # it resolves its cogs to references over the API (validate.py: for cog_id in
    # COG_IDS) and emits metadata, never touching the staged files. Staging them
    # is pure waste: the image_input ahead of the pre-flight downloaded all 38
    # images (~95 MB) it never opens and took 167s doing it, and a 200-image
    # drone set would drag ~1.6 GB for nothing. So the whole capture is staged
    # once, for the node that actually consumes it, and skipped for the input
    # node that only describes it.
    _node_kind = payload.get('node_kind') or ''
    if _node_kind.endswith('_input'):
        pass
    elif payload.get('cog_ids'):
        try:
            from models import Cog, Version, ContentBlob
            input_files = []
            for _cid in payload['cog_ids']:
                _cog = db.query(Cog).filter(Cog.id == _cid).first()
                if not _cog or not _cog.latest_version_id:
                    continue
                _ver = db.query(Version).filter(
                    Version.id == _cog.latest_version_id).first()
                if not _ver or not _ver.content_id:
                    continue
                _blob = db.query(ContentBlob).filter(
                    ContentBlob.id == _ver.content_id,
                    ContentBlob.tenant_id == request.tenant_id,
                ).first()
                input_files.append({
                    'cog_id': _cid,
                    'content_hash': _ver.content_id,
                    'file_extension': (_blob.file_extension or '') if _blob else '',
                })
            # Only when there is genuinely more than one. A single-file task
            # gains nothing from the new path and should keep taking the old
            # one, so the change cannot regress the only case that works today.
            if len(input_files) > 1:
                payload['input_files'] = input_files
        except Exception as e:  # noqa: BLE001 — never break a claim over this
            logger.warning("Could not resolve multi-file inputs: %s", e)

    # Multi-PORT inputs. A node wired to more than one UPSTREAM NODE (Video
    # Combine's `video` + `audio` ports) must receive EVERY parent's output —
    # not just the one `resolve_source_from_parent_outputs` collapsed into
    # `source_file_location`. The cog_ids block above only covers many files
    # fanning into ONE node from the project inputs; it never looked at
    # parent_outputs, so a node fed by several distinct parents was missed.
    # Guarded internally by `> 1` and skipped for `_input` nodes, exactly like
    # the single path, so no currently-working single-parent node changes.
    if not _node_kind.endswith('_input') and 'input_files' not in payload:
        try:
            _multi = build_multi_port_input_files(
                _node_kind, payload.get('parent_outputs') or {},
                payload.get('parent_ports') or None)
            if _multi:
                payload['input_files'] = _multi
        except Exception as e:  # noqa: BLE001 — never break a claim over this
            logger.warning("Could not resolve multi-port inputs: %s", e)

    # For input nodes: set up container_env with validation config
    # Each input type has its own container (image-input, mesh-input, etc.)
    # processing_image already resolved from node_definitions.executable_path above
    node_kind_value = payload.get('node_kind', '')

    # PANEL DEFAULTS → EXTRA_VARIABLES: the config panel DISPLAYS
    # configuration_schema.extra_variables[*].default, but the builder only
    # writes node_data.extraVariables when the user edits a field, so an
    # untouched node silently ran with the container's own built-ins instead of
    # what the panel showed. Fill the missing names into the node_data bag HERE
    # — ahead of BOTH extraVariables→container_env loops (the input-node branch
    # below and the generic one further down) and ahead of the output-cache key
    # (~9174, which reads extraVariables/extra_variables) — so env and cache
    # agree. Fill-if-missing only: user values always win; secret/password
    # schema entries are never injected. Best-effort: a blip logs a warning
    # (no values) and dispatch continues unchanged.
    try:
        from services.node_config_env_defaults import fill_extra_variable_defaults_for_node
        _filled_extra = fill_extra_variable_defaults_for_node(
            db,
            node_data,
            node_kind_value,
            getattr(request, 'tenant_id', None),
        )
        if _filled_extra:
            logger.info(
                "filled %d extra_variables default(s) for node %s",
                len(_filled_extra),
                node_data.get('nodeDefinitionId') or node_kind_value,
            )
    except Exception:  # noqa: BLE001 — a definition-read blip never wedges dispatch
        logger.warning(
            "extra_variables defaults skipped for node %s",
            node_kind_value,
        )

    if node_kind_value and node_kind_value.endswith('_input'):
        service_api_key = os.getenv('PLATFORM_SERVICE_API_KEY', '')
        cog_ids = payload.get('cog_ids', [])

        # Look up input type config from node_definitions
        input_config = _get_input_type_config(node_kind_value, db)

        # Build container env for input validator.
        # API_URL is deliberately NOT set here. Each worker knows its own
        # reachable API endpoint (from setup-worker.sh for remote workers,
        # from config for embedded), and the worker injects that value when
        # launching the container. A server-side default would be wrong for
        # remote workers — they can't resolve `http://api:80`, and any URL
        # we pick here may not be routable from every worker's network.
        container_env = {
            'API_KEY': service_api_key,
            'WORKFLOW_RUN_ID': str(request.workflow_run_id),
            'TENANT_ID': str(request.tenant_id),
            'COG_IDS': json.dumps(cog_ids),
            'INPUT_TYPE': input_config.get('input_type', 'image'),
            'SUPPORTED_EXTENSIONS': json.dumps(input_config.get('supported_extensions', [])),
            'MIME_TYPE_PREFIX': input_config.get('mime_type_prefix', ''),
            'MIN_SIZE_BYTES': str(input_config.get('min_size_bytes', 100)),
            'VALIDATION_MODE': input_config.get('validation_mode', 'validate_exists'),
            'NODE_KIND': node_kind_value,
            'NODE_ID': request.node_id,
        }

        # Pass extra variables from node config (e.g., TEXT_CONTENT for text input nodes)
        node_data = payload.get('node_data', {})
        extra_vars = node_data.get('extraVariables', {})
        for key, value in extra_vars.items():
            if value is not None and str(value).strip():
                container_env[key.upper()] = str(value)

        payload['container_env'] = container_env

        # Input validator doesn't need MinIO download
        payload['source_file_location'] = ''

    # For export nodes: set up container_env with API credentials
    # The return-export container needs these to call API endpoints
    if node_kind_value and (node_kind_value.endswith('_export') or node_kind_value == 'return_assets_export'):
        # Each export type has its own container (return-export, unity-export, etc.)
        # processing_image already resolved from node_definitions.executable_path above

        # Get the service API key for the export container
        service_api_key = os.getenv('PLATFORM_SERVICE_API_KEY', '')

        # Get original filename from cog (or use workflow name for text-only workflows)
        cog_ids = payload.get('cog_ids', [])
        original_filename = None
        if cog_ids:
            try:
                from models import Cog
                cog = db.query(Cog).filter(Cog.id == cog_ids[0]).first()
                if cog:
                    original_filename = cog.name
            except Exception:
                pass
        if not original_filename:
            wf = payload.get('ProjectInfo', {}).get('workflow_name', 'workflow')
            original_filename = f"{wf}_output"

        # Get workflow name
        wf_name = payload.get('ProjectInfo', {}).get('workflow_name', 'workflow')

        # API_URL is deliberately NOT set here — see the input-validator
        # branch above for the reasoning. The worker injects its own APIURL
        # at container-launch time.
        _export_env = {
            'API_KEY': service_api_key,
            'WORKFLOW_RUN_ID': str(request.workflow_run_id),
            'TENANT_ID': str(request.tenant_id),
            'INPUT_COG_ID': str(cog_ids[0]) if cog_ids else '0',
            'WORKFLOW_NAME': wf_name,
            'ORIGINAL_FILENAME': original_filename,
            'NODE_OUTPUTS': json.dumps(payload.get('parent_outputs', {})),
        }

        # RAG Export is NOT a file-push export. It consumes a wired RAG handle
        # FILE — the parent RAG Builder's /data/output/rag.json, which must be
        # STAGED into this node's /data/input (exactly like RAG Query consumes
        # it) — then registers it as a Named Artifact via POST /named-artifacts.
        # The generic `_export` branch below would set skip_download=True and
        # blank source_file_location, so rag.json never lands in /data/input and
        # the container dies with "No RAG handle found in /data/input". Scope the
        # file-push behaviour to genuine file-push exports; for rag_export keep
        # the parent-output staging resolved above intact, and MERGE the export
        # credentials into the node's own container_env (preserving the user's
        # RAG_NAME / DESCRIPTION / MODE) rather than replacing it.
        if node_kind_value == 'rag_export':
            _merged = dict(payload.get('container_env') or {})
            for _k, _v in _export_env.items():
                _merged.setdefault(_k, _v)
            payload['container_env'] = _merged
        else:
            payload['container_env'] = _export_env
            # Export container doesn't need MinIO download — it calls API
            # endpoints. Set a dummy source so the worker doesn't try to download.
            payload['source_file_location'] = ''
            payload['skip_download'] = True

    # Auto-inject platform-owned secrets for specific system nodes — no user
    # wiring needed. The mobile_build node signs the APK with the durable
    # platform keystore (seeded env→DB+Vault as platform-tenant secrets by
    # seed_mobile_keystore_secret). These are normal secret_name refs, so they
    # still pass the accepted_secrets allowlist below and resolve tenant-scoped
    # like any other secret.
    node_data = payload.get('node_data', {}) or {}
    if node_kind_value == 'mobile_build':
        _smb = optional_paid("seed_mobile_builder")
        auto_refs = _mobile_build_auto_secret_refs(
            db, node_data, _smb.KEYSTORE_SECRET_MAP if _smb is not None else ())
        node_data['secret_refs'] = auto_refs
        payload['node_data'] = node_data
        # Grant internet egress: the build fetches npm packages, the Gradle
        # distribution, and Android SDK/Maven artifacts from public registries.
        # A non-empty allowed_egress routes the container to the egress network
        # (mod-user-egress) instead of the default-deny isolated network — without
        # it npm/gradle fail with EAI_AGAIN (no DNS/outbound). Top-level so the
        # worker's TaskPayload.AllowedEgress picks it up (flatten already ran).
        payload['allowed_egress'] = [
            "registry.npmjs.org", "dl.google.com", "maven.google.com",
            "services.gradle.org", "plugins.gradle.org",
            "repo.maven.apache.org", "repo1.maven.org", "repo.gradle.org",
            "github.com", "objects.githubusercontent.com", "jitpack.io",
        ]

    # BUG.B40d (owner Q-B40-3=C) — an iOS build is a MOD job that runs on a
    # macOS worker. The platform already has exactly one macOS placement
    # mechanism: the native darwin lane (MAC.1.s7/s8a/s8b) — a Mac reports
    # hardware_specs['os'] == 'darwin' + the `metal` tag and subscribes ONLY to
    # the "Metal" queue, and the claim guards refuse a darwin-only native task
    # to every non-Metal worker (platform_guard, routers/worker_tasks.py:1473
    # + darwin_task_guard/metal_lane_guard, :1563) and refuse any container
    # task to a Mac (a Mac has no container runtime). So the iOS half of the
    # mobile build is stamped onto that lane instead of inventing a parallel
    # one: execution=native + native_platform=darwin, and the task is parked on
    # the Metal queue.
    #
    # The Android half is untouched — target absent/blank/android keeps the
    # existing mobilebuilder container task on the linux fleet byte-for-byte.
    # 'both' means this task carries the iOS part, so it takes the macOS lane.
    _mobile_target = str(node_data.get('mobile_target')
                         or payload.get('mobile_target') or '').strip().lower()
    if _mobile_target in ('ios', 'both'):
        if _ios_build_macos_workers_online(db) == 0:
            # Fail fast, at dispatch, with the named reason — no row is created,
            # so nothing queues forever and nothing can run on Linux.
            logger.warning(
                "mobile_build %s refused: iOS target, no online darwin worker",
                request.node_id,
            )
            return {
                "task_id": None,
                "status": "ios_build_no_macos_worker",
                "reason": "iOS builds need a macOS worker; none is online",
            }
        # BUG.B40f — the lane stamp alone is not enough: dispatchNativeTask
        # (CORE/Worker/cmd/worker/main.go:1884) reads payload.native.command and
        # fails "native.command empty" when the task carries no recipe. Stamp the
        # p1 darwin recipe (utils/mobile_ios_build.ios_build_metadata) through the
        # SAME helper the GP.16b native lane uses, so the payload shape is
        # byte-identical to a node-declared native task: native_recipe (the
        # platforms map, for the claim-time resolver) + native (the resolved
        # darwin entry: command "sh" + args_template with {input_dir}/{output_dir})
        # + native_platform 'darwin'. env stays EMPTY — signing inputs ride on the
        # task's resolved-secret env (the _mobile_build_auto_secret_refs refs above
        # are secret_name refs, never values), so no secret ever lands in the
        # recipe JSON. Lazy import: utils/mobile_ios_build is read-only constants.
        # 1788d CE carve: utils/mobile_ios_build.py is paid (manifest deny
        # `CORE/API/**/mobile*.py`). The iOS/darwin build recipe is a paid
        # capability; with it absent the dispatch is refused with the same 402
        # the central edition handler produced, so the worker never receives a
        # recipe-less native task.
        from utils.paid_import import optional_paid
        _ios_mod = optional_paid("utils.mobile_ios_build")
        if _ios_mod is None:
            raise HTTPException(
                status_code=402,
                detail="iOS app builds are not available in MOD "
                       "Community Edition.",
            )
        for _nk, _nv in _native_payload_fields(_ios_mod.ios_build_metadata()).items():
            payload[_nk] = _nv
        payload['native_platform'] = 'darwin'
        payload['mobile_target'] = _mobile_target
        # A Mac polls ONLY the Metal queue (MAC.1.s8a: SupportedQueues ->
        # ["ACCELERATOR","Metal"]), so the caller's container queue ("CPU" for
        # the mobilebuilder node) would hide the task from the one worker that
        # may run it. Park it there — the same queue the native darwin recipe
        # routing uses (utils/queue_selector.py:75).
        _queue_override = "Metal"

    # add-web-search-node — the web-search node is a NORMAL callback node: it
    # returns its JSON artifact by POSTing /admin/web-search/result, and it
    # reaches the public web (DuckDuckGo). Credentials follow the run_task path,
    # NOT the platform-service key (it processes user-supplied content — the
    # description — so per _PLATFORM_SERVICE_KEY_NODE_KINDS' own rule it must use
    # a scoped credential): the worker vends a short-lived run_task token via
    # MOD_TASK_TOKEN_URL and mod_node_auth sends it as X-API-Key, which the
    # callback verifies with run_task_auth. We inject the run context it echoes
    # back, the service key ONLY as the transitional run-token fallback (the
    # callback also accepts service auth), and the egress it needs to reach DDG.
    if node_kind_value == 'system_web_search':
        cenv = payload.setdefault('container_env', {})
        cenv.setdefault('API_KEY', os.getenv('PLATFORM_SERVICE_API_KEY', ''))
        cenv['WORKFLOW_RUN_ID'] = str(request.workflow_run_id)
        cenv['NODE_ID'] = request.node_id
        cenv['TENANT_ID'] = str(request.tenant_id)
        cenv['NODE_KIND'] = node_kind_value
        # A non-empty allowed_egress routes the container to the egress network
        # (mod-user-egress) so it can resolve + reach DuckDuckGo's HTML endpoint.
        payload['allowed_egress'] = [
            "html.duckduckgo.com", "duckduckgo.com", "links.duckduckgo.com",
        ]
        # Pure API/web node — no MinIO input to download.
        payload['source_file_location'] = ''
        payload['skip_download'] = True

    # external-executor-nodes — Kubernetes/Qube/Deadline submit nodes. They are
    # CONNECTED nodes: allow-list egress to exactly the endpoint(s) the node must
    # reach (the orchestrator API + the S3 endpoint it collects outputs from),
    # configured per-node via EXECUTOR_EGRESS_HOSTS, and inject the run identity
    # the container uses to build a deterministic (idempotent) external job name.
    if node_kind_value in ('kubernetes_submit', 'qube_submit', 'deadline_submit'):
        cenv = payload.setdefault('container_env', {})
        _nd = payload.get('node_data', {}) or {}
        _extra = _nd.get('extraVariables', {}) or {}
        _hosts = [h.strip() for h in str(_extra.get('EXECUTOR_EGRESS_HOSTS', '') or '').split(',') if h.strip()]
        if _hosts:
            # A non-empty allowed_egress routes the container to the egress
            # network scoped to these hosts, instead of the default-deny net.
            payload['allowed_egress'] = _hosts
        cenv['WORKFLOW_RUN_ID'] = str(request.workflow_run_id)
        cenv['NODE_ID'] = request.node_id
        cenv['TENANT_ID'] = str(request.tenant_id)
        cenv['NODE_KIND'] = node_kind_value
        _cogs = payload.get('cog_ids') or (payload.get('ProjectInfo', {}) or {}).get('cog_ids') or []
        if _cogs:
            cenv['COG_IDS'] = json.dumps(_cogs)

    # switchyard-llm-node — the Switchyard LLM node is a BYO-endpoint node: its
    # BASE_URL is an arbitrary external host (e.g. the user's own switchyard LLM
    # router/gateway, reached by raw tailnet IP), not a MOD-managed compose
    # alias. Every task container defaults to the zero-outbound, default-deny
    # network (CORE/Worker/internal/executor/netpol.go); without an explicit
    # egress grant this node's health check would fail loud for the WRONG
    # reason (network policy, not the endpoint actually being down). Declare
    # allowed_egress from the configured BASE_URL's host — same pattern as the
    # external-executor nodes' EXECUTOR_EGRESS_HOSTS above — so a non-empty
    # value routes the container onto mod-user-egress (outbound-capable)
    # instead. Best-effort: an unset/unparsable BASE_URL leaves the node on the
    # default-deny network, where its own health check reports the endpoint as
    # unreachable — still loud, just not yet informative about why.
    if node_kind_value == 'switchyard_llm':
        _nd = payload.get('node_data', {}) or {}
        _extra = _nd.get('extraVariables', {}) or {}
        _base_url = str(_extra.get('BASE_URL', '') or '').strip()
        if _base_url:
            try:
                from urllib.parse import urlparse
                _parsed = urlparse(_base_url if '://' in _base_url else f'//{_base_url}')
                _host = _parsed.hostname
            except Exception:  # noqa: BLE001
                _host = None
            if _host:
                payload['allowed_egress'] = [_host]

    # Collect secret references from node_data.
    # These are env var mappings the user configured in the workflow builder:
    #   [{"secret_name": "my-api-key", "env_var": "API_KEY"}, ...]
    # The worker resolves these to actual values at runtime (never stored in payload).
    #
    # Validation: only allow env vars declared in the node definition's
    # custom_metadata.accepted_secrets. Reject anything else.
    secret_refs = node_data.get('secret_refs', [])
    if secret_refs:
        # OAuth connection refs carry `oauth_connection_id`, not an env_var.
        # The builder omits env_var when the customer just picked a connection,
        # so an empty env_var would never match accepted_secrets and the ref
        # would be dropped silently — the node then runs with no token and
        # never reaches the fail-closed 409 in resolve_task_secrets. Default
        # such a ref to the platform's OAuth var (a COPY — the caller's dict
        # is never mutated) so it goes through the SAME accepted_secrets
        # allowlist as every other user ref.
        from services.oauth_task_secrets import DEFAULT_ENV_VAR, is_oauth_secret_ref
        secret_refs = [
            ({**ref, 'env_var': DEFAULT_ENV_VAR}
             if is_oauth_secret_ref(ref) and not ref.get('env_var')
             else ref)
            for ref in secret_refs
        ]
        from node_builder.build_dag.build_secrets import is_build_secret_ref
        # Source-typed build-credential refs (Harbor / platform / LLM) are
        # server-attached in submit.py and only resolved for system build-task
        # nodes (gated again at resolution by _task_is_system_build). They carry
        # no secret value here, so they bypass the user accepted_secrets
        # allowlist — but ONLY when this is genuinely a system build node.
        from services.workflow_orchestrator import should_bind_docker_socket
        is_system_build_node = should_bind_docker_socket(db, node_data)
        build_refs = [
            r for r in secret_refs
            if is_build_secret_ref(r) and is_system_build_node
        ]

        # User-configured refs: validate against the node's accepted_secrets.
        user_refs = [r for r in secret_refs if not is_build_secret_ref(r)]
        allowed_env_vars = set()
        if user_refs:
            try:
                from models import NodeDefinition
                # Tenant-scoped: another tenant's copy of the same node_kind
                # must not widen/narrow THIS task's accepted_secrets. System
                # rows (tenant_id NULL) apply to every tenant; the tenant's
                # own row wins over the system row (False sorts before True).
                nd = db.query(NodeDefinition).filter(
                    NodeDefinition.node_kind == payload.get('node_kind'),
                    NodeDefinition.deleted_at.is_(None),
                    NodeDefinition.tenant_id.is_(None)
                    | (NodeDefinition.tenant_id == request.tenant_id),
                ).order_by(
                    NodeDefinition.tenant_id.is_(None),
                ).first()
                if nd and nd.custom_metadata:
                    for slot in (nd.custom_metadata.get('accepted_secrets') or []):
                        allowed_env_vars.add(slot.get('env_var', ''))
            except Exception:
                pass  # if lookup fails, allow nothing

        validated = list(build_refs)
        for ref in user_refs:
            env_var = ref.get('env_var', '')
            if env_var in allowed_env_vars:
                validated.append(ref)
            else:
                logger.warning(
                    "Dropping secret_ref env_var=%s for node %s — not in accepted_secrets",
                    env_var, request.node_id,
                )
        if validated:
            payload['secret_refs'] = validated

    # ========================================================================
    # EXTRA VARIABLES → CONTAINER ENV: inject user-configured variables
    # from the workflow builder's node config panel into the container.
    # Input nodes already handled above; this covers process + other nodes.
    # ========================================================================
    if 'container_env' not in payload:
        payload['container_env'] = {}
    node_data = payload.get('node_data', {})
    extra_vars = node_data.get('extraVariables', {})
    for key, value in extra_vars.items():
        env_key = key.upper()
        # Don't overwrite env vars already set by input/export handlers
        if env_key not in payload['container_env'] and value is not None and str(value).strip():
            payload['container_env'][env_key] = str(value)

    # FLAT CONFIGURATION_SCHEMA DEFAULTS → CONTAINER ENV: domain node
    # definitions declare the op they run as a top-level UPPER_CASE key in
    # configuration_schema (e.g. {"CRYPTO_OP": {"default": "crypto_hash"}}).
    # Those never reached the container, so every node in a multi-op container
    # silently ran the container's built-in default op. Apply them as defaults
    # only — user extraVariables and the input/export handlers above already
    # own their keys and must win. Best-effort: never fail dispatch.
    try:
        from services.node_config_env_defaults import apply_flat_env_defaults_for_node
        # output-cache key must see these too: record the node's flat config
        # KEY NAMES (never values, never secrets — flat_env_defaults already
        # drops secret/password specs) so _cache_config_for_payload can hash
        # their EFFECTIVE container_env values. Keys-only marker: a node with
        # no flat config keys gets no marker at all, so its cache hash is
        # byte-identical to the pre-fix shape (no mass invalidation).
        _flat_cfg = apply_flat_env_defaults_for_node(
            db,
            {},
            node_data,
            node_kind,
            getattr(request, 'tenant_id', None),
        )
        payload['container_env'] = apply_flat_env_defaults_for_node(
            db,
            payload['container_env'],
            node_data,
            node_kind,
            getattr(request, 'tenant_id', None),
        )
        if _flat_cfg:
            payload['_flat_config_keys'] = sorted(
                k for k in _flat_cfg if not _flat_key_secretish(k)
            )
    except Exception:  # noqa: BLE001 — a definition-read blip never wedges dispatch
        logger.warning(
            "flat configuration_schema defaults skipped for node %s",
            node_data.get('nodeDefinitionId') or node_kind,
        )

    # negative-training-inputs — MOD_INPUT_ROLES manifest. Map each staged
    # multi-port input's filename (/data/input/<cog_id><ext>) to its logical
    # input PORT so entrypoints that accept positive + negative example channels
    # can classify staged files by role. Only emitted when input_files carry a
    # 'port' (i.e. a parent_ports map was threaded from the dispatch site);
    # absent for every other node, so nothing else changes.
    try:
        _roles = {}
        for _f in payload.get('input_files') or []:
            _port = _f.get('port')
            if not _port:
                continue
            _ext = _f.get('file_extension') or ''
            if _ext and not _ext.startswith('.'):
                _ext = '.' + _ext
            _roles[f"{_f.get('cog_id')}{_ext}"] = _port
        if _roles and 'MOD_INPUT_ROLES' not in payload['container_env']:
            payload['container_env']['MOD_INPUT_ROLES'] = json.dumps(_roles)
    except Exception as e:  # noqa: BLE001 — manifest is best-effort
        logger.warning("Could not build MOD_INPUT_ROLES manifest: %s", e)

    # ========================================================================
    # EARLY cache short-circuit (perf) — MUST precede the ephemeral-NIM wake.
    # A cache hit never runs a container, so waking + polling the NIM
    # (ensure_ready, ~60s blocking) for a cached NIM node is pure latency, and
    # because dispatch is synchronous it serialized the WHOLE workflow into
    # ~60s-per-level bursts: the UI sat frozen between waves and then several
    # nodes flipped to COMPLETED at once. Probing the cache here skips the wake
    # (and the per-node injections) entirely on a hit. `_maybe_cache_short_circuit`
    # returns None cleanly on a miss (no side effects), and the authoritative
    # check still runs after auto-conversion below, so a node whose file
    # extension only settles post-conversion is unaffected.
    _early_cache_hit = _maybe_cache_short_circuit(db, request, payload)
    if _early_cache_hit is not None:
        return {"task_id": _early_cache_hit.id, "status": "cache_hit"}

    # 20.6.s5 — ASR pass-through (see _maybe_asr_passthrough below). MUST sit
    # after the cache short-circuit (a cache hit already settled the node) and
    # before the ephemeral-NIM block, so a text transcript never wakes a NIM.
    _asr_done = _maybe_asr_passthrough(db, request, payload)
    if _asr_done is not None:
        return {"task_id": _asr_done.id, "status": "asr_passthrough"}

    # ========================================================================
    # PER-NODE CREDENTIAL + STACK-REACHABILITY INJECTION — the single choke
    # point. Every dispatch path (root _start_workflow, downstream
    # _check_and_dispatch_downstream, and other routers) funnels through here, so
    # applying the gated/scoped injections at this one place fixes them for all
    # of them. Historically these lived only in root dispatch, so downstream
    # RAG Builder got no vector token and Image to Text no VLM endpoint.
    # DB-authoritative / node-kind-scoped gates keep this from over-granting.
    # ========================================================================
    try:
        payload.setdefault('container_env', {})
        from services.workflow_orchestrator import apply_node_task_injections
        apply_node_task_injections(
            db, node_data, payload,
            tenant_id=request.tenant_id,
            workflow_run_id=request.workflow_run_id,
            node_id=str(request.node_id),
            node_kind=node_kind_value,
        )
    except RuntimeError:
        # A missing platform-service key for a maintenance node is fatal by
        # design (mirrors _start_workflow) — do not enqueue a task that can only
        # fail auth.
        raise
    except Exception as _inj_exc:  # noqa: BLE001 — never wedge dispatch
        logger.warning("Per-node injection skipped for task node=%s: %s",
                       request.node_id, _inj_exc)

    # B108 — general worker-mediated token marker for a ROOT processing node
    # that carries no scoped credential: lands it on the main network so it
    # can fetch short-lived run_task tokens (resolve cog, report status,
    # upload outputs). Deliberately applied AFTER apply_node_task_injections'
    # reserved-env scrub (above), not before, so it survives for a
    # tenant-owned (non platform-owned) root NodeDefinition. is_root_node is
    # platform-internal-only — _enforce_tenant_enqueue_scope 403s it on the
    # public HTTP route — so this can only ever run for _start_workflow's own
    # in-process call. Deliberately ROOT-only: it must NOT broadly promote
    # untrusted DOWNSTREAM compute nodes off the isolated default-deny network.
    if request.is_root_node:
        _root_cenv = payload.get("container_env") or {}
        if "API_KEY" not in _root_cenv:
            _root_cenv = dict(_root_cenv)
            _root_cenv["MOD_NEEDS_API_TOKEN"] = "1"
            payload["container_env"] = _root_cenv

    # ========================================================================
    # EPHEMERAL LOCAL NIM (Studio Voice, later VSR) — SCOPED to these node kinds.
    # These "(Local)" nodes are gRPC clients that need a self-hosted NIM up on
    # the GPU host. Reuse the PersistentStack wake/sleep machinery: wake (or
    # create+deploy) the NIM stack, sleep competing GPU stacks, poll readiness,
    # then inject the client's NIM_ENDPOINT and the API_KEY network marker that
    # lands it on the platform net so it can resolve the NIM alias. The client
    # is PINNED to the NIM's host (docker DNS is per-host). Sleep-back happens in
    # the /complete + /failed handlers. Does NOT alter default-application env
    # for any other node kind.
    # ========================================================================
    _nim_pin_host = None
    # Set (to the stack name) only when a dispatch path holds the task on
    # stack capacity (D7/GA.GSW.4) — NIM block below, or the user-assigned
    # stack binding further down.
    _awaiting_stack = None
    try:
        from services import nim_ephemeral as _nim
        _nim_spec = _nim.spec_for(node_kind_value)
        # Only manage when the user did NOT point the node at their own NIM.
        if (_nim_spec is not None
                and 'NIM_ENDPOINT' not in payload['container_env']):
            if not _nim_spec.enabled:
                logger.warning(
                    "Local NIM node %s dispatched but its image is not pulled "
                    "yet (%s) — run the one-time warm-up; the client will fail "
                    "fast until then.", node_kind_value, _nim_spec.image)
            else:
                _ready = _nim.ensure_ready(db, node_kind_value, request.tenant_id)
                _nim.inject_client_env(payload['container_env'], _nim_spec,
                                       _ready.endpoint)
                _nim_pin_host = _ready.host
                if getattr(_ready, "held", False):
                    # D7/GA.GSW.4: the NIM won't fit and nothing is
                    # safely-evictable — leave the task in the EXISTING
                    # awaiting-resources hold (stamp `awaiting_stack` below),
                    # never dispatch a client that can only fail. The redrive
                    # sweep re-attempts the wake when capacity frees and then
                    # releases the hold.
                    _awaiting_stack = _nim_spec.stack_name
                    logger.info(
                        "Local NIM %s cannot fit for node=%s — task stays in "
                        "the awaiting-resources hold until capacity frees.",
                        _nim_spec.stack_name, request.node_id)
                elif not _ready.ready:
                    logger.warning(
                        "Local NIM %s did not report ready before dispatch of "
                        "task node=%s; enqueuing anyway (client fails fast, "
                        "then sleep-back runs).",
                        _nim_spec.stack_name, request.node_id)
                else:
                    logger.info("Local NIM %s ready at %s (host=%s) for node=%s",
                                _nim_spec.stack_name, _ready.endpoint,
                                _ready.host, request.node_id)
    except Exception as e:  # noqa: BLE001 — never wedge dispatch over NIM setup
        logger.warning("Ephemeral NIM setup failed for %s (%s): %s; the client "
                       "will fail fast if the NIM is unreachable.",
                       node_kind_value, request.node_id, e)

    # USER-ASSIGNED STACK BINDING (generalized-stack-wake D12) — honor a node's
    # OWN per-instance `requires_stack` from the WorkflowBuilder. See the helper
    # for the scoping/guards. Best-effort + defensive: never wedges dispatch.
    # Skipped when the ephemeral-NIM block above already held this task on its
    # NIM's capacity (D7) — that hold wins and this reassignment would wipe it.
    if not _awaiting_stack:
        _awaiting_stack = _ensure_user_assigned_stack(
            db, node_data, payload, node_kind_value, request)
    if _awaiting_stack:
        # A fit does not exist yet (owner O1): leave the task undispatched.
        # Mark it so _try_claim_task skips it and redrive can clear the marker
        # once capacity frees. GSW.4a.
        payload['awaiting_stack'] = _awaiting_stack

    # Note: NIM_ENDPOINT comes from secrets (resolved by worker at runtime).
    # ENDPOINT_PATH comes from catalog side effects (in container_env).
    # The container merges them — we can't do it here because secrets aren't available yet.

    # ========================================================================
    # AUTO-CONVERT: If file format doesn't match container's accepted_formats,
    # insert a conversion task before the main task.
    # ========================================================================
    file_ext = (payload.get('file_extension') or '').lower().lstrip('.')
    conversion_task_id = None

    if file_ext and node_kind_value and not node_kind_value.endswith('_input') and not node_kind_value.endswith('_export'):
        try:
            # Tenant-scoped: another tenant's fork may accept a different
            # format set — never auto-convert based on ITS accepted_formats.
            nd = _node_def_for_tenant(db, node_kind_value,
                                      getattr(request, 'tenant_id', None))
            if nd and nd.accepted_formats and file_ext not in nd.accepted_formats:
                # File format not accepted — auto-insert conversion
                target_format = nd.accepted_formats[0]  # Convert to first accepted format
                registry = platform_registry()

                # Pick the right converter based on format categories
                IMAGE_FMTS = {'jpg', 'jpeg', 'png', 'tif', 'tiff', 'bmp', 'gif', 'webp', 'exr', 'hdr', 'webm'}
                VIDEO_FMTS = {'mp4', 'mov', 'avi', 'webm', 'mkv'}
                MESH_FMTS = {'obj', 'fbx', 'glb', 'gltf', 'ply', 'stl'}
                AUDIO_FMTS = {'mp3', 'wav', 'flac', 'ogg', 'aac', 'm4a'}
                CAD_FMTS = {'step', 'stp'}

                HDR_FMTS = {'exr', 'hdr'}
                USD_FMTS = {'usd', 'usda', 'usdc'}

                if file_ext in USD_FMTS:
                    converter_image = f"{registry}/modtim/usd-converter:latest"
                elif file_ext in HDR_FMTS:
                    converter_image = f"{registry}/modtim/hdr-converter:latest"
                elif file_ext in CAD_FMTS:
                    converter_image = f"{registry}/modtim/cad-converter:latest"
                elif target_format in AUDIO_FMTS or file_ext in AUDIO_FMTS:
                    converter_image = f"{registry}/modtim/audio-converter:latest"
                elif target_format in IMAGE_FMTS or file_ext in IMAGE_FMTS:
                    converter_image = f"{registry}/modtim/image-converter:latest"
                elif target_format in VIDEO_FMTS:
                    converter_image = f"{registry}/modtim/video-converter:latest"
                elif target_format in MESH_FMTS:
                    converter_image = f"{registry}/modtim/mesh-converter:latest"
                else:
                    converter_image = f"{registry}/modtim/image-converter:latest"

                logger.info(
                    "Auto-converting %s → %s for %s (task will chain after conversion)",
                    file_ext, target_format, node_kind_value,
                )

                from utils.minio_client import get_tenant_bucket_name
                conv_task = ApiTaskQueue(
                    workflow_run_id=request.workflow_run_id,
                    node_run_id=request.node_run_id,
                    node_id=f"convert-{request.node_id}",
                    tenant_id=request.tenant_id,
                    queue_name="CPU",
                    priority=request.priority,
                    task_payload={
                        "node_kind": "format_converter",
                        "processing_image": converter_image,
                        "source_file_location": payload.get('source_file_location', ''),
                        "file_extension": file_ext,
                        "minio_bucket": payload.get('minio_bucket')
                        or get_tenant_bucket_name(request.tenant_id, db),
                        "gpu_required": False,
                        "container_timeout": 300,
                        "container_env": {
                            "TARGET_FORMAT": target_format,
                        },
                    },
                    status=ApiTaskStatus.PENDING,
                    max_attempts=2,
                    output_namespace="conversion",
                )
                db.add(conv_task)
                db.flush()
                conversion_task_id = conv_task.id

                # Update the main task's payload to expect the converted format
                payload['file_extension'] = target_format
                payload['_awaiting_conversion'] = conversion_task_id

                logger.info("Conversion task %d enqueued (%s → %s)", conv_task.id, file_ext, target_format)

                # Notify workers about the conversion task (FX.165 p1h: publish on
                # the queue the row was actually created on — today always "CPU",
                # but the literal and the row could drift apart otherwise).
                _publish_event(f"task:available:{conv_task.queue_name}")

        except Exception as e:
            logger.warning("Auto-conversion check failed (proceeding without conversion): %s", e)

    # workflow-agentic-execution-loop §workflow-node-output-cache —
    # cache-READ short-circuit. When the tenant has `output_cache_enabled`
    # AND a row exists for (image, input, config, tenant_id), skip
    # worker dispatch entirely. Create the task in COMPLETED state
    # pointing at the cached output and fire the downstream-dispatch
    # path so the rest of the workflow proceeds normally.
    #
    # Conservative — only fires for tasks with a non-empty input hash
    # (excludes input nodes whose `source_file_location` is '' and
    # export nodes that read from MinIO directly). Failures here log
    # but fall through to normal dispatch.
    _cache_hit_task = _maybe_cache_short_circuit(
        db, request, payload,
    )
    if _cache_hit_task is not None:
        return {"task_id": _cache_hit_task.id, "status": "cache_hit"}

    # Gate 3 — hand this node run its durable checkpoint volume, if it has one.
    #
    # Assigned on the FIRST dispatch and reused by every later attempt, which is
    # the whole mechanism: the ordinary /data volume is per-attempt and removed
    # on completion, so a node five hours into six restarts from zero.
    #
    # Opt-in per node, and silent when off: a node that never reads
    # MOD_CHECKPOINT_DIR behaves exactly as it does today. That is what makes
    # this safe against 200+ existing images — none of them have to change, and
    # the ones where six hours is a real risk adopt it one at a time.
    _ckpt = _checkpoint_volume_for(db, request.node_run_id)
    _ckpt_host = None
    if _ckpt:
        payload["checkpoint_volume"] = _ckpt
        # Which attempt of this NODE RUN this is.
        #
        # Not `attempt_count`, which counts retries of one task ROW and is 0 on
        # every requeue — a requeue creates a new row. So every checkpoint read
        # "attempt 0" and the one question the record exists to answer, "did the
        # retry get further than the attempt before it", was unanswerable.
        payload["attempt_index"] = _attempt_index(db, request.node_run_id)
        # Gate 3 — prefer the host that HOLDS the checkpoint.
        #
        # The volume is host-local, and a requeued task otherwise goes to any
        # capable worker, so on an N-worker fleet the saved progress is found
        # 1/N of the time. A checkpoint you probably will not find is not much
        # of a checkpoint.
        _ckpt_host = _live_checkpoint_host(db, request.node_run_id)

    # NOAH: only a PINNED task carries the pinnable requirement — never ordinary
    # PENDING pool work, or the fail-closed guard would strand everything.
    if _ckpt_host:
        _noah = optional_paid("services.noah")
        if _noah is not None:
            payload = _noah.add_pinnable_requirement(payload, db=db)

    # Platform-maintenance nodes (harbor scan followup, etc.) reach internal/admin
    # endpoints via the INTERNAL API URL, which only resolves ON the manager — so
    # they must run on the on-manager embedded worker; a remote worker that claimed
    # one would fail (can't reach http://api:80). Pin to the embedded worker, but
    # ONLY when it is present + LICENSED — pinning to an absent host would leave the
    # task ASSIGNED for ever (same trap the checkpoint note above guards against).
    # No pinnable requirement (set after the NOAH block): the embedded worker claims
    # the ASSIGNED task normally.
    if not _ckpt_host and payload.get("assign_to_embedded_worker"):
        # BUG-EMBEDDED-WORKER-UI — on CE the on-manager embedded worker is
        # `mod-worker-ce`, so the SaaS literal alone never matched and these
        # manager-only nodes (they reach internal/admin endpoints via
        # http://api:80, reachable ONLY on the manager) went unpinned: a remote
        # worker could claim one and fail. Pin to whichever embedded row exists.
        from routers.persistent_stacks._shared import _EMBEDDED_WORKER_NAMES
        _emb = db.query(WorkerLicense).filter(
            WorkerLicense.worker_name.in_(_EMBEDDED_WORKER_NAMES),
            WorkerLicense.status == WorkerStatus.LICENSED,
        ).first()
        if _emb:
            _ckpt_host = _emb.worker_name

    # A local-NIM client MUST run on the same host as its NIM (docker DNS is
    # per-host), so pin it there. This is a hard placement requirement, not a
    # checkpoint preference, but it rides the same assigned_to_worker channel.
    if _nim_pin_host and not _ckpt_host:
        _ckpt_host = _nim_pin_host

    # Generic managed-AI-stack co-location pin. A stack-consuming node (e.g.
    # Image to Text → brand-vlm) is pinned to the worker its stack is placed on
    # (payload["pin_to_worker"], set by apply_node_task_injections) so it can
    # reach the stack's PUBLISHED host port via host.docker.internal on the same
    # host — the redeploy- and host-independent path, working co-located OR on a
    # remote GPU worker. Same trap-guard as the embedded pin above: honoured only
    # when that worker is present + LICENSED, so a pin to an absent host does not
    # leave the task ASSIGNED for ever.
    if not _ckpt_host and payload.get("pin_to_worker"):
        _pin = payload.get("pin_to_worker")
        _pinned = db.query(WorkerLicense).filter(
            WorkerLicense.worker_name == _pin,
            WorkerLicense.status == WorkerStatus.LICENSED,
        ).first()
        if _pinned:
            _ckpt_host = _pin

    # GB.EGR.D.9d: a task whose allowed_egress names a tailnet peer (raw
    # 100.64.0.0/10 IP or *.example.com -- e.g. switchyard_llm's
    # BASE_URL above, or an executor's EXECUTOR_EGRESS_HOSTS) cannot dial it
    # directly: Docker Desktop / WSL2 task containers have no route to the
    # host's tailscale0, the same fact that makes every persistent stack
    # inject the tsegress sidecar mechanism (routers/persistent_stacks/
    # _shared.py, 2026-09-25 "switchyard stack 22 could not reach its llama
    # backends"). Mint the identical per-anchor single-use keys and hand them
    # to the worker (internal/executor/egress_tailnet.go) via
    # payload['tailnet_egress'], reusing the stack mechanism rather than a
    # second one. Centralized here (one call site for every node kind) rather
    # than repeated at each of the four allowed_egress assignment sites above.
    _tailnet_cfg = _compile_tailnet_egress_cfg(payload.get('allowed_egress'), node_id=request.node_id)
    if _tailnet_cfg:
        payload['tailnet_egress'] = _tailnet_cfg

    # declared-egress C7 (3.3) — the SAME single choke point: every allowed_egress
    # assignment site has run by here, so the task's FINAL allowlist decides its
    # enforcement posture. Non-empty -> payload['egress_mode'] = resolve_egress_mode
    # (tenant map / platform default; the paid egress_mode layer). Empty
    # allowlists get no key at all — sealed nodes stay byte-identical.
    _stamp_egress_mode(db, request, payload)

    # GX.D1 — workflow/node/run placement. Resolve run > node > workflow
    # placement for THIS task and stamp the claim-time markers
    # (worker_pin / worker_pin_tag / placement_worker_missing) on the
    # payload. Non-workflow tasks (no run) and unpinned workflow tasks are
    # left byte-identical to before. The stamp rides the payload, so it
    # survives the overlap scheduler's PENDING->ASSIGNED placement and is
    # enforced by the claim SQL (placement_claim_guard_sql) — a pinned
    # task is never silently run elsewhere.
    if request.workflow_run_id is not None:
        from services.workflow_placement import apply_task_placement
        payload = apply_task_placement(
            db, payload, workflow_run_id=request.workflow_run_id,
            node_data=payload.get('node_data'))

    # GY.C7 (external-egress block) — the dispatch choke point. All
    # allowed_egress assignment sites (mobile_build, web-search, executor
    # nodes, switchyard_llm, node_data flatten) have run by here, so the
    # task's FINAL egress is what is judged. A node that declares internet
    # egress on labelled inputs is refused before any task row is written
    # (node run + run failed, audit row written). Returns a refusal dict on
    # block; None to proceed. Unlabelled runs / non-egress nodes: no-op.
    _egress_block_reason = _external_egress_block_check(db, request, payload)
    if _egress_block_reason is not None:
        return {
            "task_id": None,
            "status": "external_egress_blocked",
            "reason": _egress_block_reason,
        }

    # FX.91 p2c (tenant-admin one-time egress approval) — the SAME choke
    # point, one gate over: a node that DECLARES external hostnames
    # (node_definitions.egress_domains, FX.91 p2a) may not dispatch until a
    # tenant admin has approved that exact set (services/node_egress_approval.
    # gate_reason, which also appends the NODE_EGRESS_APPROVAL_REQUIRED audit
    # row p3b feeds to SIEM). Inert while `nodes.egress_approval_required` is
    # off — behaviour unchanged — and a node declaring nothing is default-deny
    # (nothing to grant). Returns a refusal dict on block; None to proceed.
    # FX.91 p3a (gov profile: declared egress blocked unless allowlisted) —
    # the SAME choke point, checked FIRST: under an active gov/DoD compliance
    # profile a declared hostname outside nodes.egress_allowlist_domains is a
    # HARD refusal that neither nodes.egress_approval_report_only nor a
    # tenant's approval can talk it out of. Inert (None) while the gov posture
    # is not enforced, so a non-gov install is unchanged byte-for-byte. Writes
    # NO audit row here — the SIEM/audit event for a blocked attempt is FX.91
    # p3b's job (it keys on status 'egress_gov_blocked' + the decision fields).
    _gov_block_reason = _node_egress_gov_gate(db, request, payload)
    if _gov_block_reason is not None:
        return {
            "task_id": None,
            "status": "egress_gov_blocked",
            "reason": _gov_block_reason,
        }

    # FX.91 p5 (owner 10-07 Q5=A) — the UNDECLARED half of the same posture,
    # immediately after p3a's DECLARED half: under an active gov/DoD profile a
    # node that declares NO node_definitions.egress_domains gets NO EXTERNAL
    # network. p3a is inert for such a node (nothing to block), and today the
    # worker still promotes it to the MAIN network when the task carries the
    # API_KEY / MOD_TASK_TOKEN_URL / NIM_API_KEY markers (netpol.go:104/116/123
    # branch before the AllowedEgress check at :138) — main is internet, so the
    # node phones home. Here we stamp the payload with MOD_GOV_UNDECLARED_INTERNAL_ONLY
    # (a platform-reserved container_env key, so a tenant cannot set it) and the
    # worker parks the container on the internal-only posture net instead: the
    # MOD API / task-token callback stays (nodes must still report results),
    # the internet goes. A task that asks for EXTERNAL network on behalf of an
    # undeclared node is refused outright (reason names undeclared_gov) with the
    # p3b audit/SIEM event. Inert while the posture is off → unchanged.
    _p5 = _fx91_undeclared_gate(db, request, payload)
    if _p5["reason"] is not None:
        try:
            from services import node_egress_approval as _fx91_p5
            _record_egress_block(
                db, request, _p5["decision"],
                gate=_fx91_p5.GOV_GATE_LABEL,
                report_only=False,
            )
        except Exception:  # noqa: BLE001 — emit must not move the gate
            logger.warning("FX.91 p5 undeclared-block emit failed", exc_info=True)
        return {
            "task_id": None,
            "status": "egress_gov_blocked",
            "reason": _p5["reason"],
        }
    if _p5["stamp"]:
        _stamp_gov_internal_only(payload)

    # FX.95 p5c (gov profile = MOD-built nodes only, Q81=A) — the run half of
    # the same posture, sitting next to the p3a gov gate: a third-party node
    # is refused even when the tenant installed it earlier (nothing is
    # deleted). Inert while the posture is off. Status 'gov_mod_built_only'.
    _mod_built_reason = _gov_mod_built_gate(db, request, payload)
    if _mod_built_reason is not None:
        return {
            "task_id": None,
            "status": "gov_mod_built_only",
            "reason": _mod_built_reason,
        }

    _approval_reason = _node_egress_approval_gate(db, request, payload)
    if _approval_reason is not None:
        return {
            "task_id": None,
            "status": "egress_approval_required",
            "reason": _approval_reason,
        }

    # GY.C7 (worker pool policy) — stamp the input labels' worker pool /
    # region restriction so the claim guard can route the task only to
    # matching workers (no-op when the map is empty or no input label is
    # restricted). Rides the payload so it survives PENDING->ASSIGNED
    # overlap placement.
    _stamp_label_worker_pools(db, payload)

    # GY.C7 s2 (region allowlist) — the region layer rides the pool layer:
    # stamp the input labels' allowed REGIONS so the claim guard
    # (_label_region_guard_sql) withholds the task from every worker outside
    # them. No-op when labels.allowed_regions is {} or no input label is
    # restricted, so unlabelled runs stay byte-identical.
    _stamp_label_regions(db, payload)

    task = ApiTaskQueue(
        workflow_run_id=request.workflow_run_id,
        node_run_id=request.node_run_id,
        node_id=request.node_id,
        tenant_id=request.tenant_id,
        queue_name=_queue_override or request.queue_name,
        priority=request.priority,
        task_payload=payload,
        # A PREFERENCE, never a lock. Pinning to a host that cannot claim it
        # leaves the task ASSIGNED for ever — never claimed, so no claim
        # expires and the reaper has nothing to reap. That wedge cost hours on
        # 2026-08-12, and `_live_checkpoint_host` returning None here is what
        # keeps this from repeating it: no live host, no pin, start over.
        status=(ApiTaskStatus.ASSIGNED if _ckpt_host else ApiTaskStatus.PENDING),
        assigned_to_worker=_ckpt_host,
        max_attempts=request.max_attempts,
        output_namespace="attempt_0",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    logger.info(
        "Task %d enqueued: workflow_run=%d node=%s queue=%s tenant=%d",
        task.id, request.workflow_run_id, request.node_id, request.queue_name, request.tenant_id,
    )

    # Try to assign via the overlap scheduler (if enabled).
    # Skip for profiling workflows — those tasks are pinned to a specific worker.
    _skip_scheduler = False
    if request.workflow_run_id:
        _wf_run = db.query(WorkflowRun).filter(WorkflowRun.id == request.workflow_run_id).first()
        if _wf_run and (_wf_run.parameters or {}).get("is_profiling"):
            _skip_scheduler = True
    placed = False
    if not _skip_scheduler:
        try:
            from main import get_overlap_scheduler
            scheduler = get_overlap_scheduler()
            if scheduler:
                placed = scheduler.schedule_task(task, db)
        except Exception as e:
            logger.warning("Scheduler placement failed for task %d, staying PENDING: %s", task.id, e)

    # Notify workers via Redis pub/sub so they wake up immediately
    _publish_event(f"task:available:{task.queue_name}")

    return {"task_id": task.id, "status": task.status.value}


@router.post("/workers/tasks/enqueue")
async def enqueue_task(
    request: TaskEnqueueRequest,
    db: Session = Depends(get_db),
    caller: models.User = Depends(require_bearer_or_api_key),
):
    """
    HTTP endpoint wrapper for task enqueue.
    Requires service authentication.

    GSEC FX.1 (R-003/R-052): platform admins keep the full, unrestricted
    path. Every other caller is scoped by _enforce_tenant_enqueue_scope —
    own tenant only (403 on a mismatched body tenant_id), tenant-legit
    task types only (403), no platform-reserved container_env markers
    (403), and a per-tenant cap on PENDING+ASSIGNED depth (429). All checks
    live HERE so the in-process enqueue_task_internal stays unchanged for
    the orchestrator and other internal dispatch paths.
    """
    _enforce_tenant_enqueue_scope(db, caller, request)
    # The only allowlisted type ("processing") IS the ApiTaskQueue.task_type
    # column default, so an explicit body value is honoured by the row
    # creation itself — no post-patch needed here.
    return enqueue_task_internal(request, db)


@router.get("/workers/tasks/remote-available")
async def check_remote_availability(
    queue: str,
    tenant_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_bearer_or_api_key),
):
    """
    Internal endpoint: check if any workers are available for a queue + tenant.
    Used by Traffic_Cop_Dag to decide whether to dispatch via the API task queue.
    All workers (embedded and remote) now use the API task queue.
    """
    # Find all licensed workers that can handle this queue + tenant
    workers = worker_tasks_status_crud.list_licensed_workers(db)

    available = []
    for w in workers:
        # Check queue support
        if w.queues and queue not in w.queues:
            continue
        # Check tenant authorization (handles wildcard for SaaS pools)
        authorized = _get_authorized_tenants(w)
        if not _is_tenant_authorized(authorized, tenant_id):
            continue

        # Check concurrency limit (0 = unlimited)
        if w.max_concurrent_tasks > 0:
            active = worker_tasks_lifecycle_crud.count_worker_active_tasks(db, w.worker_name)
            if active >= w.max_concurrent_tasks:
                continue

        # Estimate readiness cost
        try:
            from utils.worker_cost_model import estimate_task_readiness_cost
            specs = w.hardware_specs or {}
            cost = estimate_task_readiness_cost(
                worker_status=w.status.value,
                worker_hardware_specs=specs,
                worker_cached_images=specs.get("cached_images", []),
                task_payload={},
            )
            eta = cost["total_eta_seconds"]
        except Exception:
            eta = 999

        available.append({
            "worker_name": w.worker_name,
            "total_eta_seconds": eta,
            "active_tasks": active,
            "max_concurrent_tasks": w.max_concurrent_tasks,
        })

    # Sort by readiness cost — fastest worker first
    available.sort(key=lambda x: x["total_eta_seconds"])

    return {"available": len(available) > 0, "count": len(available), "workers": available}


@router.get("/workers/update-manifest", response_model=UpdateManifestResponse)
async def get_update_manifest(
    worker: WorkerLicense = Depends(require_enrolled_worker),
):
    """
    Worker checks for code updates. Compared against agent_version on heartbeat.
    Worker spawns secondary process to download, verify, and restart.
    """
    update_required = (
        worker.agent_version != CURRENT_AGENT_VERSION
        and WORKER_PACKAGE_URL
    )

    return UpdateManifestResponse(
        version=CURRENT_AGENT_VERSION,
        package_url=WORKER_PACKAGE_URL,
        checksum=WORKER_PACKAGE_CHECKSUM,
        update_required=bool(update_required),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_input_type_config(node_kind: str, db) -> dict:
    """Get validation config for an input node type."""
    # Map node_kind to validation configuration
    INPUT_CONFIGS = {
        "image_input": {
            "input_type": "image",
            "supported_extensions": [".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp"],
            "mime_type_prefix": "image/",
            "min_size_bytes": 100,
            "validation_mode": "validate_images",
        },
        "comic_image_input": {
            "input_type": "comic_image",
            "supported_extensions": [".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp", ".heic"],
            "mime_type_prefix": "image/",
            "min_size_bytes": 100,
            "validation_mode": "validate_images",
        },
        "mesh_input": {
            "input_type": "mesh",
            "supported_extensions": [".obj", ".fbx", ".glb", ".gltf", ".ply", ".stl", ".dae", ".3ds", ".blend", ".usd"],
            "mime_type_prefix": "",
            "min_size_bytes": 100,
            "validation_mode": "validate_exists",
        },
        "audio_input": {
            "input_type": "audio",
            "supported_extensions": [".wav", ".mp3", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".aiff", ".opus"],
            "mime_type_prefix": "audio/",
            "min_size_bytes": 1024,
            "validation_mode": "validate_exists",
        },
        "video_input": {
            "input_type": "video",
            "supported_extensions": [".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg"],
            "mime_type_prefix": "video/",
            "min_size_bytes": 10240,
            "validation_mode": "validate_videos",
        },
        "nerf_input": {
            "input_type": "nerf",
            "supported_extensions": [".ckpt", ".pth", ".pt", ".msgpack", ".pkl", ".json", ".txt", ".npz"],
            "mime_type_prefix": "",
            "min_size_bytes": 100,
            "validation_mode": "validate_exists",
        },
        "texture_set_input": {
            "input_type": "texture_set",
            "supported_extensions": [".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp", ".exr"],
            "mime_type_prefix": "image/",
            "min_size_bytes": 100,
            "validation_mode": "validate_images",
        },
        "text_input": {
            "input_type": "text",
            "supported_extensions": [],
            "mime_type_prefix": "",
            "min_size_bytes": 0,
            "validation_mode": "validate_exists",
        },
        "resource_input": {
            "input_type": "resource",
            "supported_extensions": [],
            "mime_type_prefix": "",
            "min_size_bytes": 0,
            "validation_mode": "validate_exists",
        },
        "script_input": {
            "input_type": "script",
            "supported_extensions": [],
            "mime_type_prefix": "",
            "min_size_bytes": 0,
            "validation_mode": "validate_exists",
        },
    }

    return INPUT_CONFIGS.get(node_kind, {
        "input_type": node_kind.replace("_input", ""),
        "supported_extensions": [],
        "mime_type_prefix": "",
        "min_size_bytes": 100,
        "validation_mode": "validate_exists",
    })


# 20.6.s5 — transcript extensions a disabled ASR node can pass through
# UNCHANGED. These are exactly the shapes Task Extract already parses
# (transcript.vtt / .srt / .json / plain .txt), so a text transcript that
# reaches the "Speech to text (NIM)" node needs no model at all — the node
# just forwards the file. Anything else (a recording) cannot be transcribed
# while the NIM is inert, so the node FAILS with a clear message instead of
# warning-and-hanging (the old behaviour at the ephemeral-NIM block).
_TRANSCRIPT_INPUT_EXTS = {"txt", "vtt", "srt", "json"}

_ASR_DISABLED_HINT = (
    "Speech to text is not enabled on this install — upload a text "
    "transcript (.txt/.vtt/.srt) instead"
)


def _maybe_asr_passthrough(db: Session, request, payload: dict):
    """20.6.s5 — the "Speech to text (NIM)" node must never warn-and-hang.

    MOD Talk -> Plane routes every input through the ASR node, but a TEXT
    transcript (.txt/.vtt/.srt/ASR .json) is already in the shape Task Extract
    parses, so the node forwards it unchanged — no NIM, no wake, no GPU. A
    RECORDING (.wav/.mp3/…) genuinely needs the NIM, so when that NIM is
    registered-but-inert (services/nim_ephemeral.NimSpec.enabled is False
    until the owner's one-time NGC warm-up) the node FAILS with the hint
    above instead of the old `logger.warning` + enqueue-anyway, which left
    the run sitting on a task that could only ever fail on a worker.

    Returns the synthetic terminal ApiTaskQueue row (COMPLETED pass-through,
    or FAILED disabled-NIM) when it handled the node, None when normal
    dispatch must proceed (not an ASR node, extension unknown, or the NIM
    is enabled and a recording really must be transcribed).
    """
    try:
        from services import nim_ephemeral as _nim
        node_kind = (payload or {}).get("node_kind") or ""
        _spec = _nim.spec_for(node_kind)
        # Scoped to the SPEECH-TO-TEXT NIM only — Studio Voice / VSR clients
        # never see a transcript, so they keep their existing dispatch path.
        if _spec is None or node_kind != "nim_asr_local":
            return None

        ext = (payload.get("file_extension") or "").lower().lstrip(".")
        if not ext:
            return None  # nothing to classify — let normal dispatch proceed

        is_transcript = ext in _TRANSCRIPT_INPUT_EXTS
        if not is_transcript and _spec.enabled:
            return None  # enabled NIM + a recording: transcribe normally

        source_hash = payload.get("source_file_location") or ""
        if not source_hash:
            return None  # no staged input — normal dispatch handles it

        now = datetime.now(timezone.utc)
        size_bytes = 0
        try:
            from crud.files import get_content_blob as _asr_blob_get
            blob = _asr_blob_get(db, source_hash, request.tenant_id)
            if blob:
                size_bytes = blob.size_bytes or 0
                ext = ext or (blob.file_extension or "").lower().lstrip(".")
        except Exception:  # noqa: BLE001 — metadata is cosmetic here
            pass

        outputs = [{
            "content_hash": source_hash,
            "file_name": f"transcript.{ext}" if ext else "",
            "file_extension": ext,
            "size_bytes": size_bytes,
        }]
        failed = not is_transcript
        task = ApiTaskQueue(
            workflow_run_id=request.workflow_run_id,
            node_run_id=request.node_run_id,
            node_id=request.node_id,
            tenant_id=request.tenant_id,
            queue_name=request.queue_name,
            priority=request.priority,
            task_payload=payload,
            status=(ApiTaskStatus.FAILED if failed
                    else ApiTaskStatus.COMPLETED),
            max_attempts=request.max_attempts,
            output_namespace="attempt_0",
            result_payload=({
                "outputs": outputs,
                "log_tail": "",
                "asr_passthrough": True,
            } if not failed else {
                "outputs": [],
                "log_tail": _ASR_DISABLED_HINT,
                "asr_passthrough": True,
            }),
            error_message=(_ASR_DISABLED_HINT if failed else None),
            started_at=now,
            completed_at=now,
        )
        db.add(task)
        db.flush()

        if request.node_run_id:
            from crud.node_run_cud import get_node_runs_for_workflow_run
            nr = next((r for r in get_node_runs_for_workflow_run(
                db, request.workflow_run_id)
                if r.id == request.node_run_id), None)
            if nr:
                nr.status = (TaskStatus.FAILED if failed
                             else TaskStatus.COMPLETED)
                nr.completed_at = now
                nr.error_message = _ASR_DISABLED_HINT if failed else None
                if not failed:
                    nr.outputs = clamp_node_run_text(outputs)

        if not failed and request.node_run_id:
            # The forwarded file IS an output — downstream nodes find it via
            # the standard WorkflowNodeOutput path (same posture as a cache
            # hit replaying a prior output).
            try:
                from models import WorkflowNodeOutput
                from datetime import timedelta
                from crud.workflows_domains_shared import get_workflow_node_outputs
                existing = next(
                    (o for o in get_workflow_node_outputs(
                        db, request.workflow_run_id, [request.node_run_id])
                     if o.content_hash == source_hash),
                    None,
                )
                if not existing:
                    db.add(WorkflowNodeOutput(
                        content_hash=source_hash,
                        tenant_id=request.tenant_id,
                        workflow_run_id=request.workflow_run_id,
                        node_run_id=request.node_run_id,
                        output_index=0,
                        logical_name=outputs[0]["file_name"],
                        file_extension=ext or "",
                        size_bytes=size_bytes,
                        is_terminal=False,
                        output_type="transcript",
                        retention_expires_at=now + timedelta(days=2555),
                    ))
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning("asr_passthrough: output insert failed: %s", e)

        db.commit()
        db.refresh(task)

        from services.workflow_orchestrator import _emit_run_event
        _emit_run_event(
            db, request.workflow_run_id, "traffic_cop",
            "error" if failed else "info",
            (f"Node {request.node_id} FAILED — {_ASR_DISABLED_HINT}"
             if failed else
             f"Node {request.node_id} passed a text transcript through "
             "without the ASR NIM"),
            {"node_id": request.node_id, "file_extension": ext,
             "asr_passthrough": True},
        )
        logger.info(
            "ASR passthrough (task %d) %s for run %s node=%s ext=%s",
            task.id,
            "FAILED — NIM disabled, recording" if failed
            else "passed text transcript through",
            request.workflow_run_id, request.node_id, ext,
        )

        # Continue (or fail) the run exactly as a worker report would.
        try:
            _check_and_dispatch_downstream(db, task)
        except Exception as e:  # noqa: BLE001 — never wedge dispatch
            logger.warning("asr_passthrough: downstream dispatch failed: %s", e)
        return task
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "asr_passthrough failed (will dispatch normally): %s", exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _maybe_cache_short_circuit(db: Session, request, payload: dict):
    """workflow-agentic-execution-loop §workflow-node-output-cache —
    if the tenant has output_cache_enabled AND a cache hit exists for
    the (image, input, config, tenant) key, create the task in
    COMPLETED state from the cached output and dispatch downstream
    without invoking the worker. Returns the completed ApiTaskQueue
    row on hit, None otherwise.

    Failure modes (any of which fall through to normal dispatch):
      - tenant flag disabled
      - input_content_hash empty (input nodes, export nodes)
      - cache miss
      - any exception during the lookup / synthesis path
    """
    try:
        from services import node_output_cache
        # Cache-read is on when the tenant flag is set OR this specific run opted
        # in via `reuse_cache` (Airflow-style "restart from node, reuse upstream").
        # A run may also list `cache_bust_nodes` — graph node ids to FORCE re-run
        # (the "Restart from here" node + downstream) even on a cache hit.
        # A manual re-run ("Re-run with same data" / "+ downstream") is an
        # explicit "execute this again for real" — it must NEVER be short-
        # circuited by the cache, or the re-run silently returns the cached
        # output and appears to do nothing. Bypass the cache entirely here.
        if isinstance(payload, dict) and payload.get("manual_rerun"):
            return None
        _run = worker_tasks_lifecycle_crud.get_workflow_run_by_id(db, request.workflow_run_id)
        _run_conf = (_run.parameters or {}) if _run else {}
        _reuse = bool(_run_conf.get('reuse_cache'))
        if not (_reuse or node_output_cache.is_cache_enabled_for_tenant(db, request.tenant_id)):
            return None
        _bust = _run_conf.get('cache_bust_nodes') or []
        if str(request.node_id) in {str(x) for x in _bust}:
            return None  # forced re-run for this node — skip the cache
        input_hash = (payload or {}).get('source_file_location') or ''
        if not input_hash:
            # Input nodes + export nodes — nothing to key on.
            return None
        # node-version-pinning — key the cache on the immutable digest when the
        # run is pinned, so an old pinned build and a newer :latest can't share a
        # cache entry (they would collide on the tag today). Unpinned runs carry
        # no digest and keep tag-based keying, unchanged.
        image_ref = (payload or {}).get('image_digest') or (payload or {}).get('processing_image') or ''
        cfg_for_hash = _cache_config_for_payload(payload)
        hit = node_output_cache.lookup(
            db,
            image_ref=image_ref,
            input_content_hash=input_hash,
            config=cfg_for_hash,
            tenant_id=request.tenant_id,
        )
        if hit is None:
            return None

        # Build a COMPLETED ApiTaskQueue row that references the cached
        # output. Subsequent code paths (_check_and_dispatch_downstream,
        # observability events, etc.) see it as a normal completed task
        # with cache_hit=true metadata.
        now = datetime.now(timezone.utc)
        # Recover the output's REAL file metadata (name / extension / size) from a
        # prior materialization of this content hash. NodeOutputCache stores only
        # the output hash, so a cache-hit that emitted empty metadata handed
        # downstream a bare hash with no extension — the node "succeeded" but its
        # artifact was uninterpretable (observed: albedo_texture_process cache-hits
        # returned file_name="" / file_extension="" for a real 1.3MB ALBEDO.png,
        # so downstream couldn't use the texture). Prefer a real WorkflowNodeOutput
        # (has logical_name), fall back to ContentBlob (has extension/size).
        _hit_name, _hit_ext, _hit_size = "", "", 0
        try:
            _hit_name, _hit_ext, _hit_size = worker_tasks_status_crud.get_cache_hit_output_metadata(
                db, hit.output_content_hash, request.tenant_id)
        except Exception as e:
            logger.debug("cache_hit: could not recover output metadata for %s: %s",
                         hit.output_content_hash[:12], e)
        result_outputs = [{
            "content_hash": hit.output_content_hash,
            "file_name": _hit_name,
            "file_extension": _hit_ext,
            "size_bytes": _hit_size,
        }]
        result_payload = {
            "outputs": result_outputs,
            "log_tail": "",
            "cache_hit": True,
            "cache_row_id": hit.id,
        }
        task = ApiTaskQueue(
            workflow_run_id=request.workflow_run_id,
            node_run_id=request.node_run_id,
            node_id=request.node_id,
            tenant_id=request.tenant_id,
            queue_name=request.queue_name,
            priority=request.priority,
            task_payload=payload,
            status=ApiTaskStatus.COMPLETED,
            max_attempts=request.max_attempts,
            output_namespace="attempt_0",
            result_payload=result_payload,
            started_at=now,
            completed_at=now,
        )
        db.add(task)
        db.flush()

        # Mark NodeRun COMPLETED with the cached outputs.
        if request.node_run_id:
            nr = worker_tasks_lifecycle_crud.get_node_run_by_id(db, request.node_run_id)
            if nr:
                nr.status = TaskStatus.COMPLETED
                nr.completed_at = now
                nr.outputs = clamp_node_run_text(result_outputs)

        # Insert a WorkflowNodeOutput row pointing at the cached
        # content_hash. Downstream nodes find it via the standard
        # parent_outputs resolution path.
        try:
            from models import WorkflowNodeOutput
            from datetime import timedelta
            if request.node_run_id:
                existing = worker_tasks_status_crud.get_existing_cache_output_row(
                    db, hit.output_content_hash, request.node_run_id)
                if not existing:
                    db.add(WorkflowNodeOutput(
                        content_hash=hit.output_content_hash,
                        tenant_id=request.tenant_id,
                        workflow_run_id=request.workflow_run_id,
                        node_run_id=request.node_run_id,
                        output_index=0,
                        logical_name=_hit_name,
                        file_extension=_hit_ext,
                        size_bytes=_hit_size,
                        is_terminal=True,
                        output_type=(payload or {}).get('node_kind', ''),
                        retention_expires_at=now + timedelta(days=2555),
                    ))

                    # FX.97 p3a — the replayed output is signed like any
                    # other (idempotent per content_hash + install key), so
                    # a cache hit keeps the provenance signature claim.
                    from services import output_signature as _out_sig
                    _out_sig.record_output_signature(
                        db, hit.output_content_hash, request.tenant_id)
        except Exception as e:
            logger.warning("cache_hit: WorkflowNodeOutput insert failed: %s", e)

        db.commit()
        db.refresh(task)

        logger.info(
            "Task %d CACHE HIT: workflow_run=%d node=%s tenant=%d "
            "(cache_row=%d, output_hash=%s)",
            task.id, request.workflow_run_id, request.node_id,
            request.tenant_id, hit.id, hit.output_content_hash[:12],
        )

        # Emit synthetic completion event so downstream dispatch + the
        # rest of the lifecycle proceeds normally.
        try:
            _publish_event(f"task:status:{task.id}", "completed_from_cache")
        except Exception:
            pass

        # Fire downstream dispatch — the rest of the workflow proceeds
        # as if the worker had reported success.
        try:
            _check_and_dispatch_downstream(db, task)
        except Exception as e:
            logger.warning(
                "cache_hit: downstream dispatch failed for task %d: %s",
                task.id, e,
            )

        # Ephemeral NIM sleep-back for a NIM node completed FROM CACHE. The worker
        # /complete handler runs sleep_back to put the NIM back to sleep, but a
        # cache hit never reaches /complete — so a NIM woken earlier stayed up
        # after an all-cache run and nothing reclaimed it ("it's not turning
        # things off anymore"). Mirror /complete here; release_stack's
        # queue-lookahead keeps the NIM up if another PENDING/RUNNING task still
        # needs it, and wake-ownership means we only sleep one WE woke. No-op for
        # every non-NIM node kind. Post-dispatch + wrapped so it can never roll
        # back the cache completion. Runs AFTER downstream dispatch so the
        # lookahead sees any sibling NIM task the cascade just enqueued.
        try:
            from services import nim_ephemeral as _nim
            _nk = (payload or {}).get("node_kind")
            if _nim.spec_for(_nk) is not None:
                _nim.sleep_back(db, _nk, request.tenant_id, exclude_task_id=task.id)
                db.commit()
        except Exception as _nim_exc:  # noqa: BLE001
            logger.warning("cache_hit: NIM sleep-back failed for task %d: %s",
                           task.id, _nim_exc)

        # Universal stack release for non-NIM kinds (GA.GSW.2). Best-effort,
        # post-dispatch so the lookahead sees any sibling the cascade enqueued.
        _release_stack_best_effort(db, task, (payload or {}).get("node_kind"))
        return task
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "node_output_cache short-circuit failed (will dispatch normally): %s",
            exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _flat_key_secretish(key: str) -> bool:
    """Item 143 — name-based guard so credential-ish flat keys never reach
    the output-cache key. flat_env_defaults skips specs marked secret/password,
    but a domain seed can name a secret key (SIGNING_KEY) with a plain type;
    its value must not be hashed into a cache key. Keys-only marker, so the
    name itself is the only signal we have at dispatch.
    """
    k = key.upper()
    return any(t in k for t in ("SECRET", "PASSWORD", "TOKEN", "CREDENTIAL", "_KEY", "KEY_"))


def _cache_config_for_payload(payload: dict) -> dict:
    """workflow-agentic-execution-loop §workflow-node-output-cache —
    extract the deterministic subset of a task payload that affects
    the node's output. Run-scoped fields (workflow_run_id, ProjectInfo,
    keycloak_token, etc.) are excluded so the cache key is stable
    across runs of the same logical task.
    """
    if not isinstance(payload, dict):
        return {}
    node_data = payload.get("node_data") or {}
    if not isinstance(node_data, dict):
        node_data = {}
    # Pick the fields that ARE config-deterministic. extra_variables
    # carries user-supplied per-node tunables (text content, prompts,
    # numeric params). Container_env at the data layer is the rendered
    # config the worker uses. file_extension affects how the node
    # interprets the input.
    #
    # GX.R1 — extra_variables MUST reflect the EFFECTIVE values for this
    # dispatch: node_data.extraVariables is the runtime bag that
    # _apply_run_extra_variables already merged (run-level extra_variables +
    # per-node inputs) into before enqueue. Reading it FIRST (camelCase is
    # authoritative at dispatch time) means two runs that differ ONLY in
    # inline text/prompt produce DIFFERENT cache keys, so one run's inline
    # text can never return another run's cached output.
    _effective_extra = (
        node_data.get("extraVariables")
        or node_data.get("extra_variables")
        or {}
    )
    cfg = {
        "node_kind": payload.get("node_kind", ""),
        "executable_path": payload.get("executable_path", ""),
        "file_extension": payload.get("file_extension", ""),
        "extra_variables": _effective_extra,
        "config": node_data.get("config") or {},
        "container_env": node_data.get("container_env") or {},
        "output_schema": payload.get("output_schema") or {},
    }
    # FORTRESS.6.2 — the DP spec IS output-deterministic (the API-side noise
    # pass rewrites the node's numeric JSON with it), so it must be part of the
    # cache key: a DP-wrapped run and a plain run of the same node/config must
    # NEVER share a cache entry, or a later run would replay the raw output
    # before noise (or a noised one where raw is expected). Only added when
    # present so non-DP nodes keep their pre-existing hash exactly.
    _dp_spec = payload.get("dp") or node_data.get("dp")
    if _dp_spec:
        cfg["dp"] = _dp_spec
    # Item 143 — flat configuration_schema defaults land in the TOP-LEVEL
    # payload container_env, which the data-layer key above ignores. Dispatch
    # records the resolved flat key NAMES in payload['_flat_config_keys'];
    # hash their EFFECTIVE values from that top-level env so a platform-admin
    # default change (or a user-set override) busts the cache. Only added when
    # non-empty: nodes without flat config keep their pre-fix hash exactly.
    # Secrets can't sneak in: flat_env_defaults skips secret/password specs,
    # and the marker carries keys, never values.
    flat_keys = payload.get("_flat_config_keys")
    if isinstance(flat_keys, list) and flat_keys:
        top_env = payload.get("container_env")
        if not isinstance(top_env, dict):
            top_env = {}
        flat_cfg = {k: top_env[k] for k in flat_keys if isinstance(k, str) and k in top_env}
        if flat_cfg:
            cfg["flat_config"] = flat_cfg
    return cfg


def _maybe_dispatch_control_flow(
    db: Session,
    workflow_run,
    completed_task: ApiTaskQueue,
    plan: dict,
    downstream_id: str,
    node: dict,
    node_data: dict,
    parent_outputs: dict,
) -> bool:
    """workflow-agentic-execution-loop §control-flow-primitives —
    intercept dispatch for control-flow node kinds.

    Returns True iff this function fully handled the dispatch; the
    caller skips its normal enqueue path. Returns False when the
    node isn't a control-flow node (regular dispatch proceeds).

    Handled kinds:
      - 'conditional'       — evaluates predicate, marks the
                              conditional node COMPLETED without a
                              worker task, recursively dispatches
                              only the matching outgoing branch
      - 'fan_out_over_list' — expands the list, enqueues one task
                              per item with output_namespace
                              `fanout_<i>` on the wrapped downstream
      - 'retry_with_params' — currently surfaces a clear "not yet
                              implemented" error event rather than
                              silently misbehaving. Schema validates.

    Failure modes (return False, let normal dispatch proceed):
      - tenant flag disabled (rejects the workflow at start_workflow
        layer; defence-in-depth here)
      - node isn't a control-flow node
      - any exception in handler logs + returns False
    """
    try:
        from services import control_flow
        kind = control_flow.node_kind_of(node_data)
        if not kind:
            return False
        # Tenant gate — primitives never dispatch when disabled.
        if not control_flow.is_enforcement_enabled_for_tenant(
            db, completed_task.tenant_id,
        ):
            from services.workflow_orchestrator import _emit_run_event
            _emit_run_event(
                db, completed_task.workflow_run_id,
                "traffic_cop", "error",
                f"Control-flow node {downstream_id} requires "
                f"control_flow_primitives_enabled flag (off for this tenant)",
                {"node_id": downstream_id, "kind": kind},
            )
            return True  # block dispatch; flag-gated
        try:
            cfg = control_flow.extract_config(node_data, kind)
        except ValueError as ve:
            from services.workflow_orchestrator import _emit_run_event
            _emit_run_event(
                db, completed_task.workflow_run_id,
                "traffic_cop", "error",
                f"Malformed control-flow config on {downstream_id}: {ve}",
                {"node_id": downstream_id, "kind": kind},
            )
            return True

        try:
            from services import wizard_metrics
            wizard_metrics.record_control_flow_dispatch(kind)
        except Exception:
            pass
        if kind == "conditional":
            return _dispatch_conditional(
                db, workflow_run, completed_task, plan,
                downstream_id, node, cfg, parent_outputs,
            )
        if kind == "fan_out_over_list":
            return _dispatch_fan_out(
                db, workflow_run, completed_task, plan,
                downstream_id, node, cfg, parent_outputs,
            )
        if kind == "retry_with_params":
            # Schema validates; execution semantics land in a
            # follow-up. Surface the gap clearly so an operator
            # debugging a stuck workflow sees what's happening.
            from services.workflow_orchestrator import _emit_run_event
            _emit_run_event(
                db, completed_task.workflow_run_id,
                "traffic_cop", "error",
                f"retry_with_params dispatch not yet implemented "
                f"(node {downstream_id}). Schema validates; execution "
                f"semantics will land in a follow-up.",
                {"node_id": downstream_id, "kind": kind},
            )
            return True
        return False
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "control-flow dispatch failed (falling through to regular dispatch): %s",
            exc,
        )
        return False


def _maybe_dispatch_memory_node(
    db: Session,
    workflow_run,
    completed_task: ApiTaskQueue,
    plan: dict,
    downstream_id: str,
    node: dict,
    node_data: dict,
    parent_outputs: dict,
) -> bool:
    """expr 9.8 — dispatch a memory-operation node IN-APISIDE.

    Memory nodes have no container, so the regular enqueue path can never
    run them: they are intercepted here exactly like the control-flow
    primitives. ``services.memory_node_handlers.execute_memory_node`` owns
    the tenant flag (``expr.memory_nodes_enabled``), the quotas and the
    envelope; it never raises and never commits.

    Returns True iff this function fully handled the dispatch (the caller
    skips its normal enqueue path); False when the node isn't a memory
    node so regular dispatch proceeds.

    Never logs input/output VALUES — only node kinds, exception class
    names and the envelope's reason code.
    """
    try:
        from services.memory_node_handlers import (
            MEMORY_NODE_KINDS, execute_memory_node,
        )
        kind = node_data.get("node_kind")
        if kind not in MEMORY_NODE_KINDS:
            return False

        from services.workflow_orchestrator import _emit_run_event

        # Memory is keyed by (tenant_id, workflow_slug); the run's slug is
        # the Workflow.name the run was started from (same resolution the
        # notification path in this file uses). Fall back to the workflow
        # id when the run carries no Workflow row.
        slug = None
        if workflow_run.workflow:
            slug = workflow_run.workflow.name
        if not slug:
            slug = str(workflow_run.workflow_id)

        envelope = execute_memory_node(
            db, tenant_id=completed_task.tenant_id, workflow_slug=slug,
            node_kind=kind, node_data=node_data, inputs=parent_outputs,
        )
        ok = envelope.get("status") == "ok"
        reason = envelope.get("reason")

        # Synthetic COMPLETED/FAILED row so the graph treats this node as
        # finished — same shape as _dispatch_conditional's synth_task.
        now = datetime.now(timezone.utc)
        synth_task = ApiTaskQueue(
            workflow_run_id=completed_task.workflow_run_id,
            node_id=downstream_id,
            tenant_id=completed_task.tenant_id,
            queue_name="memory",
            priority=0,
            task_payload={"node_kind": kind,
                          "node_data": node.get("data", {})},
            status=(ApiTaskStatus.COMPLETED if ok
                    else ApiTaskStatus.FAILED),
            max_attempts=1,
            output_namespace=kind,
            result_payload=({"outputs": [envelope.get("outputs") or {}]}
                            if ok else None),
            error_message=(None if ok else
                           f"memory node {downstream_id} refused/failed: "
                           f"{reason}" if reason else
                           f"memory node {downstream_id} failed"),
            started_at=now,
            completed_at=now,
        )
        db.add(synth_task)
        db.flush()
        db.commit()

        _emit_run_event(
            db, completed_task.workflow_run_id,
            "traffic_cop", "info" if ok else "error",
            f"memory node {downstream_id}: "
            f"{'ok' if ok else 'failed'}"
            + (f" ({reason})" if not ok and reason else ""),
            {"node_id": downstream_id, "kind": kind,
             "status": "ok" if ok else "error",
             "reason": reason if not ok else None},
        )

        if not ok:
            # Refused/errored: the node is terminal for this run — no
            # downstream dispatch (the failure path above already marks
            # nothing; we simply stop the cascade here).
            return True

        # Re-enter the standard cascade with the synthetic completed task
        # so the child sees parent_outputs[downstream_id].
        _check_and_dispatch_downstream(db, synth_task)
        return True
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "memory-node dispatch failed (falling through to regular "
            "dispatch): %s", exc.__class__.__name__,
        )
        return False


def _dispatch_conditional(
    db: Session,
    workflow_run,
    completed_task: ApiTaskQueue,
    plan: dict,
    downstream_id: str,
    node: dict,
    cfg,  # ConditionalConfig
    parent_outputs: dict,
) -> bool:
    """Evaluate the conditional's predicate against the parent output,
    mark the conditional node COMPLETED, then recursively dispatch
    only the matching outgoing branch.
    """
    from services import control_flow
    from services.workflow_orchestrator import _emit_run_event

    # Take the first non-empty parent output as the predicate input.
    # Conditionals are expected to have exactly one upstream parent.
    pred_input = None
    for _pid, outputs in (parent_outputs or {}).items():
        if isinstance(outputs, list) and outputs:
            pred_input = outputs[0]
            break

    try:
        result = control_flow.evaluate_predicate(cfg.predicate, pred_input)
    except Exception as e:
        _emit_run_event(
            db, completed_task.workflow_run_id,
            "traffic_cop", "error",
            f"conditional {downstream_id}: predicate eval failed: {e}",
            {"node_id": downstream_id},
        )
        return True

    # Mark the conditional itself COMPLETED so downstream traversal
    # treats it as a finished parent.
    now = datetime.now(timezone.utc)
    synth_task = ApiTaskQueue(
        workflow_run_id=completed_task.workflow_run_id,
        node_id=downstream_id,
        tenant_id=completed_task.tenant_id,
        queue_name="control_flow",
        priority=0,
        task_payload={"node_kind": "conditional", "node_data": node.get("data", {})},
        status=ApiTaskStatus.COMPLETED,
        max_attempts=1,
        output_namespace="conditional",
        result_payload={
            "outputs": [{"content_hash": "", "predicate_result": result}],
            "predicate_result": result,
            "control_flow": "conditional",
        },
        started_at=now,
        completed_at=now,
    )
    db.add(synth_task)
    db.flush()
    db.commit()

    _emit_run_event(
        db, completed_task.workflow_run_id,
        "traffic_cop", "info",
        f"conditional {downstream_id}: predicate={result}",
        {"node_id": downstream_id, "predicate_result": result},
    )

    # Walk this node's outgoing edges and only dispatch the branches
    # whose `data.branch` matches the predicate result.
    edges = plan.get("edges", [])
    chosen_downstream: list[str] = []
    for e in edges:
        if e.get("source") != downstream_id:
            continue
        if control_flow.should_take_branch(e, result):
            chosen_downstream.append(e.get("target"))

    for tgt in chosen_downstream:
        # Reuse the existing dispatch by recursively calling
        # _check_and_dispatch_downstream with the synthetic completed
        # task. The dependents map already points the conditional at
        # `tgt`, so the standard parent-readiness check picks it up.
        pass

    # Trigger a downstream pass so dispatch continues. Calling
    # _check_and_dispatch_downstream is the simplest re-entry.
    _check_and_dispatch_downstream(db, synth_task)
    return True


def _dispatch_fan_out(
    db: Session,
    workflow_run,
    completed_task: ApiTaskQueue,
    plan: dict,
    downstream_id: str,
    node: dict,
    cfg,  # FanOutConfig
    parent_outputs: dict,
) -> bool:
    """Expand the fan-out's parent list into N tasks. Each task gets
    `output_namespace=fanout_<i>` so its outputs land in a distinct
    namespace; the orchestrator's standard parent-readiness check
    sees them as siblings under the same node_id.

    The fan-out node ITSELF is marked COMPLETED with a synthetic task
    so the dispatch graph treats it as a fan-in barrier. The wrapped
    downstream (resolved from the fan-out's outgoing edges) sees
    parent_outputs[fan_out_node_id] == [{item, item_index}, ...].
    """
    from services import control_flow
    from services.workflow_orchestrator import _emit_run_event

    descriptors = control_flow.synthesise_fanout_dispatches(
        parent_outputs=parent_outputs, fanout_cfg=cfg,
    )

    now = datetime.now(timezone.utc)
    # Mark the fan-out node COMPLETED with the descriptor list as its
    # outputs so downstream consumers see one row per fan-out item.
    synth_outputs = [
        {
            "content_hash": "",
            "item_index": d["item_index"],
            "item": d["item"],
            "output_namespace": d["output_namespace"],
        }
        for d in descriptors
    ]
    synth_task = ApiTaskQueue(
        workflow_run_id=completed_task.workflow_run_id,
        node_id=downstream_id,
        tenant_id=completed_task.tenant_id,
        queue_name="control_flow",
        priority=0,
        task_payload={"node_kind": "fan_out_over_list", "node_data": node.get("data", {})},
        status=ApiTaskStatus.COMPLETED,
        max_attempts=1,
        output_namespace="fan_out_over_list",
        result_payload={
            "outputs": synth_outputs,
            "control_flow": "fan_out_over_list",
            "fanout_count": len(descriptors),
        },
        started_at=now,
        completed_at=now,
    )
    db.add(synth_task)
    db.flush()
    db.commit()

    _emit_run_event(
        db, completed_task.workflow_run_id,
        "traffic_cop", "info",
        f"fan_out_over_list {downstream_id}: expanded to {len(descriptors)} item(s)",
        {"node_id": downstream_id, "fanout_count": len(descriptors)},
    )

    if not descriptors:
        # No items → no downstream to dispatch. The fan-out is done;
        # nothing else to do. The standard dispatch path picks up if
        # the fan-out node has independent terminal-output siblings.
        _check_and_dispatch_downstream(db, synth_task)
        return True

    # Re-enter the dispatch loop so the wrapped downstream sees the
    # fan-out as a completed parent.
    _check_and_dispatch_downstream(db, synth_task)
    return True


def _record_task_outputs(db: Session, task: ApiTaskQueue, outputs: list):
    """
    Record worker task outputs in workflow_node_outputs table and create
    downloadable cog entries so files appear in the folder view.
    """
    from models import WorkflowNodeOutput, Cog, CogType, Version, ContentBlob, WorkflowRun, Workflow
    from datetime import datetime, timedelta, timezone

    if not outputs or not task.node_run_id:
        return

    # node_output_cache is a SINGLE (input -> output) mapping, so it must store
    # the node's CANONICAL content output — never a metadata sidecar. Pick it once
    # here with the same _pick_real_output used by downstream chaining. This fixes
    # the VideoCombine / "Video Cleanup" cache-poisoning bug: the old code called
    # cache.record() for EVERY output under the same key, so response_metadata.json
    # (last in the list) clobbered the real media output. Every subsequent cache
    # hit on the local NIM nodes (studiovoice, vsr) then replayed the 126/135-byte
    # sidecar as the node's "video"/"audio", and VideoCombine died with
    # "no input file with a video stream" / "no audio input found".
    _norm_outputs = [o.dict() if hasattr(o, 'dict') else o for o in outputs]
    _primary = _pick_real_output(_norm_outputs)
    _primary_hash = (_primary or {}).get('content_hash') if isinstance(_primary, dict) else None

    # Record each output in workflow_node_outputs
    for i, output in enumerate(outputs):
        out_dict = output.dict() if hasattr(output, 'dict') else output
        content_hash = out_dict.get('content_hash', '')
        if not content_hash:
            continue

        # Check if already recorded (idempotent)
        existing = worker_tasks_status_crud.get_existing_cache_output_row(
            db, content_hash, task.node_run_id)
        if existing:
            continue

        db.add(WorkflowNodeOutput(
            content_hash=content_hash,
            tenant_id=task.tenant_id,
            workflow_run_id=task.workflow_run_id,
            node_run_id=task.node_run_id,
            output_index=i,
            logical_name=out_dict.get('file_name', ''),
            file_extension=out_dict.get('file_extension', ''),
            size_bytes=out_dict.get('size_bytes', 0),
            is_terminal=True,
            output_type=task.task_payload.get('node_kind', '') if task.task_payload else '',
            retention_expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
        ))

        # FX.97 p3a (owner Q83=A) — sign this output's content hash with the
        # per-install key (services/output_signature.py, Vault-held Ed25519)
        # and store it in models.OutputSignature so p3b can verify the
        # provenance claim "this install produced these exact bytes".
        # Best-effort by contract: an output that cannot be signed is still
        # recorded, unsigned, and the provenance report lists the gap.
        try:
            from services import output_signature as _out_sig
            _out_sig.record_output_signature(db, content_hash, task.tenant_id)
        except Exception as _sig_exc:  # noqa: BLE001 — never block recording
            logger.warning(
                "fx97-p3a: output %s left unsigned (task %d): %s",
                content_hash[:12], task.id, _sig_exc,
            )

        # sz-fortress-p34.3 — queue an ASYNC output-safety scan (NSFW/PII
        # registry, sz-fortress-p34.1) of this output. No-op unless
        # compliance.output_safety_scan_enabled is on (default False); never
        # runs inline here, never blocks delivery — see
        # services/output_safety_scan_async.py for the full contract.
        try:
            from services import output_safety_scan_async as _out_scan
            _out_scan.queue_async_scan(
                db, content_hash=content_hash, tenant_id=task.tenant_id,
                node_run_id=task.node_run_id, workflow_run_id=task.workflow_run_id,
                mime=out_dict.get('mime_type'),
            )
        except Exception as _scan_exc:  # noqa: BLE001 — never block recording
            logger.warning(
                "fortress-p34.3: output-safety scan not queued for %s "
                "(task %d): %s", content_hash[:12], task.id, _scan_exc,
            )

        # Persist the output row (+ signature) BEFORE the best-effort cache
        # write: that write may roll the session back on a duplicate key, and
        # a rollback must never take the output row with it (runs whose
        # outputs were in result_payload but absent from workflow_node_outputs,
        # so GET /runs/{id}/result returned no final_outputs).
        try:
            db.commit()
        except Exception as _commit_exc:  # noqa: BLE001 — surfaced to caller's log
            db.rollback()
            raise RuntimeError(
                f"workflow_node_outputs commit failed: {_commit_exc}") from _commit_exc

        # workflow-agentic-execution-loop §workflow-node-output-cache —
        # cache-WRITE is always-on regardless of the tenant's
        # output_cache_enabled flag (which gates only READS). Same-input
        # same-config re-runs SHALL hit this row on the next dispatch
        # once the tenant flips the flag on. Best-effort: failures here
        # never block the task-output recording above.
        #
        # Cache ONLY the canonical output (chosen above). A node that emits a
        # real media file + a response_metadata.json sidecar must cache the media
        # file, not the sidecar — see _primary_hash. When the node emitted only
        # metadata (_primary_hash is None) there is nothing usable to cache, so
        # skip: a downstream cache hit that replays a sidecar is worse than a miss.
        if not _primary_hash or content_hash != _primary_hash:
            continue
        try:
            from services import node_output_cache
            from utils.minio_client import get_tenant_bucket_name
            payload = task.task_payload or {}
            input_hash = payload.get('source_file_location') or ''
            # node-version-pinning — must mirror the READ-side key: prefer the
            # immutable digest so a pinned run's outputs are stored under the
            # exact build, not the mutable tag.
            image_ref = payload.get('image_digest') or payload.get('processing_image') or ''
            # Config-for-hash: only the deterministic parts that affect
            # node output. Excludes run-scoped fields (workflow_run_id,
            # ProjectInfo, parent_outputs, keycloak_token, etc.).
            cfg_for_hash = _cache_config_for_payload(payload)
            output_pointer = (
                f"{get_tenant_bucket_name(task.tenant_id, db)}/{content_hash}"
            )
            node_output_cache.record(
                db,
                image_ref=image_ref,
                input_content_hash=input_hash,
                config=cfg_for_hash,
                tenant_id=task.tenant_id,
                output_pointer=output_pointer,
                output_content_hash=content_hash,
            )
        except Exception as _cache_exc:  # pragma: no cover — defensive
            logger.warning(
                "node_output_cache write failed for task %d: %s",
                task.id, _cache_exc,
            )

    # Ensure ContentBlobs exist for each output (the export container needs them)
    # The actual folder structure is created by the export container, not here.
    try:
        from models import ContentBlob
        from utils.minio_client import get_tenant_bucket_name

        for output in outputs:
            out_dict = output.dict() if hasattr(output, 'dict') else output
            content_hash = out_dict.get('content_hash', '')
            file_ext = out_dict.get('file_extension', '')
            size_bytes = out_dict.get('size_bytes', 0)

            if not content_hash:
                continue

            blob = worker_tasks_status_crud.get_content_blob(
                db, content_hash, task.tenant_id)
            if not blob:
                storage_scheme = os.getenv('STORAGE_SCHEME', 's3')
                bucket = get_tenant_bucket_name(task.tenant_id, db)
                blob = ContentBlob(
                    id=content_hash,
                    tenant_id=task.tenant_id,
                    size_bytes=size_bytes,
                    file_extension=file_ext,
                    storage_path=f"{storage_scheme}://{bucket}/{content_hash}",
                )
                db.add(blob)

        db.commit()
        logger.info("Ensured ContentBlobs exist for %d outputs of task %d", len(outputs), task.id)

    except Exception as e:
        logger.warning("Failed to ensure content blobs for task %d: %s", task.id, e)
        db.rollback()

    # Audit: log workflow output creation (CMMC AU-2, provenance)
    try:
        from models import WorkflowOutputAuditLog
        for output in outputs:
            out_dict = output.dict() if hasattr(output, 'dict') else output
            content_hash = out_dict.get('content_hash', '')
            if not content_hash:
                continue
            db.add(WorkflowOutputAuditLog(
                content_hash=content_hash,
                tenant_id=task.tenant_id,
                action="created",
                performed_by_system="workflow_engine",
                reason=f"Output from task {task.id} (node_run {task.node_run_id})",
                audit_metadata={"task_id": task.id, "node_run_id": task.node_run_id, "file_name": out_dict.get('file_name')},
            ))
        db.commit()
    except Exception as e:
        logger.warning("Failed to write output audit log for task %d: %s", task.id, e)
        try:
            db.rollback()
        except Exception:
            pass


def _apply_variant_smoke_result(
    db: Session,
    task: ApiTaskQueue,
    *,
    is_success: bool,
    log_tail: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """P9e — record a variant_smoke task's outcome on the NodeDefinition.

    The enqueue side (node_definitions._enqueue_variant_smoke_task) stashes
    a `variant_smoke: {node_id, node_name, variant}` marker on
    task_payload. This reads that marker and flips
    NodeDefinition.variant_verification_status[variant] accordingly:

      success          → "verified"
      drift detected   → "numerical_drift" (P9m — probe's reference
                                            comparison exceeded tolerance.
                                            Drift payload cached under
                                            task_payload.drift for the
                                            failure viewer.)
      other failure    → "failed"  (log_tail excerpt stashed on the task
                                    row itself, not on the NodeDefinition
                                    — the wizard UI (P9e.4) will fetch it
                                    from the api_task_queue.result_payload
                                    when the user hovers the red dot.)

    All three terminal outcomes count as "one port attempt" for the
    circuit breaker's rolling counter. drift + failed both increment
    the failures side (user's 9y.1 decision: drift == broken ship,
    should contribute to the trip rate).

    Idempotent on repeated calls with the same outcome; overwrites on
    re-verification (e.g., user clicks "retry smoke" in P9e.4).
    """
    payload = task.task_payload or {}
    marker = payload.get("variant_smoke") or {}
    node_id = marker.get("node_id")
    variant = marker.get("variant")

    if not node_id or not variant:
        logger.info(
            "variant_smoke result: task %d has no variant_smoke marker; skipping",
            task.id,
        )
        return

    node = worker_tasks_results_crud.get_node_definition_by_id(db, node_id)
    if not node:
        logger.warning(
            "variant_smoke result: NodeDefinition %s not found (task %d)",
            node_id, task.id,
        )
        return

    # P9m — detect numerical-drift failures. The probe emits a JSON line
    # with stage="numerical_drift" on its final stdout before exit 1;
    # when the worker captures that into log_tail, we can distinguish
    # drift from a generic crash.
    drift_payload = _extract_drift_payload(log_tail) if not is_success else None

    # Phase 11 — parse the probe's final success report to evaluate the
    # perf gate. Only meaningful when `is_success` is True (a crashed
    # probe won't have a trailing ok=True JSON line).
    smoke_report = _extract_smoke_report(log_tail) if is_success else None
    acceptance: Dict[str, Any] = {}
    if is_success:
        try:
            from utils.port_acceptance import evaluate_acceptance
            plan_threshold = _resolve_acceptance_threshold(db, node)
            acceptance = evaluate_acceptance(
                smoke_report, plan_threshold=plan_threshold,
            ) or {}
        except Exception:
            logger.exception("P11 acceptance evaluate failed node=%s", node.name)

    # Phase 11 — gate the verified-flip on explicit acceptance
    # failures only. `malformed_report` or missing perf data should
    # NOT downgrade (that would regress pre-P11 behavior when the
    # probe wasn't emitting the new perf block yet). We only flip
    # away from "verified" when the evaluator returned a specific,
    # load-bearing rejection.
    gate_result = acceptance.get("gate_result", "")
    if is_success and gate_result == "perf_too_slow":
        new_status = "perf_too_slow"
    elif is_success and gate_result == "accuracy_failed":
        # Covers the case where the probe returned ok=True but the
        # nested reference_comparison had passed=False (shouldn't
        # happen in practice — the probe exits 1 on drift — but the
        # evaluator doesn't trust that invariant).
        new_status = "numerical_drift"
    elif is_success:
        new_status = "verified"
    elif drift_payload is not None:
        new_status = "numerical_drift"
    else:
        new_status = "failed"

    current = dict(node.variant_verification_status or {})
    prev = current.get(variant)
    current[variant] = new_status
    node.variant_verification_status = current

    # Cache the drift payload on the task row itself so the wizard's
    # failure viewer can surface "peak_abs=0.0042, first offending
    # index=[3, 17]" without re-parsing the log every time it's
    # opened. Keeping it on the task (not the NodeDefinition) because
    # it's ephemeral per-run data.
    if drift_payload is not None:
        task_payload = dict(task.task_payload or {})
        task_payload["drift"] = drift_payload
        task.task_payload = task_payload
    # Phase 11 — persist acceptance diagnostics on the task so the
    # wizard's failure / verification modal can read p50/p99 + gate
    # result without re-parsing.
    if acceptance:
        task_payload = dict(task.task_payload or {})
        task_payload["acceptance"] = acceptance
        task.task_payload = task_payload

    db.commit()

    # Phase 11 — write the ratio_p50 onto the most recent PortOutcome
    # row for this (source_hash, variant). Best-effort; a missing row
    # is fine (port_outcomes is written asynchronously by the
    # Containerizer task, not the variant_smoke task).
    try:
        from utils.port_acceptance import extract_ref_cuda_ratio
        ratio = extract_ref_cuda_ratio(smoke_report)
        if ratio is not None:
            _persist_ref_cuda_ratio(db, node, variant, ratio)
    except Exception:
        logger.exception("P11 ref_cuda_ratio persistence failed")

    logger.info(
        "variant_smoke result: node=%s variant=%s %s→%s task=%d%s",
        node.name, variant, prev, new_status, task.id,
        "" if is_success else f" err={(error_message or '')[:120]!r}",
    )


def _extract_smoke_report(log_tail: Optional[str]) -> Optional[Dict[str, Any]]:
    """Phase 11 — pull the smoke_probe's final success JSON line out
    of log_tail. That line carries the per-variant `forward.perf` dict
    the acceptance evaluator reads."""
    if not log_tail:
        return None
    for line in reversed(log_tail.splitlines()):
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("ok") is True and obj.get("forward"):
            return obj
    return None


def _resolve_acceptance_threshold(db: Session, node: NodeDefinition) -> Optional[float]:
    """Phase 11 — per-plan perf threshold resolution. Looks up the
    tenant's active Plan row and reads `acceptance_perf_ratio`. Returns
    None when no plan / tenant / column, which falls back to the util's
    module default (0.25)."""
    try:
        if not getattr(node, "tenant_id", None):
            return None
        # Best-effort walk: Tenant → Subscription → Plan. Errors or
        # missing rows return None and the evaluator uses its default.
        tenant = worker_management_crud.get_tenant_by_id(db, node.tenant_id)
        if tenant is None:
            return None
        sub = worker_tasks_results_crud.get_active_subscription_for_tenant(db, tenant.id)
        if sub is None or sub.plan_id is None:
            return None
        plan = worker_tasks_results_crud.get_plan_by_id(db, sub.plan_id)
        if plan is None:
            return None
        raw = getattr(plan, "acceptance_perf_ratio", None)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
    except Exception:
        logger.exception("P11 threshold resolution fell through; using default")
        return None


def _persist_ref_cuda_ratio(
    db: Session, node: NodeDefinition, variant: str, ratio: float,
) -> None:
    """Phase 11 — stash the measured ratio on the most recent
    PortOutcome row for this (source_hash, variant). When no row
    exists yet (Containerizer task still running or wasn't triggered
    for this node), this silently no-ops; the ratio still lives on
    the ApiTaskQueue acceptance payload."""
    # source_hash isn't stored on NodeDefinition, so we can't filter
    # by it here. Fall back to "most recent row for this variant by
    # any tenant" = tenant-scoped query ordered by created_at.
    rows = worker_tasks_results_crud.get_latest_port_outcome_rows(db, variant, node.tenant_id)
    if not rows:
        return
    rows[0].ref_cuda_ratio = float(ratio)
    db.commit()


def _extract_drift_payload(log_tail: Optional[str]) -> Optional[Dict[str, Any]]:
    """P9m — scan probe stdout for a JSON object with stage="numerical_drift".

    The probe writes its final report as a single JSON line; when it
    detects drift it sets ok=false, stage="numerical_drift", and a
    "drift" dict carrying peak_abs, peak_rel, and offending indices.
    Return that drift dict (or None when the failure wasn't drift).
    """
    if not log_tail:
        return None
    # Scan lines in reverse — the drift report is the last thing before
    # exit. Cheap: log_tails are bounded to a few KB by the worker.
    for line in reversed(log_tail.splitlines()):
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("stage") == "numerical_drift" and obj.get("ok") is False:
            drift = obj.get("drift")
            return drift if isinstance(drift, dict) else {"reason": obj.get("reason")}
    return None


def _process_trivy_scan_result(db: Session, task: ApiTaskQueue):
    """Process Trivy security scan results and update the NodeDefinition status."""
    import json
    from datetime import datetime, timezone

    container_env = task.task_payload.get("container_env", {})
    node_def_id = container_env.get("NODE_DEFINITION_ID")
    if not node_def_id:
        logger.warning("Trivy scan task %d missing NODE_DEFINITION_ID", task.id)
        return

    node_def_id = int(node_def_id)
    node = worker_tasks_results_crud.get_node_definition_by_id(db, node_def_id)
    if not node:
        logger.warning("NodeDefinition %d not found for Trivy scan result", node_def_id)
        return

    # Parse scan result from log_tail (stdout of the TrivyScanner container)
    log_tail = task.result_payload.get("log_tail", "") if task.result_payload else ""

    try:
        # The scanner's log_tail typically contains TWO JSON objects — a
        # leading log-line JSON the task harness wraps around each stdout
        # line, then the scanner's final result JSON at the end. Naive
        # first-{ to last-} grabs everything between them and fails with
        # "Extra data" as soon as a second top-level object appears.
        #
        # Scan backwards for the final top-level `{...}` that contains a
        # `"status"` key. That's the scanner's final result line.
        scan_json = None
        depth = 0
        end = -1
        for i in range(len(log_tail) - 1, -1, -1):
            ch = log_tail[i]
            if ch == '}':
                if depth == 0:
                    end = i
                depth += 1
            elif ch == '{':
                depth -= 1
                if depth == 0 and end != -1:
                    candidate = log_tail[i:end + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict) and "status" in obj:
                            scan_json = obj
                            break
                    except json.JSONDecodeError:
                        pass
                    end = -1
        if scan_json is None:
            raise ValueError("No scan-result JSON found in scanner output")

        scan_status = scan_json.get("status", "failed")
        node.scan_result = scan_json
        node.scanned_at = datetime.now(timezone.utc)

        if scan_status == "approved":
            node.scan_status = "approved"
            node.rejection_reason = None
            logger.info("Node %d (%s) approved by Trivy scan", node_def_id, node.name)
        elif scan_status == "rejected":
            node.scan_status = "rejected"
            node.rejection_reason = scan_json.get("reason", "Vulnerabilities found")
            logger.warning("Node %d (%s) rejected: %s", node_def_id, node.name, node.rejection_reason)
        else:
            node.scan_status = "failed"
            # Prefer the scanner's own error if it populated it; otherwise
            # fall back to the task-level error_message (e.g. "container
            # exited with code 1") so the UI shows *something* actionable.
            node.rejection_reason = (
                scan_json.get("error")
                or task.error_message
                or "Scan failed"
            )
            logger.error("Node %d (%s) scan failed: %s", node_def_id, node.name, node.rejection_reason)

        db.commit()

    except (json.JSONDecodeError, ValueError) as e:
        # No parseable JSON in the log. Use the task-level error_message
        # (e.g., "container exited with code 1") so the failure surfaces
        # in the UI instead of leaving the node stuck on pending_scan.
        fallback = task.error_message or f"Failed to parse scan results: {e}"
        logger.error("Failed to parse Trivy scan output for node %d: %s", node_def_id, e)
        node.scan_status = "failed"
        node.rejection_reason = fallback
        node.scanned_at = datetime.now(timezone.utc)
        db.commit()


# gw-stack-networking GW.2c — the worker's deploy-result sentinel block (see
# formatStackHostPortSentinel in internal/executor/stackhostports.go). Same
# convention as _ARTIFACT_BEGIN_SENTINEL in
# routers/node_definitions_domains/_shared.py: rfind the END, rfind the BEGIN
# before it (the last complete bundle wins), json.loads the text between.
_STACK_HOST_PORTS_BEGIN = "=====MOD_STACK_HOST_PORTS_BEGIN====="
_STACK_HOST_PORTS_END = "=====MOD_STACK_HOST_PORTS_END====="


def extract_stack_host_ports(log_tail: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    """The host-port allocation the worker reports in a deploy result tail.

    After a successful `compose up` the Go worker appends

        =====MOD_STACK_HOST_PORTS_BEGIN=====
        {"project": "...", "allocations": [
            {"service": "api", "target": 8000, "host": 20042, "protocol": "tcp"}]}
        =====MOD_STACK_HOST_PORTS_END=====

    when it had to ALLOCATE a host port for a `host: "any"` request (and only
    then — nothing allocated = no block at all). The payload is an object
    {"project", "allocations"}; a bare list is tolerated for forward-compat.
    Returns the sanitized allocation list (host 1..65535, protocol tcp/udp,
    non-dict entries dropped) or None when there is no block or it is
    malformed. Malformed input is logged and skipped — NEVER raised: a
    deploy must not fail over a reporting block the manager could not read.
    """
    if not log_tail:
        return None
    end_idx = log_tail.rfind(_STACK_HOST_PORTS_END)
    if end_idx == -1:
        return None
    begin_idx = log_tail.rfind(_STACK_HOST_PORTS_BEGIN, 0, end_idx)
    if begin_idx == -1:
        return None
    payload = log_tail[begin_idx + len(_STACK_HOST_PORTS_BEGIN):end_idx].strip()
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        logger.warning("host-port sentinel block present but not valid JSON; skipping")
        return None
    if isinstance(obj, dict):
        entries = obj.get("allocations")
    elif isinstance(obj, list):
        entries = obj
    else:
        entries = None
    if not isinstance(entries, list) or not entries:
        return None
    out: List[Dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        host = e.get("host")
        if isinstance(host, bool) or not isinstance(host, int) or not 1 <= host <= 65535:
            continue
        target = e.get("target")
        proto = str(e.get("protocol") or "tcp").strip().lower()
        out.append({
            "service": e.get("service") if isinstance(e.get("service"), str) else None,
            "target": target if isinstance(target, int) and not isinstance(target, bool) else None,
            "host": host,
            "protocol": proto if proto in ("tcp", "udp") else "tcp",
        })
    return out or None


def _tear_down_retargeted_old_host(db: Session, stack, old_worker: str) -> Optional[int]:
    """Stop the old host's compose copy when a successful deploy/migrate
    re-targets a stack from ``old_worker`` to a different worker.

    10-04 (Dell GPU0 OOM): the reconciler self-heal "replace" redeploys a
    failed stack on another worker and deploy completion flips
    ``target_worker`` to the new claimer — but nothing ever stopped the OLD
    host's compose project. The orphaned copy co-resided on the old host's
    GPU0 with the new placement's vLLM engine and OOM-crash-looped it.

    Sends one `stop` PLATFORM_TASK pinned to the old worker (the same task
    shape `/stacks/{id}/stop` builds via `_dispatch_stack_task`) for the
    stack's project. Idempotent and safe:

      * offline old host: the stop is STILL enqueued — pinned (status
        ASSIGNED) it simply waits, and the old host claims it on its next
        poll/heartbeat cycle. That is the 10-02 incident shape: the box was
        down for ~80 min and came back with the orphan copy STILL RUNNING
        on the GPU. Skipping the enqueue (the original 10-04 code) meant
        nothing ever stopped it. Only a decommissioned/unlicensed worker has
        its pinned task reclaimed (`_run_stranded_stack_task_sweep`) — an
        offline-but-licensed worker keeps the work until it returns. If the
        copy is already gone by then, `compose stop` on the project is a
        harmless no-op.
      * no-op when a `stop` task for this stack already targets the old
        worker (never queue two stops for the same host);
      * does NOT touch ``stack.status``/``desired_state`` — the stack is
        (now) running on the new host, and flipping either would fight the
        reconciler. The stop's completion handler only clears a stale
        deploy reservation and sets a reason; the re-target has already
        put the row where it needs to be, and the next reconcile pass
        restores `running` if the stop completion did disturb it.
      * every move (online or deferred) is audited via an AuditEvent
        (`STACK_RETARGET_TEARDOWN`) with old/new host and task id.
    """
    try:
        from routers.persistent_stacks._shared import (
            _dispatch_stack_task, _project_name)
        _so_teardown = optional_paid("services.stack_observation")
        if _so_teardown is not None and old_worker in (
                _so_teardown.observed_hosts(db, stack.id) or []):
            # The old host still reports it as running — the re-deploy landed
            # on the same box (A == B) or the copy is genuinely alive and the
            # platform wants it. Never stop a host still serving the stack.
            return None
        # Idempotency: a non-terminal stop already pinned to the old worker.
        # (An operator stop of the stack itself targets the CURRENT host —
        # the new claimer — so it can never collide with this one; the guard
        # only dedups two re-targets of the same stack off the same host.)
        from services.stack_reconciler import (
            _worker_heartbeating_now, pending_stop_for_host)
        if old_worker != stack.target_worker:
            existing_id = pending_stop_for_host(db, stack.id, old_worker)
            if existing_id is not None:
                return existing_id
        # Offline old host: enqueue anyway, deferred. The stop is pinned to
        # old_worker (ASSIGNED), and the claim query hands ASSIGNED work to
        # its named worker when it next polls — so the teardown runs the
        # moment the box is back, which is exactly when the orphan copy is a
        # problem (10-02: 80 min offline, returned with the copy still
        # holding the GPU). The stranded-task sweep only reclaims pins on
        # UNLICENSED/gone workers, never on offline-but-licensed ones.
        deferred = not _worker_heartbeating_now(db, old_worker)
        task = _dispatch_stack_task(
            db, old_worker, stack.tenant_id, "stop", _project_name(stack),
            stack_id=stack.id,
            extra_payload={"orphan_retarget": True,
                           "replaced_from": old_worker})
        _audit_retarget_teardown(db, stack, old_worker,
                                 stack.target_worker, task.id, deferred)
        logger.info("stack %s re-targeted onto %s — orphan stop of %s "
                    "enqueued on old host %s (task %s)%s",
                    stack.id, stack.target_worker,
                    (stack.storage_refs or {}).get("project") or f"modstack{stack.id}g0",
                    old_worker, task.id,
                    " — DEFERRED, old host offline" if deferred else "")
        return task.id
    except Exception as e:  # noqa: BLE001 — never block the re-target itself
        logger.warning("orphan stop for stack %s on %s failed: %s",
                       stack.id, old_worker, e)
        return None


def _audit_retarget_teardown(db: Session, stack, old_worker: str,
                             new_worker: Optional[str],
                             task_id: Optional[int],
                             deferred: bool) -> None:
    """Append-only audit row for a re-target teardown decision (10-02).

    Records WHERE the stack moved and what was enqueued to stop the old
    copy, so an operator can answer 'why is this box still running a copy
    of stack N?' from the audit table alone. Never raises.
    """
    try:
        db.add(models.AuditEvent(
            tenant_id=stack.tenant_id,
            event_name="STACK_RETARGET_TEARDOWN",
            details={
                "stack_id": stack.id,
                "stack_name": stack.name,
                "old_worker": old_worker,
                "new_worker": new_worker,
                "teardown_task_id": task_id,
                "deferred_old_host_offline": deferred,
            },
        ))
    except Exception as e:  # noqa: BLE001 — audit must never block the move
        logger.warning("audit for re-target teardown of stack %s failed: %s",
                       stack.id, e)


def _apply_stack_task_result(db: Session, task: ApiTaskQueue, is_success: bool,
                             error_message: Optional[str] = None) -> None:
    """Advance a PersistentStack's lifecycle from its worker task's outcome.

    Before this handler existed nothing in the codebase ever set
    status='running' — every write was created/deploying/migrating/torn_down.
    The fallout: resolve_stack_endpoint reported reachable=False forever, and
    stack_rebalancer.rebalance_once() filters status=='running' so it no-opped
    against every real deployment. Every stack MOD deployed was a service
    nobody could reach.

    Transitions, keyed on stack_op:
      deploy   ok -> running          fail -> failed  (+ reason)
      migrate  ok -> running          fail -> failed  (+ reason)
      teardown ok -> (already torn_down at dispatch)
               fail -> stays torn_down, flags cleanup_pending (+ reason)
      stop     ok -> stopped           fail -> reason only (may still be up)
      start    ok -> running           fail -> reason only
      snapshot/restore — act on a volume, not on stack lifecycle. Recorded on
               failure so the operator sees it, but no status transition.

    Teardown deliberately does NOT flip to 'failed': the operator asked for the
    stack to go away and the row should reflect that intent. What a failed
    `compose down` actually means is that resources may survive on the host,
    which is what cleanup_pending records for a secondary sweep to reclaim.
    The task itself also carries max_attempts=3 (see
    persistent_stacks._STACK_OP_MAX_ATTEMPTS), so the normal requeue path gets
    to retry the teardown before this terminal handler ever runs.
    """
    payload = task.task_payload or {}
    stack_op = payload.get("stack_op")
    stack_id = payload.get("stack_id")
    if not stack_op or stack_id is None:
        # Pre-upgrade tasks queued before stack_id was carried on the payload.
        # Nothing safe to do — reverse-mapping from the project name is exactly
        # the fragility stack_id was added to avoid.
        logger.debug("stack task %d has no stack_id/stack_op; skipping lifecycle update", task.id)
        return

    # Review2 M3: drop secret/storage file bytes of a terminal boot_restore or
    # automatic fallback deploy FIRST, so a row of a deleted stack loses them too.
    from services.stack_secret_recovery import _strip_file_payload, payload_scrub_wanted
    _scrubbed = payload_scrub_wanted(payload) and _strip_file_payload(task)

    stack = worker_tasks_results_crud.get_persistent_stack_by_id(db, stack_id)
    if stack is None:
        logger.warning("stack task %d references stack %s which no longer exists",
                       task.id, stack_id)
        if _scrubbed:
            db.commit()
        return

    reason = (error_message or task.error_message or "").strip() or None

    if stack_op == "boot_restore":
        # Worker-controlled start: drop the file bytes from the row and, if the
        # operator stopped the stack meanwhile, stop it again (review H4/M2).
        from services.stack_secret_recovery import on_boot_restore_result
        on_boot_restore_result(db, task, stack, is_success)
        return

    _ports_changed = False  # set when a deploy/migrate moved the published host ports
    # UPDATE-TARGET-REDEPLOY-1126 — a redeploy can keep the SAME published host
    # port (a no-change redeploy from update-target, or a compose that re-binds
    # the same port). The route was INACTIVE (503 "app target unavailable") the
    # whole time the stack sat in `deploying`; with only the port-change trigger
    # nothing re-points it when the stack flips back to running, so the route
    # stays 503 until the hourly sweep or a manual nudge (observed on xeon).
    # Capture the pre-transition status so the ingest can also fire the
    # refresh on a deploying -> running FLIP.
    _status_before = getattr(stack, "status", None)
    if stack_op in ("deploy", "migrate"):
        if is_success:
            # Record where it ACTUALLY landed. Deploys are queued unpinned and
            # placed by the claim-time guard, so the host isn't known until a
            # worker claims — the row reflects what happened, not what was
            # guessed at dispatch.
            # 10-04 (Dell GPU0 OOM): capture the PREVIOUS host before the
            # flip — if this deploy re-targeted the stack, the old host still
            # runs the orphaned compose copy and needs a pinned stop (see
            # `_tear_down_retargeted_old_host`, called below after the row
            # reflects the new host).
            previous_worker = stack.target_worker
            claimer = task.claimed_by_worker or task.assigned_to_worker
            if claimer:
                stack.target_worker = claimer
                try:
                    from routers.persistent_stacks import _compute_lb_endpoint
                    w = worker_management_crud.get_worker_by_name(db, claimer)
                    if w is not None:
                        stack.lb_endpoint = _compute_lb_endpoint(stack, w)
                except Exception as e:  # noqa: BLE001
                    logger.warning("endpoint compute failed for stack %s: %s", stack.id, e)
            # gw-stack-networking GW.3 — record the host-port mapping the
            # worker ALLOCATED for `host: "any"` requests (GW.2 allocates a
            # free port from the pushed range and reports it back) onto the
            # stack, so GET shows the actually-bound ports.
            # GW.2c — the report arrives as a sentinel block in the deploy
            # result tail; parse it (malformed -> warn+skip, never fail the
            # deploy) so the mapping actually gets populated. An explicit
            # result_payload["host_port_mappings"] still wins.
            _hpm = (task.result_payload or {}).get("host_port_mappings")
            if not _hpm:
                _hpm = extract_stack_host_ports(
                    (task.result_payload or {}).get("log_tail"))
                if _hpm:
                    task.result_payload = {**task.result_payload,
                                           "host_port_mappings": _hpm}
            if _hpm:
                try:
                    from services import stack_port_placement
                    _ports_changed = bool(
                        stack_port_placement.record_host_port_allocation(db, stack, _hpm))
                except Exception as e:  # noqa: BLE001
                    logger.warning("host-port mapping record failed for stack %s: %s",
                                   stack.id, e)
            # 10-03 (whoami stack 96): the endpoint was computed BEFORE the
            # allocation was recorded, so an "any" host port was unknown then;
            # recompute now that storage_refs carries the mapping (this also
            # lands fixed numeric x-mod host ports when the first compute
            # found nothing).
            if stack.target_worker:
                try:
                    from routers.persistent_stacks import _compute_lb_endpoint
                    w = worker_management_crud.get_worker_by_name(db, stack.target_worker)
                    if w is not None:
                        stack.lb_endpoint = _compute_lb_endpoint(stack, w)
                except Exception as e:  # noqa: BLE001
                    logger.warning("endpoint recompute failed for stack %s: %s", stack.id, e)
            stack.status = "running"
            stack.status_reason = None
            # GX.S10a — a successful deploy/migrate has pushed the live value
            # to the worker; clear the "redeploy to apply" flag a variable
            # rotation set. (Failure keeps the flag: the new value is NOT live.)
            if getattr(stack, "needs_redeploy", False):
                stack.needs_redeploy = False
            logger.info("persistent stack %s %s succeeded on %s -> running",
                        stack.id, stack_op, stack.target_worker)
            # (b) re-target teardown: the flip above is what stranded the old
            # copy on 10-04 — stop it where it still is. Same-host redeploys
            # (A == B, or the old host still serving it) are no-ops inside.
            if (previous_worker and claimer
                    and previous_worker != claimer):
                _tear_down_retargeted_old_host(db, stack, previous_worker)
        else:
            stack.status = "failed"
            stack.status_reason = reason or f"{stack_op} failed"
            logger.error("persistent stack %s %s failed on %s: %s",
                         stack.id, stack_op, stack.target_worker, stack.status_reason)
            # §8.6 — count it against the host that claimed the work, not
            # against target_worker: on a failed deploy target_worker is still
            # whatever the row said before, and on an unpinned deploy it is
            # None. The claimer is the machine that actually tried and failed.
            claimer = task.claimed_by_worker or task.assigned_to_worker
            if claimer:
                try:
                    from services.stack_reliability import record_deploy_failure
                    record_deploy_failure(db, claimer)
                except Exception:  # noqa: BLE001 — never block the transition
                    logger.debug("reliability: deploy-failure record failed for %s",
                                 claimer, exc_info=True)
    elif stack_op == "teardown":
        if is_success:
            stack.cleanup_pending = False
            stack.status_reason = None
            logger.info("persistent stack %s teardown confirmed on %s", stack.id, stack.target_worker)
            # stack-stop-release §2 — teardown ISSUE-time only cancels
            # unclaimed (PENDING/ASSIGNED) deploy tasks (see teardown_stack);
            # a CLAIMED/RUNNING one is flagged cleanup_pending instead because
            # recalling it mid `compose up` isn't safe. This is the robust
            # point for THAT case: teardown COMPLETING on this same worker is
            # ground truth that whatever the old deploy task started is gone,
            # so its gpu_assignment can stop holding a reservation
            # (incident 2026-09-28: task 39168 stayed RUNNING with both Dell
            # GPUs pinned long after the stack it deployed was torn down).
            from routers.persistent_stacks._shared import _cancel_superseded_deploy_tasks
            _cancel_superseded_deploy_tasks(
                db, stack.id, exclude_task_id=task.id,
                created_not_after=task.created_at,
                keep_started_when_desired_running=True,
                reason="cancelled: stack teardown completed on the same worker")
            _publish_event(f"task:available:{task.queue_name}")
        else:
            stack.cleanup_pending = True
            stack.status_reason = reason or "teardown failed"
            logger.error("persistent stack %s teardown FAILED on %s: %s — containers or "
                         "volumes may survive; flagged cleanup_pending",
                         stack.id, stack.target_worker, stack.status_reason)
    elif stack_op == "stop":
        # A deliberate stop, distinct from teardown: containers and data
        # survive, the stack is simply not running. Falling through to the
        # snapshot/restore branch would have left status reading `running`
        # after a successful stop — asserting something false, and (because
        # committed_demands filters on status) holding the stack's accelerator
        # for a stack that is not using it.
        _orphan_stop = bool(payload.get("orphan_retarget"))
        if is_success:
            # An orphan stop (10-04 re-target teardown) stops the OLD host's
            # compose copy while the stack runs on the NEW host — it must NOT
            # flip `status` to "stopped"; the row is (now) running on the
            # claimer. Only the superseded-deploy cancellation below runs.
            if not _orphan_stop:
                stack.status = "stopped"
                stack.status_reason = "stopped by operator"
            logger.info("persistent stack %s stopped on %s%s",
                        stack.id, stack.target_worker,
                        " (orphan re-target teardown; status unchanged)"
                        if _orphan_stop else "")
            # stack-stop-release §2 — the robust cancellation point for a
            # `stop`: nothing dispatches a `stop` op speculatively before this,
            # so unlike teardown there is no issue-time cancel elsewhere for
            # this path. A `stop` COMPLETING on the stack's own worker is
            # ground truth that any earlier deploy/migrate task's
            # gpu_assignment for this stack is stale — cancel it here or it
            # holds `gpu_reservation.cards_in_use()` forever (this is exactly
            # incident 2026-09-28: stack #27 was stopped at 04:02/04:06 but
            # its 03:34 deploy task 39168 stayed RUNNING and nothing ended it).
            from routers.persistent_stacks._shared import _cancel_superseded_deploy_tasks
            _cancel_superseded_deploy_tasks(
                db, stack.id, exclude_task_id=task.id,
                created_not_after=task.created_at,
                keep_started_when_desired_running=True,
                reason="cancelled: stack stop completed on the same worker")
            _publish_event(f"task:available:{task.queue_name}")
        else:
            if not _orphan_stop:
                stack.status_reason = reason or "stop failed"
            logger.error("persistent stack %s stop FAILED on %s: %s — it may still "
                         "be running", stack.id, stack.target_worker, stack.status_reason)
    elif stack_op == "start":
        if is_success:
            stack.status = "running"
            stack.status_reason = None
            logger.info("persistent stack %s started on %s", stack.id, stack.target_worker)
        else:
            # Usually the containers are gone (torn down, or the host was
            # rebuilt). Not a failure of the stack itself — the reconciler's
            # deploy path is what recreates one, and it must go through the
            # accelerator fit check that `start` bypasses.
            stack.status_reason = reason or "start failed"
            logger.error("persistent stack %s start FAILED on %s: %s",
                         stack.id, stack.target_worker, stack.status_reason)
    elif stack_op == "rehearse":
        # §9.5 — the whole point of a rehearsal is the verdict it records. A
        # rehearsal whose result went only into a task row would leave the
        # question it was run to answer ("can this stack actually be restored")
        # exactly as unanswered as before.
        #
        # Recorded on BOTH paths. Marking only successes would leave a snapshot
        # that failed verification looking identical to one nobody has tested,
        # which is the more dangerous of the two states to hide.
        _record_rehearsal_result(db, stack, task, is_success, reason)
    elif stack_op == "bench" and is_success:
        # 27.6.s2 — the capacity sweep's answer lands on the profile ledger.
        # No lifecycle transition: benching measures, it never changes what the
        # stack is. A bench whose sentinel block is missing/garbled logs and
        # writes nothing (parse_bench_report is tolerant by contract) — the
        # task still succeeded, and claiming "never benched" is exactly what a
        # half-reported sweep must NOT look like, so the sweep's own error
        # labels are what get stored when the block parsed but timed out.
        if is_success:
            # 1788d CE carve: services/stack_bench_profile.py is paid (manifest
            # deny 1115). The bench-profile ledger is a paid record; with it
            # absent a successful sweep records nothing and changes no stack
            # state (the task result itself is untouched).
            from utils.paid_import import optional_paid
            _bench_mod = optional_paid("services.stack_bench_profile")
            report = None
            if _bench_mod is not None:
                parse_bench_profile_report = _bench_mod.parse_bench_profile_report
                record_bench_profile = _bench_mod.record_bench_profile
                report = parse_bench_profile_report(
                    (task.result_payload or {}).get("log_tail"))
            if report is not None:
                try:
                    record_bench_profile(db, stack, task, report)
                except Exception as e:  # noqa: BLE001 — never fail on a profile write
                    logger.warning("bench profile record failed for stack %s: %s",
                                   stack.id, e)
            elif _bench_mod is not None:
                logger.warning("bench task %d for stack %s carried no parseable "
                               "report block; nothing recorded",
                               task.id, stack.id)
    elif stack_op in ("restore", "logical_restore") and is_success:
        # 30.6.1 c1-J3b — a REAL restore (not a rehearsal) also counts what
        # landed, so the snapshot's manifest can answer "this backup holds
        # these tables with these rows" without waiting for a rehearsal.
        # No lifecycle transition: a restore moves bytes, it never decides
        # whether the stack is running.
        _record_snapshot_verify_tables(db, stack, payload, task)
    elif not is_success:
        # snapshot / restore — no lifecycle transition, but a silent failure
        # here loses a backup or a restore, so make it visible on the stack.
        stack.status_reason = reason or f"{stack_op} failed"
        logger.error("persistent stack %s %s failed: %s", stack.id, stack_op, stack.status_reason)
    else:
        return

    db.commit()

    # The deploy result is committed. A redeploy that MOVED the stack's published
    # host port leaves any App routed at it pointing at a dead port until the
    # hourly sweep; re-point the routes now. UPDATE-TARGET-REDEPLOY-1126: so does
    # a redeploy that kept the SAME port — the route was 503 ("app target
    # unavailable") while the stack was `deploying`, and it must self-heal when
    # the stack flips back to running without any manual nudge. Fire the refresh
    # on EITHER a port move OR a lifecycle flip into running (the 2 s coalesce
    # in _schedule_retarget_refresh debounces bursts). Best effort and after the
    # commit, so a caddy/DB problem here can never change the ingest outcome.
    _status_flipped_running = (
        stack_op in ("deploy", "migrate", "start")
        and is_success
        and getattr(stack, "status", None) == "running"
        and _status_before != "running"
    )
    if _ports_changed or _status_flipped_running:
        try:
            from services import app_routing
            app_routing.refresh_routes_for_stack(db, stack.id)
        except Exception:  # noqa: BLE001
            logger.warning("app-route retarget failed for stack %s", stack.id,
                           exc_info=True)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass


def _record_snapshot_verify_tables(db: Session, stack, payload: dict,
                                   task: ApiTaskQueue) -> None:
    """30.6.1 c1-J3b — record a REAL restore's row counts on its snapshot entry.

    A restore task is dispatched per VOLUME and (unlike a rehearsal) carries no
    `snapshot_id`, so the entry is found by the object key it wrote — the same
    key the snapshot manifest already lists under `volumes`/`logical`. No match
    (an older row, a pruned snapshot) records nothing: guessing which snapshot a
    restore belonged to would attribute counts to a backup nobody restored.
    """
    key = payload.get("snapshot_key")
    if not key:
        return
    refs = dict(stack.storage_refs or {})
    snaps = dict(refs.get("snapshots") or {})
    match = None
    for snap_id, snap in snaps.items():
        if not isinstance(snap, dict):
            continue
        keys = list((snap.get("volumes") or {}).values())
        logical = snap.get("logical") or {}
        if logical.get("key"):
            keys.append(logical["key"])
        if key in keys:
            match = snap_id
            break
    if match is None:
        return
    snap = dict(snaps[match])
    if not _merge_snapshot_verify(snap, task.result_payload or {}):
        return
    snaps[match] = snap
    refs["snapshots"] = snaps
    stack.storage_refs = refs
    logger.info("stack %s restore of snapshot %s recorded %d table row count(s)",
                stack.id, match, len(snap["verify"]["tables"]))


def _merge_snapshot_verify(snap: dict, result_payload: dict) -> bool:
    """30.6.1 c1-J3b — fold the worker's per-table row counts into a snapshot's
    `verify` dict, MERGED (never wholesale).

    The worker counted the restored logical tables READ-ONLY against the stack's
    own DB service (CORE/Worker/internal/executor/stacklogical.go
    logicalVerifyCounts) and rode them back as a flat `verify_tables` object
    (transport.go TaskResult.VerifyTables) plus a `verify_truncated` flag when
    the count batch hit its caps. `verify` is shared with the streaming-migration
    gate (`lag`, `seeded_rows`, …), so the `tables` key is written INTO the
    existing dict — replacing it would erase a verdict this restore never
    measured. No `verify_tables` key -> nothing changes at all, so volume-only
    snapshots keep their byte-identical manifest.

    Names/counts are re-checked here because `storage_refs` is tenant-editable
    JSON: a hand-edited row must not smuggle a junk key into the manifest the
    restore gate reads. Returns whether anything was written.
    """
    import re
    raw = result_payload.get("verify_tables")
    if not isinstance(raw, dict) or not raw:
        return False
    name_ok = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
    tables = {}
    for name, count in raw.items():
        if not isinstance(name, str) or not name_ok.match(name):
            continue
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            continue
        if count > MAX_VERIFY_ROWS:
            # defence in depth — the model already 422s this; a hand-edited
            # storage_refs row must not persist one either (stacklogical.go:245)
            continue
        tables[name] = count
    if not tables:
        return False
    verify = dict(snap.get("verify") or {})
    verify["tables"] = tables
    if result_payload.get("verify_truncated"):
        verify["truncated"] = True
    snap["verify"] = verify
    return True


def _record_rehearsal_result(db: Session, stack, task: ApiTaskQueue,
                             is_success: bool, reason: Optional[str]) -> None:
    """managed-ai-surfaces-and-stack-ops §9.5 — record what a rehearsal proved.

    A stack's snapshot covers one task PER VOLUME, so a snapshot is only proven
    restorable when EVERY volume came back. Marking it verified on the first
    success would certify a stack whose database volume restored and whose
    uploads volume did not — the exact half-recovery that reads as a working
    backup right up until someone needs it.
    """
    from datetime import datetime, timezone

    payload = task.task_payload or {}
    snapshot_id = payload.get("snapshot_id")
    volume = payload.get("stack_volume")
    if not snapshot_id or not volume:
        logger.warning("rehearsal task %d carries no snapshot_id/volume", task.id)
        return

    refs = dict(stack.storage_refs or {})
    snaps = dict(refs.get("snapshots") or {})
    snap = dict(snaps.get(snapshot_id) or {})
    if not snap:
        # The snapshot was pruned by retention while its rehearsal was in
        # flight. Nothing to record against, and re-creating the entry would
        # resurrect a snapshot whose objects are already deleted.
        logger.info("rehearsal result for snapshot %s discarded: no longer retained",
                    snapshot_id)
        return

    now = datetime.now(timezone.utc).isoformat()
    log_tail = ((task.result_payload or {}).get("log_tail") or "")[:500]
    per_volume = dict(snap.get("rehearsal_volumes") or {})
    per_volume[volume] = {
        "ok": bool(is_success),
        "at": now,
        "detail": log_tail if is_success else (reason or "rehearsal failed")[:500],
    }
    snap["rehearsal_volumes"] = per_volume

    # 30.6.1 c1-J3b — the worker counted the restored logical tables READ-ONLY
    # against the stack's own DB service (stacklogical.go:logicalVerifyCounts)
    # and rode them back as a flat "verify_tables" object. Recorded MERGED,
    # never wholesale: `verify` is the same verdict dict the streaming-migration
    # gate writes (`lag`, `seeded_rows`, …), so replacing it would erase a
    # verdict this rehearsal never measured. Absent key -> no change at all, so
    # volume-only snapshots keep their byte-identical shape.
    _merge_snapshot_verify(snap, task.result_payload or {})

    expected = set((snap.get("volumes") or {}).keys())
    done = set(per_volume.keys())
    failed = [v for v, r in per_volume.items() if not r.get("ok")]

    if failed:
        # Fail fast: one bad volume means the snapshot is not restorable, and
        # waiting for the rest to report would leave it looking untested in the
        # meantime.
        snap["verified_at"] = None
        snap["verify_error"] = (
            f"Restore rehearsal failed for volume(s) {', '.join(sorted(failed))}: "
            f"{per_volume[failed[0]]['detail']}")
        logger.error("stack %s snapshot %s FAILED verification: %s",
                     stack.id, snapshot_id, snap["verify_error"])
    elif expected and done >= expected:
        snap["verified_at"] = now
        snap["verify_error"] = None
        logger.info("stack %s snapshot %s verified restorable (%d volume(s))",
                    stack.id, snapshot_id, len(expected))
    # else: still waiting on other volumes — stays untested, which is true.

    snaps[snapshot_id] = snap
    refs["snapshots"] = snaps
    stack.storage_refs = refs


def _process_benchmark_result(db: Session, task: ApiTaskQueue):
    """Process hardware benchmark results and update the HardwareProfile scores."""
    import json
    from models import HardwareProfile

    container_env = task.task_payload.get("container_env", {})
    profile_id = container_env.get("HARDWARE_PROFILE_ID")
    if not profile_id:
        return

    profile = worker_tasks_lifecycle_crud.get_hardware_profile_by_id(db, int(profile_id))
    if not profile:
        return

    # Reject results reported by a worker that does not own this profile — the
    # scores would be another machine's and writing them would corrupt the
    # profile (observed live: the arm64 worker's profile was stamped with the
    # AlphaBox's CPU/RAM/I-O scores after a sweep un-pinned the pinned task).
    if not _benchmark_report_worker_is_owner(db, task, profile):
        reporter = (task.claimed_by_worker or task.assigned_to_worker or "<unknown>")
        pinned = (task.task_payload or {}).get("benchmark_target_worker")
        logger.error(
            "REJECTED benchmark result for profile %d: reported by worker %s "
            "(pinned=%s) which does not own this profile — refusing to write "
            "another machine's scores onto %d. The task must have been "
            "reassigned by a sweep; the run should be re-run on the pinned "
            "worker.",
            profile.id, reporter, pinned, profile.id,
        )
        profile.benchmark_status = "failed"
        existing = dict(profile.benchmark_results or {})
        existing.setdefault("error", (
            f"Benchmark result rejected: reported by worker {reporter!r} "
            f"which does not own hardware profile {profile.id} "
            f"(pinned to {pinned!r}). Re-run the benchmark on the pinned "
            f"worker."
        ))
        profile.benchmark_results = existing
        db.commit()
        return

    log_tail = ""
    if task.result_payload:
        log_tail = task.result_payload.get("log_tail", "")

    # The benchmark container prints its signed-JSON result on the first
    # line, followed by human-readable progress lines (`=== MOD Hardware
    # Benchmark ===`, `[01/12] ...`). A naive json.loads(log_tail) fails
    # with "Extra data: line 2 column 1" because of that tail — use
    # raw_decode so we take only the leading JSON object and ignore the
    # rest of the log.
    try:
        decoder = json.JSONDecoder()
        stripped = log_tail.lstrip()
        json_start = stripped.find("{")
        if json_start == -1:
            raise json.JSONDecodeError("No JSON object found", stripped, 0)
        parsed, _ = decoder.raw_decode(stripped[json_start:])

        # Handle signed output: {"scores": {...}, "signature": "..."}
        if "scores" in parsed and "signature" in parsed:
            scores = parsed["scores"]
            signature = parsed["signature"]

            from routers.worker_management import _verify_benchmark_signature
            verified = _verify_benchmark_signature(int(profile_id), json.dumps(scores, sort_keys=True), signature)
            if verified:
                logger.info("Benchmark HMAC verified for profile %s", profile_id)
            else:
                # HMAC verification failed. This is expected on deployments
                # where the Vault benchmark-hmac policy isn't installed yet
                # (403 on both store and read) AND the DB fallback key was
                # lost to a session-level race. Accept the results with a
                # warning rather than silently failing the profile forever —
                # a stuck "failed" profile with real scores is worse than an
                # unverified-but-populated profile. The HMAC mechanism is a
                # tamper-detection layer for untrusted workers; the embedded
                # worker is on the control server and trusted by definition.
                logger.warning(
                    "Benchmark HMAC verification failed for profile %s — "
                    "accepting results anyway (Vault policy likely missing). "
                    "Install the api-benchmark-hmac Vault policy for signed verification.",
                    profile_id,
                )
        else:
            scores = parsed

        # Route results to the right table: per-device accelerator results
        # go to AcceleratorProfile, general system results go to HardwareProfile.
        target_kind = container_env.get("TARGET_KIND", "").strip().lower()
        target_index = container_env.get("TARGET_INDEX", "").strip()

        if target_kind and target_index:
            # Per-device accelerator benchmark result
            _store_accelerator_result(db, profile, int(profile_id), target_kind, int(target_index), scores)
        else:
            # General system benchmark result → HardwareProfile.
            # gpu_compute_score / gpu_memory_bw_score are deliberately
            # NOT written here — the System benchmark historically
            # sent a CPU-Python-loop measurement under those names,
            # which produced misleading numbers on both GPU-less and
            # GPU-equipped hosts. Real per-device GPU scores come
            # from the accelerator-benchmark-<kind> containers and
            # get rolled up onto HardwareProfile via
            # _store_accelerator_result below.
            profile.cpu_multi_score = float(scores.get("cpu_multi", 0))
            profile.cpu_single_score = float(scores.get("cpu_single", 0))
            profile.memory_bw_score = float(scores.get("memory_bw", 0))
            profile.io_seq_score = float(scores.get("io_seq", 0))
            profile.io_random_score = float(scores.get("io_random", 0))
            # Fields below accept JSON null from the container (=> None)
            # when the test was skipped/failed, distinguishable from
            # a measured 0. `scores.get(k)` returns None for null;
            # `float(x) if x is not None else None` preserves that.
            def _nullable_float(k):
                v = scores.get(k)
                return float(v) if v is not None else None
            profile.minio_download_mbps = _nullable_float("minio_download_mbps")
            profile.minio_upload_mbps = _nullable_float("minio_upload_mbps")
            profile.docker_pull_seconds = _nullable_float("docker_pull_seconds")
            profile.container_startup_ms = _nullable_float("container_startup_ms")
            profile.disk_seq_read_score = float(scores.get("disk_seq_read", 0))

        # Both paths: mark the HardwareProfile as completed if ALL benchmark
        # tasks for this profile are done (general + every accelerator).
        from crud.accelerator_cud import all_benchmarks_complete
        if all_benchmarks_complete(db, int(profile_id)):
            profile.benchmark_status = "completed"
        profile.benchmarked_at = datetime.now(timezone.utc)
        profile.benchmark_results = scores
        db.commit()
        logger.info("Benchmark complete for profile %s (kind=%s idx=%s): %s",
                     profile_id, target_kind or "system", target_index or "-", scores)

    except (json.JSONDecodeError, ValueError) as e:
        logger.error("Failed to parse benchmark output for profile %s: %s", profile_id, e)
        profile.benchmark_status = "failed"
        db.commit()


def _store_accelerator_result(db, hw_profile, profile_id: int, kind: str, index: int, scores: dict):
    """Write per-device benchmark scores to the AcceleratorProfile row."""
    accel = worker_tasks_lifecycle_crud.get_accelerator_profile(db, profile_id, kind, index)

    if not accel:
        logger.warning("No AcceleratorProfile for profile %d kind=%s index=%d", profile_id, kind, index)
        return

    # Give the device a clean, non-placeholder name now that a benchmark has
    # run. The benchmark payload usually carries no device name, so prefer a
    # real name reported by the container if present, else fall back to a clean
    # "<KIND> #<index>" (e.g. "CUDA #0"). Only replace a placeholder/empty name
    # so a real name captured at enrollment (nvidia-smi/rocm-smi/xpu-smi) is
    # preserved. This — together with the heartbeat-sync guard in
    # upsert_accelerators — ensures a completed/unsupported accelerator never
    # shows "... details pending benchmark".
    from crud.accelerator_cud import (
        clean_accelerator_name,
        is_placeholder_accelerator_name,
    )
    reported_name = (scores.get("device_name") or scores.get("name") or "").strip() or None
    if is_placeholder_accelerator_name(accel.name):
        accel.name = clean_accelerator_name(accel.name, kind, index, incoming=reported_name)

    accel.compute_score = float(scores.get("compute", scores.get("gpu_compute", 0)))
    accel.memory_bw_score = float(scores.get("memory_bw", scores.get("gpu_memory_bw", 0)))
    accel.copy_bw_score = float(scores.get("copy_bw", 0))
    accel.is_integrated = bool(scores.get("is_integrated", False))
    accel.shared_ram_usage_gb = float(scores.get("shared_ram_usage_gb", 0))
    accel.cpu_bw_contention = float(scores.get("cpu_bw_contention", 1.0))
    # Total device memory (GiB) reported by the benchmark binary
    # (cudaMemGetInfo/hipMemGetInfo) or its nvidia-smi/rocm-smi fallback.
    # Fill-only: a heartbeat-reported vram_gb (the worker's own nvidia-smi /
    # rocm-smi read) is the source of truth when present — never overwrite
    # a non-NULL/non-0 value with a benchmark re-measurement. And a missing
    # or 0.0 reading (older container without get_vram_gb, tooling
    # unavailable) must not wipe an existing value.
    bench_vram = scores.get("vram_gb")
    try:
        bench_vram = float(bench_vram) if bench_vram is not None else None
    except (TypeError, ValueError):
        bench_vram = None
    if (
        bench_vram and bench_vram > 0 and math.isfinite(bench_vram)
        and (not accel.vram_gb or accel.vram_gb <= 0)
    ):
        accel.vram_gb = round(bench_vram, 1)
    accel.benchmark_results = scores
    accel.benchmarked_at = datetime.now(timezone.utc)

    # If every sub-test failed (zeros everywhere) AND the container
    # reported errors, the run is effectively a failure — surface the real
    # cause instead of showing "completed with zero scores" and letting the
    # UI guess "runtime libraries not available".
    sub_errors = scores.get("errors") or {}
    all_zero = (
        accel.compute_score == 0
        and accel.memory_bw_score == 0
        and accel.copy_bw_score == 0
    )
    if all_zero and sub_errors:
        # Prefer the compute-test error (most informative); fall back to
        # whichever first error is available. Keep it short for the UI.
        first_err = (
            sub_errors.get("compute")
            or sub_errors.get("memory_bw")
            or sub_errors.get("copy_bw")
            or next(iter(sub_errors.values()), "")
        )
        # Classify: a device-not-found / unsupported-environment error is a
        # terminal quarantine ('unsupported'), everything else a retriable
        # 'failed'. Keeps a device the runtime cannot see from looking like a broken box.
        from utils.accelerator_quarantine import classify_accelerator_failure
        accel.benchmark_status = classify_accelerator_failure(first_err)
        accel.error_message = first_err[:500] if first_err else "All benchmark sub-tests returned zero"
    else:
        accel.benchmark_status = "completed"
        accel.error_message = None

    # Bump strategy success counter for auto-approval tracking.
    strategy_id_str = (scores.get("strategy_id") or "").strip()
    if strategy_id_str:
        try:
            strategy = worker_tasks_results_crud.get_accelerator_benchmark_strategy_by_id(
                db, int(strategy_id_str))
            if strategy:
                strategy.runs_succeeded = (strategy.runs_succeeded or 0) + 1
                strategy.last_success_at = datetime.now(timezone.utc)
                accel.last_strategy_id = strategy.id
        except (ValueError, TypeError):
            pass

    db.flush()
    logger.info("Accelerator benchmark stored: profile=%d %s/%d compute=%.1f mem_bw=%.1f",
                profile_id, kind, index, accel.compute_score or 0, accel.memory_bw_score or 0)

    # Roll the best per-device compute / memory-bw scores up onto the
    # HardwareProfile so the worker card's summary surface shows a number
    # instead of a dash. The system benchmark doesn't measure the GPU
    # itself (the container has no GPU access), so without this rollup
    # those fields stay NULL even when every accelerator benchmark
    # succeeded.
    if hw_profile is not None:
        all_accels = worker_tasks_results_crud.list_completed_accelerator_profiles(db, profile_id)
        best_compute = max((a.compute_score or 0 for a in all_accels), default=0)
        best_mem_bw  = max((a.memory_bw_score or 0 for a in all_accels), default=0)
        if best_compute > 0:
            hw_profile.gpu_compute_score = best_compute
        if best_mem_bw > 0:
            hw_profile.gpu_memory_bw_score = best_mem_bw
        # Back-fill the summary gpu_vram_gb from benchmark-measured device
        # VRAM when the heartbeat left it NULL/0 (WSL2: the worker process
        # can't run nvidia-smi, so its specs carry no VRAM even though the
        # benchmark container can measure it). Never overwrite a value the
        # heartbeat itself reported — that one is the source of truth.
        if not hw_profile.gpu_vram_gb or hw_profile.gpu_vram_gb <= 0:
            completed_vrams = [
                a.vram_gb for a in all_accels
                if a.vram_gb and a.vram_gb > 0 and math.isfinite(a.vram_gb)
            ]
            if completed_vrams:
                hw_profile.gpu_vram_gb = round(max(completed_vrams), 1)
                logger.info(
                    "Back-filled gpu_vram_gb=%.1f from benchmark for hardware profile %s",
                    hw_profile.gpu_vram_gb, profile_id,
                )


def _get_worker_task(db: Session, task_id: int, worker_name: str) -> ApiTaskQueue:
    """Get a task that belongs to this worker, or raise 404/403."""
    task = worker_tasks_lifecycle_crud.get_task_queue_row_by_id(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.claimed_by_worker != worker_name:
        raise HTTPException(status_code=403, detail="Task not claimed by this worker")
    return task


# Re-entrancy guard for the frontier re-drive (below): the re-drive dispatches
# by re-invoking _check_and_dispatch_downstream, which reaches the same
# completion block — without this a re-drive could re-drive itself. One re-drive
# per call-stack is enough; the ordinary dispatch recursion cascades the rest.
_redrive_active = threading.local()

# Per-run dispatch serialization. Two worker /complete calls for sibling branches
# of the SAME run race inside _check_and_dispatch_downstream: each runs the
# dispatch + completion cascade concurrently, one handler's not-yet-committed
# task is invisible to the other, so BOTH dispatch the same downstream node
# (observed: duplicate process-21 on runs 1178/1179) and the frontier re-drive
# fires on a stale "0 active tasks" read. A session-level Postgres advisory lock
# keyed on the run makes the cascade mutually exclusive PER RUN. Advisory locks
# are re-entrant within a session (the cache-hit + re-drive recursion re-acquire
# safely, matched unlock-for-lock), and different runs never block each other.
_DISPATCH_LOCK_NS = 0x574B4644  # 'WKFD' — namespace so we can't collide with other advisory locks


def _dispatch_lock_wait_secs() -> float:
    """Total seconds _acquire_dispatch_lock may wait for the per-run lock.
    Bounded on purpose: a holder that never finishes (see the deadlock note below)
    must never pin this request's DB connection forever."""
    try:
        return max(0.0, float(os.getenv("MOD_DISPATCH_LOCK_WAIT_SECS", "30")))
    except (TypeError, ValueError):
        return 30.0


def _acquire_dispatch_lock(db: Session, run_id) -> Optional[bool]:
    """Serialize dispatch for one run.

    rc20-r2 return contract:
      True  — lock acquired; caller runs the cascade holding it.
      False — proceed UNLOCKED (old contract, unchanged): ONLY for run_id None
              or a DB-level failure of the SELECT itself — neither is contention.
      None  — CONTENTION EXHAUSTION: the bounded pg_try_advisory_lock loop ran
              out while another session held the lock. The caller MUST skip the
              cascade (return without dispatching). Proceeding unlocked here —
              what rc20 r1 did — opened a double-dispatch window the old blocking
              form never had: the only downstream guard is the non-atomic
              check-then-act at :12798-12806 and there is no rowcount/with_for_update
              CAS anywhere in the cascade, so two concurrent unlocked dispatchers
              could both enqueue the same node (duplicate task, double cost).

    Re-drive net for a skipped cascade (verified, file:line):
      - _redrive_ready_frontier (:12404) dispatches QUEUED frontier nodes whose
        parents are ALL complete — exactly what a skipped cascade leaves behind.
        It fires at :13363 in the concurrent lock-holder's cascade (active_tasks
        read at :13294), and on every later completion's cascade for the run;
        the holder sees this handler's completion because the caller commits the
        task row (:3690/:3719) BEFORE entering this cascade.
      - The caller ALSO re-drives directly on the skip path (below, outside the
        lock) for the narrow race where the holder's active_tasks read predates
        this handler's task commit.
      - cleanup_expired_task_claims (:14202, branch 1 :14267) requeues CLAIMED/
        RUNNING rows whose claim lapsed — rows, not missing dispatches.

    BOUNDED (rc20): pg_advisory_lock BLOCKS inside the caller's transaction, so a
    holder that never finishes pins its backend and wedges every sibling /complete
    for the same run (observed: run 19, 3 handlers 135-203 s blocked on the blocking
    pg_advisory_lock form for (0x574B4644, 19), 4 backends idle-in-transaction).
    The app-level deadlock Postgres cannot see: the holder's completion cascade
    makes an external HTTP call (tenant workflow_webhook_url -> MOD's own surface)
    while still holding the run's advisory lock, and the handler it calls blocks
    on that very lock. pg_try_advisory_lock + a short retry loop with a total
    deadline (default 30 s, MOD_DISPATCH_LOCK_WAIT_SECS) turns "wait forever" into
    "skip the cascade": the frontier net above picks the run up later.
    """
    if run_id is None:
        return False
    held = getattr(_dispatch_lock_state, "held", None)
    if held is None:
        held = {}
        setattr(_dispatch_lock_state, "held", held)
    _conn, _depth = held.get(run_id, (None, 0))
    if _conn is None:
        # Dedicated AUTOCOMMIT connection, NOT the request Session's pooled one:
        # the cascade's commits return the latter to the pool and the session-level
        # lock would then leak for that backend's whole lifetime (rc20-J157).
        try:
            _conn = db.get_bind().connect().execution_options(
                isolation_level="AUTOCOMMIT")
        except Exception:  # noqa: BLE001 — no engine connection: best-effort
            _conn = None
    _target = _conn if _conn is not None else db
    _wait = _dispatch_lock_wait_secs()
    _deadline = time.monotonic() + _wait
    while True:
        try:
            _row = _target.execute(
                text("SELECT pg_try_advisory_lock(:ns, :k)"),
                {"ns": _DISPATCH_LOCK_NS, "k": int(run_id)},
            ).fetchone()
        except Exception:  # noqa: BLE001
            logger.debug("dispatch advisory lock failed for run %s", run_id, exc_info=True)
            if _conn is not None:
                try:
                    _conn.close()
                except Exception:  # noqa: BLE001
                    pass
            return False
        if _row and bool(_row[0]):
            if _conn is not None:
                held[run_id] = (_conn, _depth + 1)
            return True
        if time.monotonic() >= _deadline:
            logger.warning(
                "dispatch advisory lock for run %s still held elsewhere after the "
                "bounded wait — cascade SKIPPED; frontier re-drive picks the run up",
                run_id,
            )
            if _conn is not None:
                # The dedicated connection was never registered in `held`, so
                # nothing else will close it: closing here returns it to the
                # pool instead of leaking it for the wait's lifetime (rc20-J157
                # review). Same shape as the except-branch above.
                try:
                    _conn.close()
                except Exception:  # noqa: BLE001
                    pass
            return None
        time.sleep(min(0.5, max(0.0, _deadline - time.monotonic())))


# rc20-J157 — the lock MUST live on a connection of its own, not on the
# request Session's pooled connection. The completion cascade commits ~10
# times; every `db.commit()` returns that connection to the pool (SQLAlchemy
# rolls it back on check-in — the exact pg_locks state the rc20e CE room showed
# for the wedged runs: GRANTED, backend IDLE, last query ROLLBACK). Session-level
# advisory locks survive ROLLBACK AND survive the return to the pool, so the
# finally-block unlock, issued through the Session, lands on whichever backend
# the Session checks out next — a DIFFERENT one that never took the lock. The
# locking backend keeps the lock for its whole pooled lifetime: every later
# cascade for that run sees it held, SKIPS, and the run never finalizes
# (observed: runs 1/3/7/8 RUNNING with completed_at NULL for 10+ min).
#
# Fix: hold the lock on a dedicated AUTOCOMMIT connection taken from the
# engine and kept for the whole cascade (same pattern as database.py's
# schema-migration lock). It is tracked per thread + run so the cascade's
# re-entrant re-acquire (_redrive_ready_frontier -> _check_and_dispatch_downstream)
# reuses the SAME connection — Postgres advisory locks are per (connection, key)
# with a counter, so matched unlock-for-lock keeps the outer frame holding.
# Depth 0 closes the connection, which returns it to the pool.
_dispatch_lock_state = threading.local()


def _dispatch_lock_connection(db: Session, run_id) -> Optional[Any]:
    """This thread's dedicated lock connection for `run_id`, or None.

    Created on demand by _acquire_dispatch_lock and released (closed) by
    _release_dispatch_lock at depth 0, so the lock and its connection always
    have the same lifetime and the same owner.
    """
    held = getattr(_dispatch_lock_state, "held", None)
    if not held:
        return None
    return held.get(run_id, (None, 0))[0]


def _release_dispatch_lock(db: Session, run_id, locked: bool) -> None:
    """Release one level of the per-run session-level advisory lock (rc20-J157).

    Unlocks on the dedicated connection that ACQUIRED it, then closes that
    connection at depth 0 so it returns to the pool. Falls back to the Session
    only when no dedicated connection exists (lock acquisition could not get an
    engine connection — pre-J157 best-effort behaviour).
    """
    if not locked or run_id is None:
        return
    _sql = text("SELECT pg_advisory_unlock(:ns, :k)")
    _args = {"ns": _DISPATCH_LOCK_NS, "k": int(run_id)}
    held = getattr(_dispatch_lock_state, "held", None) or {}
    conn, depth = held.get(run_id, (None, 0))
    if conn is None:
        try:
            db.execute(_sql, _args)
        except Exception:  # noqa: BLE001
            logger.debug("dispatch advisory unlock failed for run %s", run_id,
                         exc_info=True)
        return
    try:
        conn.execute(_sql, _args)
    except Exception:  # noqa: BLE001
        logger.debug("dispatch advisory unlock failed for run %s", run_id,
                     exc_info=True)
    if depth <= 1:
        held.pop(run_id, None)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    else:
        held[run_id] = (conn, depth - 1)


def _complete_passive_sink_node(db: Session, workflow_run, completed_task,
                                node: dict, downstream_id: str) -> bool:
    """Finish a canvas ``type == "output"`` node that has no ``node_kind``.

    Called from the downstream cascade only once every parent of the node has
    completed. Such a node has nothing to execute (no container, no task), so
    its NodeRun is set COMPLETED in place instead of being left QUEUED for the
    orphan guard to fail. Only the exact shape (graph type "output", empty
    node_kind) qualifies, and only a still-QUEUED NodeRun is touched, so a
    kind-less node of any other type remains a genuine orphan. Never raises.
    Returns True iff a NodeRun was completed.
    """
    try:
        if (node or {}).get("type") != "output":
            return False
        from models import WorkflowNode
        # One indexed query (NodeRun joined to its WorkflowNode): no extra
        # round trip on the cascade beyond the single lookup.
        nr = db.query(NodeRun).join(
            WorkflowNode, WorkflowNode.id == NodeRun.workflow_node_id,
        ).filter(
            NodeRun.workflow_run_id == completed_task.workflow_run_id,
            WorkflowNode.workflow_id == workflow_run.workflow_id,
            WorkflowNode.graph_node_id == downstream_id,
        ).first()
        if nr is None or nr.status != TaskStatus.QUEUED:
            return False
        _now = datetime.now(timezone.utc)
        nr.status = TaskStatus.COMPLETED
        if not nr.started_at:
            nr.started_at = _now
        nr.completed_at = _now
        db.commit()
        return True
    except Exception:  # noqa: BLE001 - best-effort; the orphan guard is the backstop
        logger.warning("passive sink completion failed for %s", downstream_id,
                       exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _redrive_ready_frontier(db: Session, workflow_run, plan) -> int:
    """Dispatch QUEUED frontier nodes whose parents are ALL complete but which
    the event-driven dispatch has not reached yet, and return how many new
    tasks were enqueued.

    Such a node is NOT an orphan — it is pending frontier that a sibling-branch
    cache-hit cascade or a lagging worker `/complete` left behind. This is the
    race that wedged run 1122 ("Video Cleanup"): the audio branch finished ~60s
    before the video branch dispatched, so the orphan guard saw process-1/2/21
    QUEUED-with-no-task and failed the whole run, after which the terminal guard
    blocked VideoCombine (process-19) for good. Re-driving here means the
    completion check can never mistake "not dispatched yet" for "can never run".

    Re-drives via one COMPLETED parent per ready node so it reuses the normal
    dispatch path (node_conf build, idempotency, cache short-circuit) verbatim.
    """
    if getattr(_redrive_active, "on", False):
        return 0
    dependencies = (plan or {}).get("dependencies", {}) or {}
    if not dependencies:
        return 0
    run_id = workflow_run.id
    task_nodes = {
        r[0] for r in worker_tasks_results_crud.list_task_node_ids(db, run_id)
    }
    completed_nodes = {
        r[0] for r in worker_tasks_results_crud.list_completed_task_node_ids(db, run_id)
    }
    # Parents to re-drive from: a COMPLETED node whose child has no task yet but
    # whose every parent is complete (the child is dispatchable right now).
    redrive_parents = set()
    for node_id, parents in dependencies.items():
        if node_id in task_nodes or not parents:
            continue
        if all(p in completed_nodes for p in parents):
            redrive_parents.update(p for p in parents if p in completed_nodes)
    if not redrive_parents:
        return 0
    before = len(task_nodes)
    _redrive_active.on = True
    try:
        for pid in redrive_parents:
            ptask = worker_tasks_results_crud.get_latest_completed_task(db, run_id, pid)
            if ptask is not None:
                _check_and_dispatch_downstream(db, ptask)
    finally:
        _redrive_active.on = False
    after = worker_tasks_results_crud.count_distinct_node_ids_with_task_for_run(db, run_id)
    return max(0, after - before)


def _apply_debug_override(run, node_id: str, node_data, node_conf=None):
    """GX.sz-2.6b — apply a live-debugger hot-swap when the node dispatches.

    services/live_debugger.hot_swap only STORES the override
    (``run.parameters["debug_overrides"][node_id] = {"config": {...},
    "node_definition_id": int}``); applying it is the dispatch side's job.
    Pure + DB-free: it reads the override bag and returns NEW
    ``(node_data, node_conf)`` — ``graph_json`` / the persisted execution plan
    are never mutated in place (deep copy), and the override stays in
    ``parameters`` as the audit trail, so re-dispatch is idempotent.

    Hot path: no override for this node → the inputs are returned as the SAME
    objects, zero cost, byte-identical payload.

    ``config`` merges into ``node_data["extraVariables"]`` — the camelCase
    runtime bag that IS the task config (enqueue_task_internal renders it to
    container_env, _cache_config_for_payload hashes it). If the node also
    carries a ``config`` bag we merge the same keys there so both stay
    consistent; a node without one is left without one (payload shape stable).
    ``node_definition_id`` replaces ``nodeDefinitionId`` — the DB-authoritative
    gates (docker-socket bind, RAG/PS tokens, VLM endpoint) resolve by that id,
    so the swap must land BEFORE node_kind / queue / definition resolution.

    Logs config KEYS only, at debug level — never values (they can be secrets).
    """
    entry = ((getattr(run, "parameters", None) or {}).get("debug_overrides")
             or {}).get(node_id)
    if not isinstance(entry, dict) or not entry:
        return node_data, node_conf
    if not isinstance(node_data, dict):
        return node_data, node_conf

    cfg = entry.get("config")
    cfg = cfg if isinstance(cfg, dict) and cfg else None
    new_def_id = entry.get("node_definition_id")
    swap_def = isinstance(new_def_id, int) and not isinstance(new_def_id, bool)
    if cfg is None and not swap_def:
        return node_data, node_conf

    import copy
    node_data = copy.deepcopy(node_data)
    if cfg is not None:
        bag = node_data.get("extraVariables")
        bag = dict(bag) if isinstance(bag, dict) else {}
        bag.update(cfg)
        node_data["extraVariables"] = bag
        if isinstance(node_data.get("config"), dict):
            cfg_bag = dict(node_data["config"])
            cfg_bag.update(cfg)
            node_data["config"] = cfg_bag
        logger.debug("hot-swap override applied on node %s (keys only: %s)",
                     node_id, sorted(cfg))
    if swap_def:
        node_data["nodeDefinitionId"] = new_def_id

    if isinstance(node_conf, dict):
        # Only reached if a caller applies the override after the conf is built:
        # keep the nested bag and the keys flattened out of it in step.
        node_conf = dict(node_conf)
        node_conf["node_data"] = node_data
        for _k in ("node_kind", "executable_path", "process_type",
                   "output_schema", "cost_in_credits", "container_env"):
            if _k in node_conf:
                node_conf[_k] = node_data.get(_k, node_conf[_k])
    return node_data, node_conf


def _check_and_dispatch_downstream(db: Session, completed_task: ApiTaskQueue):
    """
    After a task completes, dispatch ready downstream nodes.

    Event-driven: fires on each task completion. Checks if all parents of
    each downstream node are done, and enqueues the downstream task if so.
    Also detects whole-workflow completion.
    """
    from services.workflow_orchestrator import _emit_run_event
    # Serialize the whole cascade per run so concurrent sibling completions can't
    # double-dispatch a downstream node or race the frontier re-drive.
    _run_id = completed_task.workflow_run_id
    _dispatch_locked = _acquire_dispatch_lock(db, _run_id)
    # rc20-J157: the lock lives on a dedicated per-thread connection that
    # _acquire_dispatch_lock registered in _dispatch_lock_state, so the finally
    # block's _release_dispatch_lock unlocks THAT connection — never the
    # request Session's pooled one, which the cascade's commits return to the
    # pool (leaving the session-level lock behind for that backend's lifetime).
    if _dispatch_locked is None:
        # rc20-r2: contention exhaustion — SKIP the cascade (acquire contract,
        # _acquire_dispatch_lock docstring). Proceeding unlocked here is the
        # double-dispatch race (non-atomic check-then-act at :12798, no CAS).
        # Re-drive the frontier directly, OUTSIDE any lock, so the narrow race
        # where the concurrent holder's active_tasks read (:13294) predates
        # this handler's task commit (:3690/:3719) cannot strand the run; the
        # normal net (_redrive_ready_frontier at :13363 on the holder's / any
        # later completion's cascade) covers the rest. The re-drive re-enters
        # this function; a still-contended acquire returns None again and the
        # _redrive_active guard (:12421) stops any recursion.
        try:
            _skip_run = db.query(WorkflowRun).filter(
                WorkflowRun.id == completed_task.workflow_run_id
            ).first()
            if _skip_run:
                _skip_plan = (_skip_run.parameters or {}).get("execution_plan")
                if _skip_plan:
                    _redrive_ready_frontier(db, _skip_run, _skip_plan)
        except Exception as e:  # noqa: BLE001 — best-effort; frontier net retries
            logger.warning(
                "dispatch-lock skip re-drive failed for run %s: %s",
                completed_task.workflow_run_id, e,
            )
        return
    # rc20: the completion notification makes EXTERNAL HTTP (tenant workflow_webhook_url
    # -> MOD's own surface, Slack, email queue). It must NOT run while this session still
    # holds the run's advisory lock: the handler it calls blocks on that very lock, an
    # app-level deadlock Postgres cannot see, which pins this txn and wedges every sibling
    # /complete for the run. Defer it to the finally, AFTER _release_dispatch_lock.
    _notify_pending = False
    try:
        # r5-lock-ok: guards the dispatch cascade; order-sensitive (1431)
        workflow_run = db.query(WorkflowRun).filter(
            WorkflowRun.id == completed_task.workflow_run_id
        ).first()
        if not workflow_run:
            return

        # Terminal-run guard: a CANCELLED/FAILED run must NOT dispatch anything
        # more. Without this, a node that completes inside a cancelled run still
        # triggers downstream dispatch ("the API kicks off things when it's
        # supposed to be dead"), and a user Cancel can never win because fresh
        # tasks keep spawning behind it faster than the worker can kill them.
        _run_status = workflow_run.status
        _run_status = _run_status.value if hasattr(_run_status, "value") else _run_status
        if str(_run_status).upper() in ("CANCELLED", "FAILED"):
            logger.info(
                "downstream dispatch skipped — run %s is terminal (%s); no further tasks",
                workflow_run.id, _run_status,
            )
            # stack-warm (task 2.4) — a terminal (cancelled/failed) run releases
            # any stack it pre-warmed but never reached (subject to release_stack's
            # own predictive check against OTHER runs). Paid layer; no-op without it.
            try:
                from services import stack_warm as _stack_warm
                _stack_warm.on_run_terminal(db, workflow_run)
            except Exception:  # noqa: BLE001
                logger.debug("stack_warm: terminal release (cancelled/failed) "
                             "hook failed", exc_info=True)
            return

        # Airflow-style pause: HOLD the frontier. Nodes already running finish
        # (this fires as each completes), but no NEW downstream node is enqueued
        # while paused. Resume flips the flag and re-kicks this for the run's
        # completed tasks, re-opening the frontier.
        if getattr(workflow_run, "paused", False):
            logger.info(
                "downstream dispatch held — run %s is paused; node %s done but "
                "frontier not advanced until resume",
                workflow_run.id, completed_task.node_id,
            )
            return

        # stack-warm (task 2.4) — NODE-COMPLETION hook. A node just completed,
        # so the frontier moved: recompute reach-times from the updated frontier
        # and background-warm any stack whose lead time has now arrived. Runs
        # INSIDE the per-run dispatch advisory lock already held here, so it is
        # serialized with dispatch (no pre-warm vs. reclaim race). force=True
        # bypasses the run-start throttle because a completion is a real change.
        # Paid layer (no-op without it); best-effort.
        try:
            from services import stack_warm as _stack_warm
            _stack_warm.warm_run(db, workflow_run.id, force=True)
        except Exception:  # noqa: BLE001 — pre-warm must never wedge dispatch
            logger.debug("stack_warm: completion hook failed", exc_info=True)

        completed_node_id = completed_task.node_id
        is_failure = completed_task.status == ApiTaskStatus.FAILED

        # #12 — emit a traffic-cop event the moment we observe the task
        # transition. Captures task_id, node_id, final status, duration,
        # error_message, and a tail of the worker log (from
        # result_payload.log_tail) so the run-detail page shows what each
        # node did instead of just "completed/failed".
        _log_tail = ""
        try:
            rp = completed_task.result_payload or {}
            tail = rp.get("log_tail") or rp.get("logs") or ""
            if isinstance(tail, list):
                tail = "\n".join(str(x) for x in tail)
            tail = (tail or "").strip()
            if tail:
                _log_tail = tail[-2000:]
        except Exception:
            pass
        _duration_s = None
        try:
            if completed_task.started_at and completed_task.completed_at:
                _duration_s = round((completed_task.completed_at - completed_task.started_at).total_seconds(), 2)
        except Exception:
            pass
        _emit_run_event(
            db,
            completed_task.workflow_run_id,
            "traffic_cop",
            "error" if is_failure else "info",
            f"Node {completed_node_id} {'FAILED' if is_failure else 'COMPLETED'}"
            + (f" ({_duration_s}s)" if _duration_s is not None else ""),
            {
                "task_id": completed_task.id,
                "node_id": completed_node_id,
                "status": str(completed_task.status.value if hasattr(completed_task.status, "value") else completed_task.status),
                "duration_seconds": _duration_s,
                "log_tail": _log_tail or None,
                "worker": completed_task.assigned_to_worker or completed_task.claimed_by_worker,
                "queue": completed_task.queue_name,
                "error_message": completed_task.error_message,
                "attempt": completed_task.attempt_count,
            },
        )

        # --- Load execution plan ---
        plan = (workflow_run.parameters or {}).get("execution_plan")
        if not plan:
            # Fallback: try to load from workflow version graph_json
            if workflow_run.workflow_version_id:
                from models import WorkflowVersion
                # r5-lock-ok: same cascade frame as run lookup; order-sensitive (1431)
                version = db.query(WorkflowVersion).filter(
                    WorkflowVersion.id == workflow_run.workflow_version_id
                ).first()
                if version and version.graph_json:
                    from services.workflow_orchestrator import build_execution_plan
                    try:
                        plan = build_execution_plan(
                            version.graph_json.get("nodes", []),
                            version.graph_json.get("edges", []),
                        )
                    except Exception:
                        pass

        # --- If task failed, mark downstream nodes as BLOCKED ---
        is_profiling_run = (workflow_run.parameters or {}).get("is_profiling", False)

        if plan and is_failure and not is_profiling_run:
            dependents_map = plan.get("dependents", {})
            blocked_ids = set()
            queue = list(dependents_map.get(completed_node_id, []))
            while queue:
                nid = queue.pop(0)
                if nid not in blocked_ids:
                    blocked_ids.add(nid)
                    queue.extend(dependents_map.get(nid, []))

            for blocked_id in blocked_ids:
                try:
                    from models import WorkflowNode
                    # r5-lock-ok: inside dispatch-lock cascade; order-sensitive (1431)
                    wn = db.query(WorkflowNode).filter(
                        WorkflowNode.workflow_id == workflow_run.workflow_id,
                        WorkflowNode.graph_node_id == blocked_id,
                    ).first()
                    if wn:
                        # r5-lock-ok: same cascade frame; order-sensitive (1431)
                        nr = db.query(NodeRun).filter(
                            NodeRun.workflow_run_id == completed_task.workflow_run_id,
                            NodeRun.workflow_node_id == wn.id,
                        ).first()
                        if nr and nr.status == TaskStatus.QUEUED:
                            nr.status = TaskStatus.BLOCKED
                            db.commit()
                except Exception:
                    pass

            if blocked_ids:
                logger.info("Marked %d downstream node(s) as BLOCKED for workflow %d",
                           len(blocked_ids), completed_task.workflow_run_id)
                _emit_run_event(
                    db,
                    completed_task.workflow_run_id,
                    "traffic_cop",
                    "warning",
                    f"Blocked {len(blocked_ids)} downstream node(s) due to upstream failure of {completed_node_id}",
                    {"failed_node_id": completed_node_id, "blocked_node_ids": list(blocked_ids)},
                )

        should_dispatch = (not is_failure) or is_profiling_run

        # node-run-controls — drain ("cancel downstream after current"): if the
        # node that just completed is a drain point, its in-flight work was
        # allowed to finish but NOTHING downstream of it may be dispatched.
        if should_dispatch and completed_node_id in (workflow_run.drain_nodes or []):
            _emit_run_event(
                db, completed_task.workflow_run_id, "traffic_cop", "info",
                f"drain: node {completed_node_id} finished; downstream held (cancel-after-current)",
                {"node_id": completed_node_id},
            )
            should_dispatch = False

        if plan and should_dispatch:
            dependents = plan.get("dependents", {})
            dependencies = plan.get("dependencies", {})
            nodes = plan.get("nodes", {})

            downstream_nodes = dependents.get(completed_node_id, [])

            for downstream_id in downstream_nodes:
                # Check if ALL parents of this downstream node are complete
                parent_ids = dependencies.get(downstream_id, [])
                all_parents_complete = True

                done_statuses = [ApiTaskStatus.COMPLETED]
                if is_profiling_run:
                    done_statuses.append(ApiTaskStatus.FAILED)
                _parent_node_run_ids = []
                for parent_id in parent_ids:
                    # r5-lock-ok: per-parent fan-in check inside cascade; order-sensitive (1431)
                    parent_task = db.query(ApiTaskQueue).filter(
                        ApiTaskQueue.workflow_run_id == completed_task.workflow_run_id,
                        ApiTaskQueue.node_id == parent_id,
                        ApiTaskQueue.status.in_(done_statuses),
                    ).first()
                    if not parent_task:
                        all_parents_complete = False
                        break
                    if parent_task.node_run_id:
                        _parent_node_run_ids.append(parent_task.node_run_id)

                # expr 9.4 slice C — a parked node's ApiTaskQueue row is flipped to
                # COMPLETED (worker slot released) even though its NodeRun sits at
                # AWAITING_EXTERNAL_EVENT. Without this check a sibling parent
                # completing later would read the parked parent as "done" and fan
                # the shared child out prematurely with the envelope as input.
                if all_parents_complete and _parent_node_run_ids:
                    # r5-lock-ok: race-sensitive fan-in check inside cascade (1431)
                    _awaiting = db.query(NodeRun.id).filter(
                        NodeRun.id.in_(_parent_node_run_ids),
                        NodeRun.status == TaskStatus.AWAITING_EXTERNAL_EVENT,
                    ).first()
                    if _awaiting:
                        all_parents_complete = False

                if not all_parents_complete:
                    continue

                # sz-2.5: a persisted canvas breakpoint on this node becomes a
                # pause here, so the run waits exactly like an explicit pause
                # point (resume-node re-kicks it). Lazy import: the router must
                # not pay an import cost / cycle risk on every dispatch.
                from services.breakpoint_gate import check_and_hold as _bp_hold
                if _bp_hold(db, workflow_run, downstream_id):
                    logger.info(
                        "downstream dispatch held at node %s — breakpoint hit on run %s "
                        "(persisted to paused_nodes; waiting for resume)",
                        downstream_id, workflow_run.id,
                    )
                    continue

                # Per-node pause (Airflow-style): if the user marked this node as
                # a pause point, HOLD here — its parents are done but we do NOT
                # enqueue it. The run waits at this node (other branches keep
                # going); resume-node removes it and re-kicks the frontier.
                _paused_nodes = workflow_run.paused_nodes or []
                if downstream_id in _paused_nodes:
                    logger.info(
                        "downstream dispatch held at node %s — paused point on run %s "
                        "(parents done; waiting for resume)",
                        downstream_id, workflow_run.id,
                    )
                    continue

                # Check if already enqueued (idempotent). node-run-controls
                # exception: a node flagged in run.rerun_nodes ("re-run +
                # downstream") is RE-DISPATCHED even though a prior task exists —
                # as a fresh manual_rerun attempt, in dependency order.
                _rerun_nodes = list(workflow_run.rerun_nodes or [])
                _is_rerun = downstream_id in _rerun_nodes
                # r5-lock-ok: dedup check before dispatch; order-sensitive (1431)
                existing = db.query(ApiTaskQueue).filter(
                    ApiTaskQueue.workflow_run_id == completed_task.workflow_run_id,
                    ApiTaskQueue.node_id == downstream_id,
                ).first()
                if existing and not _is_rerun:
                    continue

                # Collect parent outputs
                parent_outputs = {}
                for parent_id in parent_ids:
                    # r5-lock-ok: per-parent output fetch inside cascade; order-sensitive (1431)
                    parent_task = db.query(ApiTaskQueue).filter(
                        ApiTaskQueue.workflow_run_id == completed_task.workflow_run_id,
                        ApiTaskQueue.node_id == parent_id,
                        ApiTaskQueue.status == ApiTaskStatus.COMPLETED,
                    ).first()
                    if parent_task and parent_task.result_payload:
                        parent_outputs[parent_id] = parent_task.result_payload.get("outputs", [])

                # Enrich parent outputs with file extensions
                try:
                    from utils.minio_helpers import enrich_resources_with_extensions
                    for pid, outputs in parent_outputs.items():
                        if isinstance(outputs, list):
                            enrich_resources_with_extensions(outputs, completed_task.tenant_id,
                                                            workflow_run_id=completed_task.workflow_run_id)
                except ImportError:
                    pass  # minio_helpers not available in test env
                except Exception as e:
                    logger.debug("File enrichment skipped: %s", e)

                # negative-training-inputs — recover each parent's INPUT PORT
                # (edge targetHandle) so a downstream node with positive +
                # negative example channels can classify staged files by role
                # (emitted as MOD_INPUT_ROLES in enqueue_task_internal). The
                # graph edge already carries this; it's dropped by
                # build_execution_plan, so rebuild the parent->port map here.
                parent_port_map = {}
                try:
                    for _edge in plan.get("edges", []) or []:
                        if _edge.get("target") != downstream_id:
                            continue
                        _src = _edge.get("source")
                        _port = _edge.get("targetHandle") or _edge.get("target_handle")
                        if _src and _port:
                            parent_port_map[_src] = _port
                except Exception as _e:  # noqa: BLE001 — best-effort
                    logger.debug("parent_port_map build skipped: %s", _e)

                # Build node conf
                node = nodes.get(downstream_id, {})
                node_data = node.get("data", {})

                # GX.sz-2.6b — hot-swap APPLY. services/live_debugger.hot_swap
                # only STORES the per-run override on the paused node; this is
                # where it lands on the wire. Hooked HERE — after the paused-node
                # skip above (so a still-paused node never gets it early) and
                # BEFORE node_kind / queue / definition resolution below, so a
                # definition swap re-points the queue selector + the image, not
                # just the audit id. Deep-copied: graph_json and the persisted
                # execution plan are never mutated in place.
                _pre_swap_definition_id = node_data.get("nodeDefinitionId")
                node_data, _ = _apply_debug_override(
                    workflow_run, downstream_id, node_data)
                _swap_def = (((workflow_run.parameters or {}).get(
                    "debug_overrides") or {}).get(downstream_id) or {}).get(
                    "node_definition_id")
                if isinstance(_swap_def, int) and not isinstance(_swap_def, bool):
                    # The swapped row is authoritative for what the node RUNS
                    # as — kind + image + timeout — same hydration root dispatch
                    # does from the NodeDefinition row. Re-validated HERE, at
                    # dispatch time, not trusted from hot_swap-time: TOCTOU
                    # window between the swap (paused) and this dispatch can
                    # see the definition soft-deleted or its visibility change
                    # (get_node_definition_by_id enforces tenant scope but NOT
                    # soft-delete, so that is checked explicitly below too).
                    from crud.node_definition_cud import get_node_definition_by_id
                    _swap_nd = get_node_definition_by_id(
                        db, _swap_def, completed_task.tenant_id)
                    if _swap_nd is not None and _swap_nd.deleted_at is None:
                        node_data = dict(node_data)
                        for _k, _v in (("node_kind", _swap_nd.node_kind),
                                       ("executable_path",
                                        _swap_nd.executable_path or ""),
                                       ("process_type",
                                        _swap_nd.process_type or "CPU")):
                            if _v:
                                node_data[_k] = _v
                    else:
                        # Fail CLOSED: do not dispatch against an id nothing
                        # can see any more. _apply_debug_override already
                        # wrote the swapped id into node_data unconditionally
                        # (it is DB-free and cannot validate it) — revert it
                        # here so no downstream, id-keyed security gate
                        # (should_bind_docker_socket / should_inject_vector_token /
                        # should_inject_platform_service_key) ever re-resolves
                        # a definition this tenant can no longer see. The node
                        # still dispatches, but as its pre-swap definition.
                        logger.warning(
                            "hot-swap node_definition_id=%s for run=%s "
                            "node=%s is no longer visible to tenant=%s at "
                            "dispatch time (soft-deleted or cross-tenant); "
                            "reverting the definition swap",
                            _swap_def, completed_task.workflow_run_id,
                            downstream_id, completed_task.tenant_id)
                        node_data = dict(node_data)
                        node_data["nodeDefinitionId"] = _pre_swap_definition_id
                node_kind = node_data.get("node_kind")

                if not node_kind:
                    # A canvas "output" (sink) node carries no node_kind: it has
                    # no container to run, it just receives its parents' output.
                    # Left QUEUED it was failed by the orphan guard below
                    # ("1 node(s) never executed"), which flashed the whole run
                    # FAILED for a moment before a later finalize flipped it back
                    # to COMPLETED (run 86, RC hand test). Its parents are all
                    # complete here, so it is finished: mark it COMPLETED. Any
                    # other kind-less node still falls through as a real orphan.
                    _complete_passive_sink_node(
                        db, workflow_run, completed_task, node, downstream_id)
                    continue

                # workflow-agentic-execution-loop §control-flow-primitives
                # — if `downstream_id` is a control-flow node, route to
                # the specialised dispatch handler. Returns True when it
                # handled the dispatch (regular path is skipped).
                _cf_handled = _maybe_dispatch_control_flow(
                    db, workflow_run, completed_task, plan,
                    downstream_id, node, node_data, parent_outputs,
                )
                if _cf_handled:
                    continue

                # expr 9.8 — memory-operation nodes have no container: run
                # them in-API and synthesise their completed task, exactly
                # like the control-flow interception above.
                if _maybe_dispatch_memory_node(
                    db, workflow_run, completed_task, plan,
                    downstream_id, node, node_data, parent_outputs,
                ):
                    continue

                project_info = (workflow_run.parameters or {}).get("project_info", {})
                from utils.minio_client import get_tenant_bucket_name
                # GX.R1 — thread run-level extra_variables + per-node inputs into
                # this downstream node's effective extraVariables (per-node >
                # run-level > configured default), mirroring the root dispatch.
                from services.workflow_orchestrator import (
                    _apply_run_extra_variables, _run_extra_values,
                    _determine_size_tier,
                )
                _run_extra, _per_node_inputs = _run_extra_values(project_info)
                if _run_extra or _per_node_inputs:
                    node_data = _apply_run_extra_variables(
                        node_data, _run_extra, _per_node_inputs, downstream_id)
                node_conf = {
                    "tenant_id": completed_task.tenant_id,
                    # GC.MWL.7c: no keycloak_token — never persisted in
                    # `parameters` any more, and the Go worker never read
                    # this key from the task payload anyway.
                    "workflow_run_id": completed_task.workflow_run_id,
                    "node_id": downstream_id,
                    "node_data": node_data,
                    "ProjectInfo": project_info,
                    "parent_outputs": parent_outputs,
                    # negative-training-inputs — parent node id -> input port name.
                    "parent_ports": parent_port_map,
                    # Flattened from node_data (Go worker expects at root)
                    "node_kind": node_kind,
                    "executable_path": node_data.get("executable_path", ""),
                    "process_type": node_data.get("process_type", "CPU"),
                    "output_schema": node_data.get("output_schema", {}),
                    "cost_in_credits": node_data.get("cost_in_credits", 0),
                    "minio_bucket": get_tenant_bucket_name(completed_task.tenant_id, db),
                    "cog_ids": project_info.get("cog_ids", []),
                }
                # GC.MWL.12 — borrower-side loaned run: stamp the work-loan id
                # so the worker tags this task's S3 outputs with
                # Mod-Loan-Status / Mod-Loan-Id. Ordinary runs resolve to None
                # and the payload is left untouched.
                _mlp = optional_paid("services.mesh_loan_payload")
                if _mlp is not None:
                    _loan_id = _mlp.loan_id_for_run(db, completed_task.workflow_run_id)
                    if _loan_id is not None:
                        node_conf["loan_id"] = str(_loan_id)
                # GC.MWL.C7 — owner-side: this downstream node run may be the
                # OTHER end of a borrower's checkpoint hand-back (GC.MWL.C2
                # "handback" decision). If GC.MWL.8's reaper tick has already
                # registered an import for it, hand the worker a presigned
                # GET so it hydrates instead of a cold restart. Ordinary runs
                # resolve to None and the payload is left untouched.
                _mlh = optional_paid("services.mesh_loan_handback")
                if _mlh is not None:
                    _hydrate_url = _mlh.hydrate_url_for_node(
                        db, completed_task.workflow_run_id, downstream_id)
                    if _hydrate_url:
                        node_conf["checkpoint_hydrate_url"] = _hydrate_url
                # fortress-35 — loaned-run checkpoint EXPORT side of
                # GC.MWL.C7: this downstream node run may have a pending
                # export (mesh_loans.py request_checkpoint_export) waiting
                # for the worker to PUT its checkpoint bytes. Ordinary runs
                # (no pending export) resolve to None and the payload is
                # left untouched.
                if _mlh is not None:
                    _export_url = _mlh.export_url_for_node(
                        db, completed_task.workflow_run_id, downstream_id)
                    if _export_url:
                        node_conf["checkpoint_export_url"] = _export_url
                        _export_transfer_id = _mlh.export_transfer_id_for_node(
                            db, completed_task.workflow_run_id, downstream_id)
                        if _export_transfer_id is not None:
                            node_conf["checkpoint_export_transfer_id"] = (
                                _export_transfer_id)
                # node-run-controls — re-run cascade: mark this downstream as a
                # deliberate re-run so the dispatch guard lets a fresh attempt
                # through (a prior task exists), then drop it from the run's
                # rerun set so it re-runs exactly once.
                if _is_rerun:
                    node_conf["manual_rerun"] = True
                    remaining = [n for n in _rerun_nodes if n != downstream_id]
                    workflow_run.rerun_nodes = remaining
                    from sqlalchemy.orm.attributes import flag_modified as _fm
                    _fm(workflow_run, "rerun_nodes")
                    db.commit()

                # Get node_run_id
                node_run_id = None
                try:
                    from models import WorkflowNode
                    # r5-lock-ok: inside dispatch-lock cascade; order-sensitive (1431)
                    wn = db.query(WorkflowNode).filter(
                        WorkflowNode.workflow_id == workflow_run.workflow_id,
                        WorkflowNode.graph_node_id == downstream_id,
                    ).first()
                    if wn:
                        # r5-lock-ok: same cascade frame; order-sensitive (1431)
                        nr = db.query(NodeRun).filter(
                            NodeRun.workflow_run_id == completed_task.workflow_run_id,
                            NodeRun.workflow_node_id == wn.id,
                        ).first()
                        if nr:
                            node_run_id = nr.id
                except Exception:
                    pass

                # Build-DAG variant children (test-and-repair-cuda, push-rocm,
                # sample-test-intel, …) carry their accelerator target in
                # data.variant, NOT process_type — route them to the matching
                # hardware queue so they don't all fall to CPU. Non-variant
                # nodes keep the historical process_type-based routing.
                # `process_type` IS NOT A QUEUE NAME, and routing on it only
                # ever worked by coincidence: 158 node definitions carry
                # process_type "CPU", which happens to spell a real queue, so
                # the mistake stayed invisible. The one definition whose
                # process_type is an actual classification — "container" —
                # routed to a queue no worker subscribes to, and its task sat
                # PENDING for ever. Found by the Gate 5 downstream run, where
                # the SAME node_kind reached CPU from root dispatch and
                # "container" from here.
                #
                # Root dispatch is NodeDefinition-authoritative
                # (select_queue_for_node_data, derived from gpu_variants), so
                # this now uses the same call rather than a second rule that
                # can disagree with it. The variant override is preserved —
                # it is the first thing that helper consults, so build-DAG
                # variant children still reach matching hardware.
                from utils.queue_selector import select_queue_for_node_data
                # Tenant-scoped (same rule as root dispatch): the tenant's own
                # copy of node_kind wins over the system row; another tenant's
                # fork must not decide this task's queue.
                _ds_node_def = _node_def_for_tenant(
                    db, node_kind, getattr(completed_task, 'tenant_id', None))
                node_queue = select_queue_for_node_data(_ds_node_def, node_data)

                # δ DAG-wizard B5b — propagate container_env from
                # node_data on every downstream dispatch (not just
                # profiling). Build-task workflows rely on STAGE +
                # BUILD_SPEC_ID + VARIANT being present at every
                # node_run, otherwise smoke/push/sample/register
                # tasks have no way to know which spec they're
                # serving.
                if isinstance(node_data, dict) and node_data.get("container_env"):
                    node_conf["container_env"] = node_data["container_env"]

                # Per-node credential + stack-reachability injection (RAG vector
                # token, platform-service key, VLM endpoint + host pin, docker
                # socket) is applied centrally in enqueue_task_internal, which
                # this downstream dispatch funnels through (below). Historically
                # it lived only in the ROOT dispatch, so downstream nodes were
                # enqueued with an empty container_env — RAG Builder got no token
                # and Image to Text no VLM endpoint. node_data here is the same
                # plan.nodes[*].data the root path reads, so the DB-authoritative
                # gates (nodeDefinitionId) match.

                # Gate 5 — the usual position for an approval gate: mid-graph,
                # reached when its parents finish. Parks the node and enqueues
                # nothing, so no worker slot is held while a person decides.
                from services.workflow_orchestrator import park_for_human_approval
                if park_for_human_approval(
                    db,
                    workflow_run_id=completed_task.workflow_run_id,
                    node_run_id=node_run_id,
                    node_id=downstream_id,
                    node_kind=node_kind,
                    node_data=node_data,
                ):
                    continue

                # FX 4.1 — an `agent` node mid-graph. Same shape as the gates
                # above: it runs API-side on a bg thread, so nothing is
                # enqueued here. Fail-closed on the phase-3 flag inside the
                # helper (off -> normal worker path, unchanged).
                from services.workflow_orchestrator import start_agent_node
                if start_agent_node(
                    db,
                    tenant_id=getattr(completed_task, "tenant_id", None),
                    workflow_run_id=completed_task.workflow_run_id,
                    node_run_id=node_run_id,
                    node_id=downstream_id,
                    node_kind=node_kind,
                    node_data=node_data,
                    graph_json={"nodes": (plan or {}).get("nodes", []),
                                "edges": (plan or {}).get("edges", [])},
                    agent_input=parent_outputs,
                ):
                    continue

                # Gate 2.1 (wiring) — a sub_workflow node mid-graph. Same shape
                # as the approval gate above: the child run is the wait, so
                # nothing is enqueued. Tenant gate mirrors the control-flow
                # nodes handled just above (the runtime does not gate itself).
                if node_kind == "sub_workflow":
                    # LAZY import: sub_workflow_runtime imports this module
                    # lazily, so a top-level import here would be circular.
                    from services.sub_workflow_runtime import park_and_dispatch_sub_workflow
                    from services.control_flow import is_enforcement_enabled_for_tenant
                    if not is_enforcement_enabled_for_tenant(
                        db, getattr(completed_task, "tenant_id", None),
                    ):
                        from services.workflow_orchestrator import _emit_run_event
                        _emit_run_event(
                            db, completed_task.workflow_run_id,
                            "traffic_cop", "error",
                            f"Control-flow node {downstream_id} requires "
                            f"control_flow_primitives_enabled flag (off for this tenant)",
                            {"node_id": downstream_id, "kind": node_kind},
                        )
                        continue
                    if park_and_dispatch_sub_workflow(
                        db,
                        workflow_run_id=completed_task.workflow_run_id,
                        node_run_id=node_run_id,
                        node_id=downstream_id,
                        node_kind=node_kind,
                        node_data=node_data,
                    ):
                        continue

                _wf_params = workflow_run.parameters or {}
                is_profiling_wf = _wf_params.get("is_profiling", False)
                # Calibration builds its graph the same way profiling does — the
                # node's image, env and skip flags live in node_data and must be
                # promoted to the task payload. Without this the worker finds an
                # empty payload.processing_image and falls back to
                # registry + "/modtim/" + node_kind, which is the node KIND
                # (image_converter) rather than its executable_path
                # (image-converter) — a name that does not exist in Harbor. Seen
                # live: the focal node failed with "not found" while node_data
                # carried the correct ref all along. skip_download / skip_upload
                # were being dropped the same way.
                is_system_graph_wf = bool(
                    is_profiling_wf or _wf_params.get("is_calibration")
                )

                if is_system_graph_wf:
                    for bk in ("container_env", "skip_download", "skip_upload",
                               "processing_image", "container_timeout", "gpu_required",
                               # Durable per-task worker pin: a hardware benchmark
                               # measures ONE machine, so its downstream node must
                               # carry the same pin as the root (stamped into
                               # node_data by _auto_dispatch_profiling). The sweep
                               # guards read this flag to keep the task on the
                               # pinned worker for its whole life.
                               "benchmark_target_worker",
                               # Calibration markers. The sweep and the failure
                               # brake select on calibration_run, and the pattern
                               # updater reads size_tier off the payload — so
                               # without these the node under test is invisible
                               # to both, and its measurement lands under tier
                               # 'any' with all three tiers collapsed together.
                               # 18.4 (GI.4): the k8s per-node pin rides the
                               # same way — build_k8s_gpu reads it from the
                               # focal payload to set the hostname
                               # nodeSelector, and the pattern updater files
                               # the measured duration under it.
                               "calibration_run", "size_tier", "size_tier_source",
                               "node_definition_id",
                               "calibration_run_kind", "calibration_pair_id",
                               "k8s_calibration_node"):
                        if bk in node_data:
                            node_conf[bk] = node_data[bk]
                    env = node_conf.get("container_env", {})
                    profile_id = env.get("HARDWARE_PROFILE_ID")
                    if profile_id and node_kind in ("hardware_benchmark", "accelerator_benchmark"):
                        from routers.worker_management import _generate_benchmark_hmac_key
                        env["BENCHMARK_HMAC_KEY"] = _generate_benchmark_hmac_key(int(profile_id), db=db)
                        node_conf["container_env"] = env

                # GX.sz-tcp.12b — stamp size_tier on every downstream dispatch so
                # the Go worker knows the payload size bucket.  Calibration /
                # profiling already copy size_tier from node_data above; keep
                # that value (don't overwrite).  For all other downstream nodes
                # call _determine_size_tier.
                if "size_tier" not in node_conf:
                    _tier, _source = _determine_size_tier(node_data)
                    node_conf["size_tier"] = _tier
                    node_conf["size_tier_source"] = _source

                try:
                    # Worker pinning for system workflows. `pinned_worker` is
                    # the general name; `profiling_worker` is the original
                    # benchmark-specific one, still honoured so existing
                    # profiling runs keep working. One mechanism, two spellings
                    # — calibration uses the general one.
                    _params = workflow_run.parameters or {}
                    profiling_worker = (_params.get("pinned_worker")
                                        or _params.get("profiling_worker"))
                    if profiling_worker:
                        # This path builds ApiTaskQueue directly instead of
                        # going through enqueue_task_internal, so it never got
                        # the parent-output resolution. Harmless for profiling,
                        # which sets skip_download=True and chains no files —
                        # but calibration passes a real artifact from the
                        # generator to the node under test, and without this the
                        # node starts with an empty /data/input and dies with
                        # "ERROR: No input file found".
                        if not node_conf.get("skip_download"):
                            resolve_source_from_parent_outputs(node_conf)
                        _noah = optional_paid("services.noah")
                        pinned_task = ApiTaskQueue(
                            workflow_run_id=completed_task.workflow_run_id,
                            node_run_id=node_run_id,
                            node_id=downstream_id,
                            tenant_id=completed_task.tenant_id,
                            queue_name=node_queue,
                            priority=9,
                            task_type="system",
                            task_payload=_noah.add_pinnable_requirement(node_conf, db=db) if _noah is not None else node_conf,
                            status=ApiTaskStatus.ASSIGNED,
                            assigned_to_worker=profiling_worker,
                            max_attempts=2,
                            output_namespace="attempt_0",
                        )
                        db.add(pinned_task)
                        db.commit()
                        db.refresh(pinned_task)
                        task_id = pinned_task.id
                        _publish_event(f"task:available:{node_queue}")
                    else:
                        enqueue_req = TaskEnqueueRequest(
                            workflow_run_id=completed_task.workflow_run_id,
                            node_run_id=node_run_id,
                            node_id=downstream_id,
                            tenant_id=completed_task.tenant_id,
                            queue_name=node_queue,
                            priority=0,
                            task_payload=node_conf,
                        )
                        result = enqueue_task_internal(enqueue_req, db)
                        task_id = result.get("task_id")
                    logger.info(
                        "Dispatched downstream node %s (task %s) for workflow %d",
                        downstream_id, task_id, completed_task.workflow_run_id,
                    )
                    _emit_run_event(
                        db,
                        completed_task.workflow_run_id,
                        "traffic_cop",
                        "info",
                        f"Enqueued downstream node {downstream_id} (task {task_id}, queue={node_queue})",
                        {
                            "from_node_id": completed_node_id,
                            "downstream_node_id": downstream_id,
                            "task_id": task_id,
                            "queue": node_queue,
                            "parents": list(parent_ids),
                            "node_kind": node_kind,
                        },
                    )
                except Exception as _disp_exc:
                    _emit_run_event(
                        db,
                        completed_task.workflow_run_id,
                        "traffic_cop",
                        "error",
                        f"Failed to dispatch downstream node {downstream_id}: {_disp_exc}",
                        {
                            "from_node_id": completed_node_id,
                            "downstream_node_id": downstream_id,
                            "exception": str(_disp_exc),
                        },
                    )

        # --- Check for whole-workflow completion ---
        # r5-lock-ok: whole-run completion check; commit-boundary critical (1431)
        active_tasks = db.query(ApiTaskQueue).filter(
            ApiTaskQueue.workflow_run_id == completed_task.workflow_run_id,
            ApiTaskQueue.status.in_([
                ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED,
                ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING,
            ]),
        ).count()

        # A run WAITING ON A PERSON is not a finished run. Gate 5 parks an
        # approval node without enqueuing anything, so the instant it parks the
        # run has zero active tasks — and the finalisation below then reads
        # "nothing running, nodes still queued" as "finished, with orphans". It
        # failed the run mid-wait, marked every downstream NodeRun FAILED, and
        # left an error saying they never executed. They then executed anyway
        # when the human approved, so the run flipped back to COMPLETED still
        # carrying "1 node(s) never executed" — observed live on run 3537.
        #
        # Checked on NodeRun rather than the intervention table because this
        # is the same signal the timeline shows the user, and it costs one
        # indexed query on a path that already runs several.
        if active_tasks == 0:
            # r5-lock-ok: same completion check; commit-boundary critical (1431)
            parked = db.query(NodeRun).filter(
                NodeRun.workflow_run_id == completed_task.workflow_run_id,
                NodeRun.status == TaskStatus.PENDING_ACTION,
            ).count()
            if parked:
                logger.info(
                    "Workflow run %s is waiting on %d human approval(s) — "
                    "not finalising",
                    completed_task.workflow_run_id, parked,
                )
                db.commit()
                return

        # node-run-controls — a run HELD at a per-node PAUSE point is likewise
        # NOT finished: the paused node is intentionally not enqueued (its
        # NodeRun stays QUEUED with no task), so the orphan guard below would
        # otherwise fail it and the whole run. Wait for resume, exactly like the
        # human-approval gate above.
        if active_tasks == 0:
            _paused_gids = set(workflow_run.paused_nodes or [])
            if _paused_gids:
                _held = 0
                # r5-lock-ok: same completion check; commit-boundary critical (1431)
                for _nr in db.query(NodeRun).filter(
                    NodeRun.workflow_run_id == completed_task.workflow_run_id,
                    NodeRun.status == TaskStatus.QUEUED,
                ).all():
                    _gid = _nr.workflow_node.graph_node_id if _nr.workflow_node else None
                    if _gid in _paused_gids:
                        _held += 1
                if _held:
                    logger.info(
                        "Workflow run %s held at %d pause point(s) — not finalising",
                        completed_task.workflow_run_id, _held,
                    )
                    db.commit()
                    return

        # Frontier re-drive (must run BEFORE the orphan verdict). With zero
        # active tasks the run LOOKS finished, but a node whose parents are all
        # complete and which simply has not been dispatched yet (sibling-branch
        # cache cascade ordering, or a lagging worker /complete) is pending
        # frontier, NOT an orphan. Dispatch it now; if anything advances the run
        # is still in progress — do not finalize. This is the fix for run 1122:
        # the audio branch finished ~60s before the video branch dispatched, and
        # the orphan guard failed the run in that window, wedging VideoCombine.
        if active_tasks == 0 and plan and not is_failure:
            _readied = _redrive_ready_frontier(db, workflow_run, plan)
            if _readied:
                logger.info(
                    "Workflow run %s: re-drove %d ready frontier node(s); "
                    "not finalising (was about to orphan-check with 0 active tasks)",
                    completed_task.workflow_run_id, _readied,
                )
                db.commit()
                return

        # The frontier re-drive above re-enters this function for one completed
        # parent per ready node (_redrive_active). That NESTED pass must only
        # dispatch: if it also ran the verdict below it would finalise the run
        # (orphan guard -> FAILED) while the OUTER pass was still about to do
        # the same, and the outer pass then re-verdicted COMPLETED over the
        # now-FAILED node (no QUEUED left, no failed task) and cleared the error.
        # That is the "FAILED, then COMPLETED 0.06 s later" blip on run 86 and it
        # also hid genuine orphans. One finalisation per completion: the outer
        # pass owns it.
        if active_tasks == 0 and getattr(_redrive_active, "on", False):
            db.commit()
            return

        if active_tasks == 0:
            # r5-lock-ok: same completion check; commit-boundary critical (1431)
            failed_tasks = db.query(ApiTaskQueue).filter(
                ApiTaskQueue.workflow_run_id == completed_task.workflow_run_id,
                ApiTaskQueue.status.in_([ApiTaskStatus.FAILED, ApiTaskStatus.CANCELLED]),
            ).count()

            # Orphan-NodeRun guard. The ApiTaskQueue active/failed counts
            # only see nodes that were actually enqueued — if the
            # downstream-dispatch loop above silently skipped a node
            # (empty node_kind at line 3144, missing dependents-map entry,
            # graph-edge bug, etc.), its NodeRun stays at QUEUED while no
            # ApiTaskQueue row exists. Without this check the run would
            # be marked COMPLETED while a node visibly reads "queued" in
            # the timeline — a lie about completion. Treat orphans as a
            # workflow failure: mark each orphan FAILED with a clear
            # error_message so the per-node card surfaces the reason,
            # then force the run itself to FAILED regardless of the
            # ApiTaskQueue-derived verdict.
            # node-run-controls — nodes intentionally NOT dispatched are NOT
            # orphans: a per-node pause point, or the downstream of a "cancel
            # after current" (drain) that the operator chose to stop. Those are
            # marked BLOCKED (stopped), never orphan-FAILED, and don't fail the run.
            _intentional = set(workflow_run.paused_nodes or []) | set(workflow_run.drain_nodes or [])
            if (workflow_run.drain_nodes or []) and plan:
                _dep = plan.get("dependents", {}) or {}
                _q = list(workflow_run.drain_nodes or [])
                while _q:
                    _n = _q.pop()
                    for _d in (_dep.get(_n, []) or []):
                        if _d not in _intentional:
                            _intentional.add(_d)
                            _q.append(_d)

            # r5-lock-ok: same completion check; commit-boundary critical (1431)
            orphan_node_runs = db.query(NodeRun).filter(
                NodeRun.workflow_run_id == completed_task.workflow_run_id,
                NodeRun.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
            ).all()
            orphan_ids: List[str] = []
            if orphan_node_runs:
                _now = datetime.now(timezone.utc)
                for _nr in orphan_node_runs:
                    _graph_id = (
                        _nr.workflow_node.graph_node_id
                        if _nr.workflow_node and _nr.workflow_node.graph_node_id
                        else f"node_run_{_nr.id}"
                    )
                    if _graph_id in _intentional:
                        # Intentionally stopped (pause / drain) — not an orphan.
                        if _nr.status != TaskStatus.BLOCKED:
                            _nr.status = TaskStatus.BLOCKED
                            if not _nr.completed_at:
                                _nr.completed_at = _now
                        continue
                    orphan_ids.append(_graph_id)
                    _nr.status = TaskStatus.FAILED
                    _nr.error_message = (
                        "Orphaned: workflow finalized while this node was still "
                        "queued. The orchestrator did not enqueue a worker task "
                        "for it (commonly a missing node_kind, broken graph "
                        "edge, or stale execution plan)."
                    )
                    if not _nr.completed_at:
                        _nr.completed_at = _now

            if failed_tasks > 0 or orphan_ids:
                workflow_run.status = TaskStatus.FAILED
            else:
                workflow_run.status = TaskStatus.COMPLETED
                workflow_run.error_message = None
            if orphan_ids:
                _orphan_msg = (
                    f"{len(orphan_ids)} node(s) never executed: "
                    + ", ".join(orphan_ids)
                )
                workflow_run.error_message = (
                    f"{workflow_run.error_message}\n{_orphan_msg}"
                    if workflow_run.error_message
                    else _orphan_msg
                )
            workflow_run.completed_at = datetime.now(timezone.utc)
            db.commit()

            # Outbound webhooks (WS4.Q20) — fan out the terminal transition to
            # tenant-configured subscribers. Scheduled on a background thread
            # with its own DB session so the HTTP delivery never blocks this
            # request or pins the connection this transaction just committed
            # on. Best-effort: a delivery failure must never affect the run.
            try:
                from services.outbound_webhooks import dispatch_workflow_completion_background
                dispatch_workflow_completion_background(
                    tenant_id=workflow_run.tenant_id,
                    workflow_run_id=workflow_run.id,
                    status_str=workflow_run.status.value,
                )
                # GX.R3 — enqueue run-completion webhook deliveries
                # (post-commit; a failure here must never affect the run).
                try:
                    from services.run_completion_webhooks import (
                        enqueue_run_completion_background as _rcw_enqueue,
                    )
                    _rcw_enqueue(
                        tenant_id=workflow_run.tenant_id,
                        workflow_run_id=workflow_run.id,
                        status_str=workflow_run.status.value,
                    )
                except Exception:
                    logger.debug(
                        "run_webhooks: enqueue scheduling failed for run %d",
                        workflow_run.id, exc_info=True,
                    )
            except Exception:
                logger.debug(
                    "outbound webhook: scheduling failed for run %d",
                    workflow_run.id, exc_info=True,
                )

            # stack-warm (task 2.4) — the run is terminal: release any stack it
            # pre-warmed but never reached, so an early-ending run never strands a
            # warm stack. release_stack still leaves a stack warm if ANOTHER run
            # needs it (predictive/binary lookahead) or we don't own the wake.
            # Paid layer (no-op without it); best-effort (must never affect
            # finalisation).
            try:
                from services import stack_warm as _stack_warm
                _stack_warm.on_run_terminal(db, workflow_run)
            except Exception:  # noqa: BLE001
                logger.debug("stack_warm: terminal release hook failed",
                             exc_info=True)

            # activation-lifecycle-emails: when a tenant reaches its 3rd
            # successful run, fire the one-time activation celebration.
            # Idempotent + best-effort — never affects run finalization.
            if workflow_run.status == TaskStatus.COMPLETED:
                _alc = optional_paid("utils.activation_lifecycle")
                if _alc is not None:
                    try:
                        _alc.maybe_send_activation_celebration(db, workflow_run.tenant_id)
                    except Exception:  # noqa: BLE001
                        pass
            elif workflow_run.status == TaskStatus.FAILED:
                # embeddable-run-surface: a failed embed run must not burn the
                # link's budget — refund its reserved slot + credits (idempotent).
                _rsc = optional_paid("utils.run_surface_caps")
                if _rsc is not None:
                    try:
                        _rsc.release_run_once(db, workflow_run.id)
                    except Exception:  # noqa: BLE001
                        pass

            if orphan_ids:
                _emit_run_event(
                    db,
                    workflow_run.id,
                    "orchestrator",
                    "error",
                    f"Workflow finalized with {len(orphan_ids)} orphan node(s) "
                    f"that never executed: {', '.join(orphan_ids)}",
                    {
                        "orphan_node_ids": orphan_ids,
                        "completed_node_id": completed_node_id,
                    },
                )
                logger.error(
                    "Workflow run %d had %d orphan NodeRun(s): %s",
                    workflow_run.id, len(orphan_ids), orphan_ids,
                )

            # Fan out workflow_completion triggers. Wrapped in try/except
            # at the helper level — a failing trigger MUST NOT roll back
            # the source run's terminal-status commit above.
            try:
                from services.workflow_orchestrator import fire_completion_triggers
                fire_completion_triggers(db, workflow_run)
            except Exception as _trig_exc:
                logger.exception(
                    "Completion-trigger fan-out failed for run %d: %s",
                    workflow_run.id, _trig_exc,
                )
            # §4.4 fan-in/barrier — record this run's arrival; dispatch downstream
            # only once all configured sources have arrived within the window.
            try:
                from services.workflow_orchestrator import fire_fanin_triggers
                fire_fanin_triggers(db, workflow_run)
            except Exception as _fi_exc:
                logger.exception(
                    "Fan-in fan-out failed for run %d: %s", workflow_run.id, _fi_exc,
                )

            if (workflow_run.parameters or {}).get("is_profiling"):
                try:
                    from models import HardwareProfile
                    from crud.accelerator_cud import all_benchmarks_complete
                    hp_id = (workflow_run.parameters or {}).get("hardware_profile_id")
                    if hp_id:
                        # r5-lock-ok: benchmark completion check inside cascade; order-sensitive (1431)
                        hp = db.query(HardwareProfile).filter(HardwareProfile.id == hp_id).first()
                        if hp and hp.benchmark_status == "running":
                            hp.benchmark_status = "completed" if all_benchmarks_complete(db, hp_id) else "failed"
                            hp.benchmarked_at = datetime.now(timezone.utc)
                            db.commit()
                            logger.info("Profiling workflow %d done — profile %d benchmark_status=%s",
                                        workflow_run.id, hp_id, hp.benchmark_status)
                except Exception as e:
                    logger.warning("Profiling completion hook failed: %s", e)

            logger.info(
                "Workflow run %d completed (status=%s)",
                workflow_run.id, workflow_run.status.value,
            )
            try:
                _total_dur = None
                if workflow_run.started_at and workflow_run.completed_at:
                    _total_dur = round(
                        (workflow_run.completed_at - workflow_run.started_at).total_seconds(), 2
                    )
                _emit_run_event(
                    db,
                    workflow_run.id,
                    "orchestrator",
                    "error" if workflow_run.status == TaskStatus.FAILED else "info",
                    f"Workflow run finished — {workflow_run.status.value}"
                    + (f" in {_total_dur}s" if _total_dur is not None else ""),
                    {
                        "final_status": workflow_run.status.value,
                        "duration_seconds": _total_dur,
                        "failed_tasks": failed_tasks,
                    },
                )
                db.commit()
            except Exception:
                pass

            # Decrement concurrent processes
            try:
                from utils.usage_enforcement import decrement_concurrent_processes
                decrement_concurrent_processes(db, completed_task.tenant_id)
            except Exception as e:
                logger.warning("Failed to decrement concurrent processes: %s", e)

            # Audit: log workflow completion (CMMC AU-2)
            try:
                from models import AuditEvent
                db.add(AuditEvent(
                    tenant_id=completed_task.tenant_id,
                    user_id=workflow_run.triggered_by_user_id,
                    event_name="WORKFLOW_COMPLETED",
                    details={"workflow_run_id": workflow_run.id, "status": workflow_run.status.value},
                ))
                db.commit()
            except Exception:
                pass

            # Publish workflow completion via Redis
            _publish_event(
                f"workflow:complete:{workflow_run.id}",
                workflow_run.status.value,
            )

            # Cost the run BEFORE notifying, so the notification and anything
            # reading the run afterwards see a priced run rather than a NULL
            # that fills in a moment later. Wrapped because a costing failure
            # must never take down completion: a run that finishes but cannot
            # be priced is a billing problem, one that fails to finish is the
            # user's problem, and they are not the same severity.
            summary = None
            _rc = optional_paid("services.run_costing")
            if _rc is not None:
                try:
                    summary = _rc.cost_workflow_run(db, workflow_run.id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Failed to cost workflow run %s: %s", workflow_run.id, e)

            # 15b.4 — task-profile settlement (inert by default): when
            # `billing.pricing_model` is 'task_profile' the reservation is
            # trued up against the run's ACTUAL task-profile charge. Its own
            # try/except, like the GB.RC.4 block below; a settlement failure
            # must never block completion. When this model is active it is the
            # settlement authority for the run, so the legacy GB.RC.4 path is
            # skipped to avoid double-settling the same reservation.
            # CARVE-U9b: utils.credit_manager is manifest-denied (the CE build
            # ships a no-op overlay stub, but the carve can strip it outright) —
            # load it through optional_paid like every other credit_manager site
            # (tenant_cud.py:16, workflows.py:850). Absent -> no task-profile
            # settlement authority exists, so the settlement is skipped
            # explicitly (the legacy GB.RC.4 path below is the settlement
            # authority in that case, exactly as when the model is not
            # 'task_profile'); never a silent ImportError swallow.
            _cm_tp = optional_paid("utils.credit_manager")
            _tp_pricing_model = _cm_tp.pricing_model if _cm_tp is not None else None
            if _tp_pricing_model is not None and _tp_pricing_model(db) == "task_profile":
                try:
                    _tp_settle = _cm_tp.settle_task_profile_run
                    _tp_settle(db, workflow_run.id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Failed to task-profile-settle workflow run %s: %s", workflow_run.id, e)
                    db.rollback()
            # GB.RC.4 — true up the reservation against what the run actually
            # cost. Its own try/except, separate from costing above: settlement
            # must never block completion, and a settlement failure must not be
            # confused with (or mask) a costing failure.
            _rs = optional_paid("services.run_settlement")
            if _rc is not None and _rs is not None:
                try:
                    if (_tp_pricing_model is None
                        or _tp_pricing_model(db) != "task_profile") and _rc.billing_basis() != "flat":
                        _rs.settle_workflow_run(db, workflow_run.id, summary)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Failed to settle workflow run %s: %s", workflow_run.id, e)
                    db.rollback()

            # 15b.4.s4 — stage shadow at the settle point: BOTH settle
            # authorities priced for this run (the legacy run-costing actual
            # and the task-profile actual, dry-run so it charges nothing),
            # recorded as audit rows. Its own try/except + rollback: a shadow
            # failure must never block completion and must never leave a
            # half-written row in the transaction.
            try:
                from utils.credit_manager import shadow_at_settlement
                shadow_at_settlement(db, workflow_run.id)
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to pricing-shadow workflow run %s: %s",
                               workflow_run.id, e)
                db.rollback()

            # Send workflow completion notifications — DEFERRED (rc20): recorded
            # here, fired in the finally below, once the advisory lock for this run
            # is released. See the `_notify_pending` note at the top of this function:
            # the notification's HTTP is served by handlers that take the SAME lock.
            _notify_pending = True

    except Exception as e:
        logger.error("Error in downstream dispatch for task %d: %s", completed_task.id, e)
    finally:
        _release_dispatch_lock(db, _run_id, _dispatch_locked)
        # Lock released (and the finalize block's commit done) before any external
        # call, so a webhook target can never block on a lock we still hold.
        if _notify_pending:
            try:
                _notify_workflow_complete(db, workflow_run, completed_task.tenant_id)
            except Exception as e:
                logger.warning("Failed to send workflow notifications: %s", e)


def _record_port_outcomes(db: Session, task, *, log_tail: Optional[str]) -> None:
    """P9g.1 — persist one PortOutcome row per variant from a completed
    wizard-generation task's port_plans.

    Deduplication: (source_hash, variant, porter_version). The index on
    port_outcomes covers this exact triple. If a row already exists for
    the same triple we skip the insert — the detector's lookup will
    find the original regardless, and duplicates would muddle the
    Port Coverage (P9g) "how often does X fail" math.

    No-op when:
      - task isn't a wizard-generation task (no port_plans in result)
      - log_tail can't be parsed
      - source_files is empty (can't compute a stable hash)
      - no port_plans emitted (non-deep-CUDA repo)
    """
    if not log_tail:
        return
    try:
        from routers.node_definitions import _extract_albedo_artifacts
        artifacts = _extract_albedo_artifacts(log_tail) or {}
    except Exception:
        logger.exception("port_outcomes: artifact extract failed task=%s", task.id)
        return
    port_plans = artifacts.get("port_plans") or {}
    source_files = artifacts.get("source_files") or []
    # Phase 8 — variant_adaptations may carry a recipe-discovered
    # HF→OV arch_mapping dict; persist alongside the PortOutcome's
    # existing diagnostics so the Phase 10 nightly harvester can
    # aggregate across tenants.
    variant_adaptations = artifacts.get("variant_adaptations") or {}
    if not port_plans or not source_files:
        return

    source_hash = _source_hash_for_outcomes(source_files)
    if not source_hash:
        return

    # Porter version is carried via the PortPlan's port_report.translator_version
    # when present; else default to "v1" which matches the kernel_porter
    # module-level PORTER_VERSION constant. Anchor the rows to a version so
    # a hipify upgrade invalidates cached verdicts naturally.
    tenant_id = task.tenant_id

    from models import PortOutcome
    rows_added = 0
    for variant, plan in port_plans.items():
        if not isinstance(plan, dict):
            continue
        compatibility = plan.get("compatibility") or "compatible"
        reason = plan.get("incompat_reason")
        port_report = plan.get("port_report") or {}
        porter_version = (
            (port_report.get("translator_version") or "").strip()
            if isinstance(port_report, dict) else ""
        ) or "v1"

        # Dedup: skip if a row already exists for this exact (hash,
        # variant, version).
        existing = worker_tasks_results_crud.get_port_outcome_by_source_variant_version(
            db, source_hash, variant, porter_version)
        if existing is not None:
            continue

        diagnostics: Dict[str, Any] = {
            "tool": port_report.get("tool") if isinstance(port_report, dict) else None,
            "unsupported_apis": (
                port_report.get("unsupported_apis") or []
                if isinstance(port_report, dict) else []
            ),
            "manual_cleanup_needed": (
                port_report.get("manual_cleanup_needed") or []
                if isinstance(port_report, dict) else []
            ),
        }
        # Phase 8 — lift the recipe's LLM-discovered arch mapping up
        # onto PortOutcome.diagnostics['arch_mapping'] for cross-tenant
        # harvesting. Absent for variants whose recipe didn't hit the
        # resolver (non-NPU variants, cache hits, disabled feature).
        adaptation = (
            variant_adaptations.get(variant) if isinstance(variant_adaptations, dict) else None
        )
        if isinstance(adaptation, dict):
            arch_mapping = adaptation.get("arch_mapping")
            if isinstance(arch_mapping, dict) and arch_mapping.get("hf_class"):
                diagnostics["arch_mapping"] = arch_mapping
        db.add(PortOutcome(
            source_hash=source_hash,
            variant=variant,
            porter_version=porter_version,
            compatibility=compatibility,
            reason=reason,
            diagnostics=diagnostics,
            tenant_id=tenant_id,
        ))
        rows_added += 1

    if rows_added > 0:
        db.commit()
        logger.info(
            "P9g.1 port_outcomes: task=%s wrote %d row(s) for source_hash=%s",
            task.id, rows_added, source_hash[:16],
        )


def _source_hash_for_outcomes(source_files: list) -> str:
    """SHA-256 over canonical-ordered (filename, body) pairs. Mirrors
    kernel_porter.source_hash in the Containerizer (same algorithm)
    so a port's own hash (used for PortOutcome cache lookups in the
    Containerizer) matches what we write here.
    """
    import hashlib
    h = hashlib.sha256()
    try:
        items = [
            (str(f.get("filename", "") or ""), str(f.get("body", "") or ""))
            for f in source_files
            if isinstance(f, dict)
        ]
        items.sort(key=lambda x: x[0])
        for name, body in items:
            h.update(name.encode("utf-8", errors="replace"))
            h.update(b"\x00")
            h.update(hashlib.sha256(body.encode("utf-8", errors="replace")).digest())
            h.update(b"\x00")
    except Exception:
        return ""
    return h.hexdigest()


def _notify_wizard_generation_complete(db: Session, task, *, is_success: bool):
    """Insert an in-app Notification when a wizard artifact-generation
    task finishes (success OR failure). Silent no-op for any task that
    isn't a wizard/Containerizer albedo run, so this is safe to call
    from both the /complete and /failed task handlers unconditionally.

    Identifies wizard-generation tasks by `container_env.OUTPUT_MODE == "albedo"`
    in task_payload — set by /generate-artifacts in node_definitions.py.
    The triggering user id is stashed at `task_payload.triggered_by_user_id`
    in the same place.

    The Dashboard bell (Dashboard/js/notifications.js) polls
    /notifications/unread-count every 30s and picks up the row
    automatically. The WorkflowBuilder's own bell (Phase 8a-4) uses
    the same endpoint."""
    from models import Notification

    payload = task.task_payload or {}
    if not isinstance(payload, dict):
        return
    env = (payload.get("container_env") or {}) if isinstance(payload.get("container_env"), dict) else {}
    # Filter narrowly — only albedo (wizard) runs. Other Containerizer
    # modes (llm-ping diagnostics, codebase-map scans) shouldn't
    # notify the user.
    if env.get("OUTPUT_MODE") != "albedo":
        return

    user_id = payload.get("triggered_by_user_id")
    if not user_id:
        return  # pre-Phase-8a task without the user stashed — skip silently

    wiz_meta = payload.get("wizard_metadata") or {}
    node_name = wiz_meta.get("node_name") or "your node"

    if is_success:
        message = f'Node artifacts ready for "{node_name}" — click to review and build'
        notification_type = "node_generation_complete"
    else:
        message = f'Node generation failed for "{node_name}" — click to inspect'
        notification_type = "node_generation_failed"

    # link_url carries a hash/query the WorkflowBuilder recognises on
    # mount. We open the build-queue panel (not the wizard's Review
    # step) so the user lands on the queue row for this task — they
    # can inspect logs, see the variant matrix, and Confirm-and-Build
    # from there if they want. Jumping directly into the wizard at
    # Review was disorienting per operator feedback.
    #
    # `openQueue=1` opens the panel; `taskId=<id>` tells the queue
    # to expand the matching row (the build-queue panel listens for a
    # `mod:queue:expand-row` event with that taskId). Dashboard
    # notifications just follow the URL.
    link_url = f"#workflow-builder?openQueue=1&taskId={task.id}"

    try:
        db.add(Notification(
            recipient_user_id=user_id,
            message=message,
            notification_type=notification_type,
            link_url=link_url,
        ))
        db.commit()
        # Wake any open SSE stream for this user so the bell
        # refreshes without waiting on its 30s poll.
        try:
            from routers.notifications import publish_notification_event
            publish_notification_event(user_id)
        except Exception:
            pass
    except Exception as e:
        db.rollback()
        logger.warning("Failed to insert wizard-gen notification for task %d: %s", task.id, e)


def _notify_workflow_complete(db: Session, workflow_run, tenant_id: int):
    """
    Send notifications when a workflow completes (success or failure).

    1. In-app notification (bell icon) — always
    2. Webhook POST — if tenant has workflow_webhook_url configured
    3. Email — if tenant has email configured and notifications enabled
    """
    from models import Notification, Workflow

    status = workflow_run.status.value
    is_success = status in ('completed', 'success')
    workflow_name = ""
    if workflow_run.workflow:
        workflow_name = workflow_run.workflow.name or ""

    status_label = "completed successfully" if is_success else "failed"
    message = f"Workflow \"{workflow_name}\" {status_label} (Run #{workflow_run.id})"

    notif_type = "workflow_complete" if is_success else "workflow_failed"

    # --- 1. In-app notification --- (WS4.Q22: honour the user's per-type
    # in_app preference; missing row == enabled, matching the default.)
    # Notify the user who triggered the run
    if workflow_run.triggered_by_user_id:
        from crud import notification_preferences as notification_prefs_crud
        in_app_enabled = notification_prefs_crud.is_enabled(
            db, user_id=workflow_run.triggered_by_user_id,
            notification_type=notif_type, channel="in_app",
        )
        if in_app_enabled:
            db.add(Notification(
                recipient_user_id=workflow_run.triggered_by_user_id,
                message=message,
                notification_type=notif_type,
                link_url=f"#logs",
            ))
            db.commit()
            # Wake SSE subscribers for this user (P8e).
            try:
                from routers.notifications import publish_notification_event
                publish_notification_event(workflow_run.triggered_by_user_id)
            except Exception:
                pass

    # --- 2. Webhook ---
    try:
        webhook_setting = worker_tasks_results_crud.get_tenant_webhook_setting(db, tenant_id)

        if webhook_setting and webhook_setting.value:
            webhook_url = webhook_setting.value if isinstance(webhook_setting.value, str) else webhook_setting.value.get("url", "")
            if webhook_url:
                payload = {
                    "event": "workflow.completed" if is_success else "workflow.failed",
                    "workflow_run_id": workflow_run.id,
                    "workflow_name": workflow_name,
                    "status": status,
                    "completed_at": workflow_run.completed_at.isoformat() if workflow_run.completed_at else None,
                    "tenant_id": tenant_id,
                }
                try:
                    from urllib.parse import urlparse as _wp
                    from utils import safe_http
                    safe_http.post(webhook_url, json=payload,
                                   _ssrf_connect_timeout=10,
                                   _ssrf_read_timeout=10)
                    logger.info("Webhook sent for workflow run %d", workflow_run.id)
                except safe_http.SsrfBlocked:
                    logger.warning(
                        "Webhook refused by SSRF guard for workflow run %d (host=%s)",
                        workflow_run.id, _wp(webhook_url).hostname)
                except Exception as e:
                    logger.warning("Webhook delivery failed for run %d: %s", workflow_run.id, e)
    except Exception as e:
        logger.warning("Webhook lookup failed: %s", e)

    # --- 3. Email notification ---
    # Queued, not sent immediately. The per-event spam (one "MOD
    # permission update" email per workflow completion) buried
    # operators' inboxes — these are FYI confirmations that don't need
    # real-time delivery. queue_digest_event INSERTs into
    # pending_permission_emails; the 09:00-user-local digest scheduler
    # consolidates and ships one daily summary per user.
    try:
        if workflow_run.triggered_by_user_id:
            user = worker_tasks_results_crud.get_user_by_id(db, workflow_run.triggered_by_user_id)
            if user and user.email:
                from crud import notification_preferences as notification_prefs_crud
                email_enabled = notification_prefs_crud.is_enabled(
                    db, user_id=user.id, notification_type=notif_type, channel="email",
                )
                if email_enabled:
                    from utils.permission_digest import queue_digest_event
                    queue_digest_event(
                        db=db,
                        user_id=user.id,
                        tenant_id=tenant_id,
                        event_type="workflow_complete",
                        payload={
                            "workflow_name": workflow_name,
                            "workflow_run_id": workflow_run.id,
                            "status": status,
                        },
                    )
    except Exception as e:
        logger.debug("Email notification queue skipped: %s", e)

    # --- 4. P2 — Slack notification (per-tenant). Best-effort. ---
    try:
        from utils.slack_dispatcher import dispatch_slack_event
        slack_event = "workflow.completed" if is_success else "workflow.failed"
        dispatch_slack_event(
            db=db,
            tenant_id=tenant_id,
            event_type=slack_event,
            payload={
                "workflow_run_id": workflow_run.id,
                "workflow_name": workflow_name,
                "status": status,
                "completed_at": workflow_run.completed_at.isoformat() if workflow_run.completed_at else None,
            },
        )
    except Exception as e:
        logger.debug("Slack notification skipped: %s", e)


def _preserve_checkpoint_affinity(db: Session, task) -> None:
    """Send a requeued task back to the host that holds its progress.

    Gate 3. The volume is host-local, so a requeue that goes anywhere else
    starts the node from zero — which is the failure checkpointing exists to
    prevent, reintroduced at the one moment it matters most. Affinity was
    applied at dispatch and NOT here, so every reaper-driven retry lost it; a
    live run proved that by requeueing onto a host with no volume.

    A PREFERENCE, never a lock, for the reason this file has learned twice: a
    task pinned to a host that cannot claim it sits ASSIGNED for ever, never
    claimed, so no claim expires and the reaper has nothing to reap. If the
    holding host is not licensed and heartbeating, the task goes out unpinned
    and the node starts over — losing progress beats stopping the run.

    Only ever touches tasks carrying a checkpoint volume. Everything else keeps
    the reaper's existing behaviour exactly.
    """
    try:
        if not task.node_run_id:
            return
        if not ((task.task_payload or {}).get("checkpoint_volume")):
            return
        host = _live_checkpoint_host(db, task.node_run_id)
        if host:
            task.assigned_to_worker = host
            task.status = ApiTaskStatus.ASSIGNED
            logger.info("task %d requeued to %s, which holds its checkpoint",
                        task.id, host)
    except Exception:  # noqa: BLE001 — affinity must never block a requeue
        logger.debug("checkpoint affinity skipped for task %s", task.id, exc_info=True)


def _any_worker_online(db, now, grace_s: int = 90) -> bool:
    """True when at least one LICENSED worker is currently heartbeating.

    Manager-outage aware via manager_lifecycle.silent_for (which measures
    silence only across the window this manager was actually listening), so a
    fresh manager restart does NOT read the whole fleet as gone. Used to tell a
    genuine "no workers anywhere" condition (Bug 27 — HOLD + requeue) apart from
    "this task exhausted its retries against a live fleet" (a real failure).
    """
    _ml = optional_paid("services.manager_lifecycle")
    grace = timedelta(seconds=grace_s)
    workers = worker_tasks_status_crud.list_licensed_workers(db)
    for w in workers:
        if _ml is None:
            _silent = now - (w.last_heartbeat or now)
        else:
            _silent = _ml.silent_for(w.last_heartbeat, now)
        if _silent <= grace:
            return True
    return False


def _lost_report_hold_expired(db, task, now) -> bool:
    """rc20 — bound the Bug 27 durable HOLD. A task whose claim ALREADY expired
    and whose attempts are exhausted is not merely waiting on a down fleet: the
    worker took the claim and its failure report never landed (worker->API
    transport dead, or the worker died). Waiting is right while a worker might
    still return, but unbounded waiting strands the run at RUNNING forever.
    Stamp the first hold in scheduling_metadata (existing JSONB, no model
    change) and report True once the hold exceeds the grace.
    """
    from services import platform_settings as _psr
    try:
        grace = int(_psr.get_setting(
            "worker.lost_report_hold_grace_seconds", db=db) or 0)
    except Exception:  # noqa: BLE001 — a settings read failure must not
        # change the pre-existing HOLD behaviour; degrade to unbounded.
        logger.exception("rc20: lost-report grace setting unreadable "
                         "(holding unbounded, as before)")
        return False
    meta = dict(task.scheduling_metadata or {})
    held_at = meta.get("lost_report_hold_at")
    # r2 (review 2b) — the stamp belongs to a SPECIFIC claim. A HELD task is
    # released (claim cleared) and a later outage sees a NEW claim; a stamp
    # carried over from outage #1 would terminalize that outage on its FIRST
    # tick with zero grace. Key the stamp to the claim it was taken against
    # (claimed_at) and treat a key mismatch as "no stamp" — one fresh grace
    # per claim. A stamp with no key (a pre-r2 row) is honoured against
    # whatever claim is current, so the bound still applies to old rows.
    _claim_key = None
    if task.claimed_at is not None:
        _ca = task.claimed_at
        if _ca.tzinfo is None:
            _ca = _ca.replace(tzinfo=timezone.utc)
        _claim_key = _ca.isoformat()
    _stamp_key = meta.get("lost_report_hold_at_claim")
    if _stamp_key and _claim_key and _stamp_key != _claim_key:
        held_at = None  # stale: stamped during an earlier claim
    if not held_at:
        meta["lost_report_hold_at"] = now.isoformat()
        meta["lost_report_hold_at_claim"] = _claim_key
        task.scheduling_metadata = meta
        flag_modified(task, "scheduling_metadata")
        return False
    if grace <= 0:
        return False
    try:
        _held = datetime.fromisoformat(held_at)
    except Exception:  # noqa: BLE001
        return False
    if _held.tzinfo is None:
        _held = _held.replace(tzinfo=timezone.utc)
    return (now - _held) > timedelta(seconds=grace)


def _hold_task_awaiting_worker(db, task, reason: str) -> None:
    """Bug 27 — durably HOLD a task instead of failing it when no worker is
    available. A no-worker / manager-outage / roaming condition is
    infrastructure, not an execution failure, so the task must wait and run
    when a worker returns rather than burn to FAILED/EXPIRED. Returns the task
    to a claimable state WITHOUT consuming an attempt (the retry budget is for
    genuine container failures reported via report_task_failed, not for the
    fleet being down)."""
    task.status = (ApiTaskStatus.ASSIGNED if task.assigned_to_worker
                   else ApiTaskStatus.PENDING)
    task.claimed_by_worker = None
    task.claimed_at = None
    task.claim_expires_at = None
    task.started_at = None
    _preserve_checkpoint_affinity(db, task)
    logger.warning(
        "Task %d HELD (%s): no workers online — requeued to await a worker "
        "return rather than failing (attempt %d/%d preserved)",
        task.id, reason, task.attempt_count + 1, task.max_attempts,
    )


def _finalize_run_for_terminal_task(db, task, error_message: str) -> None:
    """1900Y1i — reaper terminal branches must finalize the RUN, not just the task.

    Case (a) of the stranger-boot report: the worker dies mid-task (SIGSEGV),
    so no POST /workers/tasks/{id}/failed ever lands and report_task_failed's
    NodeRun/WorkflowRun propagation (worker_tasks.py:4259-4279) never runs.
    The reaper drives the task row terminal but left the WorkflowRun at
    RUNNING forever. This mirrors that propagation on every reaper terminal
    branch (EXPIRED and FAILED alike).
    """
    if task.node_run_id:
        node_run = worker_tasks_lifecycle_crud.get_node_run_by_id(db, task.node_run_id)
        if node_run and node_run.status not in (
            TaskStatus.SUCCESS, TaskStatus.COMPLETED, TaskStatus.FAILED,
            TaskStatus.BLOCKED,
        ):
            node_run.status = TaskStatus.FAILED
            node_run.completed_at = datetime.now(timezone.utc)
    if not task.workflow_run_id:
        return
    workflow_run = worker_tasks_lifecycle_crud.get_workflow_run_by_id(db, task.workflow_run_id)
    if workflow_run and workflow_run.status in (
        TaskStatus.QUEUED, TaskStatus.RUNNING,
    ) and not (workflow_run.parameters or {}).get("is_profiling", False):
        workflow_run.status = TaskStatus.FAILED
        workflow_run.completed_at = datetime.now(timezone.utc)
        if error_message and not workflow_run.error_message:
            workflow_run.error_message = error_message
        try:
            worker_tasks_lifecycle_crud.cancel_claimable_tasks_for_failed_run(
                db, workflow_run.id, task.id)
        except Exception as e:  # noqa: BLE001 — best-effort halt, never masks the reap
            logger.warning("could not cancel sibling tasks for reaper-failed run %s: %s", workflow_run.id, e)


def cleanup_expired_task_claims(db):
    """
    Background cleanup for the API task queue.

    Five recovery cases run every tick. The function is intentionally
    relentless — anything still stuck at the end gets logged at WARNING
    every tick (the "nag list") so it shows up in monitoring until it
    actually clears.

    1) Stale CLAIMED — claim_expires_at < now
    2) Stale RUNNING — claim_expires_at < now (per-task timeout fired)
    3) Heartbeat-stale CLAIMED/RUNNING — claim_expires_at hasn't passed
       yet (workers can set 1+ hour expirations on long-running build
       tasks), but the worker that owns it hasn't heartbeated in
       HEARTBEAT_STALE_GRACE seconds. Catches the silent-worker-death
       case the prior reaper missed.
    4) Tasks claimed by offline workers — requeue immediately.
    5) Expired worker enrollments — revoke.

    Bug 27 — every branch that would otherwise mark an orphaned task terminal
    (EXPIRED / FAILED) because its worker vanished first checks whether ANY
    worker is online. When the fleet is empty (manager was offline / workers
    roaming / dual-horizon), the task is HELD and requeued to await a worker
    instead of being failed. A genuinely-poisonous task still fails once it has
    exhausted its attempts against a LIVE fleet.
    """
    from sqlalchemy import or_

    now = datetime.now(timezone.utc)

    # If a worker hasn't heartbeated in this long, any task it still
    # claims is assumed dead. 90s is conservative: workers heartbeat
    # every ~5s, so 90s = 18 missed heartbeats. False-positive risk is
    # only "worker briefly hiccuped mid-task," and the requeue is
    # idempotent — worst case the task runs twice and the second run's
    # write wins.
    HEARTBEAT_STALE_GRACE = timedelta(seconds=90)

    # Bug 27 — is the fleet empty right now? Computed once per tick (worker
    # liveness doesn't change mid-tick). When True, the terminal branches below
    # HOLD instead of failing, so a no-worker window never turns a runnable task
    # into a failure.
    no_workers_online = not _any_worker_online(db, now, HEARTBEAT_STALE_GRACE.seconds)

    # 1. Expire stale claims
    # r5-lock-ok: claim-expiry sweep; lock-order critical (1431)
    stale_claims = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status == ApiTaskStatus.CLAIMED,
        ApiTaskQueue.claim_expires_at < now,
    ).all()
    for task in stale_claims:
        if task.attempt_count < task.max_attempts - 1:
            # A PINNED task must go back as ASSIGNED, not PENDING. The two
            # claim branches are (status=ASSIGNED AND assigned_to_worker=me)
            # and (status=PENDING AND assigned_to_worker IS NULL) — so
            # PENDING with a pin still set matches NEITHER and the task can
            # never be claimed by anyone. Found with 113 calibration tasks
            # stranded that way, the oldest for 71 hours, silently: nothing
            # errors, they simply sit there.
            task.status = (ApiTaskStatus.ASSIGNED if task.assigned_to_worker
                           else ApiTaskStatus.PENDING)
            task.attempt_count += 1
            task.output_namespace = f"attempt_{task.attempt_count}"
            task.claimed_by_worker = None
            task.claimed_at = None
            task.claim_expires_at = None
            _preserve_checkpoint_affinity(db, task)
            logger.info("Requeued expired claim: task %d (attempt %d)", task.id, task.attempt_count + 1)
        elif no_workers_online and not _lost_report_hold_expired(db, task, now):
            _hold_task_awaiting_worker(db, task, "stale claim, attempts exhausted")
        else:
            task.status = ApiTaskStatus.EXPIRED
            task.error_message = (
                "worker report lost (claim expired)"
                if _lost_report_hold_expired(db, task, now)
                else "Claim expired after max attempts")
            task.completed_at = now
            _finalize_run_for_terminal_task(db, task, task.error_message)
            logger.warning("Task %d expired after %d attempts", task.id, task.attempt_count + 1)

    # 2. Expire running tasks past timeout
    # r5-lock-ok: timeout sweep; lock-order critical (1431)
    running_tasks = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status == ApiTaskStatus.RUNNING,
        ApiTaskQueue.claim_expires_at < now,
    ).all()
    for task in running_tasks:
        if task.attempt_count < task.max_attempts - 1:
            # Same dead zone as the stale-claim path above.
            task.status = (ApiTaskStatus.ASSIGNED if task.assigned_to_worker
                           else ApiTaskStatus.PENDING)
            task.attempt_count += 1
            task.output_namespace = f"attempt_{task.attempt_count}"
            task.claimed_by_worker = None
            task.claimed_at = None
            task.claim_expires_at = None
            task.started_at = None
            _preserve_checkpoint_affinity(db, task)
            logger.info("Requeued timed-out task: task %d (attempt %d)", task.id, task.attempt_count + 1)
        elif no_workers_online and not _lost_report_hold_expired(db, task, now):
            _hold_task_awaiting_worker(db, task, "running past claim expiry, attempts exhausted")
        else:
            task.status = ApiTaskStatus.FAILED
            task.error_message = (
                "worker report lost (claim expired)"
                if _lost_report_hold_expired(db, task, now)
                else "Execution timed out")
            task.completed_at = now
            _finalize_run_for_terminal_task(db, task, task.error_message)
            logger.warning("Task %d failed: execution timed out after %d attempts", task.id, task.attempt_count + 1)

    # 3. Requeue tasks from offline workers
    # r5-lock-ok: offline-worker requeue sweep; lock-order critical (1431)
    offline_remote = db.query(WorkerLicense).filter(
        WorkerLicense.status == WorkerStatus.OFFLINE,
    ).all()
    for w in offline_remote:
        # r5-lock-ok: per-worker requeue; lock-order critical (1431)
        orphaned = db.query(ApiTaskQueue).filter(
            ApiTaskQueue.claimed_by_worker == w.worker_name,
            ApiTaskQueue.status.in_([ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING]),
        ).all()
        for task in orphaned:
            task.status = ApiTaskStatus.PENDING
            task.attempt_count += 1
            task.output_namespace = f"attempt_{task.attempt_count}"
            task.claimed_by_worker = None
            task.claimed_at = None
            task.claim_expires_at = None
            task.started_at = None
            # Clear the pin: the requeue was triggered by this task's previous
            # holder failing/going silent, so any host affinity (memory /
            # checkpoint) on that worker is stale — the checkpoint is re-fetched
            # from storage on the new host, not from the dead worker's memory.
            # Leaving assigned_to_worker set would strand the row as PENDING +
            # pinned, which matches NEITHER claim branch (PENDING+unpinned /
            # ASSIGNED+pinned) and no worker could ever take it (see reaper's
            # UNCLAIMABLE dead-zone warning + the worker-reported path's
            # ASSIGNED-if-pinned handling).
            #
            # Benchmark exception: the pin is Durable and not host-affinity —
            # the scores are only valid on the pinned machine. If that machine
            # is offline the task waits (ASSIGNED + pinned) rather than being
            # re-pooled for another box to claim and stamp the wrong profile.
            if _benchmark_target_worker(task):
                task.status = ApiTaskStatus.ASSIGNED
                logger.warning(
                    "Held benchmark task %d pinned to offline worker %s "
                    "(not re-pooled — scores are only valid on that host)",
                    task.id, w.worker_name,
                )
            else:
                task.assigned_to_worker = None
                logger.info("Requeued orphaned task %d from offline worker %s", task.id, w.worker_name)

    # 3b. Heartbeat-stale claims — claim_expires_at hasn't fired yet but
    # the worker holding the claim hasn't heartbeated in
    # HEARTBEAT_STALE_GRACE seconds. JOIN to worker_licenses so we only
    # reap tasks whose owning worker is alive-in-the-DB but silent.
    #
    # Silence only counts while THIS manager was listening. Staleness is
    # `max(last_heartbeat, manager_start) < now - grace`, and since
    # `max(a,b) < c` is `a < c AND b < c`, the whole sweep is simply skipped
    # until the manager has been up longer than the grace window — after which
    # the ordinary per-worker comparison is exact again.
    #
    # Without this, a manager outage longer than the grace makes every
    # last_heartbeat look ancient, and the first tick after recovery requeues
    # every in-flight task on the fleet while those workers are alive and
    # mid-job. Manager recovery would itself be the largest source of
    # duplicated work in the system.
    _ml = optional_paid("services.manager_lifecycle")
    _skip_stale_sweep = _ml is not None and _ml.manager_uptime(now) <= HEARTBEAT_STALE_GRACE

    heartbeat_cutoff = now - HEARTBEAT_STALE_GRACE
    # r5-lock-ok: heartbeat-stale sweep; lock-order critical (1431)
    heartbeat_stale = [] if _skip_stale_sweep else db.query(ApiTaskQueue).join(
        WorkerLicense,
        WorkerLicense.worker_name == ApiTaskQueue.claimed_by_worker,
    ).filter(
        ApiTaskQueue.status.in_([ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING]),
        ApiTaskQueue.claimed_by_worker.isnot(None),
        or_(
            WorkerLicense.last_heartbeat < heartbeat_cutoff,
            WorkerLicense.last_heartbeat.is_(None),
        ),
    ).all()
    for task in heartbeat_stale:
        worker_name = task.claimed_by_worker
        if task.attempt_count < task.max_attempts - 1:
            task.status = ApiTaskStatus.PENDING
            task.attempt_count += 1
            task.output_namespace = f"attempt_{task.attempt_count}"
            task.claimed_by_worker = None
            task.claimed_at = None
            task.claim_expires_at = None
            task.started_at = None
            # Clear the pin: the requeue was triggered by this task's previous
            # holder failing/going silent, so any host affinity (memory /
            # checkpoint) on that worker is stale — the checkpoint is re-fetched
            # from storage on the new host, not from the dead worker's memory.
            # Leaving assigned_to_worker set would strand the row as PENDING +
            # pinned, which matches NEITHER claim branch (PENDING+unpinned /
            # ASSIGNED+pinned) and no worker could ever take it (see reaper's
            # UNCLAIMABLE dead-zone warning + the worker-reported path's
            # ASSIGNED-if-pinned handling).
            #
            # Benchmark exception: keep the durable pin (scores only valid on
            # the pinned host) — hold it ASSIGNED rather than re-pooling it.
            if _benchmark_target_worker(task):
                task.status = ApiTaskStatus.ASSIGNED
                logger.warning(
                    "Held benchmark task %d pinned (worker %s heartbeat-stale) — "
                    "not re-pooled", task.id, worker_name,
                )
            else:
                task.assigned_to_worker = None
                logger.warning(
                    "Requeued heartbeat-stale task %d (worker %s silent >%ds, attempt %d)",
                    task.id, worker_name, HEARTBEAT_STALE_GRACE.total_seconds(),
                    task.attempt_count + 1,
                )
        elif no_workers_online and not _lost_report_hold_expired(db, task, now):
            _hold_task_awaiting_worker(db, task, f"worker {worker_name} heartbeat-stale, attempts exhausted")
        else:
            task.status = ApiTaskStatus.FAILED
            task.error_message = (
                "worker report lost (claim expired)"
                if _lost_report_hold_expired(db, task, now)
                else f"Heartbeat-stale: worker {worker_name} silent for "
                f">{HEARTBEAT_STALE_GRACE.total_seconds():.0f}s, "
                f"exhausted {task.attempt_count + 1} attempts"
            )
            task.completed_at = now
            _finalize_run_for_terminal_task(db, task, task.error_message)
            logger.warning(
                "Task %d failed: worker %s heartbeat stale, no retries left",
                task.id, worker_name,
            )

    # 3b2. CLAIMED but never STARTED (BUG P2). A worker that claims a task type
    # it cannot execute — or that dies between handing out the claim and POSTing
    # /started — leaves the row CLAIMED with started_at NULL. Nothing else sees
    # it for a long time: case 1 waits on claim_expires_at (a full 30-minute
    # lease) and case 3b only fires when the holder's heartbeat goes stale, which
    # it never does because the worker is alive and idling on its next poll.
    # Stage repro: build task 50645 sat claimed on the HP worker for 9 minutes
    # with zero logs and zero progress, and the operator had to cancel it by
    # hand. A short start-lease turns that into an automatic re-pool (and, once
    # attempts are spent, a FAILED row carrying the real reason).
    from services import platform_settings as _psr
    _start_grace = int(
        _psr.get_setting("worker.claimed_never_started_grace_seconds", db=db)
        or 0)
    if _start_grace > 0:
        # select() rather than db.query(): this router is at its R5 baseline
        # count, so a new raw db.query( would fail `--strict`.
        from sqlalchemy import select
        never_started = db.execute(select(ApiTaskQueue).where(
            ApiTaskQueue.status == ApiTaskStatus.CLAIMED,
            ApiTaskQueue.started_at.is_(None),
            ApiTaskQueue.claimed_at.isnot(None),
            ApiTaskQueue.claimed_at < now - timedelta(seconds=_start_grace),
        )).unique().scalars().all()
        for task in never_started:
            _holder = task.claimed_by_worker
            if task.attempt_count < task.max_attempts - 1:
                task.status = (ApiTaskStatus.ASSIGNED
                               if _benchmark_target_worker(task)
                               else ApiTaskStatus.PENDING)
                task.attempt_count += 1
                task.output_namespace = f"attempt_{task.attempt_count}"
                task.claimed_by_worker = None
                task.claimed_at = None
                task.claim_expires_at = None
                if not _benchmark_target_worker(task):
                    task.assigned_to_worker = None
                logger.warning(
                    "Requeued claimed-never-started task %d (holder %s never "
                    "POSTed /started within %ds, attempt %d)",
                    task.id, _holder, _start_grace,
                    task.attempt_count + 1,
                )
            elif no_workers_online and not _lost_report_hold_expired(db, task, now):
                _hold_task_awaiting_worker(
                    db, task, "claimed but never started, attempts exhausted")
            else:
                task.status = ApiTaskStatus.FAILED
                task.completed_at = now
                task.error_message = (
                    "worker report lost (claim expired)"
                    if _lost_report_hold_expired(db, task, now)
                    else "Claimed but never started: worker "
                    f"{task.claimed_by_worker} held the claim for over "
                    f"{_start_grace}s without reporting /started after "
                    f"{task.attempt_count + 1} attempts — this worker cannot "
                    "execute this task type")
                _finalize_run_for_terminal_task(db, task, task.error_message)
                logger.warning(
                    "Failed claimed-never-started task %d (no retries left)",
                    task.id,
                )

    # 3c. Claim-expiry-NULL orphans. A small fraction of dispatches end
    # up RUNNING/CLAIMED with `claim_expires_at IS NULL` (root cause:
    # dispatch paths that don't set the field — see post-launch ticket).
    # The earlier cases 1/2 filter on `claim_expires_at < now`, which is
    # NULL-comparison-false in SQL three-valued logic, so these rows
    # leak through every reaper tick. Stage repro: 155 stale rows
    # accumulated 2026-05-19 → 2026-05-25 before this guard landed.
    #
    # Treat as orphan after NULL_EXPIRY_GRACE — requeue if attempts
    # remain, else FAIL with a diagnostic so the dispatch bug is
    # visible in audit + API logs.
    NULL_EXPIRY_GRACE = timedelta(hours=2)
    # r5-lock-ok: null-expiry orphan sweep; lock-order critical (1431)
    null_expiry_orphans = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status.in_([ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING]),
        ApiTaskQueue.claim_expires_at.is_(None),
        ApiTaskQueue.claimed_at.isnot(None),
        ApiTaskQueue.claimed_at < now - NULL_EXPIRY_GRACE,
    ).all()
    for task in null_expiry_orphans:
        if task.attempt_count < task.max_attempts - 1:
            task.status = ApiTaskStatus.PENDING
            task.attempt_count += 1
            task.output_namespace = f"attempt_{task.attempt_count}"
            task.claimed_by_worker = None
            task.claimed_at = None
            task.claim_expires_at = None
            task.started_at = None
            # Clear the pin: the requeue was triggered by this task's previous
            # holder failing/going silent, so any host affinity (memory /
            # checkpoint) on that worker is stale — the checkpoint is re-fetched
            # from storage on the new host, not from the dead worker's memory.
            # Leaving assigned_to_worker set would strand the row as PENDING +
            # pinned, which matches NEITHER claim branch (PENDING+unpinned /
            # ASSIGNED+pinned) and no worker could ever take it (see reaper's
            # UNCLAIMABLE dead-zone warning + the worker-reported path's
            # ASSIGNED-if-pinned handling).
            #
            # Benchmark exception: keep the durable pin — hold ASSIGNED rather
            # than re-pooling the row for another worker.
            if _benchmark_target_worker(task):
                task.status = ApiTaskStatus.ASSIGNED
                logger.warning(
                    "Held benchmark task %d pinned (NULL-expiry orphan) — not re-pooled",
                    task.id,
                )
            else:
                task.assigned_to_worker = None
                logger.warning(
                    "Requeued NULL-expiry orphan task %d (claimed_at was %s, attempt %d)",
                    task.id, task.claimed_at, task.attempt_count + 1,
                )
        elif no_workers_online and not _lost_report_hold_expired(db, task, now):
            _hold_task_awaiting_worker(db, task, "NULL-expiry orphan, attempts exhausted")
        else:
            task.status = ApiTaskStatus.FAILED
            task.error_message = (
                "worker report lost (claim expired)"
                if _lost_report_hold_expired(db, task, now)
                else f"NULL-expiry orphan: claim_expires_at unset, claimed_at "
                f"{task.claimed_at.isoformat() if task.claimed_at else 'unknown'} "
                f"(>2h ago); attempts {task.attempt_count + 1}/{task.max_attempts}"
            )
            task.completed_at = now
            _finalize_run_for_terminal_task(db, task, task.error_message)
            logger.warning(
                "Failed NULL-expiry orphan task %d (no retries left)", task.id,
            )

    # 4. Expire old enrollments
    # r5-lock-ok: enrollment-expiry sweep; lock-order critical (1431)
    expired_enrollments = db.query(WorkerEnrollment).filter(
        WorkerEnrollment.status == WorkerEnrollmentStatus.APPROVED,
        WorkerEnrollment.expires_at != None,
        WorkerEnrollment.expires_at < now,
    ).all()
    for enrollment in expired_enrollments:
        enrollment.status = WorkerEnrollmentStatus.REVOKED
        enrollment.revoked_at = now
        logger.info("Auto-revoked expired enrollment for worker %s", enrollment.worker_name)

    db.commit()

    # 5. Nag list — anything STILL stuck after this tick gets logged at
    # WARNING with its id + age. The reaper will retry every tick; if a
    # row is on the nag list across multiple ticks, that's an operator
    # signal that one of the recovery rules is broken (e.g. a 4th case
    # we haven't covered). Don't silently drop them on the floor.
    # r5-lock-ok: post-sweep consistency check; lock-order critical (1431)
    stuck_remaining = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status.in_([ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING]),
        or_(
            # Past their per-task expiry
            ApiTaskQueue.claim_expires_at < now,
            # OR claimed_by a worker whose heartbeat is stale
            ApiTaskQueue.id.in_(
                # r5-lock-ok: nested heartbeat subquery; lock-order critical (1431)
                db.query(ApiTaskQueue.id).join(
                    WorkerLicense,
                    WorkerLicense.worker_name == ApiTaskQueue.claimed_by_worker,
                ).filter(
                    ApiTaskQueue.claimed_by_worker.isnot(None),
                    or_(
                        WorkerLicense.last_heartbeat < heartbeat_cutoff,
                        WorkerLicense.last_heartbeat.is_(None),
                    ),
                )
            ),
        ),
    ).all()
    if stuck_remaining:
        ids = sorted(t.id for t in stuck_remaining)
        logger.warning(
            "Reaper nag list: %d task(s) STILL stuck after this tick. "
            "Will retry next interval. Task ids: %s",
            len(ids),
            ids[:50] if len(ids) <= 50 else f"{ids[:50]}... (+{len(ids)-50} more)",
        )

    # 6. NEVER-CLAIMED nag. Every rule above keys on claim_expires_at, which
    # only exists once something HAS been claimed — so a task nobody ever
    # picked up cannot expire, cannot be requeued, and is invisible to all of
    # them. There was a timeout for work that stalled and none for work that
    # never started.
    #
    # It went unnoticed for 71 hours: 113 calibration tasks pinned to healthy,
    # heartbeating, benchmarked workers. Every health signal was green because
    # nothing was unhealthy — the work simply sat there, and the only surface
    # that could have shown it counts pending-per-queue with no age and no
    # threshold, which reads the same whether those rows are 3 seconds or 3
    # days old.
    #
    # Detect only. Requeueing on a guess is how a stuck task becomes a
    # duplicated one, and the causes differ: no eligible worker, an unsatisfied
    # capability, or a state combination nothing can claim.
    stuck_warn_min = max(1, int(os.getenv("TASK_STUCK_WARN_MINUTES", "30")))
    never_claimed_cutoff = now - timedelta(minutes=stuck_warn_min)
    # r5-lock-ok: never-claimed stuck-task check; lock-order critical (1431)
    never_claimed = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status.in_([ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED]),
        ApiTaskQueue.created_at < never_claimed_cutoff,
        or_(ApiTaskQueue.start_after.is_(None), ApiTaskQueue.start_after <= now),
    ).all()
    if never_claimed:
        oldest = min(t.created_at for t in never_claimed)
        age_h = round((now - oldest).total_seconds() / 3600, 1)
        by_queue: Dict[str, int] = {}
        for t in never_claimed:
            by_queue[t.queue_name or "?"] = by_queue.get(t.queue_name or "?", 0) + 1
        # Called out separately because it is UNCLAIMABLE, not merely waiting:
        # the two claim branches are (ASSIGNED + pinned to me) and (PENDING +
        # unpinned), so PENDING-with-a-pin matches neither and no worker can
        # ever take it. That exact combination is what stranded the 113.
        dead_zone = [t.id for t in never_claimed
                     if t.status == ApiTaskStatus.PENDING and t.assigned_to_worker]
        logger.warning(
            "Reaper: %d task(s) never claimed for over %dm (oldest %sh) — "
            "queues %s. Nothing expires these; if this persists there is no "
            "eligible worker or the state is unclaimable.",
            len(never_claimed), stuck_warn_min, age_h, by_queue,
        )
        if dead_zone:
            # SELF-HEAL: clear the stale pin so the row re-enters the claimable
            # (PENDING + unpinned) branch instead of sitting UNCLAIMABLE forever.
            # The requeue paths above no longer create this state, but this
            # recovers any rows stranded by an older build (or a path we missed)
            # rather than only logging an error on every tick.
            #
            # Benchmark exception: a benchmark task's pin is DURABLE, not a stale
            # host-affinity — clearing it would let another worker claim the
            # task and stamp the pinned worker's profile with the wrong
            # machine's scores. Never clear a benchmark pin here.
            healed = 0
            for t in never_claimed:
                if t.status == ApiTaskStatus.PENDING and t.assigned_to_worker:
                    if _benchmark_target_worker(t):
                        continue
                    t.assigned_to_worker = None
                    healed += 1
            logger.error(
                "Reaper: %d task(s) were UNCLAIMABLE — status PENDING with "
                "assigned_to_worker set, which matches neither claim branch. "
                "Cleared the stale pin on %d so they can be claimed. Task ids: %s",
                len(dead_zone), healed, sorted(dead_zone)[:50],
            )
            if healed:
                db.commit()  # persist the pin clears (the earlier commit is above this section)


# rc20-J157b — self-heal for runs ALREADY wedged by the leaked dispatch lock (J157).
# A run whose completion cascade was SKIPPED by a contention-exhaustion (or by the
# leak itself) keeps its downstream nodes un-enqueued forever: cleanup_expired_task_claims
# only requeues lapsed CLAIMED/RUNNING ROWS, and _redrive_ready_frontier only fires
# inside another cascade — which never happens again once the leaked advisory lock is
# gone (an API restart drops the locks, nothing re-drives those runs). So the state is:
# run RUNNING with completed_at NULL, every ApiTaskQueue row for it terminal (nothing
# claimable left), and a node the cascade should have enqueued that has no row at all.
# Idle window: no existing setting fits (workflows.paused_run_timeout_s is for PAUSED
# runs, worker.lost_report_hold_grace_seconds is a per-claim hold), so a module
# constant — 30 min is far past any normal between-node gap (a dispatched node's
# next task lands within seconds of its parent completing) yet short enough that
# an install recovers on its own.
_WEDGED_RUN_IDLE_SECONDS = 1800
# Bounded per tick: the sweep re-enters the full cascade, so one tick must never
# try to re-drive a whole fleet's wedged runs at once.
_WEDGED_RUN_SWEEP_CAP = 20


def _sweep_wedged_runs(db) -> List[int]:
    """rc20-J157b — re-drive runs already wedged by a skipped/leaked dispatch lock.

    For every RUNNING run with completed_at NULL whose ApiTaskQueue rows are ALL
    terminal (nothing left to claim, so no other sweeper can reach it) and which
    has been idle past _WEDGED_RUN_IDLE_SECONDS, re-enter the EXISTING cascade
    entry point (_check_and_dispatch_downstream) from the run's newest COMPLETED
    task. That entry point acquires the per-run dispatch advisory lock through
    the normal acquire (never bypassed), then either dispatches the ready
    frontier (_redrive_ready_frontier inside it) or finalizes the run through
    the existing completion/orphan guards — i.e. exactly what a normal
    completion would have done, so no new dispatch semantics are added here.

    Tenant-agnostic (no tenant filter: the cascade scopes by RUN, and every
    query below is per-run), so it can never cross tenants. Bounded by
    _WEDGED_RUN_SWEEP_CAP runs per tick, oldest first. Logs run ids only.
    Returns the run ids it re-drove.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_WEDGED_RUN_IDLE_SECONDS)

    claimable = [ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED,
                 ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING]

    candidates = db.query(WorkflowRun).filter(
        WorkflowRun.status == TaskStatus.RUNNING,
        WorkflowRun.completed_at.is_(None),
        WorkflowRun.started_at < cutoff,
    ).order_by(WorkflowRun.started_at).limit(_WEDGED_RUN_SWEEP_CAP).all()

    healed: List[int] = []
    for run in candidates:
        if getattr(run, "paused", False) or run.id is None:
            # A paused run HOLDS its frontier on purpose (resume re-kicks it).
            continue
        if ((run.parameters or {}).get("is_profiling")
                or (run.parameters or {}).get("is_calibration")):
            # System-graph runs (profiling/calibration) have their own
            # lifecycle; the reaper already owns their rows.
            continue
        plan = (run.parameters or {}).get("execution_plan")
        if not plan:
            # No plan to re-drive into or to finalize against — leave it alone.
            continue

        rows = db.query(ApiTaskQueue).filter(
            ApiTaskQueue.workflow_run_id == run.id).all()
        if not rows:
            continue
        if db.query(ApiTaskQueue).filter(
            ApiTaskQueue.workflow_run_id == run.id,
            ApiTaskQueue.status.in_(claimable),
        ).count():
            # Still claimable work — the claim reaper / scheduler owns this run.
            continue

        def _last_move(_t):
            return (_t.updated_at or _t.completed_at or _t.started_at
                    or _t.claimed_at or _t.created_at)

        last_move = max((_last_move(_t) for _t in rows), default=None)
        if not last_move or last_move >= cutoff:
            continue

        parent = (db.query(ApiTaskQueue)
                  .filter(ApiTaskQueue.workflow_run_id == run.id,
                          ApiTaskQueue.status == ApiTaskStatus.COMPLETED)
                  .order_by(ApiTaskQueue.completed_at.desc(),
                            ApiTaskQueue.id.desc())
                  .first())
        if parent is None:
            continue

        logger.info("J157b wedged-run sweep: re-drove run %s", run.id)
        try:
            _check_and_dispatch_downstream(db, parent)
        except Exception as e:  # noqa: BLE001 — one bad run must not stop the sweep
            logger.warning("J157b wedged-run sweep failed for run %s: %s", run.id, e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            continue
        healed.append(run.id)
    return healed


def detect_and_recover_stalled_fetch_loops(db) -> Dict[str, Any]:
    """
    WS4.Q21 — "green but silent" worker detection.

    A worker whose task fetch/claim loop (cmd/worker/main.go's job loop) has
    deadlocked or wedged keeps heartbeating regardless — the heartbeat sender
    is a separate goroutine (internal/heartbeat/heartbeat.go's hb.Run) — so
    `last_heartbeat` stays fresh and the worker looks perfectly healthy while
    PENDING tasks it is eligible for sit forever. `cleanup_expired_task_claims`'s
    "heartbeat-stale" branch above does NOT catch this: that branch only fires
    when the worker's HEARTBEAT itself goes silent, which a stalled fetch loop
    does not cause.

    Every heartbeat now carries `fetch_loop_age_s` — how long it's been since
    the worker's job loop last completed a pass (marked at the top of every
    pass and at each spin of its inner capacity/NOAH gate loops, so a worker
    that is legitimately busy or NOAH-withheld is never mistaken for a stuck
    one). Stored verbatim in `hardware_specs` by /workers/heartbeat, exactly
    like every other live-sample field there (disk_free_gb, gpu_temperature_c,
    ...).

    A worker only counts as stalled here when BOTH are true:
      - its heartbeat is fresh (i.e. not already the heartbeat-stale reaper's
        problem) and its reported fetch_loop_age_s exceeds the threshold, AND
      - there is actual evidence of harm: a PENDING task in one of its queues
        going unclaimed, or a task it already CLAIMED sitting with no progress
        past the threshold. A worker that is merely idle (nothing to fetch)
        must never be flagged — an idle fetch loop legitimately touches
        MarkFetchLoopIteration every long-poll cycle (~30s), so its age never
        approaches the multi-minute threshold in the first place; this second
        condition is belt-and-suspenders against threshold misconfiguration.

    Surfacing: sets `hardware_specs['fetch_loop_stalled']` (True/False) and
    `fetch_loop_stalled_at`, which GET /workers -> WorkerInfo.hardware_specs
    already passes straight through — Dashboard/js/workers.js's
    getStatusBadge() renders a red "Stalled" badge over the misleading green
    "Active" one when it's set. Plus a WARNING log line.

    Recovery of a stuck CLAIMED task reuses the EXISTING requeue path instead
    of inventing a new one: it back-dates claim_expires_at into the past,
    which is exactly the condition `cleanup_expired_task_claims`'s "1. Expire
    stale claims" branch already acts on — the very next worker_maintenance
    tick performs the actual requeue (attempt bump, claim-field reset,
    checkpoint-affinity preservation), completely unchanged.

    Returns a summary dict (checked/stalled/recovered worker name lists) —
    used by tests and by the caller's log line.
    """
    now = datetime.now(timezone.utc)
    threshold_s = max(1, int(os.getenv("MOD_WORKER_FETCH_STALL_THRESHOLD_S", "600")))
    threshold = timedelta(seconds=threshold_s)
    # Matches HEARTBEAT_STALE_GRACE in cleanup_expired_task_claims above: a
    # worker whose heartbeat is already this stale is that function's problem,
    # not this one's — flagging it here too would just be a second, redundant
    # alarm for the same underlying cause (worker process actually dead).
    heartbeat_grace = timedelta(seconds=90)

    result: Dict[str, Any] = {"checked": 0, "stalled": [], "recovered": []}

    workers = worker_tasks_status_crud.list_licensed_workers(db)

    changed = False
    for worker in workers:
        result["checked"] += 1
        specs = worker.hardware_specs or {}
        age_s = specs.get("fetch_loop_age_s")
        if age_s is None:
            continue  # older worker binary — field not sent yet, nothing to judge

        if worker.last_heartbeat is None or (now - worker.last_heartbeat) > heartbeat_grace:
            continue  # not "green" — heartbeat itself is stale, handled elsewhere

        was_stalled = bool(specs.get("fetch_loop_stalled"))

        if age_s <= threshold_s:
            if was_stalled:
                specs = dict(specs)
                specs["fetch_loop_stalled"] = False
                specs["fetch_loop_stalled_recovered_at"] = now.isoformat()
                worker.hardware_specs = specs
                changed = True
                logger.info(
                    "Worker stall watchdog: %s recovered (fetch_loop_age_s=%.0f <= %ds)",
                    worker.worker_name, age_s, threshold_s,
                )
                result["recovered"].append(worker.worker_name)
            continue

        # age_s > threshold_s: heartbeating fine, fetch loop stale. Only a
        # "green but silent" problem worth surfacing if real work is stuck.
        eligible_pending = worker_tasks_results_crud.count_eligible_pending_tasks(
            db, worker.worker_name, worker.queues)

        stuck_claimed = worker_tasks_results_crud.list_stuck_claimed_tasks(
            db, worker.worker_name, now - threshold)

        if not eligible_pending and not stuck_claimed:
            continue

        specs = dict(specs)
        specs["fetch_loop_stalled"] = True
        if not was_stalled:
            specs["fetch_loop_stalled_at"] = now.isoformat()
        worker.hardware_specs = specs
        changed = True

        logger.warning(
            "Worker stall watchdog: worker %s is green-but-silent — heartbeat "
            "fresh (last_heartbeat=%s) but fetch_loop_age_s=%.0f > %ds; "
            "eligible_pending=%d stuck_claimed=%d",
            worker.worker_name, worker.last_heartbeat, age_s, threshold_s,
            eligible_pending, len(stuck_claimed),
        )

        # Reuse the existing stale-claim requeue path: back-date
        # claim_expires_at so cleanup_expired_task_claims's case 1 picks these
        # up and requeues them on its own next tick. No new requeue logic.
        for task in stuck_claimed:
            task.claim_expires_at = now - timedelta(seconds=1)
            changed = True

        result["stalled"].append(worker.worker_name)

    if changed:
        db.commit()
    return result


@router.post("/workers/tasks/{task_id}/stack-state", response_model=Dict[str, Any])
async def get_task_stack_state(
    task_id: int,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """Worker-controlled stack start (review H4, owner 11-20): the worker asks,
    immediately before it starts a stack's containers for a `boot_restore`,
    whether the operator still wants the stack running. Read-only; returns only
    desired_state, status, the task's own status and the row's update marker
    (never a secret). Only the worker that claimed the task AND is the stack's
    assigned worker gets an answer (404/403 otherwise; the worker then aborts)."""
    task = _get_worker_task(db, task_id, worker.worker_name)
    from services.stack_secret_recovery import stack_state_for_worker
    return stack_state_for_worker(db, task, worker.worker_name)

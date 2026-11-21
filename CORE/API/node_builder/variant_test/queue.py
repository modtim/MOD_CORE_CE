"""wizard-rebuild §9 — speculative-variant pending-test queue.

When a variant's sample-test would need accelerator hardware that
isn't currently online in the cluster, the build pipeline doesn't
fail the variant — it queues the test in ``variant_test_pending``
with a 30-day TTL. The worker-heartbeat hook drains the queue when
a matching-hardware worker joins. The APScheduler hourly sweep
expires rows whose TTL elapsed without ever finding hardware.

Public API:

  - `cluster_has_accelerator(db, variant)` — does any currently
    online worker advertise an accelerator matching this variant?
    Reused by both the enqueue path (do we need to queue?) and the
    heartbeat-drain path (can we run pending rows yet?).

  - `enqueue_pending(db, *, build_spec_id, variant, image_ref,
    ttl_days=30)` — insert a pending row. Idempotent: re-enqueueing
    an existing pending row is a no-op.

  - `drain_for_worker(db, worker_license, capabilities)` — called
    from the worker-heartbeat handler when a worker reports its
    accelerator capabilities. Claims matching pending rows
    (atomic UPDATE) and returns them. The caller is expected to
    dispatch the actual sample-test runs (in v1 this is via the
    existing api_task_queue + system-sample-test-one-variant
    node; the queue module itself only marks the rows claimed).

  - `expire_stale(db)` — hourly sweep entry point. Marks
    `expires_at < now` rows as `expired` and (when bound to a
    NodeDefinition) flips the variant's verification verdict to
    `untested_awaiting_hardware`. Returns the count expired.

  - `queue_depth_by_accelerator(db)` — admin dashboard helper.
    Returns ``{accelerator: count_pending}`` across all tenants.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# Accelerator-key to AcceleratorProfile.kind mapping. The wizard's
# variant keys (cuda / rocm / intel / npu / cpu) align with the
# `kind` field workers report on heartbeat — except `cpu` which is
# always present (every worker has a CPU) so no accelerator-profile
# match is needed.
_VARIANT_TO_KIND: Dict[str, str] = {
    "cuda":  "cuda",
    "rocm":  "rocm",
    "intel": "intel",
    "npu":   "npu",
    "cpu":   "cpu",   # synthetic match — every worker
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def online_accelerator_kinds(db) -> set:
    """The set of accelerator kinds (lowercase: cuda/rocm/intel/npu/...) for
    which at least one ONLINE worker advertises a matching AcceleratorProfile.

    One aggregated query — the variant-test backfill calls this once per sweep
    rather than probing per variant.

    Link direction matters: a worker points at its (deduplicated) hardware
    profile via ``WorkerLicense.hardware_profile_id`` → ``HardwareProfile.id``;
    AcceleratorProfile rows hang off the hardware profile. HardwareProfile has
    NO worker_id column (it's shared across identical hardware), so the join
    MUST go through WorkerLicense.hardware_profile_id — the previous
    ``HardwareProfile.worker_id`` join raised AttributeError and this function
    was, mercifully, never wired into the enqueue path.

    Online = LICENSED, heartbeating within 5 min, not paused, not mid-update
    (same "active worker" definition as the unrunnable-task sweep).
    """
    import models
    stale_cutoff = _utcnow() - timedelta(minutes=5)
    rows = (
        db.query(models.AcceleratorProfile.kind)
        .join(
            models.HardwareProfile,
            models.AcceleratorProfile.hardware_profile_id == models.HardwareProfile.id,
        )
        .join(
            models.WorkerLicense,
            models.WorkerLicense.hardware_profile_id == models.HardwareProfile.id,
        )
        .filter(
            models.WorkerLicense.status == models.WorkerStatus.LICENSED,
            models.WorkerLicense.last_heartbeat > stale_cutoff,
            models.WorkerLicense.paused.is_(False),
            models.WorkerLicense.update_requested.is_(False),
            models.WorkerLicense.deleted_at.is_(None),
        )
        .distinct()
        .all()
    )
    return {(k or "").strip().lower() for (k,) in rows if k}


def cluster_has_accelerator(db, variant: str) -> bool:
    """True iff any ONLINE worker advertises an accelerator matching `variant`.
    `cpu` always returns True (every worker has a CPU)."""
    if variant == "cpu":
        return True
    kind = _VARIANT_TO_KIND.get(variant)
    if not kind:
        return False
    return kind in online_accelerator_kinds(db)


def enqueue_pending(
    db,
    *,
    build_spec_id: Optional[int],
    node_definition_id: Optional[int] = None,
    variant: str,
    image_ref: str,
    ttl_days: int = 30,
) -> int:
    """Insert a pending row. Idempotent — if a pending row already
    exists for this (build_spec_id, variant) we leave it alone and
    return the existing id.

    At least one of build_spec_id / node_definition_id must be set.

    Returns the row's id.
    """
    if build_spec_id is None and node_definition_id is None:
        raise ValueError(
            "enqueue_pending requires at least one of build_spec_id, node_definition_id",
        )
    import models
    existing = (
        db.query(models.VariantTestPending)
        .filter(
            models.VariantTestPending.variant == variant,
            models.VariantTestPending.status == "pending",
        )
    )
    if build_spec_id is not None:
        existing = existing.filter(
            models.VariantTestPending.build_spec_id == build_spec_id,
        )
    else:
        existing = existing.filter(
            models.VariantTestPending.node_definition_id == node_definition_id,
        )
    row = existing.first()
    if row is not None:
        return row.id

    expires = _utcnow() + timedelta(days=ttl_days)
    new_row = models.VariantTestPending(
        build_spec_id=build_spec_id,
        node_definition_id=node_definition_id,
        variant=variant,
        image_ref=image_ref,
        status="pending",
        expires_at=expires,
    )
    db.add(new_row)
    db.commit()
    db.refresh(new_row)
    logger.info(
        "variant_test_pending: enqueued id=%d variant=%s spec=%s nd=%s "
        "expires=%s",
        new_row.id, variant, build_spec_id, node_definition_id, expires,
    )
    return new_row.id


def drain_for_worker(
    db,
    worker_license_id: int,
    capabilities: Iterable[str],
) -> List[Dict[str, Any]]:
    """Worker heartbeat hook. Atomically claim pending rows whose
    variant matches one of the worker's advertised accelerator
    kinds. Returns the claimed rows as dicts so the caller can
    dispatch the actual sample-test runs.

    `capabilities` is the list of accelerator kinds the worker
    reports — e.g. ``["cuda", "cpu"]``. Caller is responsible for
    extracting this from the heartbeat payload's accelerator list.
    """
    import models
    kinds = list({(c or "").strip().lower() for c in capabilities if c})
    if not kinds:
        return []
    # Map worker kinds → variant keys (1:1 except cpu which is
    # always covered).
    variants = [v for v, k in _VARIANT_TO_KIND.items() if k in kinds]
    if not variants:
        return []

    # 1580 — this hook is the REAL dispatch path for change-gate /
    # node-wizard variant-smoke sweep work (run_weekly_sweep ->
    # enqueue_pending -> VariantTestPending rows, drained here on every
    # heartbeat with no prior exclusion check — the path the 10-14 453-job
    # fan-out incident actually went through; `_try_claim_task`'s ApiTaskQueue
    # guard never sees these rows). Reuse the shared helper (same matching
    # semantics as the ApiTaskQueue claim guard) rather than re-parsing the
    # setting here. A worker named/id'd in
    # `scheduler.system_job_excluded_workers` claims nothing — its rows stay
    # `pending` for a non-excluded worker's next heartbeat.
    try:
        from routers.worker_tasks import _is_worker_system_excluded
        worker_name = (
            db.query(models.WorkerLicense.worker_name)
            .filter(models.WorkerLicense.id == worker_license_id)
            .scalar()
        )
        if _is_worker_system_excluded(db, worker_name, worker_license_id):
            return []
    except Exception:  # noqa: BLE001 — a lookup/settings failure must not
        # block the drain; degrade to "not excluded" (today's behaviour)
        # and log.
        logger.exception(
            "1580: drain_for_worker system-exclusion check failed "
            "(proceeding without it) worker_license_id=%s",
            worker_license_id,
        )

    # Atomic-ish UPDATE … RETURNING. SQLAlchemy 2.x supports this on
    # PostgreSQL; falls back to SELECT+UPDATE on other dialects.
    from sqlalchemy import update
    now = _utcnow()
    stmt = (
        update(models.VariantTestPending)
        .where(
            models.VariantTestPending.status == "pending",
            models.VariantTestPending.variant.in_(variants),
            models.VariantTestPending.expires_at > now,
        )
        .values(
            status="claimed",
            claimed_by_worker_id=worker_license_id,
            claimed_at=now,
        )
        .returning(
            models.VariantTestPending.id,
            models.VariantTestPending.build_spec_id,
            models.VariantTestPending.node_definition_id,
            models.VariantTestPending.variant,
            models.VariantTestPending.image_ref,
        )
    )
    rows = db.execute(stmt).all()
    db.commit()
    out = [
        {
            "id": r[0],
            "build_spec_id": r[1],
            "node_definition_id": r[2],
            "variant": r[3],
            "image_ref": r[4],
        }
        for r in rows
    ]
    if out:
        logger.info(
            "variant_test_pending: drained %d rows for worker=%d "
            "kinds=%s", len(out), worker_license_id, kinds,
        )
    return out


def expire_stale(db) -> int:
    """Hourly sweep. Mark TTL-elapsed pending rows as `expired` and
    (when bound to a NodeDefinition) flip the corresponding
    verification row's verdict to ``untested_awaiting_hardware``
    so the scheduler refuses to route real workloads.

    Returns the count expired.
    """
    import models
    from sqlalchemy import update
    now = _utcnow()
    stmt = (
        update(models.VariantTestPending)
        .where(
            models.VariantTestPending.status == "pending",
            models.VariantTestPending.expires_at <= now,
        )
        .values(status="expired")
        .returning(
            models.VariantTestPending.id,
            models.VariantTestPending.node_definition_id,
            models.VariantTestPending.variant,
        )
    )
    rows = db.execute(stmt).all()
    if not rows:
        db.commit()
        return 0
    # For each expired row that's bound to a NodeDefinition, mark
    # the verification row's verdict.
    for _id, nd_id, variant in rows:
        if nd_id is None:
            continue
        vrow = (
            db.query(models.NodeVariantVerification)
            .filter(
                models.NodeVariantVerification.node_definition_id == nd_id,
                models.NodeVariantVerification.variant == variant,
            )
            .first()
        )
        if vrow is not None:
            vrow.verdict = "untested_awaiting_hardware"
            vrow.scored_at = now
    db.commit()
    logger.info(
        "variant_test_pending.expire_stale: marked %d rows expired",
        len(rows),
    )
    return len(rows)


def queue_depth_by_accelerator(db) -> Dict[str, int]:
    """Admin dashboard helper. Returns ``{accelerator: count}``
    for all pending rows across all tenants. Used by the platform
    admin's queue-depth-per-accelerator panel."""
    import models
    from sqlalchemy import func
    rows = (
        db.query(
            models.VariantTestPending.variant,
            func.count(models.VariantTestPending.id),
        )
        .filter(models.VariantTestPending.status == "pending")
        .group_by(models.VariantTestPending.variant)
        .all()
    )
    return {variant: int(count) for variant, count in rows}

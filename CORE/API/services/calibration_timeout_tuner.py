"""GA.CHF.2 — evidence-based calibration container_timeout tuning.

The coordinator reads a node's kill switch from
`NodeDefinition.container_timeout` (services/calibration_workflow.py line 180,
fallback 900) when it builds a calibration focal task. That value is a static
default and has never been checked against what calibrated runs ACTUALLY take.
This module closes the loop: it measures completed calibration focal tasks
(`api_task_queue.started_at` -> `completed_at`, selected exactly like the
coordinator's own sweep on `task_payload->>'calibration_run' == 'true'`) and,
when the observed p99 runs close to the configured timeout, proposes — and can
apply — a new timeout through the same column the coordinator reads.

Proposal rule: for a node_kind whose p99 runtime exceeds 0.8x the current
timeout, propose `ceil(2 * p99)`. Kinds with fewer than MIN_SAMPLES completed
samples are skipped — a handful of runs is not evidence.

No scheduler wiring here; these are plain functions an operator (or a later
endpoint) drives. Dry-run is the default: nothing is written unless
`dry_run=False`. No rows are ever hard-deleted; changes are column updates on
`node_definitions` plus one audit event per changed row.
"""

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from models import (
    ApiTaskQueue,
    ApiTaskStatus,
    AuditAction,
    NodeDefinition,
)

# Calibration generator and stressor tasks are calibration-marked too, but
# their timeouts come from env vars / duration math, not from a
# NodeDefinition row — tuning them here would write nothing.
_EXCLUDED_KINDS = ("calibration_input_generator", "calibration_stressor")

MIN_SAMPLES = 5
PROPOSAL_THRESHOLD = 0.8
PROPOSAL_FACTOR = 2.0


def _calibration_mark() -> bool:
    return ApiTaskQueue.task_payload["calibration_run"].as_string() == "true"


def _completed_focal_tasks(db: Session, since: Optional[datetime] = None):
    q = db.query(ApiTaskQueue).filter(
        _calibration_mark(),
        ApiTaskQueue.status == ApiTaskStatus.COMPLETED,
        ApiTaskQueue.started_at.isnot(None),
        ApiTaskQueue.completed_at.isnot(None),
    )
    if since is not None:
        q = q.filter(ApiTaskQueue.completed_at >= since)
    return q.all()


def _duration_seconds(task) -> Optional[float]:
    if task.started_at is None or task.completed_at is None:
        return None
    delta = (task.completed_at - task.started_at).total_seconds()
    return delta if delta > 0 else None


def _p99_nearest_rank(values: List[float]) -> float:
    """Nearest-rank p99: sorted[ceil(0.99*n)-1]. Deterministic, no
    interpolation — a timeout proposal should be traceable to an actual
    observed run, not a synthetic fraction between runs."""
    ordered = sorted(values)
    idx = max(0, math.ceil(0.99 * len(ordered)) - 1)
    return float(ordered[idx])


def observed_p99_by_kind(
    db: Session, since: Optional[datetime] = None
) -> Dict[str, Tuple[float, int]]:
    """Completed calibration focal tasks grouped by node_kind.

    Returns {node_kind: (p99_seconds, n)} for kinds with at least
    MIN_SAMPLES samples. Kinds below that bar, the generator, and the
    stressor are omitted rather than estimated.
    """
    durations: Dict[str, List[float]] = {}
    for task in _completed_focal_tasks(db, since=since):
        payload = task.task_payload or {}
        kind = payload.get("node_kind")
        if not kind or kind in _EXCLUDED_KINDS:
            continue
        seconds = _duration_seconds(task)
        if seconds is None:
            continue
        durations.setdefault(kind, []).append(seconds)

    return {
        kind: (_p99_nearest_rank(vals), len(vals))
        for kind, vals in durations.items()
        if len(vals) >= MIN_SAMPLES
    }


def _active_node_defs_for_kind(db: Session, kind: str) -> List[NodeDefinition]:
    return (
        db.query(NodeDefinition)
        .filter(
            NodeDefinition.node_kind == kind,
            NodeDefinition.deleted_at.is_(None),
        )
        .all()
    )


def _current_timeout_for_kind(
    db: Session, kind: str
) -> Optional[int]:
    rows = _active_node_defs_for_kind(db, kind)
    if not rows:
        return None
    return max(int(r.container_timeout or 0) for r in rows)


def propose_timeouts(db: Session, since: Optional[datetime] = None) -> List[dict]:
    """Propose new timeouts for kinds whose evidence says the current kill
    switch is too tight.

    A proposal is emitted when p99 > PROPOSAL_THRESHOLD * current timeout,
    with `new = ceil(PROPOSAL_FACTOR * p99)`. Kinds with no active
    NodeDefinition row (nothing to write) or no current timeout are skipped.
    """
    proposals: List[dict] = []
    for kind, (p99, n) in sorted(observed_p99_by_kind(db, since=since).items()):
        current = _current_timeout_for_kind(db, kind)
        if not current or current <= 0:
            continue
        if p99 <= PROPOSAL_THRESHOLD * current:
            continue
        proposed = int(math.ceil(PROPOSAL_FACTOR * p99))
        if proposed <= current:
            continue
        proposals.append({
            "node_kind": kind,
            "current": current,
            "p99": p99,
            "n": n,
            "proposed": proposed,
        })
    return proposals


def apply_timeouts(
    db: Session,
    proposals: List[dict],
    actor: Optional[int],
    dry_run: bool = True,
) -> List[dict]:
    """Apply proposals by writing `NodeDefinition.container_timeout` — the
    exact column the coordinator reads at dispatch — one column update per
    active node definition of the kind, plus one audit event per changed
    row. Dry-run (the default) writes nothing and returns the proposals
    untouched.
    """
    if dry_run:
        return list(proposals)

    from audit import log_admin_action

    actor_id = int(actor) if actor else 0
    applied: List[dict] = []
    for proposal in proposals:
        kind = proposal.get("node_kind")
        new_timeout = int(proposal.get("proposed", 0))
        if not kind or new_timeout <= 0:
            continue
        for row in _active_node_defs_for_kind(db, kind):
            old = row.container_timeout
            row.container_timeout = new_timeout
            db.flush()
            log_admin_action(
                db,
                AuditAction.ADMIN_CONFIG_CHANGE,
                actor_id,
                resource_type="node_definition",
                resource_id=row.id,
                metadata={
                    "node_kind": kind,
                    "field": "container_timeout",
                    "before": old,
                    "after": new_timeout,
                    "p99_seconds": proposal.get("p99"),
                    "samples": proposal.get("n"),
                    "source": "calibration_timeout_tuner",
                },
            )
        applied.append(dict(proposal))
    db.commit()
    return applied

"""sz-2.5 — pause at persisted breakpoints during downstream dispatch.

`_check_and_dispatch_downstream` already holds a node when the operator marked
an explicit pause point (`WorkflowRun.paused_nodes`). sz-2.1 added the other
half of the same idea: a `WorkflowBreakpoint` row, the canvas right-click that
says "stop here every time". This module is the bridge — it turns a live
breakpoint row into a persisted pause at the exact moment the orchestrator
would otherwise enqueue the node, so the run waits there and resume-node's
existing re-kick works unchanged.

Ownership is the whole point of the sz-2.1 keying: breakpoints are per-user, so
a teammate's breakpoint on the same node must NOT pause my run. A run with no
user (schedule / webhook triggered) has nobody's breakpoint to honour and is
never paused.

Fail-safe in every direction: an unrecognised `condition_json` shape, an
exhausted paused-run quota (sz-2.9), or any exception at all means "do not
pause" — dispatch must never break because of a debug feature. The service only
flushes; the caller owns the commit.

The WS push (`publish_debug_event`, routers/workflows_debug_ws.py) is a later
item; the router must not be imported here, so the hit is recorded as a
`WorkflowRunEvent` row (the run-detail Logs panel) plus a logger line.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

import models
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from services import paused_run_limits as prl

logger = logging.getLogger(__name__)

EVENT_SOURCE = "breakpoint"
# The only condition shapes this gate understands. Anything else is a
# not-yet-implemented predicate (or a hand-edited row) and fails safe: no pause.
ALWAYS_CONDITION = {"always": True}


def _matches(condition: Optional[dict]) -> bool:
    """NULL / {"always": true} -> pause; any other shape -> do not."""
    if condition is None:
        return True
    return isinstance(condition, dict) and condition == ALWAYS_CONDITION


def check_and_hold(db: Session, workflow_run, node_id: str) -> bool:
    """Persist a breakpoint pause for `node_id` on `workflow_run`.

    Returns True when the node must be HELD (the caller skips enqueueing it),
    False when dispatch should proceed. Never raises.
    """
    try:
        if workflow_run is None or not node_id:
            return False

        # Breakpoints belong to the user who started the run. A schedule/webhook
        # run has no user -> nobody's breakpoint applies.
        user_id = getattr(workflow_run, "triggered_by_user_id", None)
        if not user_id:
            return False

        rows: List[models.WorkflowBreakpoint] = (
            db.query(models.WorkflowBreakpoint)
            .filter(
                models.WorkflowBreakpoint.tenant_id == workflow_run.tenant_id,
                models.WorkflowBreakpoint.workflow_version_id
                == workflow_run.workflow_version_id,
                models.WorkflowBreakpoint.node_id == node_id,
                models.WorkflowBreakpoint.user_id == user_id,
                models.WorkflowBreakpoint.enabled.is_(True),
                models.WorkflowBreakpoint.deleted_at.is_(None),
            )
            .all()
        )
        if not rows:
            return False

        matching: List[models.WorkflowBreakpoint] = []
        for bp in rows:
            if _matches(bp.condition_json):
                matching.append(bp)
            else:
                logger.info(
                    "breakpoint_gate: unsupported condition_json on breakpoint %s "
                    "(node %s) — not pausing",
                    bp.id, node_id,
                )
        if not matching:
            return False

        quota = prl.can_pause(db, workflow_run.tenant_id)
        if not quota.get("allowed", False):
            logger.warning(
                "breakpoint_gate: paused-run limit reached for tenant %s "
                "(%s/%s) — breakpoints %s not applied, node %s dispatched",
                workflow_run.tenant_id, quota.get("paused"), quota.get("quota"),
                [bp.id for bp in matching], node_id,
            )
            return False

        held = list(workflow_run.paused_nodes or [])
        if node_id in held:
            # Already waiting here: the pause is in force, but the stats must not
            # double-count the same hold.
            return True

        # B99: a node-level hold is released by resume-node / step_over, which
        # only edit paused_nodes. If the gate re-armed here the released node
        # would be held again on the frontier re-kick and never dispatch (that
        # is exactly the hot-swap/step_over dead-end the sz-2.11 test pinned).
        # The run's own `breakpoint_hit` event row (written below) is the
        # per-run marker: once this run has been held at this node, the hold is
        # spent — a later re-kick dispatches, and a fresh run holds again.
        # Matched on event_metadata["node_id"] (not the message string) so a
        # cosmetic message-copy change can't silently re-arm the gate.
        spent = db.query(
            models.WorkflowRunEvent
        ).filter(
            models.WorkflowRunEvent.workflow_run_id == workflow_run.id,
            models.WorkflowRunEvent.source == EVENT_SOURCE,
        ).all()
        if any((e.event_metadata or {}).get("node_id") == node_id for e in spent):
            logger.info(
                "breakpoint_gate: run %s already held at node %s this run "
                "(hold spent by resume/step_over) — dispatching, not re-holding",
                workflow_run.id, node_id,
            )
            return False

        now = datetime.now(timezone.utc)
        workflow_run.paused_nodes = held + [node_id]
        flag_modified(workflow_run, "paused_nodes")
        # B99: a breakpoint is a NODE-level hold — exactly like the operator's
        # pause-node, which only edits paused_nodes. Setting the run-level
        # `paused` flag here froze the whole frontier: resume_node_in_run and
        # step_over/hot_swap only clear paused_nodes, so nothing ever cleared
        # run.paused and the top-of-dispatch guard held every node. The sz-2.9
        # quota + expiry sweep now count node-held runs too (see
        # paused_run_limits._paused_runs), so the quota check above still holds.

        for bp in matching:
            bp.hit_count = (bp.hit_count or 0) + 1
            bp.last_hit_at = now

        db.add(
            models.WorkflowRunEvent(
                workflow_run_id=workflow_run.id,
                source=EVENT_SOURCE,
                level="info",
                message=f"breakpoint_hit at node {node_id}",
                event_metadata={
                    "node_id": node_id,
                    "breakpoint_ids": [bp.id for bp in matching],
                },
            )
        )
        db.flush()
        logger.info(
            "breakpoint_gate: run %s held at node %s (breakpoints %s persisted to "
            "paused_nodes)",
            workflow_run.id, node_id, [bp.id for bp in matching],
        )
        return True
    except Exception as exc:  # never break dispatch because of a debug feature
        logger.warning("breakpoint_gate: %s — not pausing", type(exc).__name__)
        return False

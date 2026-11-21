"""project-agent §3.2/§3.3 — following a project's work down to the metal.

The drill-down already existed for four of its five levels. This supplies
the top one and the walk between them:

    project   support_chat_sessions (kind='project')
      run     workflow_runs.project_session_id
      node    node_runs.inputs / .outputs (JSONB, per stage)
      event   workflow_run_events (source / level / message)
      stdout  container output, via get_workflow_detailed_log

§3.3 is the reason the node level matters. When a project's assembled work
produces the wrong answer, the per-stage outputs are already recorded, so
the first stage that deviated can be identified instead of guessed at.

What "deviated" can mean here is bounded by design D4: the user supplies
the two ENDPOINTS — real input, expected output — so middle stages have no
semantic reference. Type and shape checks are what is honest in the middle,
and they catch most composition failures (a stage emitting JSON where the
next wanted a file). Semantic scoring belongs at the ends, against the real
example.

So this does NOT guess whether a stage's output was "correct". It reports
the first stage that failed, produced nothing, or produced something the
next stage could not have consumed — and says plainly when every stage
looks fine, because "all stages passed and the answer is still wrong" is a
different and more useful finding than a false accusation.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Why a stage is flagged. Ordered by how much they tell you.
DEVIATION_FAILED = "failed"            # the stage errored
DEVIATION_NO_OUTPUT = "no_output"      # succeeded, produced nothing
DEVIATION_TYPE_BREAK = "type_break"    # output the next stage cannot consume
DEVIATION_NONE = "none"                # every stage looks fine


def project_runs(db, project_session_id: str, *, limit: int = 200) -> List[Any]:
    """Every run dispatched for a project, oldest first.

    Oldest first because a project is read as a story — what was built,
    then what was run, then what was repaired. Run lists elsewhere are
    newest-first because they are read as a status.
    """
    import models
    return (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.project_session_id == project_session_id)
        .order_by(models.WorkflowRun.id.asc())
        .limit(limit)
        .all()
    )


def project_summary(db, project_session_id: str) -> Dict[str, Any]:
    """One line's worth of "how is my project going", plus the counts
    behind it so the number is checkable rather than asserted."""
    import models
    runs = project_runs(db, project_session_id)
    by_status: Dict[str, int] = {}
    for r in runs:
        key = getattr(r.status, "value", str(r.status)) if r.status else "unknown"
        by_status[key] = by_status.get(key, 0) + 1

    node_run_count = 0
    if runs:
        node_run_count = (
            db.query(models.NodeRun)
            .filter(models.NodeRun.workflow_run_id.in_([r.id for r in runs]))
            .count()
        )
    return {
        "project_session_id": project_session_id,
        "run_count": len(runs),
        "runs_by_status": by_status,
        "node_run_count": node_run_count,
        "run_ids": [r.id for r in runs],
    }


def _has_output(node_run) -> bool:
    out = node_run.outputs
    if out is None:
        return False
    if isinstance(out, (dict, list, str)):
        return len(out) > 0
    return True


def _output_keys(node_run) -> List[str]:
    out = node_run.outputs
    return sorted(out.keys()) if isinstance(out, dict) else []


def _input_keys(node_run) -> List[str]:
    inp = node_run.inputs
    return sorted(inp.keys()) if isinstance(inp, dict) else []


def first_deviating_stage(db, workflow_run_id: int) -> Dict[str, Any]:
    """§3.3 — the first stage of a run whose output stopped looking right.

    Walks node runs in execution order and returns the first that failed,
    produced nothing, or produced something the following stage did not
    consume. Returns ``reason == DEVIATION_NONE`` when every stage looks
    fine — which, on a run whose final answer is wrong, is itself the
    finding: the stages worked and the composition did not.
    """
    import models

    node_runs = (
        db.query(models.NodeRun)
        .filter(models.NodeRun.workflow_run_id == workflow_run_id)
        .order_by(
            models.NodeRun.started_at.asc().nullslast(),
            models.NodeRun.id.asc(),
        )
        .all()
    )
    if not node_runs:
        return {
            "workflow_run_id": workflow_run_id,
            "reason": DEVIATION_NONE,
            "node_run_id": None,
            "detail": "no node runs recorded for this run",
            "stages_examined": 0,
        }

    for index, nr in enumerate(node_runs):
        status = getattr(nr.status, "value", str(nr.status)) if nr.status else ""
        if status and status.lower() in ("failed", "error"):
            return {
                "workflow_run_id": workflow_run_id,
                "reason": DEVIATION_FAILED,
                "node_run_id": nr.id,
                "workflow_node_id": nr.workflow_node_id,
                "stage_index": index,
                "detail": nr.error_message or "stage failed with no message",
                "stages_examined": index + 1,
            }

        if status and status.lower() in ("succeeded", "success", "completed") \
                and not _has_output(nr):
            return {
                "workflow_run_id": workflow_run_id,
                "reason": DEVIATION_NO_OUTPUT,
                "node_run_id": nr.id,
                "workflow_node_id": nr.workflow_node_id,
                "stage_index": index,
                "detail": "stage reported success but recorded no output",
                "stages_examined": index + 1,
            }

        # Type/shape check against the NEXT stage. This is the honest
        # middle-of-the-pipeline check per D4 — no semantic reference
        # exists here, but a downstream stage that consumed none of what
        # its predecessor produced is a real break regardless.
        nxt = node_runs[index + 1] if index + 1 < len(node_runs) else None
        if nxt is not None and _has_output(nr):
            produced = set(_output_keys(nr))
            consumed = set(_input_keys(nxt))
            if produced and consumed and not (produced & consumed):
                return {
                    "workflow_run_id": workflow_run_id,
                    "reason": DEVIATION_TYPE_BREAK,
                    "node_run_id": nr.id,
                    "workflow_node_id": nr.workflow_node_id,
                    "stage_index": index,
                    "detail": (
                        f"produced {sorted(produced)} but the next stage "
                        f"consumed {sorted(consumed)}"
                    ),
                    "stages_examined": index + 2,
                }

    return {
        "workflow_run_id": workflow_run_id,
        "reason": DEVIATION_NONE,
        "node_run_id": None,
        "detail": (
            "every stage ran, produced output, and fed the next. If the "
            "final answer is wrong the fault is in the composition or in a "
            "stage's semantics, not in a stage that visibly broke."
        ),
        "stages_examined": len(node_runs),
    }

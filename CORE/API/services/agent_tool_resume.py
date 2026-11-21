"""EXPR 4.4 slice B — resume a PARKED agent node when its tool child run finishes.

Sibling of ``services/sub_workflow_runtime.complete_parent_for_child``, mirrored
step for step (:343). The difference is semantic, not structural: a plain
sub_workflow node is completed ONCE by its child, an agent node needs RE-ENTRY
— the child's terminal output becomes one more tool result in the agent's
history and the loop continues on a fresh thread with the remaining tool budget.

Pinned contract (slice A writes it, this slice reads it): a parked agent NodeRun
has ``status=PENDING_ACTION`` and
``outputs = {CHILD_MARKER: child_run_id, "agent_state": {history, pending_ref,
pending_args, tool_calls_used, max_tool_calls}, ...}``. A parked node WITHOUT
``agent_state`` is a plain sub_workflow park and is left to
``complete_parent_for_child`` — this returns False so the normal path runs.

Race guard, tenant scoping, and the never-raise contract are the same as the
sub-workflow completion hook: the marker lookup is JSONB containment on
NodeRun.outputs, the tenant check goes through the PARENT's own WorkflowRun
(NodeRun has no tenant column), and the PENDING_ACTION -> RUNNING flip is an
atomic conditional UPDATE so a double fan-out resumes the loop exactly once.

No module-level import of the orchestrator: the orchestrator grows a call-site
hook into this module, so a top-level import back would be the FX.106 p3a
circular shape. The orchestrator is imported lazily where used.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified

import models
from product_names import themed_logger
from services import sub_workflow_runtime as swr
from services import agent_node_runtime as art
try:
    from services import llm_metering
except ImportError:
    llm_metering = None
try:
    from services import run_purposes
except (ImportError, ModuleNotFoundError):
    run_purposes = None

logger = themed_logger(__name__, 'agents')

CHILD_MARKER = swr.CHILD_MARKER
AGENT_STATE_KEY = "agent_state"
PENDING_REF_KEY = "pending_ref"
PENDING_ARGS_KEY = "pending_args"
RESUMED_HISTORY_KEY = "resumed_history"

# Mirrors agent_node_loop.run_agent_loop's own max_history_chars default: a
# single enormous child-run output must not blow up the JSONB row on THIS
# commit, before the resumed loop's own per-turn truncation gets a chance to
# run (agent_node_loop._truncate_history only fires on the NEXT tool call).
MAX_TOOL_RESULT_CHARS = 200_000

CHILD_TERMINAL_STATUSES = (models.TaskStatus.COMPLETED, models.TaskStatus.FAILED)

TOOL_ERROR_CHILD_FAILED = "tool_child_failed"
TOOL_ERROR_CHILD_CANCELLED = "tool_child_cancelled"


def resume_agent_for_child(db, child_run) -> bool:
    """Terminal fan-out: a tool CHILD run finished -> re-enter the parked agent.

    MUST NOT raise: this runs inside ``fire_completion_triggers``, where a
    failure would roll back the child's committed terminal status.

    Returns True when the agent loop was re-claimed (and handed to a thread),
    False when there is no parked AGENT parent (no marker, or no ``agent_state``
    -> the plain sub-workflow path owns it), when the parent is foreign-tenant
    / already terminal, or when the atomic claim lost a race.
    """
    try:
        return _resume_agent_for_child_inner(db, child_run)
    except Exception as exc:  # noqa: BLE001 — never propagate out of the fan-out
        logger.warning(
            "agent tool resume for child run %s failed (%s)",
            child_run.id, type(exc).__name__, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _bounded_tool_content(content: Any) -> Any:
    """Cap a tool result's serialized size before it lands in NodeRun.outputs.

    A huge child-run output (a large file listing, a big LLM transcript, ...)
    must not blow up the JSONB row or the next LLM turn's context. Small
    results pass through unchanged (no re-serialization cost / shape change).
    """
    try:
        text = content if isinstance(content, str) else json.dumps(content)
    except (TypeError, ValueError):
        text = str(content)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return content
    return {"truncated": True, "preview": text[:MAX_TOOL_RESULT_CHARS]}


def _resume_agent_for_child_inner(db, child_run) -> bool:
    from services.workflow_orchestrator import _emit_run_event

    if child_run.status not in CHILD_TERMINAL_STATUSES:
        return False

    # The marker row: JSONB containment on NodeRun.outputs. The agent_state
    # narrowing is done in Python below (the JSONB key-existence operator is
    # dialect-specific; containment on the marker is the same query the
    # sub-workflow completion hook runs, so both stay in lockstep).
    candidates = (
        db.query(models.NodeRun)
        .filter(
            models.NodeRun.outputs[CHILD_MARKER].as_integer() == child_run.id,
        )
        .all()
    )
    parent_node_run = None
    parent_run = None
    for row in candidates:
        run = db.get(models.WorkflowRun, row.workflow_run_id)
        if run is None or run.id == child_run.id:
            continue
        if run.tenant_id != child_run.tenant_id:
            # Foreign tenant with a forged marker — never resumed.
            continue
        state = (row.outputs or {}).get(AGENT_STATE_KEY)
        if not isinstance(state, dict):
            # No agent_state: a plain sub_workflow park. This hook deliberately
            # abstains so complete_parent_for_child owns the node.
            continue
        if row.status in swr.TERMINAL_NODE_STATUSES:
            # Idempotent: the parent already reached terminal on an earlier
            # fan-out, so a second call is a no-op.
            return False
        if row.status != models.TaskStatus.PENDING_ACTION:
            # Already re-entered (RUNNING) by an earlier resume: no double entry.
            return False
        parent_node_run = row
        parent_run = run
        break
    if parent_node_run is None or parent_run is None:
        return False

    state = parent_node_run.outputs[AGENT_STATE_KEY]
    history = state.get("history")
    if not isinstance(history, list):
        history = []
    pending_ref = state.get(PENDING_REF_KEY)
    pending_args = state.get(PENDING_ARGS_KEY)
    # EXPR 4.4 S2 — the parent run's purpose rides the parked agent_state
    # (agent_node_runtime:529). Read it here so BOTH the telemetry below and
    # the re-entry dispatch carry the real purpose: without it, re-entry runs
    # with purpose=None and a sandbox-parented agent leaks cache rows +
    # quality signals on every resumed leg.
    purpose = state.get("purpose")
    try:
        used = int(state.get("tool_calls_used") or 0)
    except (TypeError, ValueError):
        used = 0
    try:
        budget_total = int(state.get("max_tool_calls") or 0)
    except (TypeError, ValueError):
        budget_total = 0
    budget_left = max(1, budget_total - used)

    # Atomic claim: the same conditional UPDATE the sub-workflow completion uses
    # for PENDING_ACTION -> terminal, here PENDING_ACTION -> RUNNING. Postgres
    # row-locks the matched row, so a racing fan-out blocks, re-evaluates the
    # WHERE clause against the committed status, matches zero rows and loses
    # cleanly instead of re-entering the loop twice.
    claimed = (
        db.query(models.NodeRun)
        .filter(
            models.NodeRun.id == parent_node_run.id,
            models.NodeRun.status == models.TaskStatus.PENDING_ACTION,
        )
        .update({"status": models.TaskStatus.RUNNING},
                synchronize_session="fetch")
    )
    if claimed == 0:
        # Lost the race: another resume already claimed this parked node.
        db.rollback()
        return False

    # The tool result from the child's terminal outputs. A FAILED/CANCELLED
    # child is NOT a parent-run cancel (unlike sub_workflow_runtime:435-446):
    # the agent sees the error entry and may retry, bounded by the budget.
    child_ok = child_run.status == models.TaskStatus.COMPLETED
    child_outputs = (swr.child_terminal_outputs(db, child_run)
                     if child_ok else None)
    tool_error = (None if child_ok else
                  TOOL_ERROR_CHILD_FAILED
                  if child_run.status == models.TaskStatus.FAILED
                  else TOOL_ERROR_CHILD_CANCELLED)
    if child_ok:
        tool_entry = {"role": "tool", "status": "ok",
                      "content": _bounded_tool_content(child_outputs)}
    else:
        tool_entry = {"role": "tool", "status": "error", "error": tool_error}
    resumed_history = copy.deepcopy(history) + [tool_entry]

    # EXPR 4.4 S1+S3 — the resumed child's outcome is the ONLY real outcome of
    # a parked sub_workflow tool call (the forward path deliberately skips
    # parked dispatches), so the resume side records it: one quality signal per
    # resumed child (ok AND failed — the sandbox skip lives INSIDE the helper)
    # and an output-cache row only on an ok, non-sandbox outcome. Best-effort:
    # a telemetry failure must never stop the DAG from advancing.
    try:
        tool_outcome = {"ok": child_ok, "output": child_outputs,
                        "error": tool_error}
        art._record_agent_tool_quality_signal(
            db, tenant_id=parent_run.tenant_id, tool_ref=pending_ref,
            arguments=pending_args, outcome=tool_outcome, purpose=purpose,
        )
        if child_ok and (run_purposes is None
                         or not run_purposes.is_sandbox_purpose(purpose)):
            # FORTRESS.6.2 (910c) — resume-side write needs the same dp-spec
            # guard as the forward path (agent_node_runtime.run_agent_node):
            # without graph_json, _record_agent_tool_cache can't see a dp
            # spec on the tool's target node and would store the resumed
            # child's RAW (un-noised) output.
            art._record_agent_tool_cache(
                db, tenant_id=parent_run.tenant_id, tool_ref=pending_ref,
                arguments=pending_args, output=child_outputs,
                graph_json=_graph_json_for_run(db, parent_run.id),
            )
    except Exception as exc:  # noqa: BLE001 — class name + run id only (GSEC)
        logger.warning(
            "agent tool resume: outcome recording failed (%s) for run %s",
            type(exc).__name__, parent_run.id,
        )

    outputs = dict(parent_node_run.outputs or {})
    outputs.pop(CHILD_MARKER, None)
    outputs.pop(AGENT_STATE_KEY, None)
    outputs.pop(PENDING_REF_KEY, None)
    outputs.pop(PENDING_ARGS_KEY, None)
    outputs[RESUMED_HISTORY_KEY] = resumed_history
    outputs["tool_calls_used"] = used
    parent_node_run.outputs = outputs
    flag_modified(parent_node_run, "outputs")
    db.commit()

    _emit_run_event(
        db, parent_run.id, "orchestrator", "info",
        f"Agent tool child run #{child_run.id} finished; agent node resumed",
        {"child_run_id": child_run.id, "tool_calls_used": used,
         "tool_budget_left": budget_left},
    )
    db.commit()

    wnode = db.get(models.WorkflowNode, parent_node_run.workflow_node_id)
    graph_node_id = (wnode.graph_node_id
                     if wnode is not None and wnode.graph_node_id
                     else str(parent_node_run.id))

    # Hand re-entry to a background thread exactly like the park side does, so
    # the fan-out request never blocks on the LLM turn. Production thread: its
    # OWN session, closed in finally, never the caller's.
    from database import SessionLocal
    threading.Thread(
        target=_resume_agent_thread,
        args=(SessionLocal, parent_run.tenant_id, parent_run.id,
              parent_node_run.id, graph_node_id, parent_node_run.workflow_node_id,
              purpose, pending_ref, pending_args, resumed_history, budget_left),
        daemon=True,
    ).start()
    return True


def _resume_agent_thread(db_factory, tenant_id: int, workflow_run_id: int,
                         node_run_id: int, graph_node_id: str,
                         workflow_node_id,
                         purpose: Optional[str],
                         pending_ref, pending_args,
                         history: List[Any], budget_left: int):
    """The monkeypatchable thread target: re-enter run_agent_node."""
    session = db_factory()
    try:
        node_config = _resume_node_config(db=session, tenant_id=tenant_id,
                                          workflow_run_id=workflow_run_id,
                                          workflow_node_id=workflow_node_id,
                                          pending_ref=pending_ref,
                                          pending_args=pending_args,
                                          budget_left=budget_left,
                                          history=history)
        _advance_resumed_agent(db=session, node_run_id=node_run_id,
                               node_id=graph_node_id, tenant_id=tenant_id,
                               workflow_run_id=workflow_run_id,
                               node_config=node_config, purpose=purpose)
    except Exception:  # noqa: BLE001 — a resume thread must not raise
        logger.warning("agent tool resume thread failed (%s) for run %s",
                       type(workflow_run_id).__name__, workflow_run_id)
    finally:
        session.close()


def _resume_node_config(db, *, tenant_id: int, workflow_run_id: int,
                        workflow_node_id, pending_ref, pending_args,
                        budget_left: int, history: List[Any]) -> Dict[str, Any]:
    """The node_config the resumed dispatch runs with (slice A's contract).

    ``run_agent_node`` takes no prior-history kwarg (signature at
    agent_node_runtime.py:192), so the prior history travels inside
    ``node_config["resumed_history"]`` and the resumed tool budget inside
    ``node_config["max_tool_calls"]`` — which
    ``agent_node_runner.effective_max_tool_calls`` already honours as the
    node_config value, so the cap is passed through, never counted twice.
    """
    node_config: Dict[Any, Any] = {}
    wnode = db.get(models.WorkflowNode, workflow_node_id)
    if wnode is not None:
        graph_run = db.get(models.WorkflowRun, workflow_run_id)
        version = (db.get(models.WorkflowVersion, graph_run.workflow_version_id)
                   if graph_run is not None else None)
        node = next((n for n in ((version.graph_json or {}).get("nodes") or [])
                    if isinstance(n, dict) and n.get("id") == wnode.graph_node_id),
                    None) if version is not None else None
        node_config = dict((node or {}).get("data") or {})
        node_config["node_kind"] = "agent"
    node_config["max_tool_calls"] = budget_left
    node_config[RESUMED_HISTORY_KEY] = history
    if pending_ref is not None:
        node_config["resumed_pending_ref"] = pending_ref
    if pending_args is not None:
        node_config["resumed_pending_args"] = pending_args
    return node_config


def _advance_resumed_agent(db, *, node_run_id: int, node_id: str, tenant_id: int,
                           workflow_run_id: int,
                           node_config: Dict[str, Any],
                           purpose: Optional[str] = None) -> None:
    """run_agent_node for the resumed history + terminal write (thread side).

    The parked agent's re-entry is a fresh agent-node dispatch on its own
    session, with LLM metering attributed to the tenant exactly as
    agent_node_runtime does for an off-request dispatch.

    Mirrors workflow_orchestrator._bg_agent_node step for step: the envelope
    `run_agent_node` returns is written back via `_agent_node_advance` (same
    helper the FIRST dispatch uses) so the resumed node reaches a terminal
    state and the DAG advances — without this, a successful resume would
    leave the NodeRun in RUNNING forever (it was claimed RUNNING by the
    atomic resume) and the workflow run would never progress. A raised
    runtime lands the same failure-advance path, so the resume thread never
    strands the node RUNNING either.
    """
    from services.workflow_orchestrator import _agent_node_advance

    graph_json = _graph_json_for_run(db, workflow_run_id)
    try:
        with (llm_metering.llm_tenant_context(tenant_id) if llm_metering is not None else nullcontext()):
            result = art.run_agent_node(
                db,
                tenant_id=tenant_id,
                workflow_run_id=workflow_run_id,
                node_id=node_id,
                node_config=node_config,
                agent_input=node_config.get(RESUMED_HISTORY_KEY) or [],
                graph_json=graph_json,
                node_run_id=node_run_id,
                purpose=purpose,
            )
        if not isinstance(result, dict):
            result = {"status": "failed",
                      "failure_reason": "agent runtime returned no envelope"}
        status = result.get("status")
        if status == "parked":
            # EXPR 4.4 slice C — the RESUMED agent parked again on its next
            # tool call: slice A already stamped PENDING_ACTION + agent_state +
            # CHILD_MARKER on this session, so nothing here is terminal (no
            # synthetic ApiTaskQueue row, no DAG advance). Commit so the new
            # park persists, then wait for the next child's fan-out.
            try:
                db.commit()
            except Exception as exc:  # noqa: BLE001 — park is best-effort
                logger.warning(
                    "agent tool resume: park commit failed (%s) for node run %s",
                    type(exc).__name__, node_run_id,
                )
            return
        completed = status == "completed"
        _agent_node_advance(
            db,
            workflow_run_id=workflow_run_id,
            node_run_id=node_run_id,
            node_id=node_id,
            tenant_id=tenant_id,
            completed=completed,
            outputs=result.get("outputs"),
            error_message=(None if completed
                           else result.get("failure_reason") or status),
        )
    except Exception as exc:  # noqa: BLE001 — the resume thread never strands RUNNING
        logger.warning(
            "agent tool resume: run_agent_node raised (%s) for node run %s",
            type(exc).__name__, node_run_id,
        )
        try:
            _agent_node_advance(
                db,
                workflow_run_id=workflow_run_id,
                node_run_id=node_run_id,
                node_id=node_id,
                tenant_id=tenant_id,
                completed=False,
                outputs=None,
                error_message=("agent tool resume failure: "
                               + f"{type(exc).__name__}: {str(exc)[:500]}"),
            )
        except Exception as exc2:  # noqa: BLE001 — advance is best-effort
            logger.warning(
                "agent tool resume: failure advance failed (%s) for node run %s",
                type(exc2).__name__, node_run_id,
            )


def _graph_json_for_run(db, workflow_run_id: int) -> Dict[str, Any]:
    run = db.get(models.WorkflowRun, workflow_run_id)
    version = (db.get(models.WorkflowVersion, run.workflow_version_id)
               if run is not None else None)
    return dict(version.graph_json) if version is not None and isinstance(
        version.graph_json, dict) else {}

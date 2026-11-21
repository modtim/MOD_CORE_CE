"""project-agent §6.1 — running a dispatched build, and recording its outcome.

`project_loop.tick` SEQUENCES: it decides a task is ready, opens a wizard child
session, and writes a ``ProjectHandoff(kind="wizard_dispatch",
status="dispatched")`` carrying the node-wizard payload. Nothing acted on that
row — the build the payload described was never run, no ``WorkflowRun`` was
created, ``project_dispatch.stamp_run`` had no caller, the spend ledger stayed
0 and no task could ever complete. The loop stopped exactly where the work
should have started.

This module is the missing consumer. For each dispatched handoff it:

  - translates the node-wizard payload into a real node-build request,
  - runs the build through the same path that
    ``/node_definitions/build-and-register-node`` uses (adapter →
    ``BuildSpecRecord`` → ``create_build_workflow`` → a workflow run),
  - creates a ``WorkflowRun`` STAMPED to the project (``stamp_run``: the
    project's user is the authority, ``project_session_id`` is the provenance,
    so the run's cost and events attribute back to the project),
  - records an agent ``dispatched`` event against that run,
  - links the run to the task's in-flight record so the stall check reads
    THIS task's own run events,
  - flips the handoff to ``consumed``.

A build that cannot even launch is a failed attempt: it is settled as failed
(which advances the escalation ladder) and its in-flight stamp is cleared so
the next tick retries at the next rung rather than waiting forever.

``reconcile_runs`` closes the other half. A build runs asynchronously on the
Go worker, so its SUCCESS or FAILURE lands later. ``reconcile_runs`` reads the
linked run's terminal status and, on success, records the artifact (the task
is done and drops out of the ready set) and, on failure, records the failure —
either way clearing the in-flight stamp. Driven by repeated ticks, dispatch →
build → outcome becomes one closed loop.

The builder is injectable (``builder=``) so the orchestration here — stamping,
events, the handoff flip, failure recording — is testable without a live
worker; production uses ``_default_builder``, wired to the real build path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# The WorkflowRun.purpose value marking a run the project agent dispatched to
# build one of its nodes. Distinct from 'production' and 'wizard_test_run' so
# these runs are filterable and never mistaken for a user's own workflow.
PROJECT_BUILD_PURPOSE = "project_build"

# A base to wrap when the payload names no concrete source. The generate phase
# (Containerizer LLM) reads the node-wizard payload's instructions and writes
# the Dockerfile/entrypoint on top of this; it only needs somewhere to start.
_DEFAULT_WRAP_BASE = "python:3.11-slim"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- translating a node-wizard payload into a build request ----------------

def _instructions_from_payload(payload: Dict[str, Any]) -> str:
    """Fold the payload's descriptive hints into the free-text instructions
    the generate phase reads.

    The node-wizard payload is deliberately descriptive (purpose, schema
    hints, what to avoid, a human's guidance) rather than a Dockerfile — the
    reasoning about the node stays with the parent agent (§6.1). The generate
    phase is where that description becomes a container, so everything the
    wizard was told is threaded into its instructions here.
    """
    lines: List[str] = []
    if payload.get("purpose"):
        lines.append(f"Purpose: {payload['purpose']}")
    if payload.get("input_schema_hint"):
        lines.append(f"Expected input: {payload['input_schema_hint']}")
    if payload.get("output_schema_hint"):
        lines.append(f"Expected output: {payload['output_schema_hint']}")
    avoid = payload.get("avoid_approaches") or []
    if avoid:
        lines.append("Do NOT repeat these approaches that already failed: "
                     + "; ".join(str(a) for a in avoid))
    if payload.get("human_guidance"):
        lines.append(f"The user suggested: {payload['human_guidance']}")
    return "\n".join(lines)


def _build_request_from_handoff(session, handoff) -> Dict[str, Any]:
    """A ``from_wizard_inputs_request``-shaped dict for one dispatched task.

    Carries the project's real example (``sample_input``/``sample_output``)
    so the built node's publish gate is graded against something real (§1.1)
    rather than "the container exited 0". The payload's own sample fields win
    when present; the project criteria are the fallback.
    """
    payload = handoff.payload or {}
    criteria = session.project_criteria or {}

    req: Dict[str, Any] = {
        "node_name": payload.get("node_name") or handoff.task_key or "node",
        "wrap_base_image": _DEFAULT_WRAP_BASE,
        "additional_instructions": _instructions_from_payload(payload),
    }

    sample_in = payload.get("sample_input") or criteria.get("input") or {}
    if isinstance(sample_in, dict) and sample_in.get("body_b64"):
        req["sample_input_filename"] = sample_in.get("filename") or "input"
        req["sample_input_body_b64"] = sample_in["body_b64"]

    sample_out = payload.get("sample_output") or criteria.get("output") or {}
    if isinstance(sample_out, dict) and sample_out.get("body_b64"):
        req["sample_output_filename"] = sample_out.get("filename") or "output"
        req["sample_output_body_b64"] = sample_out["body_b64"]

    return req


# --- the real builder ------------------------------------------------------

def _start_execution(conf: Dict[str, Any], run_id: int) -> None:
    """Dispatch the run to the worker on a daemon thread.

    The orchestrator opens its OWN DB session (``SessionLocal``) — the
    request's session must not be shared across the thread boundary. Mirrors
    the wizard's ``_dispatch_test_run``.
    """
    import threading
    from database import SessionLocal
    from services.workflow_orchestrator import start_workflow_execution

    def _bg() -> None:
        try:
            start_workflow_execution(SessionLocal, conf, run_id)
        except Exception:  # noqa: BLE001
            logger.exception("project build execution failed run=%s", run_id)

    threading.Thread(target=_bg, name=f"project-build-{run_id}",
                     daemon=True).start()


def _default_builder(db, session, handoff):
    """Run a node build for one dispatched task and return its WorkflowRun.

    Reuses the production build path verbatim: adapter → persisted
    ``BuildSpecRecord`` → ``create_build_workflow`` (Workflow +
    WorkflowVersion with the build DAG) → a project-stamped ``WorkflowRun``
    dispatched to the worker. Raising here is fine — ``consume_dispatch``
    treats a build that cannot launch as a failed attempt.
    """
    import crud
    import models
    from node_builder.build_dag.adapter import from_wizard_inputs_request
    from node_builder.build_dag.submit import create_build_workflow
    from services import project_dispatch as pd

    req = _build_request_from_handoff(session, handoff)
    spec = from_wizard_inputs_request(req, tenant_id=handoff.tenant_id)

    rec = models.BuildSpecRecord(tenant_id=handoff.tenant_id,
                                 spec_json=spec.model_dump())
    db.add(rec)
    db.flush()
    spec._build_spec_id = rec.id

    built = create_build_workflow(
        db, spec, user_id=session.user_id, include_generate_phase=True)
    version = (
        db.query(models.WorkflowVersion)
        .filter(models.WorkflowVersion.id == built["workflow_version_id"])
        .first()
    )
    graph = built.get("graph_json") or {}
    conf = {
        "tenant_id": handoff.tenant_id,
        "nodes": graph.get("nodes", []),
        "edges": graph.get("edges", []),
        "project_info": {
            "user_id": session.user_id,
            "workflow_name": built.get("workflow_name"),
            "build_spec_id": rec.id,
        },
    }
    run = crud.create_workflow_run(
        db, version=version, user_id=session.user_id, conf=conf,
        run_name=built.get("workflow_name"), purpose=PROJECT_BUILD_PURPOSE)
    # §6.1a — the run acts under the project's user, and its provenance is the
    # project. Both set here, before the execution thread reads the row.
    pd.stamp_run(run, session)
    db.flush()
    rec.workflow_run_id = run.id

    _start_execution(conf, run.id)
    return run


# --- consuming dispatched handoffs -----------------------------------------

def consume_dispatch(db, session, handoff, *,
                     builder: Optional[Callable[..., Any]] = None,
                     at: Optional[str] = None) -> Dict[str, Any]:
    """Run one dispatched build and record what happened.

    On success: the run is stamped to the project, an agent ``dispatched``
    event is recorded against it, the run is linked to the task's in-flight
    record, and the handoff is consumed.

    On a build that cannot launch: the attempt is settled as failed (which
    advances the ladder), the in-flight stamp is cleared so the next tick
    retries, and the handoff is still consumed — the row has been acted on,
    even though the build did not start.
    """
    from services import project_context as pc
    from services import project_dispatch as pd
    from services import project_escalation as esc
    from services import project_events as pe
    from services.project_runs import project_runs

    builder = builder or _default_builder
    at = at or _now_iso()
    task_key = handoff.task_key
    result: Dict[str, Any] = {"handoff_id": handoff.id, "task_key": task_key}

    try:
        run = builder(db, session, handoff)
    except Exception as exc:  # noqa: BLE001
        logger.exception("project dispatch build failed task=%s: %s",
                         task_key, exc)
        if task_key:
            pc.settle_attempt(session, task_key,
                              reason=f"build could not launch: {exc}", at=at)
            esc.clear_in_flight(session, task_key)
        handoff.status = "consumed"
        runs = project_runs(db, session.id)
        if runs and task_key:
            pe.record(db, workflow_run_id=runs[-1].id, verb=pe.VERB_ESCALATED,
                      level="warn", task_key=task_key,
                      message=f"{task_key}: build could not launch: {exc}")
        db.commit()
        result.update({"status": "build_failed", "error": str(exc)})
        return result

    # Defensive: a builder that neglected to stamp is still made to attribute
    # the run to the project — an unstamped run is invisible to project_runs
    # and every downstream cost and event query keyed on it.
    if getattr(run, "project_session_id", None) != session.id:
        pd.stamp_run(run, session)
        db.flush()

    if task_key:
        esc.link_in_flight_run(session, task_key, run.id)
    handoff.status = "consumed"
    pe.record(
        db, workflow_run_id=run.id, verb=pe.VERB_DISPATCHED, task_key=task_key,
        message=f"{task_key}: build dispatched",
        extra={"handoff_id": handoff.id, "rung": handoff.rung,
               "workflow_run_id": run.id},
    )
    db.commit()
    result.update({"status": "dispatched", "workflow_run_id": run.id})
    return result


def run_pending_dispatches(db, session, *,
                           builder: Optional[Callable[..., Any]] = None,
                           limit: int = 25) -> List[Dict[str, Any]]:
    """Consume every outstanding ``wizard_dispatch`` handoff for a project."""
    import models
    handoffs = (
        db.query(models.ProjectHandoff)
        .filter(
            models.ProjectHandoff.project_session_id == session.id,
            models.ProjectHandoff.kind == "wizard_dispatch",
            models.ProjectHandoff.status == "dispatched",
        )
        .order_by(models.ProjectHandoff.id.asc())
        .limit(limit)
        .all()
    )
    return [consume_dispatch(db, session, h, builder=builder) for h in handoffs]


# --- reconciling finished builds -------------------------------------------

def _record_artifact(session, task_key: str, run_id: int, *,
                     review: Optional[Dict[str, Any]] = None) -> None:
    """Record a completed build as an artifact so the task drops out of the
    ready set. Idempotent — a task is recorded once.

    ``review`` (project-agent §9.2/§9.3, see
    ``project_acceptance.review_intermediate_stages``) is observational only:
    it is stored under the additive ``intermediate_review`` key and never
    changes whether the artifact is recorded.
    """
    artifacts = list(session.project_artifacts or [])
    if any(isinstance(a, dict) and a.get("task_key") == task_key
           for a in artifacts):
        return
    record = {
        "task_key": task_key,
        "workflow_run_id": run_id,
        "recorded_at": _now_iso(),
    }
    if review is not None:
        record["intermediate_review"] = review
    artifacts.append(record)
    session.project_artifacts = artifacts


def reconcile_runs(db, session, *, at: Optional[str] = None) -> List[Dict[str, Any]]:
    """Settle in-flight tasks whose builds have finished.

    A build runs asynchronously, so its outcome lands after the tick that
    dispatched it. This reads each in-flight task's linked run and, on a
    terminal status, records the outcome and clears the in-flight stamp:

      - completed/success → the artifact is recorded (task done) and a
        ``published`` event written;
      - failed/failure → the attempt is settled as failed (the ladder
        advances) and an event written.

    Tasks whose runs are still running are left alone — that is what the
    stall check is for.
    """
    import models
    from services import project_context as pc
    from services import project_escalation as esc
    from services import project_events as pe

    at = at or _now_iso()
    plan = session.project_plan if isinstance(session.project_plan, dict) else {}
    in_flight = dict(plan.get("_in_flight") or {})
    settled: List[Dict[str, Any]] = []

    for task_key, entry in list(in_flight.items()):
        run_id = (entry or {}).get("run_id")
        if not run_id:
            continue
        run = (db.query(models.WorkflowRun)
               .filter(models.WorkflowRun.id == run_id).first())
        if run is None:
            continue
        status = (getattr(run.status, "value", str(run.status))
                  if run.status else "").lower()

        if status in ("completed", "success"):
            review = None
            try:
                from services import project_acceptance as pa

                out = (db.query(models.WorkflowNodeOutput)
                       .filter(models.WorkflowNodeOutput.workflow_run_id == run_id,
                               models.WorkflowNodeOutput.is_terminal.is_(True))
                       .first())
                artefact = ({"kind": "file",
                             "filename": out.logical_name or "",
                             "mime": out.mime_type or "",
                             "size": out.size_bytes,
                             "description": task_key}
                            if out else {"kind": "", "description": task_key})
                review = pa.review_intermediate_stages(
                    session.project_criteria, [(task_key, artefact, None)],
                    db=db)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "project_build: intermediate stage review failed for task %s "
                    "run %s — observational only, the tick continues",
                    task_key, run_id)
                review = None
            _record_artifact(session, task_key, run_id, review=review)
            esc.clear_in_flight(session, task_key)
            pe.record(db, workflow_run_id=run_id, verb=pe.VERB_PUBLISHED,
                      task_key=task_key,
                      message=f"{task_key}: build succeeded")
            settled.append({"task_key": task_key, "status": "succeeded",
                            "run_id": run_id})
        elif status in ("failed", "failure"):
            pc.settle_attempt(session, task_key,
                              reason=(run.error_message or "build run failed"),
                              at=at)
            esc.clear_in_flight(session, task_key)
            pe.record(db, workflow_run_id=run_id, verb=pe.VERB_ESCALATED,
                      level="warn", task_key=task_key,
                      message=f"{task_key}: build failed")
            settled.append({"task_key": task_key, "status": "failed",
                            "run_id": run_id})

    if settled:
        db.commit()
    return settled

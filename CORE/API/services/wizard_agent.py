"""workflow-agentic-execution-loop §wizard-agentic-loop — the agent
loop driver.

The driver function `run_agentic_loop` orchestrates one wizard turn
end-to-end. Per iteration:

  1. Builder composes (or refines) a candidate graph
  2. Test-input selector picks an input
  3. Candidate is dispatched as a sandbox workflow run
       (purpose='wizard_test_run' — isolated MinIO prefix, no
        credit charge — plumbing landed in Section 0)
  4. Driver waits for completion
  5. Output critic judges the result
  6. Either: emit final verdict, OR feed critique into next Builder
     iteration

Caps:
  - WIZARD_AGENT_MAX_ITERATIONS = 3 (hard limit; per-tenant override
    via tenants.wizard_agent_max_iterations once that column lands —
    currently hard-coded)
  - dispatch_timeout_seconds = 120 (poll budget per iteration)

Yielded SSE events extend the existing wizard vocabulary:

  - 'agent_started'          — loop entry
  - 'test_input_selected'    — descriptor for this iteration's input
  - 'test_run_dispatched'    — sandbox WorkflowRun.id created
  - 'test_run_succeeded'     — run completed
  - 'test_run_failed'        — run failed (terminal worker error)
  - 'output_critic_started'  — critic is running
  - 'output_critic_verdict'  — critic produced a result
  - 'iteration_continuing'   — feeding critique back into Builder
  - 'agent_finished'         — loop exited; followed by 'final'

Heavy dependency injection — the driver takes a `Hooks` bundle so
tests stub each step (build/select/dispatch/wait/critique/persist).
Production wires the hooks to:

  - Hooks.build_proposal     → existing Builder LLM call
  - Hooks.select_test_input  → services.wizard_test_input.select_test_input
  - Hooks.dispatch_test_run  → services.workflow_orchestrator path that
                               creates a WorkflowRun with purpose=
                               'wizard_test_run'
  - Hooks.wait_for_completion→ poll WorkflowRun.status
  - Hooks.load_output        → MinIO download of terminal output
  - Hooks.critique           → services.wizard_output_critic.critique_output
  - Hooks.persist_iteration  → INSERT INTO wizard_test_runs

State machine verdict flow:

      Builder turn N
            │
            ▼
   [select test input] ──── None ──── ▶ EXHAUSTED_NO_INPUT
            │                                  │
            ▼                                  ▼
   [dispatch + wait] ──── FAILED ───── ▶ next iteration with
            │                            critique="run failed"
            ▼
   [critique]
   ├─ SATISFIES → return SUCCESS
   ├─ NEEDS_FIX → if iter<MAX: next iteration with failure_mode
   │              else: return BEST_EFFORT with last candidate
   └─ NEEDS_USER_CLARIFICATION → return USER_CLARIFICATION
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


WIZARD_AGENT_MAX_ITERATIONS = 3

# Terminal driver states.
RESULT_SUCCESS = "SUCCESS"
RESULT_BEST_EFFORT = "BEST_EFFORT"
RESULT_USER_CLARIFICATION = "USER_CLARIFICATION"
RESULT_NO_INPUT = "NO_INPUT"
# mesh-native-llm-routing §4.7 — the operator's CHosen wizard model is
# unreachable. The build fails loudly BY NAME before test-input selection;
# no other model is ever substituted.
RESULT_LLM_UNREACHABLE = "LLM_UNREACHABLE"
# GSEC FX.16 R-022 — the loop built + validated a candidate graph but is
# GATED behind an explicit human confirmation before it may execute it on a
# worker. `final_candidate` carries the proposed graph summary so the caller
# can surface a confirm affordance; execution happens only when the caller
# re-enters the loop with `confirmed=True`.
RESULT_NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"


@dataclass
class AgentHooks:
    """Dependency-inject every step. Tests stub each; production
    wires the live implementations."""
    # (prompt, prior_critique, user_goal, iteration_n) -> dict candidate_graph
    build_proposal: Callable[..., Dict[str, Any]]
    # (db, tenant_id, candidate_graph, wizard_session_id, iteration_n)
    #     -> Optional[TestInputDescriptor]
    select_test_input: Callable[..., Any]
    # (db, tenant_id, candidate_graph, test_input_descriptor,
    #  wizard_session_id, iteration_n) -> int workflow_run_id
    dispatch_test_run: Callable[..., int]
    # (db, workflow_run_id, timeout_seconds) -> dict {status, output_pointer}
    wait_for_completion: Callable[..., Dict[str, Any]]
    # (db, output_pointer) -> Optional[bytes]
    load_output: Optional[Callable[..., Optional[bytes]]]
    # (output_pointer, output_bytes, candidate_graph, user_goal,
    #  llm_pin) -> CriticResult
    critique: Callable[..., Any]
    # (db, wizard_session_id, iteration_n, candidate_graph,
    #  test_input_descriptor, workflow_run_id, critic_verdict,
    #  critic_reason) -> Optional[int] row id
    persist_iteration: Callable[..., Optional[int]]
    # mesh-native-llm-routing §2 — optional STATIC graph critic. When set, the
    # loop audits the candidate GRAPH itself (real catalog nodes? wiring
    # accomplishes the goal?) instead of dispatching a real sandbox execution,
    # so the builder<->critic loop is fast and model-agnostic (no execution,
    # works with whatever model the operator bound). Real execution stays an
    # explicit "test this workflow" action.
    # (candidate_graph, user_goal, llm_pin) -> CriticResult-like
    graph_critique: Optional[Callable[..., Any]] = None
    # mesh-native-llm-routing §4.7 — one-shot reachability probe of the
    # chosen wizard model's endpoint, run BEFORE the first builder turn
    # (hence before any test-input selection).
    # (llm_pin) -> Optional[str]: None when reachable / nothing to probe;
    # a user-facing message naming the model + endpoint host otherwise.
    # Must never raise (defensive: a probe bug must not break the wizard).
    probe_llm: Optional[Callable[..., Optional[str]]] = None
    # GSEC FX.16 R-022 — optional audit sink for the confirmation gate and
    # the confirmed execution. Called with (db, tenant_id, event_name,
    # details_dict) where details is METADATA ONLY (node/edge names +
    # counts, never input payloads or secrets). Must never raise; the loop
    # swallows any exception. None → no audit emit (unit tests).
    # (db, tenant_id, event_name, details) -> None
    record_audit_event: Optional[Callable[..., None]] = None


@dataclass
class AgentResult:
    result: str  # one of RESULT_* constants
    final_candidate: Optional[Dict[str, Any]] = None
    user_question: Optional[str] = None
    failure_mode: Optional[str] = None
    iteration_count: int = 0
    test_run_ids: List[int] = field(default_factory=list)
    best_effort: bool = False
    # GSEC FX.16 R-022 — True when the loop stopped at the
    # NEEDS_CONFIRMATION gate: the candidate is ready to execute but the
    # caller must obtain an explicit human confirmation first.
    needs_confirmation: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "result": self.result,
            "final_candidate": self.final_candidate,
            "user_question": self.user_question,
            "failure_mode": self.failure_mode,
            "iteration_count": self.iteration_count,
            "test_run_ids": self.test_run_ids,
            "best_effort": self.best_effort,
            "needs_confirmation": self.needs_confirmation,
        }


def _yield(events: List[Dict[str, Any]], event: Dict[str, Any]) -> None:
    """Helper: append to the event list so the driver can return both
    the result + the event stream (callers iterate `events` to drive
    the SSE channel)."""
    events.append(event)


def _emit_terminal_metrics(
    result: str, iteration_count: int, last_verdict: Optional[str],
) -> None:
    """Section F §10 — emit Prometheus metrics at agent_finished.
    Records: agent result counter, iteration-count histogram, and
    (when present) the final critic verdict.

    All emits route through services.wizard_metrics which no-ops
    when prometheus_client isn't installed."""
    try:
        from services import wizard_metrics
        wizard_metrics.record_agent_result(result)
        wizard_metrics.record_iteration_count(iteration_count)
        if last_verdict:
            wizard_metrics.record_critic_verdict(last_verdict)
    except Exception:
        pass


def _graph_summary(candidate: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """GSEC FX.16 R-022 — metadata-only digest of a candidate graph.

    Node/edge NAMES and counts only — never data payloads, parameter
    values or secrets — so it is safe to carry in an SSE event or an
    AuditEvent.details column."""
    candidate = candidate or {}
    nodes = candidate.get("nodes") or []
    edges = candidate.get("edges") or []
    node_names: List[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        data = n.get("data") or {}
        node_names.append(str(data.get("nodeDefinitionName") or
                             data.get("label") or n.get("type") or "?"))
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "node_names": node_names[:50],
        "workflow_name": candidate.get("name"),
    }


def _safe_record_audit(
    hooks: AgentHooks, db, tenant_id: int,
    event_name: str, details: Dict[str, Any],
) -> None:
    """Best-effort audit emit for the confirmation gate. A missing hook
    or any exception is swallowed — audit must never break the loop."""
    if hooks.record_audit_event is None:
        return
    try:
        hooks.record_audit_event(db, tenant_id, event_name, details)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent_loop: record_audit_event failed: %s", e)


def run_agentic_loop(
    *,
    db,
    tenant_id: int,
    user_goal: str,
    wizard_session_id: str,
    hooks: AgentHooks,
    max_iterations: int = WIZARD_AGENT_MAX_ITERATIONS,
    dispatch_timeout_seconds: int = 120,
    llm_pin: Optional[str] = None,
    confirmed: bool = False,
    require_human_confirm: bool = False,
    user_id: Optional[int] = None,
) -> tuple[AgentResult, List[Dict[str, Any]]]:
    """Public entry point — the driver wrapped in the background-tenant context.

    mega-qa-hardening Billing #4 — the loop's hooks make the LLM calls
    (builder, graph critic, output critic). When the loop is driven OFF a
    request thread — a wizard turn dispatched from a scheduler job, a
    background thread, a worker task — there is no request principal on that
    thread, so `utils.llm_client` has no tenant to hand `record_call` and the
    row would be dropped: the per-tenant token data this table exists to
    produce (and, once a price exists, the charging escape it gates) would be
    lost. The caller knows the tenant from the job/session row it loaded, so
    this entry point establishes it for the whole turn with
    `services.llm_metering.llm_tenant_context`, exactly as
    `services/build_farm.py:134` wraps its LLM phase.

    Safe inside a request too: `record_call` prefers an explicit tenant
    argument, and an in-request call always has one (the request principal,
    resolved by `utils.llm_client`), so this block can never steal a request's
    attribution. The contextvar is scoped to the thread/job — a ContextVar set
    on the caller's thread does not cross into a `threading.Thread` target, so
    each background job re-establishes its own here and the `finally: reset`
    inside the context manager guarantees no leak into the next job.

    `user_id` is optional: pass it when the job row carries the initiating
    user so the usage row is attributed to a person, not just a tenant.

    1647c1 CE: services.llm_metering is paid-only, so on CE there is no
    background-tenant context to establish — the loop runs unmetered (nothing
    records the LLM rows, nothing charges). Skip with one log.
    """
    from utils.paid_import import optional_paid

    _lm = optional_paid("services.llm_metering")
    if _lm is None:
        logger.info("agent_loop: llm_metering absent in this edition — running unmetered")
        return _drive_agentic_loop(
            db=db, tenant_id=tenant_id, user_goal=user_goal,
            wizard_session_id=wizard_session_id, hooks=hooks,
            max_iterations=max_iterations,
            dispatch_timeout_seconds=dispatch_timeout_seconds,
            llm_pin=llm_pin, confirmed=confirmed,
            require_human_confirm=require_human_confirm,
        )
    with _lm.llm_tenant_context(tenant_id, user_id=user_id):
        return _drive_agentic_loop(
            db=db, tenant_id=tenant_id, user_goal=user_goal,
            wizard_session_id=wizard_session_id, hooks=hooks,
            max_iterations=max_iterations,
            dispatch_timeout_seconds=dispatch_timeout_seconds,
            llm_pin=llm_pin, confirmed=confirmed,
            require_human_confirm=require_human_confirm,
        )


def _drive_agentic_loop(
    *,
    db,
    tenant_id: int,
    user_goal: str,
    wizard_session_id: str,
    hooks: AgentHooks,
    max_iterations: int = WIZARD_AGENT_MAX_ITERATIONS,
    dispatch_timeout_seconds: int = 120,
    llm_pin: Optional[str] = None,
    confirmed: bool = False,
    require_human_confirm: bool = False,
) -> tuple[AgentResult, List[Dict[str, Any]]]:
    """Drive one wizard turn end-to-end. Returns (result, events).

    `events` is the SSE-shaped list the pipeline forwards to the
    client. Each dict has a `type` key + payload.

    Tests construct an `AgentHooks` with fakes for every step. The
    state machine in this function should be the same in tests and
    production — the difference is purely the hook implementations.
    """
    events: List[Dict[str, Any]] = []
    _yield(events, {"type": "agent_started",
                    "wizard_session_id": wizard_session_id,
                    "max_iterations": max_iterations})

    # ---------- 0. LLM reachability probe (mesh-native-llm-routing §4.7) ----------
    # The chosen model must be reachable BEFORE the build starts. An
    # unreachable model fails loudly BY NAME here — before the builder
    # runs and long before test-input selection — so the user is never
    # left with a misleading "no matching file" dead-end, and no other
    # model silently answers in its place.
    if hooks.probe_llm is not None:
        try:
            unreachable = hooks.probe_llm(llm_pin=llm_pin)
        except Exception as e:  # noqa: BLE001
            logger.warning("agent_loop: probe_llm raised: %s", e)
            unreachable = None
        if unreachable:
            _emit_terminal_metrics(RESULT_LLM_UNREACHABLE, 0, None)
            _yield(events, {"type": "agent_finished",
                            "result": RESULT_LLM_UNREACHABLE,
                            "reason": unreachable})
            return (AgentResult(
                result=RESULT_LLM_UNREACHABLE,
                user_question=unreachable,
                failure_mode=unreachable,
                iteration_count=0,
                test_run_ids=[],
            ), events)

    last_candidate: Optional[Dict[str, Any]] = None
    last_critique: Optional[str] = None
    test_run_ids: List[int] = []

    for iteration in range(max_iterations):
        # ---------- 1. Builder ----------
        try:
            candidate = hooks.build_proposal(
                user_goal=user_goal,
                prior_critique=last_critique,
                iteration_n=iteration,
            )
        except Exception as e:
            logger.warning("agent_loop: build_proposal raised: %s", e)
            _yield(events, {"type": "builder_failed",
                            "iteration": iteration, "reason": str(e)})
            # No candidate to test — return whatever we had previously.
            res = RESULT_BEST_EFFORT if last_candidate else RESULT_NO_INPUT
            _emit_terminal_metrics(res, iteration, None)
            return (AgentResult(
                result=res,
                final_candidate=last_candidate,
                failure_mode=f"builder failed at iteration {iteration}: {e}",
                iteration_count=iteration,
                test_run_ids=test_run_ids,
                best_effort=bool(last_candidate),
            ), events)
        last_candidate = candidate

        # ---------- Static graph critique (no-execution, model-agnostic) ----------
        # When a graph_critique hook is provided, audit the GRAPH itself rather
        # than running it on a worker. Keeps the builder<->critic loop fast and
        # independent of which model the operator bound. Real execution is an
        # explicit user action, not a tax on every interactive reply.
        if hooks.graph_critique is not None:
            try:
                gc = hooks.graph_critique(
                    candidate_graph=candidate, user_goal=user_goal,
                    llm_pin=llm_pin,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("agent_loop: graph_critique raised: %s", e)
                gc = None
            if gc is None:
                verdict = "NEEDS_FIX"
                failure_mode = "graph critique unavailable"
                user_question = None
            else:
                verdict = getattr(gc, "verdict", "NEEDS_FIX")
                failure_mode = getattr(gc, "failure_mode", None)
                user_question = getattr(gc, "user_question", None)
            _yield(events, {"type": "graph_critic_verdict",
                            "iteration": iteration, "verdict": verdict,
                            "failure_mode": failure_mode,
                            "user_question": user_question})
            _safe_persist(
                hooks, db, wizard_session_id, iteration,
                candidate, None, None, verdict,
                failure_mode or user_question,
            )
            if verdict == "SATISFIES_GOAL":
                _emit_terminal_metrics(RESULT_SUCCESS, iteration + 1, verdict)
                _yield(events, {"type": "agent_finished",
                                "result": RESULT_SUCCESS,
                                "iteration_count": iteration + 1})
                return (AgentResult(
                    result=RESULT_SUCCESS, final_candidate=candidate,
                    iteration_count=iteration + 1,
                    test_run_ids=test_run_ids), events)
            if verdict == "NEEDS_USER_CLARIFICATION":
                _emit_terminal_metrics(
                    RESULT_USER_CLARIFICATION, iteration + 1, verdict)
                _yield(events, {"type": "agent_finished",
                                "result": RESULT_USER_CLARIFICATION,
                                "iteration_count": iteration + 1})
                return (AgentResult(
                    result=RESULT_USER_CLARIFICATION, final_candidate=candidate,
                    user_question=user_question or (
                        "The wizard needs a bit more detail to build this. "
                        "Could you clarify the goal?"),
                    iteration_count=iteration + 1,
                    test_run_ids=test_run_ids), events)
            # NEEDS_FIX → feed the critique into the next builder turn.
            last_critique = failure_mode or "the graph didn't satisfy the goal"
            if iteration + 1 < max_iterations:
                _yield(events, {"type": "iteration_continuing",
                                "iteration": iteration,
                                "critique": last_critique})
            continue  # skip the real-execution path below

        # ---------- 2. Test-input selection ----------
        try:
            descriptor = hooks.select_test_input(
                db=db, tenant_id=tenant_id,
                candidate_graph=candidate,
                wizard_session_id=wizard_session_id,
                iteration_n=iteration,
            )
        except Exception as e:
            logger.warning("agent_loop: select_test_input raised: %s", e)
            descriptor = None
        if descriptor is None:
            _yield(events, {"type": "test_input_unavailable",
                            "iteration": iteration})
            # Persist a partial iteration row so the lineage is intact.
            _safe_persist(
                hooks, db, wizard_session_id, iteration,
                candidate, None, None,
                "NEEDS_USER_CLARIFICATION",
                "no test input available for the candidate's input type",
            )
            _emit_terminal_metrics(RESULT_NO_INPUT, iteration + 1, None)
            _yield(events, {"type": "agent_finished",
                            "result": RESULT_NO_INPUT})
            return (AgentResult(
                result=RESULT_NO_INPUT,
                final_candidate=candidate,
                user_question=(
                    "I need a sample input to test this workflow but couldn't "
                    "find a matching file in your library. Upload one or tell "
                    "me what input type to expect."
                ),
                iteration_count=iteration + 1,
                test_run_ids=test_run_ids,
            ), events)
        _yield(events, {"type": "test_input_selected",
                        "iteration": iteration,
                        "tier": descriptor.tier,
                        "input_type": descriptor.input_type})

        # ---------- 2b. Human-confirmation gate (GSEC FX.16 R-022) ----------
        # The candidate graph is LLM-generated and was steered by untrusted
        # repo docs / web content. Executing it dispatches real work to a
        # worker, so when the operator enabled the gate and this turn did
        # NOT carry an explicit user confirmation, the loop STOPS here —
        # before any dispatch — and returns NEEDS_CONFIRMATION with the
        # proposed graph summary attached. Generation (step 1), static
        # graph validation and test-input selection ran unattended as
        # always; only EXECUTION is gated. The caller surfaces the
        # summary, the user confirms, and the caller re-enters the loop
        # with confirmed=True (which may then dispatch).
        if require_human_confirm and not confirmed:
            _safe_record_audit(
                hooks, db, tenant_id, "WIZARD_AGENT_EXEC_CONFIRM_REQUIRED",
                {"wizard_session_id": wizard_session_id,
                 "iteration": iteration,
                 "test_input_tier": getattr(descriptor, "tier", None),
                 "graph": _graph_summary(candidate)},
            )
            _yield(events, {"type": "needs_confirmation",
                            "iteration": iteration,
                            "reason": (
                                "executing an LLM-generated workflow requires "
                                "explicit user confirmation"
                            ),
                            "graph_summary": _graph_summary(candidate)})
            res = RESULT_NEEDS_CONFIRMATION
            _emit_terminal_metrics(res, iteration + 1, None)
            _yield(events, {"type": "agent_finished",
                            "result": res,
                            "needs_confirmation": True})
            return (AgentResult(
                result=res,
                final_candidate=candidate,
                needs_confirmation=True,
                iteration_count=iteration + 1,
                test_run_ids=test_run_ids,
            ), events)

        # ---------- 3. Dispatch sandbox test run ----------
        _safe_record_audit(
            hooks, db, tenant_id, "WIZARD_AGENT_TEST_RUN_EXECUTED",
            {"wizard_session_id": wizard_session_id,
             "iteration": iteration,
             "user_confirmed": bool(confirmed),
             "gate_enabled": bool(require_human_confirm),
             "graph": _graph_summary(candidate)},
        )
        try:
            workflow_run_id = hooks.dispatch_test_run(
                db=db, tenant_id=tenant_id,
                candidate_graph=candidate,
                test_input_descriptor=descriptor,
                wizard_session_id=wizard_session_id,
                iteration_n=iteration,
            )
        except Exception as e:
            logger.warning("agent_loop: dispatch_test_run raised: %s", e)
            _yield(events, {"type": "test_run_failed",
                            "iteration": iteration,
                            "reason": f"dispatch failed: {e}"})
            last_critique = (
                f"the test workflow run failed to dispatch ({e}); the "
                f"graph may reference nodes that aren't available."
            )
            _safe_persist(
                hooks, db, wizard_session_id, iteration,
                candidate, descriptor, None,
                "NEEDS_FIX", last_critique,
            )
            continue  # try next iteration

        test_run_ids.append(workflow_run_id)
        _yield(events, {"type": "test_run_dispatched",
                        "iteration": iteration,
                        "workflow_run_id": workflow_run_id})

        import time as _time
        _dispatch_started_at = _time.time()

        # ---------- 4. Wait for completion ----------
        try:
            completion = hooks.wait_for_completion(
                db=db, workflow_run_id=workflow_run_id,
                timeout_seconds=dispatch_timeout_seconds,
            )
        except Exception as e:
            logger.warning("agent_loop: wait_for_completion raised: %s", e)
            completion = {"status": "ERROR", "output_pointer": None,
                          "error": str(e)}

        status = (completion.get("status") or "").upper()
        output_pointer = completion.get("output_pointer")
        if status in ("FAILED", "ERROR", "TIMEOUT"):
            reason = completion.get("error") or f"run ended with status {status}"
            _yield(events, {"type": "test_run_failed",
                            "iteration": iteration, "reason": reason})
            last_critique = (
                f"the test run terminated with status={status}; "
                f"reason: {reason}. The graph may not be runnable on "
                f"this input type."
            )
            _safe_persist(
                hooks, db, wizard_session_id, iteration,
                candidate, descriptor, workflow_run_id,
                "NEEDS_FIX", last_critique,
            )
            continue

        _yield(events, {"type": "test_run_succeeded",
                        "iteration": iteration,
                        "output_pointer": output_pointer})
        try:
            from services import wizard_metrics
            wizard_metrics.record_test_run_duration(
                _time.time() - _dispatch_started_at,
            )
        except Exception:
            pass

        # ---------- 5. Critic ----------
        _yield(events, {"type": "output_critic_started",
                        "iteration": iteration})
        output_bytes: Optional[bytes] = None
        if output_pointer and hooks.load_output is not None:
            try:
                output_bytes = hooks.load_output(db=db, pointer=output_pointer)
            except Exception as e:
                logger.warning("agent_loop: load_output raised: %s", e)
                output_bytes = None
        try:
            critic = hooks.critique(
                output_pointer=output_pointer,
                output_bytes=output_bytes,
                candidate_graph=candidate,
                user_goal=user_goal,
                llm_pin=llm_pin,
            )
        except Exception as e:
            logger.warning("agent_loop: critique raised: %s", e)
            critic = None

        if critic is None:
            verdict = "NEEDS_FIX"
            failure_mode = "critic call failed; treating as NEEDS_FIX"
            user_question = None
        else:
            verdict = getattr(critic, "verdict", "NEEDS_FIX")
            failure_mode = getattr(critic, "failure_mode", None)
            user_question = getattr(critic, "user_question", None)

        _yield(events, {"type": "output_critic_verdict",
                        "iteration": iteration,
                        "verdict": verdict,
                        "failure_mode": failure_mode,
                        "user_question": user_question})
        try:
            from services import wizard_metrics
            wizard_metrics.record_critic_verdict(verdict)
        except Exception:
            pass

        _safe_persist(
            hooks, db, wizard_session_id, iteration,
            candidate, descriptor, workflow_run_id,
            verdict, failure_mode or user_question,
        )

        if verdict == "SATISFIES_GOAL":
            _emit_terminal_metrics(RESULT_SUCCESS, iteration + 1, verdict)
            _yield(events, {"type": "agent_finished",
                            "result": RESULT_SUCCESS,
                            "iteration_count": iteration + 1})
            return (AgentResult(
                result=RESULT_SUCCESS,
                final_candidate=candidate,
                iteration_count=iteration + 1,
                test_run_ids=test_run_ids,
            ), events)

        if verdict == "NEEDS_USER_CLARIFICATION":
            _emit_terminal_metrics(RESULT_USER_CLARIFICATION, iteration + 1, verdict)
            _yield(events, {"type": "agent_finished",
                            "result": RESULT_USER_CLARIFICATION,
                            "iteration_count": iteration + 1})
            return (AgentResult(
                result=RESULT_USER_CLARIFICATION,
                final_candidate=candidate,
                user_question=user_question or (
                    "The wizard needs more information to confidently "
                    "produce this workflow. Could you clarify the goal?"
                ),
                iteration_count=iteration + 1,
                test_run_ids=test_run_ids,
            ), events)

        # NEEDS_FIX → next iteration. Feed failure_mode in.
        last_critique = failure_mode or "the output didn't satisfy the goal"
        if iteration + 1 < max_iterations:
            _yield(events, {"type": "iteration_continuing",
                            "iteration": iteration,
                            "critique": last_critique})

    # Loop exhausted without SATISFIES — surface best-effort.
    _emit_terminal_metrics(RESULT_BEST_EFFORT, max_iterations, None)
    _yield(events, {"type": "agent_finished",
                    "result": RESULT_BEST_EFFORT,
                    "iteration_count": max_iterations,
                    "best_effort": True})
    return (AgentResult(
        result=RESULT_BEST_EFFORT,
        final_candidate=last_candidate,
        failure_mode=last_critique,
        iteration_count=max_iterations,
        test_run_ids=test_run_ids,
        best_effort=True,
    ), events)


def _safe_persist(
    hooks: AgentHooks, db, wizard_session_id: str, iteration_n: int,
    candidate: Optional[Dict[str, Any]],
    descriptor: Optional[Any],
    workflow_run_id: Optional[int],
    verdict: Optional[str],
    reason: Optional[str],
) -> None:
    """Best-effort wrapper around hooks.persist_iteration so a
    persistence failure never breaks the loop."""
    try:
        hooks.persist_iteration(
            db=db,
            wizard_session_id=wizard_session_id,
            iteration_n=iteration_n,
            candidate_graph=candidate or {},
            test_input_descriptor=(
                descriptor.as_dict() if descriptor is not None and hasattr(descriptor, "as_dict")
                else descriptor
            ),
            workflow_run_id=workflow_run_id,
            critic_verdict=verdict,
            critic_reason=reason,
        )
    except Exception as e:
        logger.warning("agent_loop: persist_iteration failed (%s)", e)

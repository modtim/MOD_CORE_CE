"""agent_node_runtime — FX 4.1/4.3 (slice): the agent-node runtime adapter.

Turns ONE graph `agent` node into a bounded LLM+tool dispatch and returns the
node-output envelope the orchestrator stores. Composes the committed pieces,
it does not reimplement them:

  * services/agent_tool_schemas.derive_tool_schemas — provider-neutral schemas
    for the node's declared tool refs; their NAMES are the dispatch allow-list;
  * services/agent_node_runner.effective_max_tool_calls — the `max_tool_calls`
    cap (settings default + ceiling); run_agent_loop owns the ToolCallBudget,
    so the cap is passed through, never counted twice here;
  * services/agent_node_loop.run_agent_loop — the pure loop driver, fed here
    with an `llm_step` wrapped around the platform LLM client and a
    `dispatch_tool` wrapped around the sync sub-workflow dispatcher.

LLM client reused: `utils.llm_client.call_wizard_llm` — the central LLM choke
point every other service reaches (services/mesh_llm_service.complete:61,
services/wizard_agent.py's hooks, services/project_*.py). Because an agent
node runs OFF a request thread, the tenant is re-established for the whole
dispatch with services.llm_metering.llm_tenant_context, exactly as
services/wizard_agent.py:271 does for a background wizard turn.

ORCHESTRATOR CALL SITE (the wiring task, FX 4.1): in
services/workflow_orchestrator.py `_start_workflow`, where the node's
`node_data.get("node_kind")` (line ~1550) selects the handler, add an
`agent` branch that calls
``services.agent_node_runtime.run_agent_node(db, tenant_id=tenant_id,
workflow_run_id=workflow_run_id, node_id=node.get("id"),
node_config=node_data, agent_input=<collected node inputs>,
graph_json=<the run's graph_json>)`` and writes the returned envelope into
the node's outputs — status "completed" -> node success with
outputs["final"]; "failed"/"refused" -> node failure carrying
failure_reason. Nothing here imports the orchestrator, so no cycle.

Tool refs supported synchronously: `sub_workflow` refs whose resolution is
already supported by services/sub_workflow_resolve.py. Resolution is only
half of running one — actually executing a sub-workflow (and any `node` /
`node_definition` tool) needs an async worker run, which is a LATER task
(async parking). Such a dispatch therefore records the documented
{"status": "error", "error": "async_tool_not_supported_yet"} tool entry and
lets the agent continue until it answers or the cap fires.

Pre-dispatch validation: `_pre_dispatch_validate` checks port-type compliance
(port_type_enforcement) and output-cache short-circuits (node_output_cache)
before the stub error is returned.  Never raises; never commits; logs class
names only (no prompts, no outputs).
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import logging
from contextlib import nullcontext
from typing import Any, Dict, List, Optional, Tuple

# Resolution of sub_workflow refs is done upstream (services/agent_tool_schemas
# resolves them through services/sub_workflow_resolve.py); this module only
# needs the loop driver, the cap primitive, the schema deriver and the flag.
import crud.workflows_domains_shared as wd_shared
from services import agent_node_loop as loop
from services import agent_node_runner as runner
from services import agent_tool_schemas as ats
from services import workflow_schema_export as wse
try:
    from services import expr_feature_flags as ef
except ImportError:
    ef = None
try:
    from services import llm_metering
except ImportError:
    llm_metering = None
from services import node_output_cache
from product_names import themed_logger
from services import port_type_enforcement
try:
    from services import run_purposes
except (ImportError, ModuleNotFoundError):
    run_purposes = None
from services import wizard_feedback
from utils.llm_client import call_wizard_llm, LLMClientError, _get_wizard_config

# 1201 — the platform-wide node-run content cap (platform setting
# `node_run.content_max_chars`). Agent nodes write NodeRun.outputs directly
# (park stamps below) and used to bypass the clamp that worker_tasks.py applies
# on the normal completion path; echoed conversation history made that grow
# unbounded in a column run-detail serves verbatim.
from crud.node_run_cud import clamp_node_run_text

logger = themed_logger(__name__, 'agents')

ASYNC_TOOL_ERROR = "async_tool_not_supported_yet"
REFUSAL_PHASE3_OFF = "phase3_agent_nodes_disabled"
REFUSAL_UNRESOLVED_TOOLS = "agent_unresolved_tools"
RUNTIME_FAILURE = "agent_runtime_error"
TOOL_PARK_FAILED = "tool_park_failed"

# EXPR 4.4 slice A — the parked-agent contract slice B
# (services/agent_tool_resume.py) reads. Defined LOCALLY, never imported from
# the resume module: the resume module imports THIS one, so importing back is
# the FX.106 p3a circular shape.
AGENT_STATE_KEY = "agent_state"
RESUMED_HISTORY_KEY = "resumed_history"

_STEP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["final", "tool_call"]},
        "output": {"type": "object"},
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
    },
    "required": ["type"],
}


def _has_attempted_tool_call(history: List[Dict[str, Any]]) -> bool:
    """True once this dispatch has tried at least one `tool_call` turn.

    Bug 1372: forcing the envelope to `tool_call` (see
    `_forced_tool_call_schema`) only applies to the first turn of a dispatch
    that offers tools — once the model has made an attempt (even one that
    named an unknown tool or failed), later turns go back to the normal
    final|tool_call choice so it can report the result or call again.
    """
    return any(
        isinstance(entry, dict) and entry.get("role") == "assistant"
        and "tool_call" in entry
        for entry in history
    )


def _forced_tool_call_schema(tool_names: List[str]) -> Dict[str, Any]:
    """`_STEP_SCHEMA` variant that only accepts the `tool_call` shape.

    Bug 1372: tool_schemas are offered to the model via a prompt-embedded
    JSON block, not the backend's native `tools`/`tool_choice` field (see
    `_platform_llm_step`), and that text-only hint wasn't enough signal —
    both a Strata-served and a vLLM-served Qwen3.8 route recited the tool
    description back as a `type:"final"` prose answer instead of calling it,
    even though each honours raw tool_calls / guided-JSON gates when invoked
    directly. Guided-JSON decoding makes this schema's single-value `type`
    enum a hard constraint on the backend side (not just a prompt hint), so
    a route that actually supports structured output physically cannot
    emit `final` prose for the forced first turn.
    """
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["tool_call"]},
            "tool": {"type": "string"},
            "arguments": {"type": "object"},
        },
        "required": ["type", "tool", "arguments"],
    }
    if tool_names:
        schema["properties"]["tool"] = {"type": "string", "enum": tool_names}
    return schema


def _normalize_step(raw: Any) -> Dict[str, Any]:
    """Coerce whatever the LLM returned into run_agent_loop's contract.

    Accepts the contract dict itself, an OpenAI-style message (``tool_calls``
    with a ``function`` sub-object, ``arguments`` as a JSON string), or a bare
    string answer. Anything unrecognisable is returned untouched so the loop
    files it as agent_llm_error rather than us inventing a step.
    """
    if not isinstance(raw, dict):
        return {"type": "final", "output": raw} if isinstance(raw, str) else raw  # type: ignore
    kind = raw.get("type")
    if kind in ("final", "tool_call"):
        return raw
    calls = raw.get("tool_calls")
    if isinstance(calls, list) and calls and isinstance(calls[0], dict):
        fn = calls[0].get("function")
        fn = fn if isinstance(fn, dict) else calls[0]
        name = fn.get("name") or calls[0].get("name")
        args: Any = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        if isinstance(name, str) and name:
            return {"type": "tool_call", "tool": name, "arguments": args}
    if "content" in raw:
        return {"type": "final", "output": raw.get("content")}
    return raw


_PROMPT_ECHO_COMPARE_CAP = 4000  # chars — difflib.SequenceMatcher is worst-case
# O(n*m); both system_prompt (tenant-authored) and output (LLM-authored) are
# unbounded, so never hand it the full strings. A true echo matches in the
# first N chars same as it would over the whole string, so capping costs
# nothing in detection power.


def _looks_like_prompt_echo(output: Any, system_prompt: str) -> bool:
    """Bug 1147: the agent-node LLM is asked for tool calls via a
    prompt-embedded tool schema + guided-JSON contract (``_STEP_SCHEMA``),
    NOT the backend's native ``tools``/``tool_choice`` field — see
    ``run_2323`` repro, where the ollama-served model had no tool-call
    template support (``api/show`` → no ``capabilities``, template has no
    tool block) and simply echoed its own system prompt back as a "final"
    answer instead of attempting the one declared tool.

    This heuristic catches that failure mode generically: a `final` step
    whose output text is (near-)identical to the system prompt it was given,
    while tools were actually on offer, is never a real answer.
    """
    if not isinstance(output, str) or not isinstance(system_prompt, str):
        return False
    a, b = output.strip(), system_prompt.strip()
    if not a or not b:
        return False
    if a == b:
        return True
    a = a[:_PROMPT_ECHO_COMPARE_CAP]
    b = b[:_PROMPT_ECHO_COMPARE_CAP]
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.9


def _loop_ref(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Bridge the two tool-ref vocabularies.

    services/agent_tool_schemas resolves refs shaped
    ``{kind: node, node_id}`` / ``{kind: sub_workflow, workflow_ref}`` /
    ``{kind: node_definition, name}``, while services/agent_node_loop's
    validator requires ``{kind, id}``. Emit BOTH: the original keys (so a
    dispatcher can use them) plus the ``id`` the loop insists on.
    """
    out = dict(ref)
    kind = out.get("kind")
    if out.get("id") is None:
        if kind == "node":
            out["id"] = out.get("node_id")
        elif kind == "sub_workflow":
            wr = out.get("workflow_ref")
            out["id"] = wr.get("workflow_id") if isinstance(wr, dict) else None
        elif kind == "node_definition":
            out["id"] = out.get("name")
    if out.get("id") is None:
        out["id"] = out.get("name") or out.get("node_id") or kind
    return out


def _normalize_outcome(raw: Any) -> Dict[str, Any]:
    """Coerce a dispatcher result into run_agent_loop's {ok, output, error}."""
    if not isinstance(raw, dict):
        return {"ok": False, "output": None,
                "error": "tool_dispatcher returned a non-dict result"}
    if "ok" in raw:
        if raw.get("parked") is True:
            return {"ok": True, "parked": True, "output": None, "error": None}
        return {"ok": bool(raw.get("ok")), "output": raw.get("output"),
                "error": raw.get("error")}
    status = raw.get("status")
    if status == "parked":
        return {"ok": True, "parked": True, "output": None, "error": None}
    if status == "error":
        extras = {k: v for k, v in raw.items()
                  if k not in ("status", "error", "output")}
        return {"ok": False,
                "output": raw.get("output") or (extras or None),
                "error": raw.get("error") or "tool failed"}
    return {"ok": True, "output": raw.get("output", raw), "error": None}


def _pre_dispatch_validate(
    db: Any,
    *,
    tenant_id: int,
    tool_ref: Dict[str, Any],
    arguments: Dict[str, Any],
    graph_json: Any = None,
) -> Dict[str, Any]:
    """Pre-dispatch validation: port-type check + output-cache short-circuit.

    Returns ``{"ok": bool, "output": Any|None, "cached": bool, "error": str|None}``.
    Port-type enforcement gates on the per-tenant flag; unknown/undeclared
    argument types pass through (lenient).  Cache lookups reuse the same
    canonical-JSON hashing that ``routers/worker_tasks._maybe_cache_short_circuit``
    uses (via ``node_output_cache._canonical_json``).  Never raises —
    exceptions log the type name and return ``ok=True, cached=False`` so
    the downstream dispatcher still proceeds.
    """
    try:
        ref_kind = tool_ref.get("kind") if isinstance(tool_ref, dict) else None
        ref_id = tool_ref.get("id") if isinstance(tool_ref, dict) else None

        # ---------------------------------------------------------------
        # Port-type check (when enforcement is enabled)
        # ---------------------------------------------------------------
        if port_type_enforcement.is_enforcement_enabled_for_tenant(db, tenant_id):
            input_schema = _get_input_schema(db, ref_kind, ref_id, tool_ref, tenant_id)
            if input_schema:
                for arg_name, arg_value in (arguments or {}).items():
                    port_decl = input_schema.get(arg_name)
                    if not isinstance(port_decl, dict):
                        continue
                    port_type = port_decl.get("type")
                    if port_type is None:
                        continue
                    # Infer the argument's content type from its JSON repr.
                    arg_type = _infer_arg_type(arg_value)
                    if arg_type is not None and not port_type_enforcement.are_types_compatible(
                        arg_type, port_type
                    ):
                        return {
                            "ok": False,
                            "output": None,
                            "cached": False,
                            "error": f"port_type_mismatch:{arg_name}",
                        }

        # ---------------------------------------------------------------
        # Output-cache short-circuit
        # ---------------------------------------------------------------
        # FORTRESS.6.2 (910c) — a tool target that carries a `dp` spec NEVER
        # reads the agent-tool cache. The worker path folds the dp spec into
        # its cache key (routers/worker_tasks.py:9992-9994); here the simplest
        # safe posture is to skip the cache outright: a cached agent-tool
        # result is the RAW (never noised) child output, so replaying it would
        # hand a downstream node an un-noised value AND skip the epsilon-
        # budget reserve services/workflow_orchestrator._agent_node_advance
        # does for the fresh run. Precedence matches
        # crud/workflow_cud._dp_gate_violations (data.dp wins legacy config.dp).
        if _dp_spec_for_tool_ref(db, tenant_id, tool_ref, graph_json) is not None:
            return {"ok": True, "output": None, "cached": False, "error": None}
        if node_output_cache.is_cache_enabled_for_tenant(db, tenant_id):
            key = _cache_key_parts(tool_ref, arguments)
            hit = node_output_cache.lookup(
                db,
                image_ref="",  # agent dispatch has no single image ref
                input_content_hash=key["input_content_hash"],
                config=key["config"],
                tenant_id=tenant_id,
            )
            if hit is not None:
                return {
                    "ok": True,
                    "output": hit.output_pointer,
                    "cached": True,
                    "error": None,
                }

        return {"ok": True, "output": None, "cached": False, "error": None}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "agent_node_runtime: _pre_dispatch_validate failed (%s) for tenant %s",
            type(exc).__name__,
            tenant_id,
        )
        return {"ok": True, "output": None, "cached": False, "error": None}


def _get_input_schema(
    db: Any,
    ref_kind: Optional[str],
    ref_id: Optional[str],
    tool_ref: Dict[str, Any],
    tenant_id: int,
) -> Dict[str, Any]:
    """Resolve the tool ref to its input schema (port-name → type schema)."""
    try:
        if ref_kind == "node_definition":
            name = ref_id if ref_id else tool_ref.get("name")
            if isinstance(name, str) and name.strip():
                import models
                nd = (
                    db.query(models.NodeDefinition)
                    .filter(
                        models.NodeDefinition.name == name.strip(),
                        models.NodeDefinition.tenant_id == tenant_id,
                        models.NodeDefinition.deleted_at.is_(None),
                    )
                    .first()
                )
                if nd is not None:
                    return (getattr(nd, "input_schema", None) or {}) or {}
        elif ref_kind == "sub_workflow":
            wr = tool_ref.get("workflow_ref") if isinstance(tool_ref, dict) else None
            wf_id = None
            if isinstance(wr, dict):
                wf_id = wr.get("workflow_id")
            elif isinstance(ref_id, str) and ref_id.strip():
                wf_id = int(ref_id) if ref_id.strip().isdigit() else None
            if wf_id is not None:
                import models
                wf = (
                    db.query(models.Workflow)
                    .filter(
                        models.Workflow.id == wf_id,
                        models.Workflow.tenant_id == tenant_id,
                        models.Workflow.deleted_at.is_(None),
                    )
                    .first()
                )
                if wf is not None:
                    version = (
                        db.query(models.WorkflowVersion)
                        .filter(
                            models.WorkflowVersion.workflow_id == wf_id,
                        )
                        .order_by(models.WorkflowVersion.version_number.desc())
                        .first()
                    )
                    if version is not None and version.graph_json:
                        graph_json = version.graph_json if isinstance(version.graph_json, dict) else {}
                        catalog = _tenant_catalog(db, tenant_id)
                        out = wse.build_workflow_mcp_schema(graph_json, catalog)
                        return (out.get("input_schema") or {}) or {}
        elif ref_kind == "node":
            # Node refs need graph_json for resolution — skip without it.
            pass
    except Exception:
        pass
    return {}


def _tenant_catalog(db: Any, tenant_id: int) -> Dict[int, Any]:
    """Lightweight tenant node-definition catalog."""
    import models
    rows = db.query(models.NodeDefinition).filter(
        models.NodeDefinition.tenant_id == tenant_id,
        models.NodeDefinition.deleted_at.is_(None),
    ).all()
    return {r.id: r for r in rows}


def _infer_arg_type(value: Any) -> Optional[str]:
    """Infer a port-type string from a runtime argument value."""
    if value is None:
        return None
    t = type(value).__name__
    if t == "str":
        return "string"
    if t == "int":
        return "integer"
    if t == "float":
        return "number"
    if t == "bool":
        return "boolean"
    if isinstance(value, (dict, list)):
        return "object"
    # Could not infer a meaningful type — pass through.
    return None


def _cache_config_for_validate(
    tool_ref: Dict[str, Any],
    ref_kind: Optional[str],
    ref_id: Optional[str],
) -> Dict[str, Any]:
    """Build a deterministic config dict for cache-key hashing."""
    return {
        "kind": ref_kind or "",
        "ref_id": ref_id or "",
    }


def _cache_key_parts(tool_ref: Any, arguments: Any) -> Dict[str, Any]:
    """EXPR 4.4 S1 — the ONE cache-key derivation shared by lookup and record.

    Returns ``{"input_content_hash": <64-hex>, "config": {kind, ref_id}}``
    with ``image_ref=""`` semantics (an agent dispatch has no single image
    ref). ``_pre_dispatch_validate`` reads and ``_record_agent_tool_cache``
    writes through this helper so the two can never drift.
    """
    ref_kind = tool_ref.get("kind") if isinstance(tool_ref, dict) else None
    ref_id = tool_ref.get("id") if isinstance(tool_ref, dict) else None
    return {
        "input_content_hash": hashlib.sha256(
            node_output_cache._canonical_json(arguments or {})
            .encode("utf-8")
        ).hexdigest(),
        "config": _cache_config_for_validate(tool_ref, ref_kind, ref_id),
    }


# hashlib is already imported above; node_output_cache._canonical_json
# reuses it for canonical-JSON → hex hashing.


def _node_dp_spec(entry: Any) -> Optional[Dict[str, Any]]:
    """FORTRESS.6.2 (910c) — dp spec on one graph node entry, or None.

    Same precedence as ``crud/workflow_cud._dp_gate_violations`` and the
    dispatch path: ``node["data"]["dp"]`` wins legacy ``node["config"]["dp"]``,
    and only a dict container counts.
    """
    if not isinstance(entry, dict):
        return None
    for src in ("data", "config"):
        container = entry.get(src)
        if isinstance(container, dict) and isinstance(container.get("dp"), dict):
            return container["dp"]
    return None


def _dp_spec_for_tool_ref(db: Any, tenant_id: int, tool_ref: Any,
                          graph_json: Any) -> Optional[Dict[str, Any]]:
    """FORTRESS.6.2 (910c) — the dp spec a tool TARGET carries, or None.

    A tool ref points either at a graph node (``kind="node"``) or at a
    tenant-visible NodeDefinition (``kind="node_definition"``). When that
    target declares a dp spec the agent-tool cache must not serve it (see
    the comment in ``_pre_dispatch_validate``), so both the read and the
    write sides ask this first. Cheap: the graph is scanned for dp-carrying
    entries and we abstain (None) when none exist, so non-DP dispatches pay
    one list walk and no DB query. Never raises — any failure answers None,
    which is the pre-existing (non-DP) behaviour.

    ``sub_workflow`` refs answer None: the child run owns its own DP noise +
    budget reserve through the orchestrator/worker completion path, and this
    runtime never sees the child graph.
    """
    try:
        if not isinstance(tool_ref, dict) or not isinstance(graph_json, dict):
            return None
        nodes = (graph_json.get("nodes") or [])
        dp_entries = [e for e in nodes
                      if isinstance(e, dict) and _node_dp_spec(e) is not None]
        if not dp_entries:
            return None
        kind = tool_ref.get("kind")
        if kind == "node":
            nid = tool_ref.get("node_id") or tool_ref.get("id")
            entry = (ats._graph_nodes(graph_json).get(nid)
                     if isinstance(nid, str) else None)
            return _node_dp_spec(entry)
        if kind == "node_definition":
            name = tool_ref.get("name") or tool_ref.get("id")
            if not isinstance(name, str) or not name.strip():
                return None
            catalog = {
                nd.id: nd
                for nd in wd_shared.get_node_definitions_for_tenant(db, tenant_id)
                if getattr(nd, "deleted_at", None) is None
            }
            definition = ats._resolve_definition_ref(
                db, tenant_id, catalog, {"kind": "node_definition",
                                         "name": name.strip()})
            if definition is None:
                return None
            for entry in dp_entries:
                data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
                if data.get("node_definition_id") == definition.id:
                    return _node_dp_spec(entry)
                if data.get("label") == definition.name:
                    return _node_dp_spec(entry)
        return None
    except Exception as exc:  # noqa: BLE001 — class name only
        logger.warning(
            "agent_node_runtime: dp-spec probe failed (%s) for tenant %s",
            type(exc).__name__, tenant_id,
        )
        return None


def _output_content_parts(output: Any) -> Optional[Tuple[str, str]]:
    """EXPR 4.4 S1 — (pointer, content_hash) for an agent tool output.

    Returns None when the output has no stable content hash: a missing
    output, or one that is not JSON-serialisable (repr() fallbacks change
    across processes, so such a row could never be matched fairly).
    """
    if output is None:
        return None
    try:
        canonical = json.dumps(
            output, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError):
        return None
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_agent_tool_cache(db, *, tenant_id: int, tool_ref: Any,
                             arguments: Any, output: Any,
                             graph_json: Any = None) -> None:
    """EXPR 4.4 S1 — write side of the agent-tool output cache.

    Called from `_dispatch_outcome` after an ok, NON-cached tool outcome so
    the existing `_pre_dispatch_validate` lookup can actually hit. Uses the
    exact key derivation the lookup uses (`_cache_key_parts`), the same
    per-tenant `output_cache_enabled` gate, and never records failures,
    parked runs, or outputs without a content hash. Sandbox-purpose runs
    are not recorded either — but `run.purpose` is not threaded into this
    runtime at all (EXPR 4.4 S2 does that), so there is nothing to check
    here yet; S2 adds the gate. Never raises: exceptions log the class
    name only so the agent loop keeps going.
    """
    if db is None or output is None:
        return
    try:
        # FORTRESS.6.2 (910c) — write-side symmetry with the read-side skip:
        # a dp-spec tool target's RAW (un-noised) result is never stored, so
        # no later run can replay it ahead of noise / budget reserve.
        if _dp_spec_for_tool_ref(db, tenant_id, tool_ref, graph_json) is not None:
            return
        if not node_output_cache.is_cache_enabled_for_tenant(db, tenant_id):
            return
        key = _cache_key_parts(tool_ref, arguments)
        pointer, content_hash = _output_content_parts(output)
        if not content_hash:
            return
        node_output_cache.record(
            db,
            image_ref="",  # agent dispatch has no single image ref
            input_content_hash=key["input_content_hash"],
            config=key["config"],
            tenant_id=tenant_id,
            output_pointer=pointer,
            output_content_hash=content_hash,
        )
    except Exception as exc:  # noqa: BLE001 — class name only
        logger.warning(
            "agent_node_runtime: tool-cache record failed (%s) for tenant %s",
            type(exc).__name__, tenant_id,
        )


def _record_agent_tool_quality_signal(db, *, tenant_id: int, tool_ref: Any,
                                      arguments: Any, outcome: Dict[str, Any],
                                      purpose: Optional[str]) -> None:
    """EXPR 4.4 S3 — quality-signal capture for agent tool dispatches.

    Called from `_dispatch_outcome` after EVERY tool outcome (ok AND failed,
    parked runs excluded — they have no outcome yet). Reuses
    `wizard_feedback.record_quality_signal` on `wizard_feedback_event`
    (owner Q1 REC: no new table, models.py untouched): ok outcomes get
    `wizard_feedback.AGENT_TOOL_ACTION_OK`, failures
    `AGENT_TOOL_ACTION_FAILED` (satisfies the live action CHECK's
    `rejected_%` arm). The row carries no wizard proposal, so proposal_id
    is 0 and the discriminating payload rides `feedback_event_metadata`:
    tool_ref, failure_reason, purpose. Sandbox-purpose runs are skipped
    entirely (S2 threaded `purpose`; None means "old behaviour" — record).
    Never raises: `record_quality_signal` itself is best-effort, and we
    wrap defensively so the agent loop keeps going.
    """
    if db is None or (run_purposes is not None
                       and run_purposes.is_sandbox_purpose(purpose)):
        return
    try:
        # The recorder rolls back on write failure; run it inside a savepoint
        # so that rollback can never poison the caller's session (the agent
        # loop keeps dispatching + caching on the SAME db).
        with db.begin_nested():
            wizard_feedback.record_quality_signal(
            db,
            tenant_id=tenant_id,
            proposal_id=0,
            apply_mode="replace",
            action=(wizard_feedback.AGENT_TOOL_ACTION_OK
                    if outcome.get("ok") else
                    wizard_feedback.AGENT_TOOL_ACTION_FAILED),
            feedback_event_metadata={
                "source": "agent_node_runtime",
                "tool_ref": tool_ref,
                "failure_reason": None if outcome.get("ok")
                else outcome.get("error"),
                "purpose": purpose,
            },
        )
    except Exception as exc:  # noqa: BLE001 — class name only
        logger.warning(
            "agent_node_runtime: tool quality-signal record failed (%s) for tenant %s",
            type(exc).__name__, tenant_id,
        )


def _stamp_agent_park_state(db: Any, *, tenant_id: int, park_ctx: Dict[str, Any],
                            ref: Dict[str, Any], arguments: Dict[str, Any]) -> bool:
    """Slice A step (b): write agent_state on the NodeRun BEFORE the child starts.

    A fast child's completion hook (services/agent_tool_resume) discriminates on
    ``outputs["agent_state"]``; if the stamp lands after the child starts, the
    plain sub_workflow path completes the node and the agent never resumes. So
    this is flushed (not committed — the adapter never commits) before
    ``park_and_dispatch_sub_workflow`` is called. Tenant scoping matches the
    resume side: NodeRun has no tenant column, so the check goes through the
    PARENT's own WorkflowRun.
    """
    import models
    from sqlalchemy.orm.attributes import flag_modified

    node_run = db.get(models.NodeRun, park_ctx["node_run_id"])
    if node_run is None:
        return False
    parent_run = db.get(models.WorkflowRun, node_run.workflow_run_id)
    if parent_run is None or parent_run.tenant_id != tenant_id:
        logger.warning(
            "agent_node_runtime: park refused for node_run %s / run %s (tenant scope)",
            park_ctx["node_run_id"], park_ctx["workflow_run_id"],
        )
        return False

    try:
        calls_used = int(park_ctx["calls"].get("n", 0)) + 1
    except (TypeError, ValueError, AttributeError):
        calls_used = 1
    try:
        max_calls = int(park_ctx["max_tool_calls"])
    except (TypeError, ValueError):
        max_calls = 1

    merged = dict(node_run.outputs) if isinstance(node_run.outputs, dict) else {}
    merged[AGENT_STATE_KEY] = {
        "history": copy.deepcopy(park_ctx["history"]),
        "pending_ref": ref,
        "pending_args": arguments,
        "tool_calls_used": calls_used,
        "max_tool_calls": max_calls,
        # EXPR 4.4 S2 — the parent run's purpose rides the per-run telemetry
        # (llm_metering has no purpose slot; we do not invent new metering).
        "purpose": park_ctx.get("purpose"),
    }
    # 1201 — clamp before the assignment (not in-place): history grows one entry
    # per tool round and this column is served verbatim by run-detail.
    node_run.outputs = clamp_node_run_text(merged)
    flag_modified(node_run, "outputs")
    db.flush()
    return True


def _park_node_tool_ref(db: Any, tenant_id: int, park_ctx: Dict[str, Any],
                        ref: Dict[str, Any], kind: str, arguments: Dict[str, Any],
                        graph_json: Any) -> Dict[str, Any]:
    """Park a ``node`` or ``node_definition`` tool ref by building a synthetic
    single-node child workflow and launching it as a background run.

    Follows the sub_workflow_runtime pattern:
    1) resolve the target NodeDefinition via agent_tool_schemas
    2) build a 3-node graph (input -> process -> return)
    3) create a throwaway workflow/version/run via crud.create_workflow /
       crud.create_workflow_version / crud.create_workflow_run (so the child
       gets real WorkflowNode + NodeRun rows like any other run)
    4) stamp the parent NodeRun with agent_state + CHILD_MARKER BEFORE the
       child's background thread starts
    5) background-start the orchestrator via threading.Thread

    Unresolvable refs return ``agent_park_refused``; any failure once the ref
    HAS resolved (missing system input/return nodes, graph/run creation,
    parent stamping) returns ``TOOL_PARK_FAILED`` and never leaves a
    half-stamped parent.
    """
    import models
    import schemas
    from crud import workflow_cud
    from sqlalchemy.orm.attributes import flag_modified

    from services import sub_workflow_runtime as swr
    from services.workflow_orchestrator import _emit_run_event

    # Build the tenant-visible catalog (same pattern as agent_tool_schemas:72).
    catalog = {
        nd.id: nd
        for nd in wd_shared.get_node_definitions_for_tenant(db, tenant_id)
        if getattr(nd, "deleted_at", None) is None
    }

    # Resolve the target definition.
    definition: Optional[Any] = None
    node_id_str: Optional[str] = None  # id used in the graph's process node

    if kind == "node":
        resolved = ats._resolve_node_ref(
            db, tenant_id, graph_json or {},
            park_ctx.get("node_id"), catalog, ref)
        if resolved is None:
            return {"status": "error", "error": "agent_park_refused"}
        node_id_str, definition, _ = resolved
    else:  # node_definition
        definition = ats._resolve_definition_ref(
            db, tenant_id, catalog, ref)
        if definition is None:
            return {"status": "error", "error": "agent_park_refused"}
        node_id_str = str(definition.id)

    # Look up system input / return nodes (mirrors node_trial.py:231).
    def _system_node_by_kind(nk: str):
        return (
            db.query(models.NodeDefinition)
            .filter(
                models.NodeDefinition.node_kind == nk,
                models.NodeDefinition.tenant_id.is_(None),
            )
            .first()
        )
    GENERIC_INPUT_KIND = "resource_input"
    RETURN_NODE_KIND = "return_assets_export"
    try:
        input_nd = _system_node_by_kind(GENERIC_INPUT_KIND)
        return_nd = _system_node_by_kind(RETURN_NODE_KIND)
    except Exception:  # noqa: BLE001
        return {"status": "error", "error": TOOL_PARK_FAILED}
    if input_nd is None or return_nd is None:
        return {"status": "error", "error": TOOL_PARK_FAILED}

    # Pin the resolved node to its current build (node-version-pinning).
    pin_id = None
    try:
        from crud import node_definition_cud as _ndc
        nv = _ndc.current_node_version(db, definition.id)
        pin_id = nv.id if nv else None
    except Exception:  # noqa: BLE001
        pin_id = None

    # Build a synthetic 3-node graph (input -> process -> return), same
    # shape as node_trial.py:build_trial_graph.
    input_id, proc_id, ret_id = "input-1", "process-1", "return-1"
    proc_data = {
        "nodeDefinitionId": definition.id,
        "node_kind": definition.node_kind,
        "label": definition.name,
    }
    if pin_id is not None:
        proc_data["pinnedNodeVersionId"] = pin_id
    graph_json_node = {
        "nodes": [
            {"id": input_id, "type": "input", "position": {"x": 0, "y": 0},
             "data": {
                 "nodeDefinitionId": input_nd.id,
                 "node_kind": input_nd.node_kind,
                 "label": input_nd.name,
                 "sample_input_ref": getattr(definition, "sample_input_ref", None),
             }},
            {"id": proc_id, "type": "process", "position": {"x": 260, "y": 0},
             "data": proc_data},
            {"id": ret_id, "type": "output", "position": {"x": 520, "y": 0},
             "data": {
                 "nodeDefinitionId": return_nd.id,
                 "node_kind": return_nd.node_kind,
                 "label": return_nd.name,
             }},
        ],
        "edges": [
            {"id": "e1", "source": input_id, "target": proc_id},
            {"id": "e2", "source": proc_id, "target": ret_id},
        ],
    }

    # Create a throwaway Workflow + Version + Run the same way
    # node_marketplace_paid.py:659-690 does (crud, not raw models — this is
    # what gives the child its own WorkflowNode + NodeRun rows per graph node).
    parent_run = db.get(models.WorkflowRun, park_ctx.get("workflow_run_id"))
    triggered_by = (getattr(parent_run, "triggered_by_user_id", None)
                    or park_ctx.get("triggered_by_user_id") or 0)
    run_name_suffix = park_ctx.get("node_run_id") or "?"
    wf_name = f"agent tool '{node_id_str}' of run {run_name_suffix}"

    try:
        owner = workflow_cud.create_workflow(
            db, schemas.WorkflowCreate(name=wf_name), triggered_by, tenant_id)
        ver = workflow_cud.create_workflow_version(
            db, owner, schemas.WorkflowVersionCreate(graph_json=graph_json_node),
            triggered_by)

        from utils.minio_client import get_tenant_bucket_name
        tenant_bucket = get_tenant_bucket_name(tenant_id, db)
        conf = {
            "tenant_id": tenant_id,
            "tenant_bucket": tenant_bucket,
            "nodes": graph_json_node["nodes"],
            "edges": graph_json_node["edges"],
            "project_info": {
                "tenant_id": tenant_id,
                "tenant_bucket": tenant_bucket,
                "user_id": triggered_by,
                "cog_ids": [],
                "metadata_cog_ids": [],
                "parent_folder_id": None,
                "workflow_name": wf_name,
                "extra_variables": dict(arguments),
            },
        }
        child = workflow_cud.create_workflow_run(
            db, ver, triggered_by, conf, run_name=wf_name,
            purpose=park_ctx.get("purpose") or "production")
        conf["workflow_run_id"] = child.id
    except Exception as exc:  # noqa: BLE001 — class name only
        db.rollback()
        logger.warning(
            "agent_node_runtime: node park child-create failed (%s) for node_run %s",
            type(exc).__name__, park_ctx.get("node_run_id"),
        )
        return {"status": "error", "error": TOOL_PARK_FAILED}

    # Stamp the parent NodeRun BEFORE the child's background thread starts
    # (same ordering as sub_workflow_runtime.py:260-269): agent_state first
    # (via the shared helper), then the CHILD_MARKER + PENDING_ACTION status.
    # Any failure here must not leave a half-stamped parent.
    if not _stamp_agent_park_state(db, tenant_id=tenant_id, park_ctx=park_ctx,
                                   ref=ref, arguments=arguments):
        db.rollback()
        return {"status": "error", "error": TOOL_PARK_FAILED}
    try:
        node_run = db.get(models.NodeRun, park_ctx["node_run_id"])
        if node_run is None:
            db.rollback()
            return {"status": "error", "error": TOOL_PARK_FAILED}
        merged = dict(node_run.outputs) if isinstance(node_run.outputs, dict) else {}
        merged[swr.CHILD_MARKER] = child.id
        # 1201 — same cap as the stamp above; keeps the direct write clamped so
        # the next one doesn't reintroduce the gap.
        node_run.outputs = clamp_node_run_text(merged)
        node_run.status = models.TaskStatus.PENDING_ACTION
        flag_modified(node_run, "outputs")
        db.commit()

        _emit_run_event(
            db, park_ctx["workflow_run_id"], "orchestrator", "info",
            f"Node tool '{node_id_str}' dispatched as run #{child.id}",
            {"node_id": node_id_str, "child_run_id": child.id, "tool_kind": kind},
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 — class name only
        db.rollback()
        logger.warning(
            "agent_node_runtime: node park stamp failed (%s) for node_run %s",
            type(exc).__name__, park_ctx.get("node_run_id"),
        )
        return {"status": "error", "error": TOOL_PARK_FAILED}

    # Background hand-off (same pattern as sub_workflow_runtime.py:289).
    try:
        import threading
        from database import SessionLocal
        threading.Thread(
            target=_start_node_child,
            args=(SessionLocal, conf, child.id),
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001
        pass

    logger.info(
        "Run %s: node tool %s -> child run %d (tenant %d, kind %s)",
        park_ctx.get("workflow_run_id", "?"), node_id_str, child.id,
        tenant_id, kind,
    )
    return {"status": "parked"}


def _start_node_child(db_factory, conf: dict, workflow_run_id: int):
    """Background hand-off for a node-tool child run.

    Mirrors the same pattern as sub_workflow_runtime._start_child so
    test mocks can patch it.
    """
    from services.workflow_orchestrator import start_workflow_execution
    return start_workflow_execution(db_factory, conf, workflow_run_id)


def _sync_sub_workflow_dispatcher(*, tenant_id: int, db: Any = None,
                                  park_ctx: Any = None,
                                  graph_json: Any = None) -> Any:
    """The default `dispatch_tool`: resolution-only, execution deferred.

    Resolution of a `sub_workflow` ref (its tenant visibility + live version)
    already happened upstream — services/agent_tool_schemas resolves those
    refs through services/sub_workflow_resolve.py, and a graph whose refs do
    not resolve never reaches this dispatcher (we answer
    agent_unresolved_tools). What is NOT possible yet is RUNNING one: a
    sub-workflow (and any `node` / `node_definition` tool) executes through an
    async worker run — services/sub_workflow_runtime.park_and_dispatch_sub_workflow
    parks the parent and a later task resumes it.

    Pre-dispatch validation (port-type check + output-cache short-circuit)
    runs via `_pre_dispatch_validate` before the stub error is returned.

    EXPR 4.4 slice A — ``park_ctx`` (dict or None) makes a cache-MISS
    ``sub_workflow`` ref actually park:
    ``{workflow_run_id, node_run_id, node_id, history (the loop's live list),
    max_tool_calls, calls: {"n": int}}`` (``calls`` is the loop's tool-call
    count so far, kept in sync by ``run_agent_node``; the stamp writes
    ``calls + 1``, i.e. this dispatch included).  On a miss with a
    ``sub_workflow`` ref carrying ``workflow_version_id`` we stamp
    ``agent_state`` on the NodeRun (flushed, never committed) and hand off to
    services/sub_workflow_runtime.park_and_dispatch_sub_workflow; True ->
    ``{"status": "parked"}``, False/exception -> ``tool_park_failed``.

    EXPR 4.4 — ``node`` and ``node_definition`` kinds are now also parked
    through the same child-run path.  The ref is resolved via
    services/agent_tool_schemas (``_resolve_node_ref`` /
    ``_resolve_definition_ref``), a synthetic 3-node graph is built
    (input → process → return), a throwaway workflow/version/run is created,
    and the parent is stamped with ``agent_state`` + ``CHILD_MARKER`` just
    like the sub_workflow path.  Every unresolvable shape (no node_run_id,
    no ref, foreign tenant) still falls through to ASYNC_TOOL_ERROR.
    """
    def _dispatch(ref: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(ref, dict):
            return {"status": "error", "error": "malformed tool ref"}
        kind = ref.get("kind")
        if kind not in loop.TOOL_REF_KINDS:
            return {"status": "error", "error": "unknown tool kind"}
        # Pre-dispatch: port-type check + cache short-circuit.
        val = _pre_dispatch_validate(db, tenant_id=tenant_id,
                                     tool_ref=ref, arguments=arguments,
                                     graph_json=graph_json)
        if not val.get("ok"):
            return {"status": "error", "error": val.get("error", "")}
        if val.get("cached"):
            return {"status": "ok", "output": val.get("output"), "cached": True}

        # Cache MISS: park a sub_workflow ref when the orchestrator wired us.
        if park_ctx is not None and db is not None and kind == "sub_workflow" \
                and park_ctx.get("node_run_id") is not None:
            version_id = ref.get("workflow_version_id")
            if isinstance(version_id, int) and not isinstance(version_id, bool):
                if not _stamp_agent_park_state(db, tenant_id=tenant_id,
                                               park_ctx=park_ctx, ref=ref,
                                               arguments=arguments):
                    return {"status": "error", "error": "agent_park_refused"}
                try:
                    from services.sub_workflow_runtime import park_and_dispatch_sub_workflow
                    parked = park_and_dispatch_sub_workflow(
                        db,
                        workflow_run_id=park_ctx["workflow_run_id"],
                        node_run_id=park_ctx["node_run_id"],
                        node_id=park_ctx["node_id"],
                        node_kind="sub_workflow",
                        node_data={"resolved_workflow_version_id": version_id},
                    )
                except Exception as exc:  # noqa: BLE001 — class name only
                    logger.warning(
                        "agent_node_runtime: park failed (%s) for node_run %s",
                        type(exc).__name__, park_ctx["node_run_id"],
                    )
                    parked = False
                if parked:
                    return {"status": "parked"}
                return {"status": "error", "error": TOOL_PARK_FAILED}

        # Cache MISS: park a node / node_definition ref.
        if park_ctx is not None and db is not None \
                and kind in ("node", "node_definition") \
                and park_ctx.get("node_run_id") is not None:
            try:
                return _park_node_tool_ref(
                    db, tenant_id, park_ctx, ref, kind, arguments, graph_json)
            except Exception as exc:  # noqa: BLE001 — class name only
                import traceback as _tb
                _tb.print_exception(type(exc), exc, exc.__traceback__)
                logger.warning(
                    "agent_node_runtime: node park failed (%s) for node_run %s",
                    type(exc).__name__, park_ctx.get("node_run_id"),
                )
                return {"status": "error", "error": TOOL_PARK_FAILED}

        return {"status": "error", "error": ASYNC_TOOL_ERROR,
                "tool_kind": kind, "tenant_id": tenant_id}

    return _dispatch


def _envelope(status: str, *, final: Any = None, history: Optional[List[Dict[str, Any]]] = None,
              failure_reason: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "status": status,
        "outputs": {"final": final, "history": list(history or [])},
    }
    if failure_reason is not None:
        out["failure_reason"] = failure_reason
    out.update(extra)
    return out


def run_agent_node(db, *, tenant_id: int, workflow_run_id: int, node_id: str,
                   node_config: Dict[str, Any], agent_input: Any,
                   graph_json: Dict[str, Any],
                   llm_client: Optional[Any] = None,
                   tool_dispatcher: Optional[Any] = None,
                   node_run_id: Optional[int] = None,
                   purpose: Optional[str] = None) -> Dict[str, Any]:
    """Run one agent-node dispatch; return the node-output envelope.

    ``{"status": "completed"|"failed"|"refused",
        "outputs": {"final": <agent answer>, "history": [...truncated per
        run_agent_loop's rules]}, "failure_reason": <set when the cap fires
        (agent_max_tool_calls_exceeded), on an LLM failure, or on refusal>}``

    Injection points for tests: `llm_client(history, tool_schemas)` (its
    return is normalized by _normalize_step, so a fake may answer either the
    loop contract dict or an OpenAI-style message) and
    `tool_dispatcher(ref, arguments)` (either the loop's {ok,output,error} or
    the {status:"error",error} envelope). Defaults are the platform LLM client
    and the resolution-only dispatcher documented above.

    EXPR 4.4 slice A — ``node_run_id`` causes the default dispatcher to
    park ``sub_workflow`` tool refs (handing off to
    sub_workflow_runtime.park_and_dispatch_sub_workflow).
    EXPR 4.4 — ``node`` and ``node_definition`` tool refs are also
    parked through a synthetic child run (agent_node_runtime._park_node_tool_ref).
    Both return ``{"status": "parked", "outputs": {"final": None,
    "history": [...]}}`` — the orchestrator must NOT write terminal state
    (slice C's ``_agent_node_advance`` branch).  Call sites that pass no
    ``node_run_id`` keep ASYNC_TOOL_ERROR exactly.

    * ``node_config["resumed_history"]`` — the parked history slice B hands back
      on re-entry; the loop seeds with it instead of prepending a user message.

    Never raises, never commits, logs class names only. The orchestrator call
    site is named in this module's docstring (FX 4.1 wiring task).
    """
    try:
        if ef is not None and not ef.phase3_agent_nodes_enabled(db, tenant_id):
            return _envelope("refused", failure_reason=REFUSAL_PHASE3_OFF)

        derived = ats.derive_tool_schemas(db, tenant_id=tenant_id,
                                          graph_json=graph_json,
                                          agent_node_id=node_id)
        if derived.get("errors"):
            return _envelope("refused", failure_reason=REFUSAL_UNRESOLVED_TOOLS,
                             errors=list(derived["errors"]))

        tools = [t for t in derived.get("tools", []) if isinstance(t, dict)]
        allow_list = [t["name"] for t in tools if isinstance(t.get("name"), str)]
        loop_config = {
            "tools": [{"name": t["name"], "ref": _loop_ref(t.get("ref", {})),
                       "description": t.get("description"),
                       "input_schema": t.get("input_schema")}
                      for t in tools if isinstance(t.get("name"), str)],
            "max_tool_calls": runner.effective_max_tool_calls(node_config, db),
            "system_prompt": (node_config or {}).get("system_prompt"),
        }

        if llm_client is not None:
            def _injected_step(history, tool_schemas, _client=llm_client):
                return _normalize_step(_client(history, tool_schemas))
            llm_step: Any = _injected_step
        else:
            llm_step = _platform_llm_step(tenant_id=tenant_id,
                                          node_config=loop_config,
                                          allow_list=allow_list)

        # EXPR 4.4 slice A — seeded history + park context are built BEFORE the
        # dispatcher closure so the default dispatcher captures them.
        seeded = node_config.get(RESUMED_HISTORY_KEY)
        seeded_history = [dict(e) for e in seeded if isinstance(e, dict)] \
            if isinstance(seeded, list) and seeded else None

        park_ctx = None
        if tool_dispatcher is None and node_run_id is not None and db is not None:
            park_ctx = {"workflow_run_id": workflow_run_id,
                        "node_run_id": node_run_id,
                        "node_id": node_id,
                        "history": seeded_history if seeded_history is not None
                                  else [],
                        "max_tool_calls": loop_config["max_tool_calls"],
                        "calls": {"n": 0},
                        # EXPR 4.4 S2 — the parent run's purpose rides the
                        # per-run park telemetry into agent_state.
                        "purpose": purpose}
            # The loop appends to the SAME list object the park stamp deepcopies,
            # so when no seeded history exists we hand the loop that very list.
            if seeded_history is None:
                seeded_history = park_ctx["history"]

        def _dispatch_outcome(ref, arguments, _d=tool_dispatcher, _park=park_ctx):
            if _d is None:
                _d = _sync_sub_workflow_dispatcher(tenant_id=tenant_id, db=db,
                                                   park_ctx=_park,
                                                   graph_json=graph_json)
            raw = _d(ref, arguments)
            outcome = _normalize_outcome(raw)
            # Keep park_ctx["calls"] in step with the loop's own tool_calls
            # counter BEFORE the next dispatch runs: a non-parked call here
            # means a LATER park in this same invocation must stamp
            # tool_calls_used including this one (calls+1 would otherwise
            # under-count every call after the first).
            if _park is not None and not outcome.get("parked"):
                _park["calls"]["n"] = _park["calls"].get("n", 0) + 1
            # EXPR 4.4 S1 — cache-write symmetry: record ok, non-cached,
            # non-parked outcomes under the SAME key the pre-dispatch
            # lookup hashes (failures/parked runs never recorded; the
            # helper itself gates on the tenant flag + a content hash).
            # EXPR 4.4 S2 — sandbox-purpose runs never WRITE (reads still
            # allowed via _pre_dispatch_validate).
            if outcome.get("ok") and not outcome.get("parked") \
                    and not (isinstance(raw, dict) and raw.get("cached")) \
                    and (run_purposes is None
                         or not run_purposes.is_sandbox_purpose(purpose)):
                _record_agent_tool_cache(db, tenant_id=tenant_id,
                                         tool_ref=ref, arguments=arguments,
                                         output=outcome.get("output"),
                                         graph_json=graph_json)
            # EXPR 4.4 S3 — quality-signal capture: exactly one signal row
            # per REAL tool outcome (ok AND failed; parked dispatches have
            # no outcome yet — the resume path records when the child's
            # result comes back). Sandbox purposes skip (S2 threaded
            # `purpose`; None = old behaviour, record).
            if not outcome.get("parked"):
                _record_agent_tool_quality_signal(
                    db, tenant_id=tenant_id, tool_ref=ref,
                    arguments=arguments, outcome=outcome,
                    purpose=purpose)
            return outcome

        dispatch = _dispatch_outcome

        result = loop.run_agent_loop(
            node_config=loop_config,
            agent_input=agent_input,
            llm_step=llm_step,
            dispatch_tool=dispatch,
            max_tool_calls=loop_config["max_tool_calls"],
            history=seeded_history,
        )

        if park_ctx is not None:
            # Keep the park counter aligned with what the loop actually did so a
            # second tool call in the same dispatch stamps the right count.
            try:
                park_ctx["calls"]["n"] = int(result.tool_calls)
            except (TypeError, ValueError):
                pass

        if result.status == "parked":
            return _envelope("parked", history=result.history,
                             tool_calls=result.tool_calls)
        return _envelope(result.status, final=result.output,
                         history=result.history,
                         failure_reason=result.failure_reason,
                         tool_calls=result.tool_calls)
    except Exception as exc:  # noqa: BLE001 — the adapter never raises
        logger.warning("agent_node_runtime: dispatch failed (%s) for node %s / run %s",
                       type(exc).__name__, node_id, workflow_run_id)
        return _envelope("failed", failure_reason=RUNTIME_FAILURE)


def _history_prompt(history: List[Dict[str, Any]]) -> str:
    """Serialize the conversation for the flat prompt the platform client takes."""
    try:
        return json.dumps(history, default=str)[:20_000]
    except (TypeError, ValueError):
        return str(history)[:20_000]


def _platform_llm_step(*, tenant_id: int, node_config: Dict[str, Any],
                       allow_list: List[str]):
    """The default `llm_step`: platform client + tenant metering attribution.

    `allow_list` is the derived schema names; a name outside it is passed to
    the loop as-is so it is recorded as an unknown tool and NEVER dispatched.
    """
    system_prompt = node_config.get("system_prompt")
    system_prompt = system_prompt if isinstance(system_prompt, str) else (
        "You are an agent node. Answer, or call one of the provided tools."
    )
    allowed = set(allow_list)

    def _step(history: List[Dict[str, Any]], tool_schemas: List[Dict[str, Any]]) -> Dict[str, Any]:
        with (llm_metering.llm_tenant_context(tenant_id) if llm_metering is not None else nullcontext()):
            try:
                tools_block = json.dumps(tool_schemas, sort_keys=True,
                                         separators=(",", ":"))[:6000]
            except (TypeError, ValueError) as exc:
                logger.warning("agent tool schema serialization failed: %s",
                                type(exc).__name__)
                tools_block = ""
            prompt = (system_prompt + "\n\nTools (JSON): " + tools_block
                      if tools_block else system_prompt)
            force_first_turn = bool(tool_schemas) and not _has_attempted_tool_call(history)
            if force_first_turn:
                # Bug 1372: the embedded tools block alone wasn't enough
                # signal — models recited the tool description in prose
                # instead of calling it. Force the guided-JSON decode to the
                # tool_call shape on the turn that hasn't tried a tool yet.
                schema = _forced_tool_call_schema(sorted(allowed))
                prompt += (
                    "\n\nYou must call one of the tools above now. Reply "
                    "ONLY with a tool_call JSON object naming one of: "
                    + ", ".join(sorted(allowed)) + ". Do not answer in prose."
                )
            else:
                schema = _STEP_SCHEMA
            raw = call_wizard_llm(
                prompt,
                _history_prompt(history),
                response_format="json",
                json_schema=schema,
                timeout_s=180,
                _label="agent-node",
                _tenant_id=tenant_id,
            )
        step = _normalize_step(raw)
        # Bug 1147: tool_schemas are offered to the model via a
        # prompt-embedded JSON block + guided-JSON `_STEP_SCHEMA` contract,
        # not the backend's native `tools` field, so a model/template that
        # can't actually follow that contract just echoes its own system
        # prompt back as a "final" answer — silently discarding every
        # declared tool. Fail loudly instead of returning that as a success.
        # (Bug 1372 fixes the common case structurally, above, by forcing the
        # first turn's schema to `tool_call`; this stays as the fallback for
        # routes that silently ignore the forced schema.)
        if (tool_schemas and isinstance(step, dict)
                and step.get("type") == "final"
                and _looks_like_prompt_echo(step.get("output"), system_prompt)):
            env = _get_wizard_config()
            model_name = env.get("LLM_MODEL") or "unknown"
            base_url = env.get("LLM_BASE_URL") or "unknown"
            logger.warning(
                "agent node llm_step: model=%s route=%s echoed its own "
                "system prompt instead of using any of %d declared tool(s) "
                "— tool calling appears unsupported by this model/route; "
                "the route needs to change, not the agent config",
                model_name, base_url, len(tool_schemas),
            )
            raise LLMClientError(
                f"agent node: model {model_name!r} at {base_url!r} returned "
                f"no tool call for {len(tool_schemas)} declared tool(s) "
                "(echoed its own system prompt) — tool calling unsupported "
                "by this model/route"
            )
        # A hallucinated tool name still reaches the loop, which records it
        # as an unknown tool WITHOUT dispatching — the allow-list gate lives
        # in run_agent_loop; `allowed` here is informational only.
        if isinstance(step, dict) and step.get("type") == "tool_call" \
                and step.get("tool") not in allowed:
            return step
        return step

    return _step

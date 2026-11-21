"""Service layer for ``routers/support_chat/_shared.py`` (Gate 2b
migration).

``_shared.py`` has no ``@router``-decorated endpoints — it is the flat
namespace of helpers, DTOs and constants the sibling sub-modules
(``assistant_support.py``, ``workflows.py``, ``node_wizard.py``,
``projects_agentic.py``) and ``routers/support_chat/__init__.py``
(``from ._shared import *``) depend on. The functions here hold the
policy that used to sit directly next to the ``db.query`` calls those
helpers made; ``crud/support_chat_shared.py`` now owns the query layer.

404-vs-403 (migrate_rules #5): ``crud.get_session_by_id`` /
``crud.get_workflow_by_id`` are caller-UNSCOPED. Ownership/tenant
checks happen HERE, on top of the unscoped fetch, so a legitimate 403
never collapses into a 404. ``services/_base.py`` ships
``NotFoundError`` / ``ConflictError`` but no forbidden error —
``ForbiddenError`` is declared below, same pattern as
``services/support_chat_workflows_service.py``.

``_shared.py``'s own thin wrappers stay the ONLY place these functions'
original names are bound in ``routers.support_chat._shared`` — internal
call sites elsewhere in ``_shared.py`` (and anything a test
monkeypatches) keep resolving through that module-level name, not this
one.
"""
from __future__ import annotations

import logging
import time as _time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from crud import support_chat_shared as crud
from services._base import ConflictError, NotFoundError, ServiceError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# LLM endpoint reachability (mesh-native-llm-routing §4.4/4.5)
# ---------------------------------------------------------------------

def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    """One-shot TCP dial of `host:port` (the pattern `probe_wizard_llm` and
    `resolve_wizard_builder_env` each inlined). No caching — callers that probe
    repeatedly on the hot path wrap this in `endpoint_reachable_cached`. An
    OSError (refused/timeout/unresolvable) means not reachable."""
    import socket
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return True
    except OSError:
        return False


def _base_url_host_port(base_url: str):
    """(host, port) for a base_url, mirroring the wizard probe's parse. Host
    falls back to the raw string; port defaults to 443 for https else 80."""
    from urllib.parse import urlparse
    u = urlparse((base_url or "").strip())
    host = u.hostname or (base_url or "").strip()
    port = u.port or (443 if u.scheme == "https" else 80)
    return host, port


# Reachability answers are cached briefly so a chat request does not re-dial
# the same endpoint on every message. Keyed by host:port with a monotonic
# timestamp; both success and failure are cached (a dead endpoint that keeps
# flapping shouldn't cost a 3s dial per request either).
_REACHABLE_TTL_S = 30.0
_reachable_cache: Dict[str, "tuple[float, bool]"] = {}


def endpoint_reachable_cached(
    base_url: str, *, timeout: float = 3.0, ttl: float = _REACHABLE_TTL_S,
) -> Optional[bool]:
    """Cached TCP reachability of a base_url's host:port (see
    `_tcp_reachable`). Returns None when there is no base_url to probe (e.g. a
    cloud provider with no local endpoint) — the caller treats that as
    "nothing to reach". Reuses one probe per host:port for `ttl` seconds."""
    base_url = (base_url or "").strip()
    if not base_url:
        return None
    host, port = _base_url_host_port(base_url)
    if not host:
        return None
    key = f"{host}:{port}"
    now = _time.monotonic()
    hit = _reachable_cache.get(key)
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]
    reachable = _tcp_reachable(host, port, timeout)
    _reachable_cache[key] = (now, reachable)
    return reachable


class ForbiddenError(ServiceError):
    """Caller is authenticated but not entitled to the resource (router
    maps -> 403). ``services/_base.py`` ships ``NotFoundError`` /
    ``ConflictError`` but no forbidden error — declared here per
    migrate_rules #5."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


def get_or_create_session(
    db: Session, user, kind: str, *,
    session_id: Optional[str], task_id: Optional[str] = None,
) -> models.SupportChatSession:
    """Fetch-or-create a support-chat session, enforcing ownership/kind/
    task_id policy on top of the unscoped CRUD fetch.

    Raises ``NotFoundError`` (-> 404) when ``session_id`` doesn't
    resolve, ``ForbiddenError`` (-> 403) when it belongs to a different
    user, and ``ConflictError`` (-> 409) on a kind or task_id mismatch —
    the exact checks (and order) the pre-migration inline handler did.
    """
    if session_id:
        sess = crud.get_session_by_id(db, session_id)
        if sess is None:
            raise NotFoundError("Session not found")
        if sess.user_id != user.id:
            raise ForbiddenError("Not your session")
        if sess.kind != kind:
            raise ConflictError(f"Session is kind={sess.kind}, requested {kind}")
        if kind == "logs" and task_id and sess.task_id != task_id:
            raise ConflictError("task_id mismatch on existing logs session")
        return sess

    import datetime as _dt
    import uuid as _uuid

    now = _dt.datetime.now(_dt.timezone.utc)
    sess = models.SupportChatSession(
        id=str(_uuid.uuid4()),
        user_id=user.id,
        tenant_id=user.tenant_id,
        kind=kind,
        task_id=task_id,
        created_at=now,
        last_message_at=now,
    )
    db.add(sess)
    db.flush()
    return sess


def resolve_wizard_builder_env(db) -> Optional[Dict[str, str]]:
    """mesh-native-llm-routing D2/D3 — resolve the model for the wizard builder.

    Prefer the reasoner stack when it is ACTUALLY reachable, reading its
    endpoint + served model from the stack record (not a preset's tier table,
    which gates the reasoner off even when it's running). Fall back to the
    wizard tool's configured env when the reasoner isn't reachable, so a box
    with no reasoner still works via whatever the operator bound.
    """
    try:
        # GT.P2.6d (E40) — reasoner role binding is a managed-stack (PAID)
        # concept: role_base_url/managed_key go through the CE shim
        # (Pro delegates to routers.ai_config; CE returns None / ""). On CE
        # the reasoner path is inert (base None -> unreachable) and the
        # function falls through to the wizard tool env.
        from utils.tool_config import (managed_key, resolved_effective_env,
                                       role_base_url)

        def _role_base_url(role):
            return role_base_url(role)

        def _managed_key():
            return managed_key()
        stack = crud.get_running_reasoner_stack(db)
        if stack is not None:
            svc = (stack.compose_spec or {}).get("services", {}).get("llm", {})
            served_block = (svc.get("x-mod") or {}).get("served") or {}
            served = served_block.get("model")
            ctx_tokens = served_block.get("context_tokens")
            base = _role_base_url("reasoner")
            host, port = _base_url_host_port(base)
            reachable = _tcp_reachable(host, port, 2.0) if host else False
            if reachable and served:
                logger.info("wizard builder -> reasoner stack %s at %s",
                            served, base)
                # Output budget SCALES WITH THE MODEL, not a hardcoded cap:
                # take a generous share of the model's own context window
                # (from the stack record's served.context_tokens), floored so a
                # small-context model still gets a usable budget. A bigger model
                # gets a bigger budget automatically; nothing truncates a graph.
                try:
                    out_budget = max(4096, int(ctx_tokens) // 2)
                except (TypeError, ValueError):
                    out_budget = 4096
                return {
                    "LLM_PROVIDER": "openai_compatible",
                    "LLM_MODEL": served,
                    "LLM_BASE_URL": base,
                    "LLM_API_KEY": _managed_key(),
                    "LLM_MAX_TOKENS": str(out_budget),
                }
        return resolved_effective_env("wizard") or None
    except Exception:  # noqa: BLE001
        return None


def _wizard_slot_failure_cause(db, tenant_id: Optional[int] = None) -> Optional[str]:
    """ai-config-stack-binding 3.3 — the typed resolver's sentence for why the
    WIZARD slot will not serve, for EVERY non-``ready`` outcome.

    ``resolve_wizard_builder_env`` degrades a failed resolution to ``None``
    (``routers.ai_config._resolved_effective_env`` returns ``{}`` — that is its
    contract, so no diagnostic key can leak into a task container), which made
    ``no_binding``/``stack_stopped``/``stack_missing`` indistinguishable from
    "nothing configured". The CAUSE lives in
    :func:`services.ai_slot_resolver.resolve_slot`; every consumer that REPORTS
    to a human calls it directly (same shape as 3.2's
    ``assistant_orchestrator._slot_failure_cause``).

    Returns ``None`` only when the slot unexpectedly resolves ``ready`` or the
    resolver cannot speak at all (no DB / resolver raised).
    """
    try:
        # GT.P2.6d (E41) carve-out, same shape as `stack_provider_error`'s: the
        # binding table + Settings -> AI surface is the PAID managed-AI state,
        # so in a tree without `routers.ai_config` there is nothing to report —
        # "nothing configured" stays the honest CE answer.
        from utils.paid_import import optional_paid
        if optional_paid("routers.ai_config") is None:
            return None
        from services.ai_slot_resolver import REMEDY, resolve_slot
        res = resolve_slot(db, "wizard", tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — a diagnostic must not break the build
        return None
    if (res.get("outcome") or "") == "ready":
        return None
    message = (res.get("message") or "").strip()
    attempts = res.get("attempts") or []
    if len(attempts) > 1:
        others = [(a.get("message") or "").replace(REMEDY, "").strip()
                  for a in attempts[1:]]
        others = [o for o in others if o]
        if others and message:
            message = f"{message} Fallbacks: {' '.join(others)}"
    return message or None


def probe_wizard_llm(db, env_override: Optional[Dict[str, str]] = None,
                     tenant_id: Optional[int] = None) -> Optional[str]:
    """mesh-native-llm-routing §4.7 — cheap reachability probe of the model
    the wizard build will dispatch through (the env that
    ``resolve_wizard_builder_env`` picked).

    Returns None when the endpoint is reachable (or nothing is configured to
    probe), and otherwise a user-facing message that names the model AND the
    endpoint host. It never leaks the API key, and it never falls back to
    another model — the caller must fail the build loudly. The probe is
    defensive: any internal error is swallowed (returns None) so a broken
    probe can never take the wizard down.

    ``env_override`` (§4.8): when the caller has ALREADY decided the env to
    dispatch through (e.g. it substituted an opt-in fallback binding after
    the intended endpoint went down), probe THAT env — the endpoint that
    will actually answer — instead of re-resolving the (down) intended one.
    """
    try:
        env = env_override if env_override is not None else resolve_wizard_builder_env(db)
        if not env:
            # GE.AIC.2 follow-up (2026-09-29) — an empty env here used to
            # mean only "nothing configured", so the probe returned None
            # (nothing to report) even when the wizard's provider="stack"
            # slot WAS configured but the stack is stopped/missing. That
            # made a down stack look identical to "unset" — the build would
            # proceed (or fail later with a generic error) instead of
            # failing loudly here, by name, as every other reachability
            # failure in this function does.
            # GT.P2.6d (E41) — the stack-provider diagnostic goes through
            # the CE shim (Pro delegates to routers.ai_config; CE has no
            # managed stacks → None, nothing to report; the env-path checks
            # below still fail loudly, naming model + host:port).
            from utils.tool_config import stack_provider_error
            stack_err = stack_provider_error("wizard")
            if stack_err:
                return (
                    f"Your wizard model is unreachable: {stack_err} The "
                    f"build was stopped before it started — no other model "
                    f"will answer for it."
                )
            # ai-config-stack-binding 3.3 — the legacy diagnostic above only
            # knows the transitional stack-provider failure modes. The typed
            # resolver knows WHY the wizard slot will not serve for EVERY
            # non-`ready` outcome (no_binding / stack_stopped / stack_missing
            # / unreachable / waking), with the cause already named (stack
            # name + status, or host:port) and the remedy. Only when a DB is
            # in play: with no session at all the resolver cannot see the
            # binding table, and "nothing to report" stays the honest answer
            # (the env-path checks below still fail loudly).
            cause = (_wizard_slot_failure_cause(db, tenant_id)
                     if db is not None else None)
            if cause:
                return (
                    f"Your wizard model is unreachable: {cause} The "
                    f"build was stopped before it started — no other model "
                    f"will answer for it."
                )
            return None
        base_url = (env.get("LLM_BASE_URL") or "").strip().rstrip("/")
        model = env.get("LLM_MODEL") or "(unconfigured model)"
        if not base_url:
            return (
                f"Your wizard model {model} is unreachable: no endpoint is "
                f"configured for the wizard tool. Set the wizard model in AI "
                f"Configuration or bring the model stack back up — the build "
                f"was stopped before it started."
            )
        host, port = _base_url_host_port(base_url)
        # UNcached: this is a fresh build attempt whose whole job is to report
        # the endpoint's current state accurately. Caching here would let a
        # stale "down" answer (from a prior build within the TTL) suppress a
        # now-reachable model, or vice-versa. The 30s cache belongs on the hot
        # chat path (`_llm_env`), not on the per-build probe.
        reachable = _tcp_reachable(host, port, 3.0)
        if reachable:
            return None
        return (
            f"Your wizard model {model} is unreachable: the endpoint "
            f"{host}:{port} is not responding. Bring the model's stack or "
            f"worker back online and try again. The build was stopped before "
            f"it started — no other model will answer for it."
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------
# Opt-in per-tool LLM fallback (mesh-native-llm-routing §4.8)
# ---------------------------------------------------------------------

def read_tool_fallback_settings(tool: str) -> Dict[str, Any]:
    """mesh-native-llm-routing §4.8 — the opt-in fallback toggle stored in the
    tool's EXISTING AI-Config binding (the Vault KV dict behind
    ``routers.ai_config._read_tool_config``): ``fallback_enabled`` (bool,
    default False) + ``fallback_tool`` (name of the OTHER tool whose binding
    is used when the intended endpoint is unreachable). No new columns, no
    migration — the raw config dict already carries arbitrary operator keys.
    Never raises: any read problem degrades to the default (off)."""
    try:
        # GT.P2.6d (E42) — raw tool config read via the CE shim (Pro
        # delegates to routers.ai_config._read_tool_config; CE reads the
        # same Vault paths).
        from utils.tool_config import read_tool_config
        raw = read_tool_config(tool) or {}
    except Exception:  # noqa: BLE001
        return {"fallback_enabled": False, "fallback_tool": None}
    return {
        "fallback_enabled": bool(raw.get("fallback_enabled", False)),
        "fallback_tool": (raw.get("fallback_tool") or None),
    }


def _announcement_text(intended_model: str, fallback_model: str) -> str:
    """The user-facing substitution notice (§4.8). Every reply produced on
    the fallback path carries this, so a substitution is never silent."""
    return (
        f"⚠ {intended_model} is unavailable — this answer used "
        f"{fallback_model}.\n\n"
    )


def wizard_fallback_announcement(
    db, intended_env: Optional[Dict[str, str]],
) -> tuple[Optional[Dict[str, str]], Optional[str]]:
    """mesh-native-llm-routing §4.8 — wizard fallback decision.

    Returns ``(env, announcement)``:

      * intended endpoint reachable (or no endpoint to dial, e.g. a cloud
        binding)            -> ``(intended_env, None)`` — no substitution;
      * intended down AND the wizard tool has ``fallback_enabled`` set with
        a reachable ``fallback_tool`` binding -> (fallback env, announcement
        naming BOTH models) — the caller dispatches through the fallback and
        must prepend the announcement to every user-facing reply;
      * otherwise (fallback off/unconfigured, or the fallback endpoint is
        also down)          -> ``(intended_env, None)`` — the caller's loud
        §4.7 failure path still runs (``probe_wizard_llm``), naming the
        intended model. No silent substitution ever happens.
    """
    if not intended_env:
        return intended_env, None
    base_url = (intended_env.get("LLM_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        # A cloud provider has no local endpoint to dial — reachable by
        # definition; the intended model is used, no substitution.
        return intended_env, None
    host, port = _base_url_host_port(base_url)
    if _tcp_reachable(host, port, 3.0):
        return intended_env, None
    settings = read_tool_fallback_settings("wizard")
    if not settings["fallback_enabled"] or not settings["fallback_tool"]:
        return intended_env, None
    # GT.P2.6d (E43) — fallback-tool env via the CE shim (Pro delegates to
    # routers.ai_config._resolved_effective_env; CE: managed tools resolve
    # to {}, so a managed fallback can never substitute).
    from utils.tool_config import resolved_effective_env
    fb_env = resolved_effective_env(settings["fallback_tool"]) or {}
    if not (fb_env.get("LLM_PROVIDER") and fb_env.get("LLM_API_KEY")):
        return intended_env, None
    fb_base = (fb_env.get("LLM_BASE_URL") or "").strip().rstrip("/")
    if fb_base:
        fb_host, fb_port = _base_url_host_port(fb_base)
        if not _tcp_reachable(fb_host, fb_port, 3.0):
            # The fallback endpoint is ALSO down — refuse the substitution
            # so the caller's §4.7 loud failure names the intended model
            # instead of announcing a swap to a dead endpoint.
            return intended_env, None
    intended_model = intended_env.get("LLM_MODEL") or "(unconfigured model)"
    fb_model = fb_env.get("LLM_MODEL") or "(unconfigured model)"
    announcement = _announcement_text(intended_model, fb_model)
    logger.warning(
        "wizard LLM fallback: intended %s at %s:%s is down — "
        "substituting %s (binding %r) per tool opt-in; reply will "
        "announce the substitution.",
        intended_model, host, port, fb_model, settings["fallback_tool"],
    )
    return fb_env, announcement


# BUG.B31 — the run-details LLM panel must be able to answer a greeting
# ("hi") about THIS run, so the run block carries name/status/timings/
# per-node rows plus a bounded tail of orchestrator log lines. Bytes, not
# lines, is the bound: a run with 200 nodes must not flood the prompt.
_RUN_LOG_TAIL_BYTES = 4096
_RUN_NODE_ROWS_MAX = 40


def _run_log_tail_lines(db: Session, run_id: int,
                        byte_budget: int = _RUN_LOG_TAIL_BYTES) -> List[str]:
    """Last orchestrator/traffic-cop log lines for a run, newest first,
    truncated to ``byte_budget``. Empty list when there are none."""
    try:
        rows = (
            db.query(models.WorkflowRunEvent)
            .filter(models.WorkflowRunEvent.workflow_run_id == run_id)
            .order_by(models.WorkflowRunEvent.ts.desc(),
                      models.WorkflowRunEvent.id.desc())
            .limit(40)
            .all()
        )
    except Exception:  # noqa: BLE001 — context must never fail the turn
        return []
    out: List[str] = []
    used = 0
    for row in rows:
        msg = (row.message or "").strip()
        if not msg:
            continue
        line = f"  [{row.level or 'info'}] {row.source or ''}: {msg}"
        if used + len(line) > byte_budget:
            break
        out.append(line)
        used += len(line)
    out.reverse()
    return out


def logs_run_visible_to(db: Session, user, run_id: Optional[int]) -> bool:
    """BUG.B31 — the run-details panel pins a run id, so logs-chat must
    refuse to discuss a run the caller cannot see. True for a missing run
    (nothing to leak; the context block already says "not found"), for a
    platform admin, and for a run in the caller's tenant. False otherwise."""
    if run_id is None:
        return True
    run = crud.get_workflow_run_by_id(db, run_id)
    if run is None:
        return True
    if user is not None and getattr(user, "is_platform_admin", False):
        return True
    return run.tenant_id == getattr(user, "tenant_id", None)


def logs_task_context(db: Session, task_id: Optional[str], user=None) -> str:
    """Best-effort task metadata block for logs-chat. Returns a short
    string the model can cite, never raises.

    Two id namespaces:
      - "wfrun:<id>" — a WorkflowRun (from the run-details panel):
        summarise run name, status, timings, the run-level error, each
        node's name + status + error_message, and the tail of the
        orchestrator log so the model has a real account of the run.
      - "<int>"      — an ApiTaskQueue row (system/build task).
    """
    if not task_id:
        return ""
    try:
        # Workflow-run namespace from the run-details LLM panel. Without
        # this branch the model only saw "(no DB row found)" and made up
        # a cause — the bug this fix addresses.
        if task_id.startswith("wfrun:"):
            raw = task_id.split(":", 1)[1]
            if not raw.isdigit():
                return ""
            run = crud.get_workflow_run_by_id(db, int(raw))
            if run is None:
                return f"RUN CONTEXT: run_id={raw} (not found)"
            # Tenant-scope: never expose another tenant's run to a
            # non-platform-admin asker.
            if user is not None and not getattr(user, "is_platform_admin", False):
                if run.tenant_id != getattr(user, "tenant_id", None):
                    return ""
            run_status = run.status.value if run.status else "?"
            wf_name = ""
            try:
                if run.workflow is not None:
                    wf_name = run.workflow.name or ""
            except Exception:  # noqa: BLE001 — context must never fail the turn
                wf_name = ""
            head = (f"RUN CONTEXT: run_id={run.id} "
                    f"workflow={wf_name or '(unknown)'} "
                    f"run_name={run.run_name or wf_name or '(unnamed run)'} "
                    f"status={run_status}")
            lines = [head]
            if run.started_at:
                lines.append(f"STARTED: {run.started_at.isoformat()}")
            if run.completed_at:
                lines.append(f"COMPLETED: {run.completed_at.isoformat()}")
            if run.started_at and run.completed_at:
                try:
                    dur = (run.completed_at - run.started_at).total_seconds()
                    lines.append(f"DURATION_S: {round(dur, 2)}")
                except Exception:  # noqa: BLE001
                    pass
            if run.error_message:
                lines.append(f"RUN ERROR: {run.error_message[:600]}")
            node_lines = []
            any_node_error = False
            any_node_started = False
            for nr in (run.node_runs or [])[:_RUN_NODE_ROWS_MAX]:
                nstatus = nr.status.value if nr.status else "?"
                if nstatus not in ("queued", "pending"):
                    any_node_started = True
                gid = nr.workflow_node.graph_node_id if nr.workflow_node else nr.id
                label = gid
                try:
                    nd = (nr.workflow_node.node_definition
                          if nr.workflow_node else None)
                    if nd is not None and getattr(nd, "name", None):
                        label = f"{gid} ({nd.name})"
                except Exception:  # noqa: BLE001
                    label = gid
                line = f"  - node={label} status={nstatus}"
                if nr.started_at:
                    line += f" started={nr.started_at.isoformat()}"
                if nr.completed_at:
                    line += f" completed={nr.completed_at.isoformat()}"
                if nr.error_message:
                    any_node_error = True
                    line += f" error={nr.error_message[:300]}"
                node_lines.append(line)
            if node_lines:
                lines.append("NODES:")
                lines.extend(node_lines)
            tail = _run_log_tail_lines(db, run.id)
            if tail:
                lines.append("RUN LOG (last lines, newest last):")
                lines.extend(tail)
            # The classic "run failed but every node still queued" case:
            # the failure happened before node execution (scheduling /
            # orchestration), so there is no node-level error to cite.
            # Tell the model that explicitly so it doesn't invent one.
            if (run_status == "failed" and not run.error_message
                    and not any_node_error and not any_node_started):
                lines.append(
                    "NOTE: run is failed but no node started and no node- "
                    "or run-level error was recorded — the failure occurred "
                    "before node execution (scheduling/dispatch). There is "
                    "no captured error line to quote; see ORCHESTRATOR "
                    "EVENTS in the log excerpt if present."
                )
            return "\n".join(lines)

        # Default: ApiTaskQueue row (system / build tasks).
        task = crud.get_api_task_by_id(db, int(task_id) if task_id.isdigit() else None)
        if task is None:
            return f"TASK CONTEXT: task_id={task_id} (no DB row found)"
        payload = task.task_payload or {}
        block = (
            f"TASK CONTEXT: id={task.id} node_kind={payload.get('node_kind')} "
            f"status={task.status.value if task.status else '?'} "
            f"queued_at={task.created_at.isoformat() if task.created_at else '?'}"
        )
        if getattr(task, "error_message", None):
            block += f"\nTASK ERROR: {str(task.error_message)[:600]}"
        return block
    except Exception:
        return ""


def is_wizard_agentic_loop_enabled_for_tenant(db, tenant_id: int) -> bool:
    """workflow-agentic-execution-loop §wizard-agentic-loop — read the
    per-tenant `wizard_agentic_loop_enabled` flag. Defaults to False
    so the legacy Builder+Critic wizard runs for tenants who haven't
    opted in. Defensive: missing column / unknown tenant → False."""
    if not tenant_id:
        return False
    try:
        t = crud.get_tenant_by_id(db, tenant_id)
        if t is None:
            return False
        return bool(getattr(t, "wizard_agentic_loop_enabled", False))
    except Exception:
        return False


def fire_auto_escalation_notification(
    db: Session,
    sess: models.SupportChatSession,
) -> Optional[str]:
    """Email the session transcript to the tenant admin(s) when
    escalation fires. Returns the comma-joined recipients on success
    so the bot's reply can quote them honestly; returns None on every
    failure mode (no recipients, send failure, etc.) so the
    prompt-template picks the truthful "no email was sent" variant.

    Routing:
      - Primary recipients: tenant admins of the session's tenant
        (parent_user_id IS NULL, active). They own customer service
        for their own users.
      - CC: SUPPORT_EMAIL if configured. Gives platform ops
        visibility without making them the audience of record.
      - Fallback: if a tenant has NO active admin (orphaned), the
        SUPPORT_EMAIL inbox becomes the primary so the escalation
        isn't silently dropped.

    Reuses `send_chat_transcript_email` — the same plumbing the
    user-initiated /support/email-conversation flow uses — so the
    email shape stays consistent regardless of trigger.

    Deferred imports of ``_get_tenant_admin_emails`` / ``SUPPORT_EMAIL``
    / ``_resolve_email_tenant`` / ``_escape_html`` from
    ``routers.support_chat._shared`` (rather than their ultimate source
    in ``utils.support_escalation`` / this module) so a test that
    monkeypatches those module-level names in ``_shared`` still
    intercepts them here.
    """
    from routers.support_chat._shared import (
        SUPPORT_EMAIL as _SUPPORT_EMAIL,
        _get_tenant_admin_emails,
        _resolve_email_tenant,
        _escape_html,
    )

    tenant_admin_emails = _get_tenant_admin_emails(db, sess.tenant_id)
    if not tenant_admin_emails and not _SUPPORT_EMAIL:
        logger.info(
            "support_chat: no tenant admin emails and no SUPPORT_EMAIL — "
            "auto-escalation for session=%s flagged in DB but no email sent.",
            sess.id,
        )
        return None

    msgs = crud.list_session_messages_id_asc(db, sess.id)
    transcript_html_lines: List[str] = []
    transcript_text_lines: List[str] = []
    for m in msgs:
        if m.role not in ("user", "assistant"):
            continue
        who = "User" if m.role == "user" else "Assistant"
        # Tag user messages with their detected tone so the support
        # tech sees the trajectory at a glance.
        tone_tag = (
            f" <em>[tone: {_escape_html(m.tone)}]</em>"
            if m.role == "user" and m.tone else ""
        )
        transcript_html_lines.append(
            f'<p><strong>{who}:</strong>{tone_tag}<br>'
            f'{_escape_html(m.content)}</p>'
        )
        tone_txt = f" [tone: {m.tone}]" if m.role == "user" and m.tone else ""
        transcript_text_lines.append(f"{who}{tone_txt}: {m.content}")

    transcript_html = (
        f"<p><strong>This session was auto-escalated.</strong><br>"
        f"Session ID: <code>{_escape_html(sess.id)}</code><br>"
        f"Tenant ID: {sess.tenant_id}<br>"
        f"User ID: {sess.user_id}<br>"
        f"Frustration score: {sess.frustration_score}<br>"
        f"Triggered at: {sess.auto_escalated_at.isoformat() if sess.auto_escalated_at else '—'}"
        f"</p><hr>"
        + "\n".join(transcript_html_lines)
    )
    transcript_text = (
        f"This session was auto-escalated.\n\n"
        f"Session ID: {sess.id}\n"
        f"Tenant ID:  {sess.tenant_id}\n"
        f"User ID:    {sess.user_id}\n"
        f"Frustration score: {sess.frustration_score}\n"
        f"Triggered at: {sess.auto_escalated_at.isoformat() if sess.auto_escalated_at else '—'}\n\n"
        + "\n\n".join(transcript_text_lines)
    )

    # Pick the primary recipient list. Tenant admins are the
    # audience of record; SUPPORT_EMAIL is a CC for platform-ops
    # visibility. If the tenant has no active admin, fall back to
    # SUPPORT_EMAIL as the primary so the escalation isn't dropped.
    primary_recipients = tenant_admin_emails or ([_SUPPORT_EMAIL] if _SUPPORT_EMAIL else [])
    if not primary_recipients:
        return None
    cc_support = bool(_SUPPORT_EMAIL) and tenant_admin_emails  # only CC when admins ARE the primary

    sent_to: List[str] = []
    try:
        from utils.email_sender import send_chat_transcript_email
        email_tenant = _resolve_email_tenant(db, sess.tenant_id)
        # One send per tenant admin so each gets a personally-addressed
        # email (Reply-To behaves correctly, no privacy leak of other
        # admins' addresses). CC SUPPORT_EMAIL on each so platform ops
        # see a single thread per session, not one per admin.
        for admin_email in primary_recipients:
            ok = send_chat_transcript_email(
                db=db,
                tenant_id=email_tenant,
                recipient_email=admin_email,
                recipient_name="Tenant admin",
                session_id=sess.id,
                transcript_html=transcript_html,
                transcript_text=transcript_text,
                note=(
                    f"[ESCALATED] Tenant {sess.tenant_id} user "
                    f"{sess.user_id} — frustration score "
                    f"{sess.frustration_score}. Bot promised escalation "
                    f"or the conversation tripped the auto-escalate gate."
                ),
                cc_support=cc_support,
                support_email=_SUPPORT_EMAIL or None,
            )
            if ok:
                sent_to.append(admin_email)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "support_chat: send_chat_transcript_email failed for "
            "session=%s: %s", sess.id, e,
        )
        return None

    if not sent_to:
        return None

    # Audit so admins can pull a list of recent auto-escalations.
    try:
        from audit import log_permission_audit
        from models import AuditAction
        log_permission_audit(
            db=db,
            action=AuditAction.SUPPORT_CHAT_EMAILED,
            actor_user_id=sess.user_id,
            tenant_id=sess.tenant_id,
            resource_type="support_chat_session",
            resource_id=0,
            metadata={
                "session_id": sess.id,
                "trigger": "auto_escalation",
                "frustration_score": int(sess.frustration_score or 0),
                "recipients": sent_to,
                "cc_support": _SUPPORT_EMAIL if cc_support else None,
            },
        )
    except Exception:
        pass

    return ", ".join(sent_to)

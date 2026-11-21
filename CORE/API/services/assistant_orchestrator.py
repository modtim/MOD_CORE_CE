"""MOD Assistant — orchestrator (mod-assistant-agent §5).

Replaces the §8 canned-response stub with the real LLM tool-use loop.
Reuses the existing ai_config infrastructure: the 'assistant' AI tool
slot (Settings → AI Configuration → AI Assistant) supplies the model,
base URL, and API key. Per-tenant `AssistantTenantConfig.model_id`
overrides the tool's default model. When no model is configured at
all the orchestrator falls back to the canned response pool so the
panel UI still works pre-launch.

Provider contract: OpenAI-compatible chat completions with tool
calling (`tools=[{type:function,...}]` + `tool_calls` in the
delta stream). Covers OpenAI, vLLM, Groq, Together, and the
Anthropic OpenAI-compat proxy. Anthropic-native (`/v1/messages`)
tool use is a follow-up.

Tools registered:
  - lookup_site_map(query)          → site-map entries relevant to a query
  - retrieve_docs(query, k=5)       → top-k passages from mod-docs RAG
  - fetch_logs(since_hours, level,  → recent logs (tenant-scoped, audited)
                source, limit)

Output SSE events (consumed by §8's panel):
  session       — { session_id }                  (first event)
  text-token    — { text }                        (streamed user-visible text)
  tool-call     — { name, args }                  (informational; one per tool fire)
  action-emit   — { action, url?, id?, ... }      (validated against site map)
  citation      — { handle, title, url }          (from retrieve_docs hits)
  done          — {}
  error         — { message }
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import (Any, AsyncIterator, Callable, Dict, List, Optional,
                    Tuple,)

import httpx
from sqlalchemy.orm import Session

import models
from services import assistant_confirm
from utils import (
    assistant_actions,
    assistant_help_rag,
    assistant_logs,
    assistant_site_map,
    embeddings,
    user_vector_store,
)
# BYO base URLs are stored AS TYPED (utils/tool_config.py:161 passes the
# persisted `base_url` through), and an OpenAI-style form entry is naturally
# `http://host:port/v1`. One shared normaliser (utils/llm_client.py) strips it
# so every caller that appends `/v1/chat/completions` ends up with exactly one
# `/v1` — the doubled form 404s on llama.cpp ("File Not Found").
from utils.llm_client import normalize_openai_base_url

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 6
DEFAULT_TIMEOUT_S = 120
LLM_TOOL_NAME = "assistant"  # the ai_config tool slot we resolve against

# Action vocabulary lives in utils.assistant_actions per §6. Kept as
# an alias here so any pre-§6 import keeps working.
_VALID_ACTIONS = assistant_actions.ACTION_VOCABULARY


class OrchestratorError(RuntimeError):
    """Surfaced into the SSE stream as an `error` event; never lets a raw
    upstream stack trace reach the client."""


# ---------------------------------------------------------------------------
# Tool registry — each entry is the JSON schema the model sees + the Python
# implementation we run when it picks the tool. Implementations are *not*
# async because the underlying helpers are sync; the orchestrator runs them
# inline between LLM iterations.
# ---------------------------------------------------------------------------


def _tool_lookup_site_map(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Return a tenant-filtered slice of the site map most relevant to a
    natural-language query. v1 returns the full page list — the search
    smarts can land later; the model is best positioned to pick from
    a grounded list.
    """
    site_map = assistant_site_map.get_map()
    pages = [
        {
            "display_name": p.display_name,
            "url": p.url,
            "app": p.app,
            "summary": p.summary,
            "dialogs": [{"id": d.id, "display_name": d.display_name} for d in p.dialogs],
        }
        for p in site_map.pages
    ]
    return {"pages": pages, "query": args.get("query", "")}


def _tool_retrieve_docs(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Top-k passages from the docs RAG, formatted for citation-friendly
    consumption. Hands back a `citation_handle` per passage so the model
    can emit citations that the orchestrator turns into citation SSE
    events without round-tripping the URL through the model's text.
    """
    query = args.get("query") or ""
    k = int(args.get("k") or 5)
    hits = assistant_help_rag.retrieve(query, k=k)
    citations = []
    for i, h in enumerate(hits):
        handle = f"doc:{i + 1}"
        citations.append({
            "handle": handle,
            "title": h["title"],
            "url": h["url"],
            "snippet": h["snippet"],
            "score": h["score"],
        })
    # Stash for later — when the model emits the handle in its final
    # message we look up the matching record and stream the citation.
    ctx.setdefault("_citations", {})
    for c in citations:
        ctx["_citations"][c["handle"]] = c
    return {"passages": citations}


# Per-snippet char cap for tenant-KB passages handed to the model. Mirrors the
# docs-RAG snippet sizing (utils.assistant_help_rag) so KB and doc passages
# arrive at comparable length.
_KB_SNIPPET_CHARS = 800
# Minimum cosine similarity for a prefetched KB passage to be injected into the
# turn. Above this, the KB is treated as covering the question and the model is
# told to answer from it; below, nothing is injected and normal MOD behavior
# holds. Tuned against all-MiniLM-L6-v2 cosine scores.
_KB_PREFETCH_MIN_SCORE = 0.30


def _tool_retrieve_company_knowledge(
    db: Session, tenant_id: int, query: str, k: int = 5,
) -> List[Dict[str, Any]]:
    """Fail-closed retrieval from THIS tenant's Knowledge Base.

    Embeds `query` with the local embedding model and queries the tenant's own
    vector collection (t{tenant_id}_vectors) filtered to payload source=="kb".
    KB points share that collection with workflow-node vectors, so the
    source=="kb" filter is load-bearing — without it the assistant could
    surface arbitrary workflow vectors. tenant_id is ALWAYS the authenticated
    principal's; it is never taken from the model's arguments.

    Returns [{title, snippet, score}] mirroring the retrieve_docs snippet shape
    so the citation path is identical. On ANY failure (embedding model or vector
    store unavailable, transient dim mismatch) returns [] so the assistant falls
    back to its other tools — it NEVER raises and NEVER substitutes global or
    other-tenant data.
    """
    if not query or not query.strip():
        return []
    try:
        vector = embeddings.embed_query(query.strip())
        hits = user_vector_store.query(
            tenant_id, vector, top_k=int(k or 5), where={"source": "kb"},
        )
    except user_vector_store.VectorStoreUnavailable as e:
        logger.warning(
            "assistant: company-knowledge retrieval unavailable (tenant=%s): %s",
            tenant_id, e,
        )
        return []
    except Exception as e:
        logger.warning(
            "assistant: company-knowledge retrieval failed (tenant=%s): %s",
            tenant_id, e,
        )
        return []
    results: List[Dict[str, Any]] = []
    for h in hits:
        payload = h.get("payload") or {}
        text = payload.get("text") or ""
        snippet = text if len(text) <= _KB_SNIPPET_CHARS else text[:_KB_SNIPPET_CHARS].rstrip() + "..."
        results.append({
            "title": payload.get("title") or "Untitled",
            "snippet": snippet,
            "score": float(h.get("score") or 0.0),
        })
    return results


def _tool_retrieve_company_knowledge_call(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Registry entry point for `retrieve_company_knowledge`. Mirrors
    `_tool_retrieve_docs`: it hands back a `citation_handle` per passage so KB
    hits ride the identical citation SSE path as docs hits. tenant_id comes from
    the authenticated user in ctx, never from `args`. Refuses (returns no
    passages) when the tenant's Knowledge Base is disabled — belt-and-suspenders
    for the schema-level gating in stream_chat.
    """
    if not ctx.get("knowledge_base_enabled", False):
        return {"passages": []}
    db: Session = ctx["db"]
    current_user: models.User = ctx["current_user"]
    query = args.get("query") or ""
    k = int(args.get("k") or 5)
    hits = _tool_retrieve_company_knowledge(db, current_user.tenant_id, query, k)
    citations = []
    for i, h in enumerate(hits):
        handle = f"kb:{i + 1}"
        citations.append({
            "handle": handle,
            "title": h["title"],
            "url": "",
            "snippet": h["snippet"],
            "score": h["score"],
        })
    ctx.setdefault("_citations", {})
    for c in citations:
        ctx["_citations"][c["handle"]] = c
    return {"passages": citations}


def _tool_fetch_logs(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Tenant-scoped log fetch. Honours the per-tenant
    logs_context_enabled toggle: when off, returns an empty payload +
    a hint string the model can read out to the user.
    """
    db: Session = ctx["db"]
    current_user: models.User = ctx["current_user"]
    if not ctx.get("logs_context_enabled", True):
        return {
            "rows": [],
            "disabled_hint": (
                "Logs context is disabled for this tenant. Ask the "
                "tenant admin to enable it under Settings → AI "
                "Configuration → Agent."
            ),
        }
    since_hours = int(args.get("since_hours") or 24)
    rows = assistant_logs.fetch(
        db,
        current_user,
        since_seconds=since_hours * 3600,
        level=str(args.get("level") or "all"),
        source=str(args.get("source") or "all"),
        limit=int(args.get("limit") or 200),
        session_id=ctx.get("session_id"),
    )
    return {"rows": rows}


# Per-tool JSON schema the model receives. Mirrors OpenAI's
# `tools=[{type:function, function:{name,description,parameters}}]`
# shape.
_TOOLS_FOR_MODEL: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lookup_site_map",
            "description": (
                "Return the platform's site map. Use this when the user "
                "wants to go somewhere or you need to ground a "
                "`navigate` / `open-dialog` action in a real URL or "
                "dialog id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What the user is looking for",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_docs",
            "description": (
                "Retrieve relevant passages from MOD's documentation. "
                "Use this to answer 'how do I X' / 'what does Y do' / "
                "'why is Z happening' style questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {
                        "type": "integer",
                        "description": "Number of passages to return (1-10)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_logs",
            "description": (
                "Fetch recent tenant-scoped logs (api/worker/workflow). "
                "Use this for troubleshooting questions. NEVER pretend "
                "to know about runs you haven't fetched logs for."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "since_hours": {"type": "integer", "default": 24},
                    "level": {
                        "type": "string",
                        "enum": ["info", "warn", "error", "all"],
                        "default": "all",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["api", "worker", "workflow", "all"],
                        "default": "all",
                    },
                    "limit": {"type": "integer", "default": 200},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contact_support",
            "description": (
                "Escalate to a human. Emails the user + their tenant "
                "admins a transcript of this conversation with your "
                "summary as the cover note. Call this when the user "
                "EXPLICITLY asks for human / live / real support, or "
                "when you've genuinely failed to resolve their problem "
                "(repeated tool failures, doc gap on a critical issue, "
                "clear ongoing frustration in their messages). Don't "
                "use this for first-line questions you can answer from "
                "the docs. Set severity=\"emergency\" only when the "
                "issue is service-down / data-loss / security — "
                "that adds SUPPORT_EMAIL to the recipients."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": (
                            "Short one-line subject the support inbox "
                            "will see first. Lead with the problem, not "
                            "with 'support request'."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "2-5 sentences summarising the user's issue "
                            "and what you've already tried. Plain text. "
                            "The full transcript is attached "
                            "automatically — don't repeat it here."
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["normal", "emergency"],
                        "default": "normal",
                        "description": (
                            "Use 'emergency' ONLY for service-down / "
                            "data-loss / active-security cases. "
                            "Everything else is 'normal'."
                        ),
                    },
                },
                "required": ["subject", "message"],
            },
        },
    },
]


def escalate_assistant_session(
    db: Session,
    *,
    user: "models.User",
    session_id: Optional[str],
    subject: str,
    message: str,
    severity: str = "normal",
    via: str = "assistant",
) -> Dict[str, Any]:
    """Shared escalation entry point — used by both the LLM-driven
    `contact_support` tool below AND the POST /assistant/escalate
    endpoint that backs the panel footer's "Contact human support"
    link. Single source of truth so the LLM path and the explicit
    user-click path can't drift.

    Pulls the session's AssistantMessage history, renders a transcript
    via utils.support_escalation.render_transcript, ships it through
    send_support_escalation, audits, and returns the result envelope
    (no `tool_summary` — that's the tool wrapper's responsibility).
    """
    from utils import support_escalation
    subject = (subject or "").strip() or "Support request from MOD assistant"
    message = (message or "").strip()
    severity_l = (severity or "normal").strip().lower()
    if severity_l not in ("normal", "emergency"):
        severity_l = "normal"

    try:
        import uuid as _uuid
        sid_uuid = _uuid.UUID(session_id) if session_id else None
    except (ValueError, TypeError):
        sid_uuid = None

    history_msgs: List[Dict[str, Any]] = []
    if sid_uuid is not None:
        try:
            rows = (
                db.query(models.AssistantMessage)
                  .filter(models.AssistantMessage.session_id == sid_uuid)
                  .order_by(models.AssistantMessage.created_at.asc())
                  .all()
            )
            history_msgs = [
                {"role": r.role, "content": r.content or ""}
                for r in rows if r.role in ("user", "assistant")
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("escalate_assistant_session: history fetch failed: %s", e)

    transcript_html, transcript_text = support_escalation.render_transcript(history_msgs)

    note_lines = [f"Subject: {subject}", ""]
    if message:
        note_lines.append(message)
    if severity_l == "emergency":
        note_lines.insert(0, "[URGENT — emergency severity flagged by user via assistant]")
        note_lines.insert(1, "")
    cover_note = "\n".join(note_lines).strip()

    result = support_escalation.send_support_escalation(
        db=db,
        user=user,
        session_label=f"assistant-{session_id}" if session_id else "assistant",
        transcript_html=transcript_html,
        transcript_text=transcript_text,
        note=cover_note,
        cc_support=(severity_l == "emergency"),
        origin="mod-assistant",
    )

    try:
        from audit import log_permission_audit
        from models import AuditAction
        log_permission_audit(
            db=db, action=AuditAction.SUPPORT_CHAT_EMAILED,
            actor_user_id=user.id, tenant_id=user.tenant_id,
            resource_type="assistant_session", resource_id=0,
            metadata={
                "session_id": session_id or "",
                "to": result.get("recipient", ""),
                "severity": severity_l,
                "origin": "mod-assistant",
                "subject": subject,
                # Audit rows written by an assistant
                # tool call carry `via` so an auditor can tell an assistant-driven
                # action from the same action taken by hand. No enum change.
                "via": via,
            },
        )
    except Exception:
        pass

    # Echo the resolved severity back so callers can render the right
    # badge without re-normalising.
    result["severity"] = severity_l
    result["subject"] = subject
    return result


def _tool_contact_support(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """LLM-facing wrapper around `escalate_assistant_session`. The
    model decides when to invoke this (explicit request, clear
    frustration, repeated failures) and what to say in `subject` /
    `message` / `severity`.

    Stashes the result on ctx so the orchestrator can auto-emit a
    `support-contacted` action receipt downstream, and adds a
    `tool_summary` field the model can read out to the user.
    """
    result = escalate_assistant_session(
        db=ctx["db"],
        user=ctx["current_user"],
        session_id=ctx.get("session_id") or "",
        subject=args.get("subject", ""),
        message=args.get("message", ""),
        severity=args.get("severity", "normal"),
    )
    ctx["_last_support_escalation"] = {
        "ok": result["ok"],
        "recipient": result.get("recipient", ""),
        "tenant_admin_count": result.get("tenant_admin_count", 0),
        "severity": result.get("severity", "normal"),
        "subject": result.get("subject", ""),
    }
    return {
        "ok": result["ok"],
        "tool_summary": (
            f"Support contacted (severity={result.get('severity', 'normal')}). "
            f"Transcript emailed to {result.get('recipient', '(unknown)')}; "
            f"{result.get('tenant_admin_count', 0)} tenant admin(s) notified."
            if result["ok"]
            else f"Could not contact support: {result.get('message', 'unknown error')}."
        ),
        **result,
    }


# Schema for the tenant Knowledge Base tool. NOT a member of _TOOLS_FOR_MODEL:
# it is only appended to the model's tool list in stream_chat when the tenant's
# AssistantTenantConfig.knowledge_base_enabled is true (D8). Kept alongside
# retrieve_docs so the two retrieval surfaces read together.
_KB_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "retrieve_company_knowledge",
        "description": (
            "Retrieve relevant passages from THIS company's own Knowledge "
            "Base — the tenant's uploaded documents, pasted notes, and "
            "indexed folders (e.g. their production bible, internal "
            "processes, style guides, SOPs, project-specific docs). Use this "
            "for questions about how THIS company does things or about their "
            "own materials — NOT for questions about MOD the product (use "
            "retrieve_docs for those). Answer inline from the returned "
            "snippets and cite them via citation_handles."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {
                    "type": "integer",
                    "description": "Number of passages to return (1-10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}


_TOOL_IMPLS = {
    "lookup_site_map": _tool_lookup_site_map,
    "retrieve_docs": _tool_retrieve_docs,
    "retrieve_company_knowledge": _tool_retrieve_company_knowledge_call,
    "fetch_logs": _tool_fetch_logs,
    "contact_support": _tool_contact_support,
}


# ---------------------------------------------------------------------------
# Provider config resolution.
# ---------------------------------------------------------------------------


def _resolve_env(tenant_cfg: Optional[models.AssistantTenantConfig]) -> Dict[str, str]:
    """Pull the assistant tool's effective env (provider, model, base_url,
    api_key, ...) and overlay any per-tenant overrides.
    """
    from routers.ai_config import _resolved_effective_env
    env = dict(_resolved_effective_env(LLM_TOOL_NAME))
    if tenant_cfg is not None and tenant_cfg.model_id:
        env["LLM_MODEL"] = tenant_cfg.model_id
    return env


def _has_provider_configured(env: Dict[str, str]) -> bool:
    return bool((env.get("LLM_BASE_URL") or "").strip()) and bool((env.get("LLM_MODEL") or "").strip())


def _bound_stack_asleep(db: Session, res: Dict[str, Any]) -> bool:
    """ai-config-stack-binding 4.1 — is the bound stack ASLEEP (design D4)?

    "asleep" is the platform's own vocabulary (:func:`routers.persistent_stacks
    _shared._derive_stack_state`: ``desired_state=stopped`` / otherwise not
    running): the sleep POLICY turned it off, so the slot's job is to start IT
    again (Q-AI1 = B — the binding is the truth, never another model). A stack
    that is merely not serving while still DESIRED running (an operator stop
    that has not settled, a crashed container, a never-deployed/torn-down row)
    is not the platform's sleep — that stays the named ``stack_stopped`` error
    the operator acts on, and nothing here dispatches a wake for it.
    """
    target = res.get("target") or {}
    stack_id = target.get("stack_id")
    if not stack_id:
        return False
    try:
        from crud.persistent_stacks_domain import get_stack_by_id
        from utils.accelerator_fit import ACTIVE_STACK_STATUSES
        row = get_stack_by_id(db, int(stack_id))
        if row is None or (getattr(row, "status", None) in ACTIVE_STACK_STATUSES):
            return False
        if getattr(row, "desired_state", None) != "stopped":
            return False
        # Never-deployed / torn-down: `_wake` is out of scope for those
        # (deploy mechanics belong to the family entry point / reconciler).
        return (getattr(row, "status", None)
                not in ("torn_down", "created", "failed", "provisioning_failed"))
    except Exception:  # noqa: BLE001 — a diagnostic read never breaks the turn
        return False


def _slot_failure_cause(db: Session,
                        tenant_id: Optional[int] = None, *,
                        on_progress: Optional[Callable[[str], None]] = None,
                        env_out: Optional[Dict[str, str]] = None) -> Optional[str]:
    """ai-config-stack-binding 3.2 — the resolver's sentence for why the
    assistant slot will not serve, for EVERY non-`ready` outcome.

    `_resolved_effective_env` (via :func:`_resolve_env`) degrades a failed
    resolution to `{}` — that is its contract, so the dict can never leak a
    diagnostic key into a request/task env. The CAUSE lives in the typed
    resolver (:func:`services.ai_slot_resolver.resolve_slot`), which is what
    every consumer that reports to a human must call directly:
    ``no_binding | stack_stopped | stack_missing | unreachable | waking``
    (plus the unknown-target-kind ``no_binding``). Each message already names
    the slot and the cause (stack name + status, or ``host:port``) and the
    remedy; the fallback chain's other causes are appended so "primary
    stopped, fallback missing" is one read.

    Returns ``None`` only when the slot unexpectedly resolves ``ready`` or the
    resolver cannot speak at all (no table / resolver raised) — the sole case
    where the pre-launch canned pool is still the honest degrade.
    """
    try:
        from services.ai_slot_resolver import REMEDY, resolve_slot
        res = resolve_slot(db, LLM_TOOL_NAME, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — a diagnostic must not break the turn
        return None
    if (res.get("outcome") or "") == "ready":
        return None
    # ai-config-stack-binding 4.1 (design D4) — a bound stack that is ASLEEP is
    # not a failure: someone already picked it, so the slot's job is to START IT
    # and answer, not to tell the operator to go pick another model (Q-AI1 = B).
    # This is the ONE surface that drives `wake=True`: the resolver's wake
    # branch is opt-in and nothing else in the routers ever asks for it, so the
    # documented "wake progress, then the answer" leg was unreachable. The wake
    # itself is the existing one (`services.ai_slot_wake.wake_for_slot` ->
    # `services.stack_wake.ensure_stack_ready(wait=True)`), reached through the
    # resolver; this caller only supplies the feature's own progress channel
    # ("Starting the AI model…", streamed as an SSE `progress` event) and picks
    # up the env the woken stack now serves.
    if (res.get("outcome") or "") == "stack_stopped" and _bound_stack_asleep(
            db, res):
        res = resolve_slot(db, LLM_TOOL_NAME, tenant_id=tenant_id, wake=True,
                           on_progress=on_progress)
        if (res.get("outcome") or "") == "ready":
            if env_out is not None and res.get("env"):
                env_out.update(res["env"])
            return None
    message = (res.get("message") or "").strip()
    attempts = res.get("attempts") or []
    if len(attempts) > 1:
        others = [(a.get("message") or "").replace(REMEDY, "").strip()
                  for a in attempts[1:]]
        others = [o for o in others if o]
        if others and message:
            message = f"{message} Fallbacks: {' '.join(others)}"
    if (res.get("outcome") or "") == "no_binding":
        # Nothing in the binding table for this slot, so the empty env above
        # came from the transitional Vault blob (design D5) — name THAT
        # stack's failure when there is one (GE.AIC.2's diagnostic).
        try:
            from routers.ai_config import _stack_provider_error
            legacy = _stack_provider_error(LLM_TOOL_NAME)
        except Exception:  # noqa: BLE001
            legacy = None
        if legacy:
            message = legacy
    return message or None


def _r024_gate(db: Session, tenant_id: Optional[int], surface: str,
               byte_count: Optional[int] = None,
               workflow_id: Optional[int] = None,
               run_id: Optional[int] = None,
               file_ids: Optional[tuple] = None) -> Dict[str, Any]:
    """GSEC R-024 — refuse-before-serve for external LLM bindings.

    Thin wrapper over `routers.ai_config.resolve_llm_data_routing`. The
    binding is classified from the ACTUAL provider seam
    (`_resolved_effective_env`), the same dict the LLM call below is built
    from — NOT from any env a caller happens to hold, so the decision
    tracks the ground-truth binding this request is served by. If the
    tenant's assistant binding is EXTERNAL and the tenant has not opted in,
    the turn is refused (the message names the setting). GY.C7 — the same
    seam also refuses when any referenced asset (workflow/run/file -> cogs)
    carries a restricted label (restricted/CUI/EAR/ITAR), independent of
    opt-in. Local / MOD-hosted bindings always pass. The routing audit
    event is recorded by the resolver (metadata only, never content)."""
    from routers.ai_config import _resolved_effective_env, \
        resolve_llm_data_routing
    env = _resolved_effective_env(LLM_TOOL_NAME) or {}
    return resolve_llm_data_routing(
        db, tenant_id, env, surface=surface, tool=LLM_TOOL_NAME,
        byte_count=byte_count, workflow_id=workflow_id, run_id=run_id,
        file_ids=file_ids,
    )


# GSEC R-024 — redaction of log/error text bound for an EXTERNAL provider.
# Reuses the platform's existing secret patterns (utils.secret_redaction)
# and adds a generic `key=value` secret catch (any `*key*`/`*secret*`/
# `*token*`/`*password*`/`*credential*` name) so a log line like
# `DB_PASSWORD=hunter2` or `--api-key sk-abc123` is scrubbed too.
_R024_EXTRA_PATTERNS = [
    # key=value / "key": "value" secrets with a generic sensitive name
    (r'(?i)([\w.-]*(?:key|secret|token|password|passwd|credential)[\w.-]*)'
     r'(["\']?\s*[=:]\s*["\']?)'
     r'([^"\',\s\)\] }]{4,})',
     r'\1\2<REDACTED>'),
    # bare high-entropy bearer-looking strings (sk-…, ghp_…, AKIA…)
    (r'\b(?:sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9_\-]{16,}|'
     r'xox[baprs]-[A-Za-z0-9\-]{8,})\b', '<REDACTED_TOKEN>'),
]


def _redact_for_external(obj: Any) -> Any:
    """GSEC R-024 — scrub bearer tokens, JWTs, AWS keys and key=value
    secrets out of a (possibly nested) tool result before it is handed to an
    EXTERNAL LLM provider. Recurses dicts/lists and redacts every string
    leaf. Never raises — a redaction failure returns the input unchanged
    rather than breaking the turn."""
    try:
        from utils import secret_redaction as _sr
        _base = list(getattr(_sr, "SENSITIVE_PATTERNS", []))
        _patterns = [(re.compile(p), rep) for p, rep in
                     _base + _R024_EXTRA_PATTERNS]
    except Exception:  # noqa: BLE001 — fall back to just the extra patterns
        _patterns = [(re.compile(p), rep) for p, rep in _R024_EXTRA_PATTERNS]

    def _scrub(text: str) -> str:
        if not text:
            return text
        for pat, rep in _patterns:
            text = pat.sub(rep, text)
        return text

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            return _scrub(node)
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [_walk(v) for v in node]
        return node

    try:
        return _walk(obj)
    except Exception:  # noqa: BLE001
        return obj


# ---------------------------------------------------------------------------
# System prompt.
# ---------------------------------------------------------------------------


def _build_system_prompt(current_page: Optional[str], tenant_id: int,
                         kb_enabled: bool = False) -> str:
    """Static system prompt with the user's current page + tenant id
    interpolated in. Kept as a function (not a .format() template)
    because the body contains literal JSON examples whose `{...}` would
    collide with .format() placeholders.

    When ``kb_enabled`` the tenant has loaded their OWN documents into a
    private Knowledge Base — which may cover ANY subject, not just MOD — so
    the prompt stops hard-refusing off-MOD questions and instead tells the
    model to consult retrieve_company_knowledge first.
    """
    cp = current_page or "(unknown)"
    # Injected only when the tenant has a Knowledge Base: makes off-MOD topics
    # (e.g. an uploaded car manual) IN scope when the KB may cover them.
    kb_intro = (
        "\nCOMPANY KNOWLEDGE BASE (IMPORTANT)\n"
        "This workspace has loaded its OWN documents — manuals, SOPs,\n"
        "product or company info — into a private Knowledge Base. These may\n"
        "cover ANY subject, NOT just MOD. For ANY question that might be\n"
        "answered by the tenant's own material, CALL retrieve_company_knowledge\n"
        "FIRST and answer from what it returns, citing it. Do NOT refuse or say\n"
        "'I only help with MOD' when the Knowledge Base could cover the topic\n"
        "(for example a car manual the tenant uploaded — answer it). Only\n"
        "decline if retrieve_company_knowledge returns nothing relevant AND the\n"
        "question also has no path back to MOD.\n"
    ) if kb_enabled else ""
    # Softens the "not a general-purpose assistant" limit below when a KB exists.
    kb_scope_caveat = (
        "BEFORE applying the limit below: this workspace's Company Knowledge\n"
        "Base may cover the question. If it plausibly does, it IS in scope —\n"
        "call retrieve_company_knowledge and answer. The limit below applies\n"
        "ONLY to questions the Knowledge Base does not cover.\n"
        "\n"
    ) if kb_enabled else ""
    return (
        "You are MOD Assistant, an in-product navigation + help agent.\n"
        "You exist ONLY to help people use MOD; you are not a general chatbot.\n"
        "Stay on MOD topics, keep every reply short and grounded, and never\n"
        "paste raw errors, stack traces, or internal system details to the\n"
        "user — if something fails, say so plainly in one friendly sentence.\n"
        "\n"
        "WHAT YOU CAN DO\n"
        "- Take the user somewhere in the app by emitting actions.\n"
        "- Answer 'how do I X' questions by retrieving from MOD's docs.\n"
        "- Help troubleshoot run failures by fetching logs.\n"
        "- Escalate to a human by calling contact_support when the user\n"
        "  explicitly asks for live / real / human help, you've genuinely\n"
        "  failed to resolve their problem after a real attempt, or\n"
        "  they're clearly frustrated and the docs don't cover it.\n"
        "  Don't call it for first-line questions you can answer.\n"
        "\n"
        + kb_intro +
        "HOW TO RESPOND\n"
        "- ALWAYS respond with a short, conversational message. Be warm\n"
        "  and direct. Don't echo the user's question back.\n"
        "- For 'what is X' / 'explain Y' / definition questions, call\n"
        "  retrieve_docs and ANSWER INLINE in your own words using the\n"
        "  snippet text. Cite the source via citation_handles so the\n"
        "  panel shows a 'Source: Glossary' link, but NEVER reply with\n"
        "  'check the docs for that' as your whole answer — quote /\n"
        "  paraphrase the snippet content. Users may misspell terms\n"
        "  ('tennent', 'tenent'); if your first retrieve_docs returns\n"
        "  empty, retry with the corrected spelling before giving up.\n"
        "- If the user wants to go somewhere, CALL lookup_site_map FIRST,\n"
        "  then put your final answer as JSON inside <ACTIONS>...</ACTIONS>\n"
        '  tags on its own line. The JSON shape is:\n'
        '    {\"actions\": [{\"action\": \"navigate\", \"url\": \"/billing\"}, ...],\n'
        '     \"citation_handles\": [\"doc:1\"]}\n'
        "  CRITICAL: write the <ACTIONS>...</ACTIONS> block as PLAIN TEXT on\n"
        "  its own line. NEVER wrap it in a markdown code fence (no ```json,\n"
        "  no ```). NEVER show JSON, the word ACTIONS, or code fences in the\n"
        "  visible message — the panel renders the actions as buttons itself.\n"
        "- ONLY emit actions whose target (url / id) exists in the site\n"
        "  map. Do NOT make up URLs. If a place doesn't exist, say so.\n"
        "- ONLY cite passages whose handles you got from retrieve_docs\n"
        "  in this conversation.\n"
        "- The valid actions are: navigate, open-new-tab, open-dialog,\n"
        "  open-tab, scroll-to, highlight, chain.\n"
        "\n"
        "AMBIGUOUS INTENTS — OFFER A CHOICE\n"
        "Some user intents map to more than one good destination, and\n"
        "you SHOULD let the user pick rather than guess. Common cases:\n"
        "\n"
        "- 'make a workflow' / 'create a workflow' / 'new workflow' →\n"
        "  the user might want to compose it manually on the canvas,\n"
        "  OR they might want the Workflow Wizard to walk them through\n"
        "  it. Emit BOTH options as separate top-level actions in the\n"
        "  same `actions` array so the panel renders two buttons:\n"
        '    {\"actions\": [\n'
        '      {\"action\": \"navigate\", \"url\": \"/\"},\n'
        '      {\"action\": \"chain\", \"actions\": [\n'
        '        {\"action\": \"navigate\", \"url\": \"/\"},\n'
        '        {\"action\": \"open-dialog\", \"id\": \"workflow-wizard\"}\n'
        '      ]}\n'
        '    ]}\n'
        "  Your text should briefly explain the two paths so the user\n"
        "  knows which to click. Do NOT pick one for them.\n"
        "\n"
        "- 'make a node' / 'create a node' / 'new node' is NOT\n"
        "  ambiguous — always offer the Node Wizard (a chain navigate\n"
        "  to the WorkflowBuilder + open-dialog new-node-wizard). The\n"
        "  Node Wizard is the only supported way to author a node.\n"
        "\n"
        "  Concrete shape for 'create a node' — emit THIS (fill the real\n"
        "  WorkflowBuilder url from lookup_site_map; the dialog id is fixed):\n"
        '    {\"actions\": [{\"action\": \"chain\", \"actions\": [\n'
        '      {\"action\": \"navigate\", \"url\": \"<workflow-builder url>\"},\n'
        '      {\"action\": \"open-dialog\", \"id\": \"new-node-wizard\"}\n'
        '    ]}]}\n'
        "\n"
        "RULE — ACTIONS, NOT INSTRUCTIONS: when the user asks to DO / CREATE /\n"
        "OPEN / GO to something that exists in the site map, your reply MUST be\n"
        "the <ACTIONS> block so the panel renders a clickable button. NEVER\n"
        "answer such a request with numbered manual steps ('1. Navigate to the\n"
        "Workflow Builder 2. Click the + icon…') — the button IS the help, and\n"
        "manual steps for an actionable destination count as a FAILED answer.\n"
        "\n"
        "WHAT YOU'RE FOR\n"
        "You help people use MOD. That covers MOD itself — features,\n"
        "navigation, billing, troubleshooting a run — AND the technical\n"
        "ground someone needs to actually get work done in it: writing a\n"
        "Dockerfile for a node, choosing a base image, shaping a workflow,\n"
        "reading a build error, what a container or a GPU variant is. If a\n"
        "question plausibly comes from someone trying to accomplish\n"
        "something in MOD, it is in scope. ANSWER IT.\n"
        "\n"
        "When in doubt, HELP. Refusing a question someone genuinely needed\n"
        "answered is a worse failure than answering one you didn't have to.\n"
        "\n"
        + kb_scope_caveat +
        "What you're NOT is a general-purpose assistant. If someone asks\n"
        "for something with no path back to MOD — creative writing,\n"
        "homework, unrelated code, general trivia, drafting their email —\n"
        "say briefly and warmly that you're MOD's in-product helper, name\n"
        "one or two things you CAN help with, and stop. One short sentence,\n"
        "no lecture, no apology, no explanation of your restrictions.\n"
        "Never claim you are unable to answer; you are choosing not to,\n"
        "because it isn't what you're here for.\n"
        "\n"
        "This applies to SHORT FACTUAL QUESTIONS too, not just to tasks.\n"
        "'Who won the 1998 World Cup', 'what is the capital of Peru',\n"
        "'how many grams in an ounce' — you know these, and knowing the\n"
        "answer is NOT a reason to give it. A one-line reply is still an\n"
        "answer. The test is never 'can I?' or 'is this quick?', it is\n"
        "'does answering this help them do something in MOD?'. If no,\n"
        "redirect, however small the question looks.\n"
        "\n"
        "CURRENT CONTEXT\n"
        f"- User's current page: {cp}\n"
        f"- User's tenant id: {tenant_id}\n"
        "\n"
        "Keep responses under 4 sentences unless the user explicitly\n"
        "asks for more detail.\n"
    )


# ---------------------------------------------------------------------------
# Action extraction + validation.
# ---------------------------------------------------------------------------


# Matches the <ACTIONS> block, optionally including a markdown code fence the
# model sometimes wraps it in (```json … ```). The wrapping fence is consumed
# too so its ``` markers don't leak into the visible reply — but ONLY a fence
# directly around the block; real code blocks elsewhere are left untouched.
_ACTIONS_TAG_RE = re.compile(
    r"(?:`{3,}[a-zA-Z0-9_-]*[ \t]*\n)?"      # optional opening fence
    r"<ACTIONS>\s*(.*?)\s*</ACTIONS>"        # the block
    r"(?:[ \t]*\n?[ \t]*`{3,})?",            # optional closing fence
    re.DOTALL | re.IGNORECASE,
)

# Unambiguous "create a node" intent — matches "create/make/add/new/build/author
# … node" (e.g. "create a new node", "make a node", "add a node"). Used to
# deterministically emit the Node Wizard button instead of trusting the model.
_NODE_CREATE_RE = re.compile(
    r"\b(create|make|add|new|build|author|start)\b[^.?!\n]{0,20}\bnode\b",
    re.IGNORECASE,
)

# Navigation intent — "take me to X", "go to X", "open the X page", "show me X".
# Captures the destination phrase (group "dest") so it can be resolved against
# the site map. Used both to deterministically short-circuit a clear navigation
# request and to break the degenerate lookup_site_map loop (the weak local model
# calls lookup_site_map over and over and never emits a `navigate`).
_NAV_INTENT_RE = re.compile(
    r"\b(?:take me to|bring me to|go to|goto|navigate to|jump to|send me to|"
    r"open up|open|show me|show|view)\b\s+(?P<dest>.+?)"
    r"(?:\s+(?:page|screen|tab|section|view|please))?\s*[.?!]?\s*$",
    re.IGNORECASE,
)

# Filler tokens stripped from a captured destination phrase before matching it
# against site-map page names ("the billing page" -> "billing").
_NAV_STOPWORDS = frozenset({"the", "a", "an", "my", "our", "to", "page", "screen",
                            "tab", "section", "view", "me"})


def _resolve_navigation_target(
    message: str,
    site_map: assistant_site_map.SiteMap,
) -> Optional[Dict[str, Any]]:
    """Best-effort: resolve a natural-language navigation request to exactly one
    site-map page. Returns a validated-shape ``navigate`` action dict
    ({action, url, app, label}) or None when the intent is absent or the target
    is ambiguous / unknown (caller then falls back to the model).

    Matching is deliberately conservative — an EXACT page-name match wins; a
    single unambiguous prefix/substring match is accepted; anything ambiguous
    returns None so we never guess the wrong destination.
    """
    if not message:
        return None
    m = _NAV_INTENT_RE.search(message.strip())
    if not m:
        return None
    dest_raw = (m.group("dest") or "").strip().lower()
    # Drop filler words so "the billing page" -> "billing".
    dest = " ".join(t for t in re.split(r"[\s/]+", dest_raw) if t and t not in _NAV_STOPWORDS)
    if not dest:
        return None

    pages = site_map.pages

    def _mk(page: assistant_site_map.Page) -> Dict[str, Any]:
        return {
            "action": "navigate",
            "url": page.url,
            "app": page.app,
            "label": page.display_name,
        }

    # 1. Exact display-name match (case-insensitive).
    exact = [p for p in pages if p.display_name.lower() == dest]
    if len(exact) == 1:
        return _mk(exact[0])
    if len(exact) > 1:
        # Two apps expose the same name — prefer the dashboard one, matching
        # page_for_url's tie-break.
        dash = [p for p in exact if p.app == "dashboard"]
        return _mk(dash[0] if dash else exact[0])

    # 2. Prefix match on display name (e.g. "bill" -> "Billing").
    prefix = [p for p in pages if p.display_name.lower().startswith(dest)]
    if len(prefix) == 1:
        return _mk(prefix[0])

    # 3. Single substring match anywhere in the display name.
    substr = [p for p in pages if dest in p.display_name.lower()]
    if len(substr) == 1:
        return _mk(substr[0])

    return None


def _extract_actions_block(text: str) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """Pull the <ACTIONS> JSON block out of the model's reply, returning
    (user-visible text, actions list, citation handles list). When the
    block is missing or unparseable, returns the original text with
    empty action / citation lists.
    """
    m = _ACTIONS_TAG_RE.search(text)
    if not m:
        return text.strip(), [], []
    block = m.group(1).strip()
    visible = (text[: m.start()] + text[m.end():]).strip()
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError:
        logger.warning("assistant: action block did not parse: %s", block[:200])
        return visible, [], []
    actions = parsed.get("actions") or []
    handles = parsed.get("citation_handles") or []
    return visible, actions if isinstance(actions, list) else [], handles if isinstance(handles, list) else []


# Kept as a thin shim for callers + tests that imported it pre-§6.
# Real implementation lives in utils.assistant_actions; this just
# adapts the call signature (no allowlist).
def _validate_action(
    action: Dict[str, Any],
    site_map: assistant_site_map.SiteMap,
    allowlist: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    return assistant_actions.validate_action(action, site_map, allowlist)


def _maybe_wrap_open_dialog_with_navigate(
    action: Dict[str, Any],
    site_map: assistant_site_map.SiteMap,
    current_page: Optional[str],
    current_app: Optional[str] = None,
) -> Dict[str, Any]:
    """If `action` is an open-dialog for a dialog that isn't reachable
    from the user's `current_page`, wrap it into a chain that
    navigates to a page that DOES host the dialog first.

    `current_app` ("dashboard" | "workflow_builder") scopes the
    current-page lookup so URLs that exist in both apps (most
    notably "/") disambiguate correctly. Without it, a user on the
    Dashboard home page (location.pathname = "/") looked like the
    WorkflowBuilder canvas to the wrap function.

    Returns the input unchanged when no wrap is needed or possible.
    Matches the spec:
      Scenario: Dialog id not on current page → orchestrator SHALL
      either (a) prepend a `navigate` to a page where the dialog
      exists, packaged as a `chain`, or (b) reject if no such
      navigation is reasonable; it SHALL NEVER emit a bare
      `open-dialog` for an unreachable dialog.
    """
    if not isinstance(action, dict) or action.get("action") != "open-dialog":
        return action
    dialog_id = action.get("id")
    if not isinstance(dialog_id, str):
        return action
    hosting_pages = site_map.pages_with_dialog(dialog_id)
    if not hosting_pages:
        # validator will reject — leave as-is and let it fall.
        return action
    # Is the dialog already reachable from the current page?
    current_page_obj = (
        site_map.page_for_url(current_page, app=current_app)
        if current_page else None
    )
    if current_page_obj is not None and any(
        d.id == dialog_id for d in current_page_obj.dialogs
    ):
        return action
    # Cross-page — pick the first hosting page and wrap.
    target = hosting_pages[0]
    return {
        "action": "chain",
        "actions": [
            {"action": "navigate", "url": target.url},
            {"action": "open-dialog", "id": dialog_id},
        ],
    }


# ---------------------------------------------------------------------------
# Streaming hide-the-actions-tag helper.
#
# The model wraps its emitted actions in `<ACTIONS>{...}</ACTIONS>` and
# we must NOT let any of that text reach the user's chat panel. The
# extraction function above strips it from `full_content` for
# persistence, but tokens flow through SSE events as they arrive — so
# we also need to suppress at stream time.
#
# The tag can also split across delta boundaries (one chunk ends with
# `<ACT`, the next starts with `IONS>`), so we buffer the tail until
# we're confident it isn't a prefix of `<ACTIONS>`.
# ---------------------------------------------------------------------------

_ACTIONS_TAG = "<ACTIONS>"
# A code-fence opener at the start of a line, sitting at the very end of the
# buffer — the model sometimes wraps the <ACTIONS> block in one. Matches even a
# partial backtick run (1+), since token streaming can split ``` across chunks.
# Held back until we know whether <ACTIONS> follows (suppress) or real content
# does (emit on the next feed). Anchored to line-start so inline `code` mid-
# sentence is never withheld.
_FENCE_TAIL_RE = re.compile(r"(?:\n|^)[ \t]*`{1,}[a-zA-Z0-9_-]*[ \t]*\n?$")


class _ActionsTagFilter:
    """Per-iteration buffer that yields visible text while suppressing
    anything from `<ACTIONS>` onwards. Stateful — one instance per
    streaming call.
    """

    __slots__ = ("_seen", "_pending")

    def __init__(self) -> None:
        self._seen = False
        self._pending = ""

    def feed(self, chunk: str) -> str:
        """Append chunk to the buffer. Returns the portion that's safe
        to forward as a text-token now (may be empty). Once `<ACTIONS>`
        has been seen this always returns "".
        """
        if self._seen or not chunk:
            return ""
        self._pending += chunk
        tag_at = self._pending.find(_ACTIONS_TAG)
        if tag_at >= 0:
            emit = self._pending[:tag_at]
            # Drop a code fence the model placed immediately before <ACTIONS>
            # so its ``` markers don't leak as a broken code block.
            emit = _FENCE_TAIL_RE.sub("", emit)
            self._pending = ""
            self._seen = True
            return emit
        # No full tag — emit everything except the longest tail that
        # COULD be a prefix of the tag.
        safe_len = len(self._pending)
        max_partial = len(_ACTIONS_TAG) - 1
        for n in range(min(len(self._pending), max_partial), 0, -1):
            if self._pending.endswith(_ACTIONS_TAG[:n]):
                safe_len = len(self._pending) - n
                break
        # Also hold back a trailing code-fence opener: it may be wrapping a
        # forthcoming <ACTIONS> block (suppress) — or real content may follow
        # (next feed sees it's not at the tail anymore and emits it).
        fence_m = _FENCE_TAIL_RE.search(self._pending[:safe_len])
        if fence_m:
            safe_len = fence_m.start()
        if safe_len <= 0:
            return ""
        emit = self._pending[:safe_len]
        self._pending = self._pending[safe_len:]
        return emit

    def flush(self) -> str:
        """End-of-stream flush. If the tag never arrived, anything
        we were withholding (because it might have been a tag prefix)
        gets emitted now.
        """
        if self._seen:
            return ""
        emit, self._pending = self._pending, ""
        return emit


# ---------------------------------------------------------------------------
# Streaming hide-the-chain-of-thought helper.
#
# A reasoning model emits its working inside `<think>...</think>` before the
# answer. That is the model thinking, not the assistant speaking, and it must
# never reach the panel: the reasoner shipped 2026-08-10 answered "hi" with
# ~2,000 tokens of monologue that the user watched type out.
#
# Unlike <ACTIONS> this is a PAIRED tag — suppress between the two, then resume
# — so the filter has to come back out of the hidden state rather than latching.
# Same split-across-deltas problem: a chunk may end mid-`<thi`.
#
# If the stream ends while still inside a think block (truncated by a token cap
# or a disconnect), everything held back is DISCARDED rather than flushed. A
# half-finished thought is not an answer, and emitting it would put exactly the
# text this exists to hide on screen at precisely the least useful moment.
# ---------------------------------------------------------------------------

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class _ThinkTagFilter:
    """Per-iteration buffer that drops `<think>...</think>` spans while
    forwarding everything outside them. Stateful — one instance per
    streaming call.
    """

    __slots__ = ("_in_think", "_pending")

    def __init__(self) -> None:
        self._in_think = False
        self._pending = ""

    @staticmethod
    def _held_partial(buf: str, tag: str) -> int:
        """Length of the longest suffix of `buf` that is a prefix of `tag`."""
        for n in range(min(len(buf), len(tag) - 1), 0, -1):
            if buf.endswith(tag[:n]):
                return n
        return 0

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._pending += chunk
        out: List[str] = []
        while True:
            if self._in_think:
                close_at = self._pending.find(_THINK_CLOSE)
                if close_at < 0:
                    # Still thinking. Discard everything except a tail that
                    # might be the start of the closing tag, so the buffer
                    # cannot grow with the whole chain of thought.
                    hold = self._held_partial(self._pending, _THINK_CLOSE)
                    self._pending = self._pending[len(self._pending) - hold:] if hold else ""
                    break
                self._pending = self._pending[close_at + len(_THINK_CLOSE):]
                self._in_think = False
                continue
            open_at = self._pending.find(_THINK_OPEN)
            if open_at >= 0:
                out.append(self._pending[:open_at])
                self._pending = self._pending[open_at + len(_THINK_OPEN):]
                self._in_think = True
                continue
            hold = self._held_partial(self._pending, _THINK_OPEN)
            safe_len = len(self._pending) - hold
            if safe_len > 0:
                out.append(self._pending[:safe_len])
                self._pending = self._pending[safe_len:]
            break
        return "".join(out)

    def flush(self) -> str:
        """End-of-stream flush. Anything withheld as a possible tag prefix is
        emitted — unless we are still inside a think block, in which case it is
        dropped (see the note above).
        """
        if self._in_think:
            self._pending = ""
            return ""
        emit, self._pending = self._pending, ""
        return emit


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_THINK_UNCLOSED_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Remove chain-of-thought spans from a completed reply, for persistence
    and for any non-streaming caller. Mirrors `_ThinkTagFilter`, including
    dropping an unterminated trailing block.
    """
    if not text or "<think>" not in text.lower():
        return text
    return _THINK_UNCLOSED_RE.sub("", _THINK_BLOCK_RE.sub("", text)).strip()


# ---------------------------------------------------------------------------
# Streaming LLM call (OpenAI-compatible).
# ---------------------------------------------------------------------------


def _sse(event: str, payload: Dict[str, Any]) -> bytes:
    """Encode one SSE event matching the §8 panel's parser contract."""
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


async def _stream_openai_compat(
    env: Dict[str, str],
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any], AsyncIterator[bytes]]:
    """Make one streaming chat-completion call. Yields SSE `text-token`
    bytes via the returned iterator while accumulating the full
    content + tool_calls + usage stats. The caller awaits the
    iterator to drain, then reads the captured state from the
    closure values returned alongside.

    Implementation note: we keep state in dicts so the iterator
    closure can mutate it while the caller awaits.
    """
    base_url = normalize_openai_base_url(env.get("LLM_BASE_URL"))
    model = env.get("LLM_MODEL") or ""
    api_key = env.get("LLM_API_KEY") or ""
    url = f"{base_url}/v1/chat/completions"

    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": float(env.get("LLM_TEMPERATURE", "0.3") or 0.3),
        # OUTPUT budget for one reply — NOT the context window (--ctx-size,
        # 16K) and NOT the conversation memory (_load_recent_history).
        #
        # Raised 2048 -> 4096 when the reasoner became a thinking model. A
        # reasoning model spends output tokens on <think> BEFORE it answers, so
        # this cap is shared between working and reply. Nemotron spent ~2,000
        # tokens of thought on the word "hi"; at 2048 a real question could
        # exhaust the budget mid-thought and never reach an answer.
        #
        # Truncation is worse than it looks now that think spans are stripped:
        # an unterminated <think> block is DISCARDED, so a reply cut off while
        # thinking renders as EMPTY, not as a partial answer. Headroom is cheap
        # — a 16K window against ~1.5k of history leaves plenty for the system
        # prompt and site map alongside 4096 out.
        "max_tokens": int(env.get("LLM_MAX_TOKENS", "4096") or 4096),
        "stream": True,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    full_content_parts: List[str] = []
    # Tool calls stream in deltas; we accumulate by index.
    tool_calls_by_idx: Dict[int, Dict[str, Any]] = {}
    usage_box: Dict[str, Any] = {}

    async def _iter() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread() or b"")[:400].decode("utf-8", "replace")
                    raise OrchestratorError(f"LLM HTTP {resp.status_code}: {text}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    # Some providers emit a usage chunk after the last
                    # content delta. Capture it without forwarding.
                    if evt.get("usage"):
                        usage_box.update(evt["usage"])
                    choices = evt.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    chunk = delta.get("content")
                    if chunk:
                        full_content_parts.append(chunk)
                        yield _sse("text-token", {"text": chunk})
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls_by_idx.setdefault(idx, {
                            "id": tc.get("id") or f"tc_{idx}",
                            "name": "",
                            "arguments": "",
                        })
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

    iter_obj = _iter()
    # We expose state via closure refs; pack into a sentinel record
    # the orchestrator reads after the iterator drains.
    return (
        "<deferred>",   # placeholder; orchestrator reads full_content via the join below
        # The lists/dicts above are referenced by the iterator's closure;
        # callers receive them via the returned tuple slots after drain.
        list(),         # tool_calls (filled below; see _stream_one_iteration)
        usage_box,
        iter_obj,
    )


_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
                               re.DOTALL | re.IGNORECASE)


def _recover_text_tool_calls(text: str) -> List[Dict[str, Any]]:
    """Recover tool calls a backend emitted as JSON TEXT in the message body
    instead of as structured `tool_calls`.

    llama.cpp / GGUF reasoner rungs served without a `--tool-call-parser` hand
    the call back as text — typically `<tool_call>{"name":...,"arguments":...}
    </tool_call>` (Nemotron / Llama-3.1 / Hermes), sometimes a bare top-level
    JSON object. Without this, `lookup_site_map` never runs, the model gets no
    site-map grounding, and the assistant can never emit an <ACTIONS> block —
    i.e. it answers in prose and never navigates. Returns the SAME shape as
    structured tool_calls: [{"id","name","arguments"(json str)}].
    """
    blobs = _TOOL_CALL_TAG_RE.findall(text or "")
    if not blobs:
        # Bare JSON object with name + arguments (no wrapping tags).
        stripped = (text or "").strip()
        if stripped.startswith("{") and '"name"' in stripped:
            blobs = [stripped]
    out: List[Dict[str, Any]] = []
    for i, blob in enumerate(blobs):
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        args = obj.get("arguments", obj.get("parameters", {}))
        # Only trust a bare object if it really looks like a tool call.
        if not name or "arguments" not in obj and "parameters" not in obj:
            continue
        if not isinstance(args, str):
            args = json.dumps(args or {})
        out.append({"id": f"tc_txt_{i}", "name": name, "arguments": args})
    return out


def _normalize_message_sequence(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Coerce an OpenAI-style message list into a sequence the strict chat
    templates accept: one leading `system` block, then strictly alternating
    `user` / `assistant` turns.

    The GGUF rungs we serve (granite / Llama / Mistral llama.cpp templates)
    reject two things that arise on a tool-use turn and 400 with
    "roles must alternate user/assistant/user/assistant/...":

      * more than one `system` message — the base system prompt PLUS the
        Knowledge-Base prefetch injection (a second, mid-conversation
        `system` message), and
      * the tool-call framing itself — an `assistant` turn carrying
        ``tool_calls`` with null content, followed by one or more `tool`
        role messages (which several templates don't model at all, and
        which read as consecutive same-role turns).

    We fold the tool exchange back into the user/assistant alternation the
    template understands: every `system` message is merged into one leading
    block; an `assistant` tool-call turn keeps (or is given) visible text;
    each `tool` result becomes `user`-role context; then any remaining
    consecutive same-role turns are merged. The tool output still reaches
    the model — as user context — so the next turn can answer from it. This
    matches how the backend already recovers tool calls from TEXT
    (`_recover_text_tool_calls`): it never relied on structured tool roles
    surviving the round-trip.

    Pure function (no mutation of the input) so `stream_chat` can keep the
    canonical OpenAI-shaped list across loop iterations and normalize only
    at the point of the call.
    """
    system_parts: List[str] = []
    body: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            c = (m.get("content") or "").strip()
            if c:
                system_parts.append(c)
        else:
            body.append(m)

    rewritten: List[Dict[str, str]] = []
    for m in body:
        role = m.get("role")
        content = m.get("content")
        if role == "assistant":
            text = (content or "").strip()
            tool_calls = m.get("tool_calls") or []
            if tool_calls and not text:
                names = ", ".join(
                    ((tc.get("function") or {}).get("name") or tc.get("name") or "tool")
                    for tc in tool_calls
                )
                # A blank assistant turn also trips some templates; give the
                # tool-call turn a minimal visible marker.
                text = f"(calling {names})"
            rewritten.append({"role": "assistant", "content": text})
        elif role == "tool":
            name = m.get("name") or "tool"
            rewritten.append({
                "role": "user",
                "content": f"Result from {name}:\n{content or ''}",
            })
        else:
            rewritten.append({"role": "user", "content": content or ""})

    merged: List[Dict[str, str]] = []
    for m in rewritten:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] = (
                merged[-1]["content"] + "\n\n" + m["content"]
            ).strip()
        else:
            merged.append(dict(m))

    out: List[Dict[str, Any]] = []
    if system_parts:
        out.append({"role": "system", "content": "\n\n".join(system_parts)})
    out.extend(merged)
    return out


async def _stream_one_iteration(
    env: Dict[str, str],
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    out_sse: List[bytes],
) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """Run one streaming LLM call to completion. Accumulates yielded
    text-token SSE events into `out_sse` so the caller can forward
    them in order. Returns (full_content, tool_calls, usage).
    """
    full_content_parts: List[str] = []
    tool_calls_by_idx: Dict[int, Dict[str, Any]] = {}
    usage: Dict[str, Any] = {}
    tag_filter = _ActionsTagFilter()
    think_filter = _ThinkTagFilter()

    base_url = normalize_openai_base_url(env.get("LLM_BASE_URL"))
    model = env.get("LLM_MODEL") or ""
    api_key = env.get("LLM_API_KEY") or ""
    url = f"{base_url}/v1/chat/completions"
    # Coerce the message list into the strictly-alternating shape the chat
    # template requires (see _normalize_message_sequence): merges the double
    # `system` (prompt + KB prefetch) and folds the tool exchange back into
    # user/assistant alternation, so a tool-use turn no longer 400s with
    # "roles must alternate".
    body: Dict[str, Any] = {
        "model": model,
        "messages": _normalize_message_sequence(messages),
        "temperature": float(env.get("LLM_TEMPERATURE", "0.3") or 0.3),
        # OUTPUT budget for one reply — NOT the context window (--ctx-size,
        # 16K) and NOT the conversation memory (_load_recent_history).
        #
        # Raised 2048 -> 4096 when the reasoner became a thinking model. A
        # reasoning model spends output tokens on <think> BEFORE it answers, so
        # this cap is shared between working and reply. Nemotron spent ~2,000
        # tokens of thought on the word "hi"; at 2048 a real question could
        # exhaust the budget mid-thought and never reach an answer.
        #
        # Truncation is worse than it looks now that think spans are stripped:
        # an unterminated <think> block is DISCARDED, so a reply cut off while
        # thinking renders as EMPTY, not as a partial answer. Headroom is cheap
        # — a 16K window against ~1.5k of history leaves plenty for the system
        # prompt and site map alongside 4096 out.
        "max_tokens": int(env.get("LLM_MAX_TOKENS", "4096") or 4096),
        "stream": True,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code != 200:
                text = (await resp.aread() or b"")[:400].decode("utf-8", "replace")
                raise OrchestratorError(f"LLM HTTP {resp.status_code}: {text}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if evt.get("usage"):
                    usage.update(evt["usage"])
                choices = evt.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                chunk = delta.get("content")
                if chunk:
                    full_content_parts.append(chunk)
                    # Think filter FIRST: chain-of-thought can itself contain
                    # the string "<ACTIONS>" while the model reasons about what
                    # to emit, and letting that reach the actions filter would
                    # latch it closed and swallow the real answer.
                    emit = tag_filter.feed(think_filter.feed(chunk))
                    if emit:
                        out_sse.append(_sse("text-token", {"text": emit}))
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_calls_by_idx.setdefault(idx, {
                        "id": tc.get("id") or f"tc_{idx}",
                        "name": "",
                        "arguments": "",
                    })
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

    # If the stream ended without ever emitting `<ACTIONS>`, flush the
    # buffered tail. (We were holding it back in case the next chunk
    # would have completed the tag.)
    # Order matters: drain the think filter through the actions filter, then
    # flush the actions filter's own withheld tail.
    tail = tag_filter.feed(think_filter.flush()) + tag_filter.flush()
    if tail:
        out_sse.append(_sse("text-token", {"text": tail}))

    full_content = "".join(full_content_parts)
    tool_calls = [tool_calls_by_idx[k] for k in sorted(tool_calls_by_idx.keys())]

    # Backends that don't parse tool calls natively (llama.cpp / GGUF reasoner
    # rungs) return the call as text in `content`. Recover it so the tool runs,
    # and drop the raw JSON from the buffered token stream so the user never
    # sees it — the real answer arrives on the next iteration.
    if not tool_calls and full_content:
        recovered = _recover_text_tool_calls(full_content)
        if recovered:
            logger.info("orchestrator: recovered %d tool call(s) from text body "
                        "(backend returned no structured tool_calls)", len(recovered))
            tool_calls = recovered
            out_sse[:] = [e for e in out_sse if not e.startswith(b"event: text-token\n")]
            full_content = ""

    return full_content, tool_calls, usage


# ---------------------------------------------------------------------------
# Canned fallback (no LLM configured for the tenant).
# ---------------------------------------------------------------------------


_CANNED = [
    {
        "match": ("new node", "create a node", "new wizard"),
        "text": "I can take you to the New Node Wizard — it's the fastest way to add a node from a single prompt.",
        "actions": [{"action": "navigate", "url": "/?new=node"}],
        "citations": [],
    },
    {
        "match": ("billing", "payment", "subscription"),
        "text": "Your account settings live at /settings.",
        "actions": [{"action": "navigate", "url": "/settings"}],
        "citations": [],
    },
    {
        "match": ("workers", "worker pool"),
        "text": "I'd send you to the Workers page — it shows enrolled workers, their hardware, and recent activity.",
        "actions": [{"action": "navigate", "url": "/workers"}],
        "citations": [],
    },
]


def _pick_canned(user_msg: str) -> Dict[str, Any]:
    lower = user_msg.lower()
    for entry in _CANNED:
        if any(token in lower for token in entry["match"]):
            return entry
    return {
        "text": (
            "I'm not connected to an LLM yet — set the model in "
            "Settings → AI Configuration → AI Assistant. In the "
            "meantime I respond to canned prompts like \"create a "
            "new node\", \"billing\", or \"workers\"."
        ),
        "actions": [],
        "citations": [],
    }


async def _stream_canned(user_msg: str) -> AsyncIterator[bytes]:
    entry = _pick_canned(user_msg)
    text = entry["text"]
    for i in range(0, len(text), 6):
        yield _sse("text-token", {"text": text[i:i + 6]})
    for action in entry["actions"]:
        yield _sse("action-emit", action)


# ---------------------------------------------------------------------------
# Persistence helpers.
# ---------------------------------------------------------------------------


class SessionInactiveError(Exception):
    """Raised by check_session_active() when a caller-supplied session_id
    resolves to a session that must not accept further turns. `reason` is
    "ended" (explicit POST /assistant/session/end) or "expired" (past the
    tenant's session_retention_hours). Callers translate this to HTTP 410."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def check_session_active(
    db: Session,
    current_user: models.User,
    session_id: Optional[str],
) -> None:
    """Raise SessionInactiveError if `session_id` names a session this user
    has already ended or that has aged out of retention.

    Deliberately a no-op for ids that don't resolve to a row of THIS
    (tenant, user) — including a missing/unparseable id, a foreign session,
    or one already hard-deleted by the expiry sweep. Those cases fall
    through to _ensure_session's existing find-or-create: a brand-new
    client-minted id must still be allowed to start a session (see 10.5's
    documented deferral — the client mints ids in sessionStorage, so the
    server cannot distinguish "never used" from "already swept" once the
    row itself is gone; it can only refuse ids it still has evidence for).
    """
    if not session_id:
        return
    try:
        sid_uuid = uuid.UUID(str(session_id))
    except (ValueError, TypeError):
        return
    row = (
        db.query(models.AssistantSession)
        .filter(models.AssistantSession.id == sid_uuid)
        .one_or_none()
    )
    if row is None:
        return
    if row.tenant_id != current_user.tenant_id or row.user_id != current_user.id:
        return
    if row.ended_at is not None:
        raise SessionInactiveError("ended")

    cfg = _load_tenant_cfg(db, current_user.tenant_id)
    retention_hours = int(cfg.session_retention_hours) if cfg is not None else 1
    last_active = row.last_active_at
    if last_active is not None:
        if last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last_active > timedelta(hours=retention_hours):
            raise SessionInactiveError("expired")


def end_session(
    db: Session,
    current_user: models.User,
    session_id: str,
) -> Dict[str, Any]:
    """Soft-end a session: sets ended_at, never deletes the row (ISO 27001 —
    the transcript stays queryable). Scoped to (tenant, user): ending a
    session that belongs to someone else, or doesn't exist, raises
    LookupError so the router can answer 404 without leaking whether the id
    exists for another tenant/user. Idempotent — ending an already-ended
    session succeeds and reports `already_ended=True` rather than erroring.
    """
    try:
        sid_uuid = uuid.UUID(str(session_id))
    except (ValueError, TypeError):
        raise LookupError("session not found")

    row = (
        db.query(models.AssistantSession)
        .filter(models.AssistantSession.id == sid_uuid)
        .one_or_none()
    )
    if row is None or row.tenant_id != current_user.tenant_id or row.user_id != current_user.id:
        raise LookupError("session not found")

    already_ended = row.ended_at is not None
    if not already_ended:
        row.ended_at = datetime.now(timezone.utc)
        db.commit()
    return {
        "session_id": str(row.id),
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "already_ended": already_ended,
    }


def _ensure_session(
    db: Session,
    current_user: models.User,
    session_id: Optional[str],
    current_page: Optional[str],
    current_workflow_id: Optional[int],
    model_id: Optional[str],
) -> Tuple[models.AssistantSession, bool]:
    """Find-or-create the AssistantSession row. Returns (row, is_new).
    Bumps last_active_at on every hit so the expiry sweep sees activity."""
    sid_uuid: Optional[uuid.UUID] = None
    if session_id:
        try:
            sid_uuid = uuid.UUID(str(session_id))
        except ValueError:
            sid_uuid = None
    row: Optional[models.AssistantSession] = None
    if sid_uuid is not None:
        row = (
            db.query(models.AssistantSession)
            .filter(models.AssistantSession.id == sid_uuid)
            .one_or_none()
        )
    if row is not None:
        # Enforce tenant binding on session reuse — a client cannot
        # hand us a session id from another tenant and inherit it.
        # Also clear sid_uuid so we mint a fresh one below instead of
        # colliding on the row that still exists in the foreign tenant.
        if row.tenant_id != current_user.tenant_id or row.user_id != current_user.id:
            row = None
            sid_uuid = None
    is_new = row is None
    if row is None:
        row = models.AssistantSession(
            id=sid_uuid or uuid.uuid4(),
            user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            current_page=current_page,
            current_workflow_id=current_workflow_id,
            model_id=model_id,
        )
        db.add(row)
    row.last_active_at = datetime.now(timezone.utc)
    if current_page:
        row.current_page = current_page
    if current_workflow_id:
        row.current_workflow_id = current_workflow_id
    if model_id:
        row.model_id = model_id
    db.flush()
    return row, is_new


def _load_recent_history(
    db: Session,
    session_id,
    *,
    max_messages: int = 20,
    max_chars: int = 6000,
) -> List[Dict[str, Any]]:
    """Recent (user/assistant) turns for this session, oldest→newest, capped
    by message count AND total chars so the prompt stays cheap (~2k tokens).
    Tool messages are excluded (internal noise). This is what gives the
    assistant memory of the conversation — across turns AND across surfaces,
    since the session is server-owned per (user, tenant). Best-effort: any
    failure returns [] so a history hiccup never breaks a live chat."""
    try:
        rows = (
            db.query(models.AssistantMessage)
            .filter(models.AssistantMessage.session_id == session_id)
            .filter(models.AssistantMessage.role.in_(("user", "assistant")))
            .order_by(models.AssistantMessage.created_at.desc())
            .limit(max_messages)
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("assistant history load failed: %s", e)
        return []
    out: List[Dict[str, Any]] = []
    budget = max_chars
    for m in rows:  # newest first; accumulate backward until the budget runs out
        content = (m.content or "").strip()
        if not content:
            continue
        if out and budget - len(content) < 0:
            break
        budget -= len(content)
        out.append({"role": m.role, "content": content[:max_chars]})
    out.reverse()  # → oldest first for the LLM prompt
    return out


def _persist_message(
    db: Session,
    session_id: uuid.UUID,
    role: str,
    content: str,
    actions_json: Optional[List[Dict[str, Any]]] = None,
    model_id: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    latency_ms: Optional[int] = None,
) -> models.AssistantMessage:
    msg = models.AssistantMessage(
        id=uuid.uuid4(),
        session_id=session_id,
        role=role,
        content=content,
        actions_json=actions_json or [],
        model_id=model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
    )
    db.add(msg)
    return msg


# ---------------------------------------------------------------------------
# Main entry point: stream_chat.
# ---------------------------------------------------------------------------


def _load_tenant_cfg(db: Session, tenant_id: int) -> Optional[models.AssistantTenantConfig]:
    return (
        db.query(models.AssistantTenantConfig)
        .filter(models.AssistantTenantConfig.tenant_id == tenant_id)
        .one_or_none()
    )


async def stream_chat(
    db: Session,
    current_user: models.User,
    *,
    message: str,
    session_id: Optional[str],
    current_page: Optional[str],
    current_workflow_id: Optional[int],
    current_app: Optional[str] = None,
    confirm_tokens: Optional[Dict[str, str]] = None,
) -> AsyncIterator[bytes]:
    """Generator yielding SSE bytes for one chat turn. The endpoint wraps
    this in a StreamingResponse.

    Per-tenant gating + persistence + tool-use loop + action validation
    all live here so the endpoint stays a thin shell.
    """
    tenant_cfg = _load_tenant_cfg(db, current_user.tenant_id)
    if tenant_cfg is not None and not tenant_cfg.enabled:
        yield _sse("error", {"message": "Assistant disabled for this tenant"})
        yield _sse("done", {})
        return

    env = _resolve_env(tenant_cfg)
    model_id = env.get("LLM_MODEL") or None

    sess, _ = _ensure_session(
        db, current_user, session_id, current_page, current_workflow_id, model_id,
    )
    yield _sse("session", {"session_id": str(sess.id)})

    # Load PRIOR turns before persisting the current message, so the LLM gets
    # conversation memory (the prompt was previously just [system, message] —
    # the assistant forgot everything between turns). Captured here (not after
    # the persist below) so it excludes the message we're about to store.
    prior_history = _load_recent_history(db, sess.id)

    _persist_message(db, sess.id, role="user", content=message)
    db.commit()

    # GSEC R-024 — data-routing gate. Refuse (name the setting, no silent
    # fallback) when the tenant's binding is EXTERNAL and the tenant has not
    # opted in. Local/MOD-hosted bindings always pass. Checked BEFORE the
    # not-configured fallback so a refusal is never masked by canned
    # responses.
    _routing = _r024_gate(db, current_user.tenant_id, surface="assistant",
                          byte_count=len((message or "").encode("utf-8")),
                          workflow_id=current_workflow_id)
    if not _routing.get("allowed", True):
        logger.warning(
            "assistant: R-024 refusal — external LLM binding for tenant=%s "
            "(host=%s) without tenant opt-in",
            current_user.tenant_id, _routing.get("provider_host"),
        )
        _persist_message(
            db, sess.id, role="assistant",
            content=_routing.get("message") or "", model_id=None,
        )
        db.commit()
        yield _sse("error", {"message": _routing.get("message") or
                             "LLM data routing not permitted for this tenant."})
        yield _sse("done", {})
        return

    # ai-config-stack-binding 3.2 — an empty env (the `{}` contract of
    # `_resolved_effective_env`, :func:`_resolve_env`) collapses EVERY
    # non-`ready` outcome of the typed resolver: no_binding / stack_stopped /
    # stack_missing / unreachable / waking. A slot with a NAMED failure must
    # surface that cause sentence (slot + model + host/stack where known) —
    # never the canned pool, which the user would read as a real answer.
    # The canned pool survives only when the resolver has nothing to name
    # (pre-launch: nobody picked a slot yet and no legacy blob names a dead
    # stack), which is the only honest "panel UI still works" degrade.
    if not _has_provider_configured(env):
        # 4.1: the wake's progress channel is THIS feature's own — the resolver
        # hands "Starting the AI model…" to this generator, which streams it as
        # a `progress` event BEFORE the (bounded) wait finishes.
        _wake_progress: List[str] = []
        _woken_env: Dict[str, str] = {}
        slot_err = _slot_failure_cause(db, current_user.tenant_id,
                                       on_progress=_wake_progress.append,
                                       env_out=_woken_env)
        for _p in _wake_progress:
            yield _sse("progress", {"message": _p})
        if _woken_env:
            # The sleeping bound stack is serving now: answer with IT.
            env.update(_woken_env)
            model_id = env.get("LLM_MODEL") or model_id
        if slot_err:
            logger.warning(
                "assistant: slot %s unresolved for tenant=%s: %s",
                LLM_TOOL_NAME, current_user.tenant_id, slot_err,
            )
            # Persist the cause the user was shown (model_id=None: this turn
            # cost nothing) so history + the R-024-style audit stay truthful.
            _persist_message(
                db, sess.id, role="assistant",
                content=slot_err, model_id=None,
            )
            db.commit()
            yield _sse("error", {"message": slot_err})
            yield _sse("done", {})
            return
        if _has_provider_configured(env):
            # 4.1: the sleeping bound stack was woken and now serves — the turn
            # continues to the LLM path with ITS env, never the canned pool.
            pass
        else:
            text_parts: List[str] = []
            async for ev in _stream_canned(message):
                text_parts.append("")  # we don't dedupe here; ev already encoded
                yield ev
            # Re-pick to log the response text + actions to the DB.
            canned = _pick_canned(message)
            _persist_message(
                db, sess.id, role="assistant",
                content=canned["text"],
                actions_json=canned["actions"],
            )
            db.commit()
            yield _sse("done", {})
            return

    logs_context_enabled = (
        tenant_cfg.logs_context_enabled if tenant_cfg is not None else True
    )
    # Tenant Knowledge Base gating (D8): the retrieve_company_knowledge tool is
    # only offered to the model — and only runs — when this tenant has the KB
    # enabled. Default false when there is no tenant config row.
    kb_enabled = bool(
        getattr(tenant_cfg, "knowledge_base_enabled", False)
    ) if tenant_cfg is not None else False
    ctx: Dict[str, Any] = {
        "db": db,
        "current_user": current_user,
        "session_id": str(sess.id),
        "logs_context_enabled": logs_context_enabled,
        "knowledge_base_enabled": kb_enabled,
        # One-time confirm tokens the caller already
        # holds (the UI re-sends the token the gate returned on the previous turn).
        "confirm_tokens": dict(confirm_tokens or {}),
        "_citations": {},
    }

    # Assemble the tool list for this turn. The KB tool is appended only when
    # enabled so a disabled tenant never sees it in the model's tool schema.
    tools_for_model = (
        _TOOLS_FOR_MODEL + [_KB_TOOL_SCHEMA] if kb_enabled else _TOOLS_FOR_MODEL
    )

    system_prompt = _build_system_prompt(current_page, current_user.tenant_id, kb_enabled)
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(prior_history)  # conversation memory (capped recent turns)

    # Deterministic Knowledge-Base prefetch. The local model is too weak to be
    # trusted to CALL retrieve_company_knowledge on its own — it just refuses
    # off-MOD questions — so when the tenant has a KB we retrieve on the user's
    # message ourselves and inject the passages as context. If the KB genuinely
    # covers the question (score above threshold) the model now has the answer in
    # front of it and answers from it instead of refusing; if it doesn't, nothing
    # is injected and normal MOD behavior is unchanged.
    _kb_prefetch_hits: List[Dict[str, Any]] = []
    if kb_enabled:
        try:
            _all = _tool_retrieve_company_knowledge(db, current_user.tenant_id, message, k=6)
            _kb_prefetch_hits = [h for h in _all if float(h.get("score") or 0.0) >= _KB_PREFETCH_MIN_SCORE]
        except Exception:  # noqa: BLE001 — prefetch is best-effort
            _kb_prefetch_hits = []
    if _kb_prefetch_hits:
        _passages = "\n\n".join(
            f"[{i + 1}] (from \"{h.get('title') or 'document'}\")\n{h.get('snippet') or ''}"
            for i, h in enumerate(_kb_prefetch_hits)
        )
        messages.append({
            "role": "system",
            "content": (
                "The user's company Knowledge Base — their OWN uploaded, authorized "
                "documents for this workspace — contains the passages below, which are "
                "relevant to the question they are about to ask. ANSWER THE QUESTION "
                "using this material, in your own words. This IS in scope: do NOT "
                "refuse, and do NOT say you only handle MOD or that you need external "
                "resources. If the passages don't fully answer it, use what they do "
                "provide.\n\n" + _passages
            ),
        })

    messages.append({"role": "user", "content": message})

    site_map = assistant_site_map.get_map()
    # Per-tenant allowlist for action validation. NULL / missing keys
    # are "allowed" (the orchestrator default), explicit `false`
    # disables. See utils.assistant_actions.validate_action.
    action_allowlist = (tenant_cfg.action_allowlist if tenant_cfg is not None else None) or None

    # Deterministic short-circuit for the unambiguous "create a node" intent.
    # The local model is unreliable here — it leaks the raw actions JSON as
    # visible text or dumps numbered manual steps instead of a clean button — so
    # for this one clear intent we emit the Node Wizard action ourselves and skip
    # the model entirely. The chain navigates to the WorkflowBuilder (if the user
    # isn't already there the open-dialog replays on arrival) then opens the
    # wizard; both targets are validated against the site map.
    if _NODE_CREATE_RE.search(message or ""):
        _node_actions = [{
            "action": "chain",
            "actions": [
                {"action": "navigate", "url": "/", "app": "workflow_builder", "label": "Workflow Builder"},
                {"action": "open-dialog", "id": "new-node-wizard"},
            ],
        }]
        _emitted: List[Dict[str, Any]] = []
        for _a in _node_actions:
            _v = _validate_action(_a, site_map, action_allowlist)
            if _v is not None:
                _emitted.append(_v)
        _text = "Sure — click below to open the Node Wizard and create a new node."
        yield _sse("text-token", {"text": _text})
        for _v in _emitted:
            yield _sse("action-emit", _v)
        _persist_message(
            db, sess.id, role="assistant", content=_text,
            actions_json=_emitted, model_id=None,
        )
        db.commit()
        yield _sse("done", {})
        return

    # Deterministic short-circuit for a clear navigation intent ("take me to
    # billing", "go to the workers page"). The weak local model tends to spin
    # on lookup_site_map without ever emitting a `navigate` (observed 6x in a
    # row); when the destination resolves unambiguously to a single site-map
    # page we emit ONE validated navigate action ourselves and skip the model.
    # Ambiguous / unknown targets return None and fall through to the model.
    _nav = _resolve_navigation_target(message or "", site_map)
    if _nav is not None:
        _nav_v = _validate_action(_nav, site_map, action_allowlist)
        if _nav_v is not None:
            _text = f"Sure — taking you to {_nav.get('label') or 'that page'}."
            yield _sse("text-token", {"text": _text})
            yield _sse("action-emit", _nav_v)
            _persist_message(
                db, sess.id, role="assistant", content=_text,
                actions_json=[_nav_v], model_id=None,
            )
            db.commit()
            yield _sse("done", {})
            return

    total_prompt = 0
    total_completion = 0
    started_at = datetime.now(timezone.utc)
    # Prompt-injection defence. When this turn's
    # context receives untrusted tool output (external / user-authored data),
    # the turn becomes TAINTED: untrusted results are wrapped in
    # <untrusted> delimiters with a system note, and risky tool calls in a
    # tainted turn force confirmation even when they normally would not.
    # `assistant.untrusted_confirm_enabled` (default True) switches the whole
    # defence off, restoring the pre-p3a behaviour.
    untrusted_gate_on = assistant_confirm.untrusted_confirm_enabled(
        db, current_user.tenant_id)
    turn_tainted = False
    untrusted_note_emitted = False
    # Guard against the degenerate lookup_site_map loop: once the site map has
    # been served, a repeat call is the model failing to convert grounding into
    # a `navigate`. We break the loop and resolve the navigation deterministically
    # (or stop gracefully) rather than letting it spin to MAX_ITERATIONS.
    site_map_calls = 0

    final_text = ""
    final_actions: List[Dict[str, Any]] = []
    final_citation_handles: List[str] = []

    for iteration in range(MAX_ITERATIONS):
        out_sse: List[bytes] = []
        try:
            content, tool_calls, usage = await _stream_one_iteration(
                env, messages, tools_for_model, out_sse,
            )
        except OrchestratorError as e:
            logger.warning("orchestrator: LLM error: %s", e)
            yield _sse("error", {"message": str(e)})
            db.commit()
            yield _sse("done", {})
            return
        except httpx.HTTPError as e:
            # A transport-level failure (DNS miss, connection refused, read
            # timeout) reaching the model server. Previously this escaped the
            # generator and crashed the SSE stream, which the panel rendered as
            # the raw "Network or server error" ("turtles!"). Convert it into a
            # clean, user-readable error event + a graceful `done` so the panel
            # shows a real message and can offer Retry. The base_url is logged
            # (not shown) so an operator can see WHICH stack was unreachable.
            logger.warning(
                "orchestrator: LLM transport error dialing %s (%s): %s",
                env.get("LLM_BASE_URL"), e.__class__.__name__, e,
            )
            yield _sse("error", {"message": (
                "The AI assistant is temporarily unavailable — the model "
                "service could not be reached. Please try again in a moment; "
                "if this persists, an administrator may need to check the "
                "managed AI stack."
            )})
            db.commit()
            yield _sse("done", {})
            return
        except Exception as e:  # noqa: BLE001
            # Catch-all so NO model-call or loop failure ever escapes the
            # generator as a raw exception the panel renders as "network
            # error"/a stack trace. Log the real error for operators; show the
            # user a friendly, bounded message and a graceful `done`.
            logger.exception("orchestrator: unexpected error during chat turn: %s", e)
            yield _sse("error", {"message": (
                "Something went wrong on my side and I couldn't finish that "
                "request. Please try again in a moment."
            )})
            db.commit()
            yield _sse("done", {})
            return
        # Forward streamed tokens
        for ev_bytes in out_sse:
            yield ev_bytes
        total_prompt += int(usage.get("prompt_tokens") or 0)
        total_completion += int(usage.get("completion_tokens") or 0)

        # Tool calls were emitted — invoke each, append to messages, loop.
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                fn_name = tc["name"]
                if fn_name == "lookup_site_map":
                    site_map_calls += 1
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                impl = _TOOL_IMPLS.get(fn_name)
                tool_ok = False
                # Deny by default: the gate runs for EVERY named
                # tool call, before the impl lookup. The pre-p4 order ran the
                # gate only when an impl existed, so an injected call naming a
                # risky tool that has no impl yet (TOOL_RISKY_CATEGORY)
                # skipped confirmation entirely — a latent bypass the moment
                # such a tool lands (MCP or a future impl). p3a's contract is
                # "unknown tools default to risky" in a tainted turn; the
                # gate first, the "unknown tool" stub second.
                gate = assistant_confirm.check_tool_call(
                    db, current_user, fn_name, args,
                    confirm_token=(ctx.get("confirm_tokens") or {}).get(fn_name),
                    # A turn already carrying untrusted
                    # content forces confirmation for risky calls.
                    tainted=turn_tainted,
                )
                if not gate.get("allowed"):
                    tool_result = gate
                elif impl is None:
                    tool_result = {"error": f"unknown tool {fn_name}"}
                else:
                    ctx["_audit_via"] = "assistant"
                    try:
                        tool_result = impl(args, ctx)
                        tool_ok = True
                    except Exception as e:
                        logger.warning("orchestrator: tool %s failed: %s", fn_name, e)
                        tool_result = {"error": f"tool {fn_name} failed"}
                # Persist tool result as a 'tool' message in our log
                _persist_message(
                    db, sess.id, role="tool",
                    content=json.dumps(tool_result, default=str)[:8000],
                )
                # GSEC R-024 — when the binding is EXTERNAL, the log/error
                # text these tools return is tenant data leaving MOD, so it
                # is redacted (bearer tokens, JWTs, key=value secrets, AWS
                # keys) before it reaches the provider. The raw result stays
                # in our own store / SSE above; only the model-bound copy is
                # scrubbed. Local/MOD-hosted bindings get the raw text.
                model_tool_result: Any = tool_result
                if (_routing.get("external")
                        and fn_name in ("fetch_logs", "retrieve_docs")):
                    model_tool_result = _redact_for_external(tool_result)
                result_text = json.dumps(model_tool_result, default=str)
                # External / user-authored tool
                # output is DATA, not instructions: wrap the model-bound copy
                # in <untrusted> delimiters (delimiter text escaped inside)
                # and taint the turn. The persisted / SSE copy above stays
                # raw; only what the model reads is wrapped. Gate envelopes
                # and error stubs are MOD-produced and are never wrapped.
                if (tool_ok and untrusted_gate_on
                        and fn_name in assistant_confirm.TOOL_UNTRUSTED_SOURCE):
                    result_text = assistant_confirm.wrap_untrusted(
                        result_text,
                        assistant_confirm.TOOL_UNTRUSTED_SOURCE[fn_name],
                    )
                    turn_tainted = True
                # Surface the confirmation gate on the wire.
                # The panels (CORE/Dashboard/js/assistant-panel.js + the
                # WorkflowBuilder twin) open their confirm dialog from a
                # `needs-confirmation` SSE event carrying the gate envelope
                # {name, args, needs_confirmation, confirm_token, summary,
                # risk_category, forced_by} — exactly what check_tool_call
                # returns for a refused-but-confirmable call. Before this the
                # envelope only lived in the persisted 'tool' message, so the
                # dialog could never open live and the user could only ever
                # see the refusal. The token is the gate's own single-use
                # HMAC token (assistant_confirm mints/stores it; nothing new
                # is minted here) and the UI re-sends it under
                # confirm_tokens, so the gate's TTL/single-use/args-hash
                # binding is what authorizes the re-run.
                #
                # A plain refusal (assistant off / read-only / forbidden /
                # token mismatch) is NOT confirmable: it stays a tool-call
                # event plus an `error` event, which is the shape the panels
                # already render as a non-retryable error row.
                if not gate.get("allowed") and gate.get("needs_confirmation"):
                    yield _sse("needs-confirmation", {
                        "name": fn_name,
                        "args": args,
                        "needs_confirmation": True,
                        "confirm_token": gate.get("confirm_token"),
                        "summary": gate.get("summary"),
                        "risk_category": gate.get("risk_category"),
                        **({"forced_by": gate["forced_by"]}
                           if gate.get("forced_by") else {}),
                    })
                elif not gate.get("allowed") and gate.get("error"):
                    yield _sse("error", {"message": str(gate["error"])})
                yield _sse("tool-call", {"name": fn_name, "args": args})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": fn_name,
                    "content": result_text,
                })

            # Once this turn is tainted, tell the model the
            # rules of the road (untrusted blocks are data, not commands)
            # before its next call. Added once, on the first tainted result.
            if turn_tainted and not untrusted_note_emitted:
                messages.append({
                    "role": "system",
                    "content": assistant_confirm.UNTRUSTED_NOTE,
                })
                untrusted_note_emitted = True

            # Degenerate-loop guard: the model has now called lookup_site_map
            # more than once without ever emitting a `navigate`. Stop looping
            # and resolve the navigation ourselves from the original message; if
            # it resolves to a single page, emit that one navigate — otherwise
            # end gracefully rather than spinning to the iteration cap.
            if site_map_calls >= 2:
                logger.info(
                    "orchestrator: breaking degenerate lookup_site_map loop "
                    "(session=%s, calls=%d)", sess.id, site_map_calls,
                )
                nav = _resolve_navigation_target(message or "", site_map)
                nav_v = (
                    _validate_action(nav, site_map, action_allowlist)
                    if nav is not None else None
                )
                if nav_v is not None:
                    text = f"Sure — taking you to {nav.get('label') or 'that page'}."
                    yield _sse("text-token", {"text": text})
                    yield _sse("action-emit", nav_v)
                    _persist_message(
                        db, sess.id, role="assistant", content=text,
                        actions_json=[nav_v], model_id=model_id,
                        prompt_tokens=total_prompt or None,
                        completion_tokens=total_completion or None,
                    )
                else:
                    text = (
                        "I couldn't pin down exactly where you'd like to go — "
                        "could you name the page (for example Billing, Workers, "
                        "or Settings)?"
                    )
                    yield _sse("text-token", {"text": text})
                    _persist_message(
                        db, sess.id, role="assistant", content=text,
                        model_id=model_id,
                        prompt_tokens=total_prompt or None,
                        completion_tokens=total_completion or None,
                    )
                db.commit()
                yield _sse("done", {})
                return

            continue  # next iteration

        # No tool calls — this is the final assistant turn.
        # Strip chain-of-thought before anything reads or stores this. The
        # stream already suppressed it token-by-token; this is the same removal
        # for the persisted copy, which /assistant/history replays verbatim.
        content = strip_think(content)
        final_text = content
        visible, raw_actions, handles = _extract_actions_block(content)
        if not raw_actions:
            # Loud on purpose. A reply with no <ACTIONS> block is the failure
            # mode that hid an entire outage on 2026-08-10: the model returned
            # fluent prose, the panel rendered it, no action ever arrived, and
            # NOTHING logged — because _extract_actions_block only warns when a
            # block exists and won't parse, never when one is absent. The user
            # sees "why isn't this taking me places" and the server looks fine.
            logger.info(
                "assistant: reply carried no <ACTIONS> block "
                "(session=%s, tool_calls_this_turn=%d, chars=%d). If the user "
                "asked to go somewhere, the model either never called "
                "lookup_site_map or the serving stack did not parse the call "
                "back into tool_calls — check both before blaming the prompt.",
                sess.id, iteration, len(content or ""),
            )
        # Stream a final scrubbed assistant message? The visible text
        # has already streamed via text-token events; we keep the raw
        # for persistence. Action / citation events stream below.
        final_actions = []
        for action in raw_actions:
            v = _validate_action(action, site_map, action_allowlist)
            if v is None:
                continue
            # Spec: open-dialog for a dialog that isn't on the user's
            # current page SHALL be wrapped into a chain whose first
            # step navigates to a page that DOES host it. Without this
            # wrap, clicking the action button on a foreign page is a
            # silent no-op (the open-dialog listener only exists on
            # the page the dialog lives on).
            v = _maybe_wrap_open_dialog_with_navigate(
                v, site_map, current_page, current_app,
            )
            # Re-validate after the wrap — the new chain's children
            # still need vocabulary + allowlist + grounding checks.
            v = _validate_action(v, site_map, action_allowlist)
            if v is None:
                continue
            final_actions.append(v)
            yield _sse("action-emit", v)

        # Auto-emit a `support-contacted` receipt when this turn ran the
        # contact_support tool. The model SHOULD also emit one in its
        # actions block (the system prompt nudges it), but auto-emitting
        # from the tool result is the load-bearing guarantee — without
        # it, a model that forgets to author the action leaves the user
        # with no UI confirmation that the email actually went out.
        last_esc = ctx.get("_last_support_escalation")
        if last_esc and last_esc.get("ok"):
            already_emitted = any(
                a.get("action") == "support-contacted" for a in final_actions
            )
            if not already_emitted:
                receipt = _validate_action(
                    {
                        "action": "support-contacted",
                        "recipient": last_esc.get("recipient") or "",
                        "severity": last_esc.get("severity", "normal"),
                        "tenant_admin_count": int(
                            last_esc.get("tenant_admin_count") or 0
                        ),
                        "subject": last_esc.get("subject", ""),
                    },
                    site_map, action_allowlist,
                )
                if receipt is not None:
                    final_actions.append(receipt)
                    yield _sse("action-emit", receipt)
            ctx["_last_support_escalation"] = None  # consume once

        for handle in handles:
            cit = ctx["_citations"].get(handle)
            if cit is not None:
                yield _sse("citation", {
                    "handle": cit["handle"],
                    "title": cit["title"],
                    "url": cit["url"],
                })
        final_citation_handles = handles
        break
    else:
        # for-else: we hit the iteration cap.
        yield _sse("error", {
            "message": (
                "I couldn't figure this out — try rephrasing or check the docs."
            ),
        })

    latency_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    # Persist the SCRUBBED visible text — the structured actions already live in
    # actions_json, so the raw <ACTIONS>{...}</ACTIONS> block is redundant AND
    # leaks verbatim into the UI when the transcript is re-rendered (history
    # resume, escalation). _extract_actions_block returns the visible text with
    # the block removed; identical to final_text when there's no block.
    persisted_content, _, _ = _extract_actions_block(final_text)
    _persist_message(
        db, sess.id, role="assistant",
        content=persisted_content,
        actions_json=final_actions,
        model_id=model_id,
        prompt_tokens=total_prompt or None,
        completion_tokens=total_completion or None,
        latency_ms=latency_ms,
    )
    # GY.V10 — AI transparency log (EU AI Act / Colorado AI Act
    # enablement). Append-only metadata row: model identity, prompt
    # class, sha256 of the output (never the output itself), surface,
    # actor. Never stores prompt or reply text. Never raises — a
    # transparency-log failure must not break the reply.
    try:
        from services import ai_transparency
        ai_transparency.record_ai_interaction(
            db,
            tenant_id=current_user.tenant_id,
            user_id=getattr(current_user, "id", None),
            surface="assistant",
            model=model_id,
            prompt_class="chat",
            output_text=persisted_content,
            human_oversight=None,  # no human-confirm step on assistant chat
        )
    except Exception:  # noqa: BLE001 — belt-and-braces (helper never raises)
        import logging as _lg
        _lg.getLogger(__name__).warning(
            "GY.V10: assistant ai.interaction log failed (no content logged)")
    db.commit()
    yield _sse("done", {})


# ---------------------------------------------------------------------------
# Session expiry sweep (called by main.py APScheduler job).
# ---------------------------------------------------------------------------


def expire_sessions(db: Session) -> Dict[str, int]:
    """Delete AssistantSession rows whose last_active_at is older than
    the owning tenant's session_retention_hours. Returns counts for
    log output.
    """
    from sqlalchemy import text as sa_text
    # One SQL pass uses the per-tenant retention from
    # AssistantTenantConfig (default 1h when no row exists).
    result = db.execute(sa_text(
        """
        DELETE FROM assistant_sessions s
        USING (
          SELECT s2.id
          FROM assistant_sessions s2
          LEFT JOIN assistant_tenant_config c ON c.tenant_id = s2.tenant_id
          WHERE s2.last_active_at < (
            NOW() - (COALESCE(c.session_retention_hours, 1) || ' hours')::INTERVAL
          )
        ) victims
        WHERE s.id = victims.id
        """
    ))
    db.commit()
    return {"deleted": int(result.rowcount or 0)}

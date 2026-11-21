"""wizard-learning-from-feedback §3 — feedback ingestion.

Writes the feedback-event rows that drive every Phase 2-5 model's
training set. Two event tables (per the §1 schema):

- wizard_feedback_event — one row per wizard apply call (success
  AND 4xx outcomes). Action enum: accepted | dismissed_without_action
  | rejected_<error_code>.
- wizard_finding_action_event — one row per user action on a wizard
  audit finding (Fix-via-wizard / Open-node / Ignore /
  dismissed_without_action).

On successful apply, ALSO writes embeddings to:
- tenant_<id>_proposal_embeddings (full-fidelity, per-tenant tier)
- platform_proposal_embeddings_shared (config-stripped, cross-tenant)

Embedding writes are best-effort: if the embedding model isn't
loaded or the vector DB is unreachable, we log + skip rather than
failing the apply call. The user already got their proposal applied;
losing the training-signal write is recoverable, losing the apply
result is not.

Public surface:
- record_apply_event(...) -> Optional[int]   # returns feedback_event_id
- record_finding_action(...) -> Optional[int]
- record_per_tenant_embedding(tenant_id, feedback_event_id, canvas,
                              prompt, proposal)
- record_cross_tenant_embedding(feedback_event_id, canvas, prompt,
                                proposal, intent)
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text as _sa_text
from sqlalchemy.orm import Session

# GT.D4: services.wizard_ml is Paid (open-core manifest deny, MANAGED_AI) and
# absent from the CE tree. FEATURE_SET_VERSION is a plain version tag the
# feedback events record (P2.2 E9: constant "v1", no paid import).
FEATURE_SET_VERSION = "v1"

# EXPR 4.4 S3 — agent tool-dispatch quality signals ride this same table
# (owner Q1 REC: reuse wizard_feedback_event + new action codes; models.py
# is hot and the column set already fits — metadata JSONB carries tool_ref
# / failure_reason / purpose). The codes below are chosen to satisfy the
# live wizard_feedback_event.action CHECK
# (accepted | dismissed_without_action | downstream_edited | deleted
# | debug_fix_applied | rejected_%): an ok tool outcome records
# AGENT_TOOL_ACTION_OK, a failed one AGENT_TOOL_ACTION_FAILED.
AGENT_TOOL_ACTION_OK = "accepted"
AGENT_TOOL_ACTION_FAILED = "rejected_agent_tool"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Prometheus counters — wizard-learning-from-feedback §10
# ---------------------------------------------------------------------

def _record_feedback_event_metric(event_type: str, action: str) -> None:
    """Best-effort Prometheus counter increment for every successful
    feedback event write. event_type ∈ {apply, finding_action};
    action is the row's action enum value. Both labels are bounded
    cardinality (~10 values total)."""
    try:
        # P2.2 E10: the paid metrics module is gone; the CE stub registry in
        # the pro client emits the counter in-process (no round trip).
        from services.wizard_pro_client import REGISTRY as _CE_METRICS
        _CE_METRICS.record("wizard_feedback_events_total",
                           labels={"event_type": event_type, "action": action})
    except Exception:  # pragma: no cover
        logger.debug("wizard_feedback: metric inc failed", exc_info=True)


# ---------------------------------------------------------------------
# Event-row writers (main app DB)
# ---------------------------------------------------------------------

def record_apply_event(
    db: Session,
    *,
    tenant_id: int,
    proposal_id: int,
    apply_mode: str,
    action: str,
    was_edited: bool = False,
    time_to_action_ms: Optional[int] = None,
    applied_node_ids_diff: Optional[Dict[str, Any]] = None,
    intent: Optional[str] = None,
    wizard_test_run_id: Optional[int] = None,
    applied_workflow_version_id: Optional[int] = None,
) -> Optional[int]:
    """Write one row to wizard_feedback_event and return its id.
    Returns None on insert failure (logs but doesn't raise — the
    caller's apply response shouldn't depend on this succeeding).

    Action values: 'accepted', 'dismissed_without_action', or any
    'rejected_<error_code>' string. Validated by the table's CHECK
    constraint; passing an invalid value here surfaces as a
    psycopg2 error in the log + returns None.

    apply_mode: 'replace' or 'merge' (CHECK-constrained).

    workflow-agentic-execution-loop §wizard-agentic-loop —
    `wizard_test_run_id` (optional) references the terminal iteration
    of the agent loop that produced this workflow. Null for pre-
    agent-loop wizard turns. The column is added by an idempotent
    in-startup migration in database.py; the INSERT below tolerates
    pre-migration schemas via the try/except envelope.
    """
    try:
        diff_json = json.dumps(applied_node_ids_diff) if applied_node_ids_diff is not None else None
        row = db.execute(_sa_text("""
            INSERT INTO wizard_feedback_event
                (tenant_id, proposal_id, apply_mode, action, was_edited,
                 time_to_action_ms, applied_node_ids_diff, intent,
                 wizard_test_run_id, applied_workflow_version_id,
                 feature_set_version)
            VALUES
                (:tenant_id, :proposal_id, :apply_mode, :action, :was_edited,
                 :time_to_action_ms, CAST(:applied_node_ids_diff AS JSONB), :intent,
                 :wizard_test_run_id, :applied_workflow_version_id,
                 :feature_set_version)
            RETURNING id
        """), {
            "tenant_id": tenant_id,
            "proposal_id": proposal_id,
            "apply_mode": apply_mode,
            "action": action,
            "was_edited": was_edited,
            "time_to_action_ms": time_to_action_ms,
            "applied_node_ids_diff": diff_json,
            "intent": intent,
            "wizard_test_run_id": wizard_test_run_id,
            "applied_workflow_version_id": applied_workflow_version_id,
            "feature_set_version": FEATURE_SET_VERSION,
        }).fetchone()
        db.commit()
        event_id = int(row[0]) if row else None
        if event_id is not None:
            _record_feedback_event_metric("apply", action)
        return event_id
    except Exception as e:
        # Don't let a failed event write break the apply response.
        # Roll back so the caller's session is usable for the rest of
        # its work.
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "wizard_feedback: record_apply_event failed (tenant=%s proposal=%s action=%s): %s",
            tenant_id, proposal_id, action, e,
        )
        return None


def record_quality_signal(
    db: Session,
    *,
    tenant_id: int,
    proposal_id: int,
    apply_mode: str,
    action: str,
    applied_workflow_version_id: Optional[int] = None,
    intent: Optional[str] = None,
    wizard_test_run_id: Optional[int] = None,
    feedback_event_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """workflow-agentic-execution-loop §quality-signal — record a
    post-hoc quality signal tied back to a prior wizard proposal.

    `action` must be one of:
      - 'downstream_edited'  (user kept the workflow but modified the
                              graph after apply — inserted by the
                              background detector)
      - 'deleted'            (wizard-originated workflow soft-deleted)
      - 'dismissed_without_action'  (user closed wizard without applying)
      - 'rejected_<code>'    (apply attempt rejected — code is the
                              specific failure mode)
      - 'accepted'           (apply succeeded — for symmetry with
                              `record_apply_event`; prefer that helper
                              for the apply path)

    EXPR 4.4 S3 (workflow-expressiveness-primitives §4.4 quality-signal
    capture) also rides this table for agent tool dispatches, using the
    module constants above:
      - AGENT_TOOL_ACTION_OK ('accepted') — one row per ok tool outcome
      - AGENT_TOOL_ACTION_FAILED ('rejected_agent_tool') — one row per
        failed tool outcome (matches the `rejected_%` CHECK arm)
    Those rows carry no wizard proposal, so callers pass proposal_id=0
    and put the discriminating payload in `feedback_event_metadata`
    ({tool_ref, failure_reason, purpose, source: 'agent_node_runtime'}).

    Same best-effort semantics as `record_apply_event`: write failures
    log a warning and return None; the caller's response shouldn't
    depend on this succeeding.
    """
    try:
        meta_json = (
            json.dumps(feedback_event_metadata)
            if feedback_event_metadata is not None else None
        )
        row = db.execute(_sa_text("""
            INSERT INTO wizard_feedback_event
                (tenant_id, proposal_id, apply_mode, action,
                 intent, wizard_test_run_id,
                 applied_workflow_version_id, feedback_event_metadata,
                 feature_set_version)
            VALUES
                (:tenant_id, :proposal_id, :apply_mode, :action,
                 :intent, :wizard_test_run_id,
                 :applied_workflow_version_id,
                 CAST(:feedback_event_metadata AS JSONB),
                 :feature_set_version)
            RETURNING id
        """), {
            "tenant_id": tenant_id,
            "proposal_id": proposal_id,
            "apply_mode": apply_mode,
            "action": action,
            "intent": intent,
            "wizard_test_run_id": wizard_test_run_id,
            "applied_workflow_version_id": applied_workflow_version_id,
            "feedback_event_metadata": meta_json,
            "feature_set_version": FEATURE_SET_VERSION,
        }).fetchone()
        db.commit()
        event_id = int(row[0]) if row else None
        if event_id is not None:
            _record_feedback_event_metric("quality_signal", action)
        return event_id
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "wizard_feedback: record_quality_signal failed (tenant=%s proposal=%s action=%s): %s",
            tenant_id, proposal_id, action, e,
        )
        return None


def record_finding_action(
    db: Session,
    *,
    tenant_id: int,
    audit_session_id: str,
    finding_id: str,
    severity: str,
    action: str,
    finding_title_hash: Optional[str] = None,
) -> Optional[int]:
    """Write one row to wizard_finding_action_event and return its id.
    Same best-effort semantics as record_apply_event.

    severity: 'error' | 'warning' | 'info' (CHECK).
    action: 'fix_via_wizard' | 'open_node' | 'ignore' |
            'dismissed_without_action' (CHECK).
    """
    try:
        row = db.execute(_sa_text("""
            INSERT INTO wizard_finding_action_event
                (tenant_id, audit_session_id, finding_id, severity, action,
                 finding_title_hash)
            VALUES
                (:tenant_id, :audit_session_id, :finding_id, :severity, :action,
                 :finding_title_hash)
            RETURNING id
        """), {
            "tenant_id": tenant_id,
            "audit_session_id": audit_session_id,
            "finding_id": finding_id,
            "severity": severity,
            "action": action,
            "finding_title_hash": finding_title_hash or finding_id[:32],
        }).fetchone()
        db.commit()
        event_id = int(row[0]) if row else None
        if event_id is not None:
            _record_feedback_event_metric("finding_action", action)
        return event_id
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "wizard_feedback: record_finding_action failed (tenant=%s finding=%s action=%s): %s",
            tenant_id, finding_id, action, e,
        )
        return None


# ---------------------------------------------------------------------
# Embedding writers (separate vector DB)
# ---------------------------------------------------------------------

def _render_per_tenant_few_shot(
    canvas: Optional[Dict[str, Any]],
    prompt: Optional[str],
    proposal: Optional[Dict[str, Any]],
) -> str:
    """Render a (canvas, prompt, proposal) tuple as a few-shot block
    suitable for the Builder system prompt. Stored on the per-tenant
    embedding row at write time; retrieved verbatim at request time
    so the Builder gets a concrete example without us round-tripping
    to support_chat_messages.

    P2.2 E11: the paid `serialise_*` helpers moved to the sidecar; this
    CE-side render mirrors their output shape (stable sort + truncated
    configs) so stored few-shot blocks keep their format."""
    return (
        f"PROMPT: {(prompt or '').strip()}\n"
        f"CANVAS: {_serialise_canvas(canvas)}\n"
        f"PROPOSAL: {_serialise_proposal(proposal)}"
    )


def _serialise_canvas(canvas: Optional[Dict[str, Any]]) -> str:
    """Stable full-fidelity render of a canvas (mirrors the paid
    serialise_canvas shape: NODES[name:cfg] EDGES[src->tgt]). Delegates to the
    Pro client's CE-side mirror so the render has one source of truth."""
    from services import wizard_pro_client as _pro
    return _pro._serialise_canvas(canvas)


def _serialise_proposal(proposal: Optional[Dict[str, Any]]) -> str:
    """Stable full-fidelity render of a proposal (mirrors the paid
    serialise_proposal: NAME[name] + the canvas shape of its body)."""
    from services import wizard_pro_client as _pro
    return _pro._serialise_proposal(proposal)


# ---------------------------------------------------------------------
# Cross-tenant strip pipeline (P2.2 E15 CE mirror)
#
# The paid services/wizard_ml/cross_tenant_strip.py moved to the
# mod-wizard-pro sidecar. This is a faithful CE-side mirror of its
# privacy contract (design.md D1b): configs/data/labels dropped, raw
# node ids canonicalised positionally, prompt reduced to
# (intent, allow-listed verbs), proposal name dropped. The stored
# few_shot_text must stay in the same format as before.
# ---------------------------------------------------------------------

_CROSS_TENANT_INTENTS = (
    "from_scratch",
    "suggest_next",
    "fill_gap",
    "refactor_selection",
    "audit_graph",
)

_CROSS_TENANT_VERBS = frozenset({
    "process", "transform", "extract", "generate", "create", "build",
    "combine", "merge", "split", "filter", "convert", "encode",
    "decode", "compress", "render", "resize", "crop", "scale",
    "upload", "download", "fetch", "query", "search", "classify",
    "detect", "analyze", "validate", "audit", "review", "fix",
    "refactor", "optimize", "compile", "package", "deploy", "publish",
    "schedule", "trigger", "notify", "log", "store", "save", "load",
    "read", "write", "update", "delete", "remove", "add", "insert",
    "append", "prepend", "replace", "rewrite", "summarize", "translate",
    "embed", "rank", "sort", "group", "join", "aggregate", "count",
    "preview", "thumbnail", "transcode", "stream", "buffer", "cache",
    "iterate", "loop", "batch", "queue", "dispatch", "execute", "run",
})

_CROSS_TENANT_PATH_TOKEN_RE = re.compile(r"[/\\]|^[A-Z]:|://")
_CROSS_TENANT_QUOTED_RE = re.compile(r'^["\'].*["\']$')
_CROSS_TENANT_MIXED_ID_RE = re.compile(r"^[A-Za-z0-9_-]*[0-9][A-Za-z0-9_-]*$")
_CROSS_TENANT_PROPER_NOUN_RE = re.compile(r"^[A-Z][a-z]+([A-Z][a-z]*)*$")


def _strip_token_droppable(tok: str) -> bool:
    if not tok:
        return True
    if _CROSS_TENANT_PATH_TOKEN_RE.search(tok):
        return True
    if _CROSS_TENANT_QUOTED_RE.match(tok):
        return True
    if _CROSS_TENANT_MIXED_ID_RE.match(tok):
        return True
    if _CROSS_TENANT_PROPER_NOUN_RE.match(tok):
        return True
    return False


def _strip_summarise_prompt(prompt: Optional[str]) -> str:
    if not prompt:
        return ""
    keep: List[str] = []
    for raw in re.split(r"\s+", prompt.strip()):
        tok = raw.strip(".,;:!?()[]{}<>")
        if _strip_token_droppable(tok):
            continue
        low = tok.lower()
        if low in _CROSS_TENANT_VERBS:
            keep.append(low)
    return " ".join(keep)


def _strip_node(node: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "id" in node:
        out["id"] = node["id"]
    nd_name = node.get("node_definition_name")
    if nd_name is None:
        nd_name = (node.get("data") or {}).get("nodeDefinitionName")
    if nd_name is not None:
        out["node_definition_name"] = nd_name
    data = node.get("data") or {}
    in_types = [i["type"] for i in (data.get("inputs") or [])
                if isinstance(i, dict) and "type" in i]
    out_types = [o["type"] for o in (data.get("outputs") or [])
                 if isinstance(o, dict) and "type" in o]
    if in_types or out_types:
        out["port_signature"] = {"inputs": in_types, "outputs": out_types}
    return out


def _strip_edge(edge: Dict[str, Any]) -> Dict[str, Any]:
    return {k: edge[k] for k in ("source", "target", "sourceHandle",
                                 "targetHandle", "anchor_source",
                                 "anchor_target") if k in edge}


def _strip_canonicalise(
    nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Replace raw ids with positional n0, n1, ... in stable id-sort
    order (GAP-2: raw ids can carry tenant/session signal)."""
    width = max(1, len(str(max(len(nodes) - 1, 0))))
    order = sorted(range(len(nodes)), key=lambda i: str(nodes[i].get("id", "")))
    id_map: Dict[Any, str] = {}
    canonical_nodes = [dict(n) for n in nodes]
    for rank, i in enumerate(order):
        canonical_id = f"n{rank:0{width}d}"
        canonical_nodes[i]["id"] = canonical_id
        orig_id = nodes[i].get("id")
        if orig_id is not None and orig_id not in id_map:
            id_map[orig_id] = canonical_id
    canonical_edges = []
    for e in edges:
        new_e = dict(e)
        if "source" in new_e:
            new_e["source"] = id_map.get(new_e["source"], new_e["source"])
        if "target" in new_e:
            new_e["target"] = id_map.get(new_e["target"], new_e["target"])
        canonical_edges.append(new_e)
    return canonical_nodes, canonical_edges


def _strip_for_cross_tenant(
    canvas: Optional[Dict[str, Any]],
    prompt: Optional[str],
    proposal: Optional[Dict[str, Any]],
    *,
    intent: Any = "from_scratch",
) -> Tuple[Dict[str, Any], Tuple[str, str], Dict[str, Any]]:
    canvas = canvas or {}
    proposal = proposal or {}
    c_nodes = [_strip_node(n) for n in (canvas.get("nodes") or [])]
    c_edges = [_strip_edge(e) for e in (canvas.get("edges") or [])]
    c_nodes, c_edges = _strip_canonicalise(c_nodes, c_edges)
    intent = intent if intent in _CROSS_TENANT_INTENTS else "from_scratch"
    p_nodes = [_strip_node(n) for n in (proposal.get("nodes") or [])]
    p_edges = [_strip_edge(e) for e in (proposal.get("edges") or [])]
    p_nodes, p_edges = _strip_canonicalise(p_nodes, p_edges)
    return (
        {"nodes": c_nodes, "edges": c_edges},
        (intent, _strip_summarise_prompt(prompt)),
        {"nodes": p_nodes, "edges": p_edges},
    )


def _cross_tenant_few_shot(
    canvas: Optional[Dict[str, Any]],
    prompt: Optional[str],
    proposal: Optional[Dict[str, Any]],
    *,
    intent: Any,
) -> str:
    """Render the stripped tuple exactly like the paid
    serialize_stripped: INTENT[..] VERBS[..] CANVAS[..] PROPOSAL[..]."""
    canvas_s, (intent_s, verbs), proposal_s = _strip_for_cross_tenant(
        canvas, prompt, proposal, intent=intent)

    def render_graph(g: Dict[str, Any]) -> str:
        nodes = sorted(g["nodes"], key=lambda n: str(n.get("id", "")))
        edges = sorted(
            g["edges"],
            key=lambda e: (str(e.get("source", "")), str(e.get("target", ""))),
        )
        node_strs = [f"{n.get('node_definition_name', '?')}" for n in nodes]
        edge_strs = [f"{e.get('source', '?')}->{e.get('target', '?')}"
                     for e in edges]
        return f"NODES[{','.join(node_strs)}] EDGES[{','.join(edge_strs)}]"

    return (
        f"INTENT[{intent_s}] VERBS[{verbs}] "
        f"CANVAS[{render_graph(canvas_s)}] "
        f"PROPOSAL[{render_graph(proposal_s)}]"
    )


def record_per_tenant_embedding(
    *,
    tenant_id: int,
    feedback_event_id: int,
    canvas: Optional[Dict[str, Any]],
    prompt: Optional[str],
    proposal: Optional[Dict[str, Any]],
) -> bool:
    """Embed the full-fidelity (canvas, prompt, proposal) record and
    write it to the tenant's per-tenant embedding table along with
    the rendered few_shot_text. Best-effort — returns True on
    success, False on any failure (logged).

    Lazily provisions the per-tenant table on first call.
    """
    try:
        from services import wizard_pro_client as _pro
        from database_wizard_vector import (
            ensure_tenant_embedding_table,
            get_engine,
        )
        from sqlalchemy import text

        vec = _pro.embed_per_tenant(canvas, prompt, proposal, tenant_id=tenant_id)
        if vec is None:  # sidecar absent — skip the write (existing guard)
            return False
        # E11: store the few-shot block for this record — the sidecar's
        # retrieval pool when present, else the CE-side render of THIS record
        # (the stored block keeps its format either way).
        few_shot = _pro.few_shot_block(canvas, prompt, proposal)
        table_name = ensure_tenant_embedding_table(tenant_id)
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text(f"""
                INSERT INTO {table_name}
                    (feedback_event_id, embedding, embedding_model_version, few_shot_text)
                VALUES
                    (:feedback_event_id, :embedding, :model_version, :few_shot_text)
            """), {
                "feedback_event_id": feedback_event_id,
                "embedding": str(vec),
                "model_version": _pro.model_version(),
                "few_shot_text": few_shot,
            })
        return True
    except Exception as e:
        logger.warning(
            "wizard_feedback: record_per_tenant_embedding failed (tenant=%s feedback=%s): %s",
            tenant_id, feedback_event_id, e,
        )
        return False


def record_cross_tenant_embedding(
    *,
    feedback_event_id: int,
    canvas: Optional[Dict[str, Any]],
    prompt: Optional[str],
    proposal: Optional[Dict[str, Any]],
    intent: Optional[str] = None,
) -> bool:
    """Strip the record via the §2b privacy contract, embed the
    stripped form, and write to the cross-tenant shared table.
    Best-effort.

    Gated by the WIZARD_RAG_CROSS_TENANT_ENABLED kill switch — when
    off, this function no-ops without writing. Defensive: even if a
    caller forgets to gate, the Vault-backed config flag is checked
    here too.
    """
    try:
        from services import wizard_pro_client as _pro
        cfg = _pro.wizard_config()  # {} when the sidecar is absent -> cross-tenant off
        if not (cfg.get("toggles") or cfg).get("rag_cross_tenant", False):
            return False
    except Exception as e:
        logger.debug("wizard_feedback: config read failed; skipping cross-tenant embed: %s", e)
        return False

    try:
        from services import wizard_pro_client as _pro
        from database_wizard_vector import (
            ensure_shared_embedding_table,
            get_engine,
            shared_table_name,
        )
        from sqlalchemy import text

        # The sidecar strips + embeds internally (E15 privacy contract: raw
        # record never leaves CE unstripped); the CE-side mirror below renders
        # the same stripped few_shot_text stored alongside.
        intent = intent or "from_scratch"
        few_shot = _cross_tenant_few_shot(canvas, prompt, proposal, intent=intent)
        vec = _pro.embed_cross_tenant(canvas, prompt, proposal, intent=intent)
        if vec is None:  # sidecar absent — skip the write (existing guard)
            return False
        ensure_shared_embedding_table()  # idempotent
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text(f"""
                INSERT INTO {shared_table_name()}
                    (feedback_event_id, embedding, embedding_model_version, intent, few_shot_text)
                VALUES
                    (:feedback_event_id, :embedding, :model_version, :intent, :few_shot_text)
            """), {
                "feedback_event_id": feedback_event_id,
                "embedding": str(vec),
                "model_version": _pro.model_version(),
                "intent": intent or "from_scratch",
                "few_shot_text": few_shot,
            })
        return True
    except Exception as e:
        logger.warning(
            "wizard_feedback: record_cross_tenant_embedding failed (feedback=%s): %s",
            feedback_event_id, e,
        )
        return False


def record_apply_embeddings(
    *,
    tenant_id: int,
    feedback_event_id: int,
    canvas: Optional[Dict[str, Any]],
    prompt: Optional[str],
    proposal: Optional[Dict[str, Any]],
    intent: Optional[str] = None,
    was_edited: bool = False,
) -> Dict[str, bool]:
    """Convenience wrapper — writes both the per-tenant and the
    cross-tenant embedding (when enabled). Returns a dict
    {per_tenant, cross_tenant, skipped_reason} indicating outcomes.

    When `was_edited=True`, BOTH writes are skipped. The retrieval
    pool intentionally excludes user-edited proposals — mixing
    "accepted as-is" and "accepted with edits" weakens the few-shot
    quality, since the LLM should mimic only the patterns users
    actually wanted, not the patterns the LLM proposed and the user
    half-corrected. The Phase 2 reranker training set sees both via
    the feedback event table; only the retrieval pool is filtered.
    """
    if was_edited:
        return {
            "per_tenant": False,
            "cross_tenant": False,
            "skipped_reason": "was_edited",
        }
    return {
        "per_tenant": record_per_tenant_embedding(
            tenant_id=tenant_id,
            feedback_event_id=feedback_event_id,
            canvas=canvas, prompt=prompt, proposal=proposal,
        ),
        "cross_tenant": record_cross_tenant_embedding(
            feedback_event_id=feedback_event_id,
            canvas=canvas, prompt=prompt, proposal=proposal,
            intent=intent,
        ),
    }

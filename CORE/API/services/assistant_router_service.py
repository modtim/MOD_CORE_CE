"""Service layer for the assistant admin/config/usage sub-module (Gate 2b
migration of ``routers/assistant.py``).

Named ``assistant_router_service`` (matching ``crud/assistant_router.py``)
to avoid clashing with ``services/assistant_support_service.py`` (the
support-chat sub-module) and ``services/assistant_orchestrator.py`` (the
§5 chat/escalate orchestrator — untouched by this migration; see below).

Owns the config default-fallback shape, the history transcript scrub +
retention gate, the action-allowlist vocabulary check, and the usage
roll-up shaping that ``routers/assistant.py``'s handlers previously
inlined next to their ``db.query`` calls.

NOT migrated here (by design, not oversight):
  * ``chat`` / ``escalate`` in ``routers/assistant.py`` have zero inline
    ``db.query`` — they delegate straight to ``services/assistant_orchestrator``.
    They are also asserted byte-for-byte by
    ``tests/test_assistant_limits.py`` (``inspect.getsource`` checks on the
    exact position of ``llm_rate_limit.acquire``/``finally:`` relative to
    ``StreamingResponse``), so they are left completely untouched.
  * ``_is_tenant_admin`` / ``_require_platform_admin`` stay defined in
    ``routers/assistant.py``: they are pure attribute checks with no data
    access (not the CRUD-fetch-then-authorize shape migrate_rules #5 is
    about), and ``_is_tenant_admin`` is imported directly by
    ``routers/assistant_knowledge.py`` (``from routers.assistant import
    _is_tenant_admin``) — moving it would need a re-export anyway, so it
    simply stays put.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session

import models
from crud import assistant_router as crud
from services._base import ServiceError


class ValidationError(ServiceError):
    """Caller input fails a domain rule the request schema alone can't
    express (router maps -> 400). ``services/_base.py`` ships
    ``NotFoundError`` / ``ConflictError`` but nothing for this case."""

    def __init__(self, message: str = "Invalid request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


# §9 vocabulary for the action allowlist. Persisting only the *deviations*
# from "all allowed" keeps the JSONB column terse and means the spec's
# "missing keys are allowed" default behaviour falls out automatically.
_ACTION_VOCABULARY = (
    "navigate",
    "open-new-tab",
    "open-dialog",
    "open-tab",
    "scroll-to",
    "highlight",
    "chain",
)

# mod-assistant-agent §7.6 — the launcher button needs to know whether the
# assistant feature is on for the requesting user's tenant before it
# renders. Returning the full per-tenant config (not just `enabled`) here
# lets the launcher avoid a second round-trip when the panel eventually
# wires in §8 — same response shape regardless of caller surface.
_DEFAULT_ACTION_ALLOWLIST: dict = {}  # empty -> all actions implicitly allowed

# Defensively strip any raw <ACTIONS>{...}</ACTIONS> block — assistant
# messages persisted before the orchestrator started scrubbing them carry
# the block in `content`, and it must NEVER render verbatim in the panel.
# Structured actions come back via `actions`, so the block is redundant.
_ACTIONS_BLOCK_RE = re.compile(r"<ACTIONS>.*?</ACTIONS>", re.DOTALL | re.IGNORECASE)


def _row_to_dict(row: models.AssistantTenantConfig) -> dict:
    return {
        "enabled": bool(row.enabled),
        "model_id": row.model_id,
        "system_prompt_override": row.system_prompt_override,
        "action_allowlist": row.action_allowlist or _DEFAULT_ACTION_ALLOWLIST,
        "logs_context_enabled": bool(row.logs_context_enabled),
        "session_retention_hours": int(row.session_retention_hours),
        "knowledge_base_enabled": bool(row.knowledge_base_enabled),
    }


def _platform_admin_enabled() -> bool:
    """GE.MAA.2 (mod-assistant-agent §9.6/9.7) — the platform-admin kill
    switch (`PUT /platform/assistant/config`) is ANDed into every tenant's
    effective `enabled` here, so the one place the launcher already checks
    (this GET) reflects it without touching the chat/escalate endpoints
    that are asserted byte-for-byte elsewhere. Fails open (True) — a Vault
    hiccup must not silently disable the assistant for every tenant."""
    try:
        from services import assistant_platform_config_service as platform_svc
        return platform_svc.is_platform_enabled()
    except Exception:
        return True


def get_config(db: Session, tenant_id: int) -> dict:
    """Return the tenant's assistant config, or the platform defaults if the
    backfilled row is ever missing (defensive fallback instead of a 500).
    `enabled` is ANDed with the platform-admin kill switch."""
    row = crud.get_tenant_config(db, tenant_id)
    if row is None:
        cfg = {
            "enabled": True,
            "model_id": None,
            "system_prompt_override": None,
            "action_allowlist": _DEFAULT_ACTION_ALLOWLIST,
            "logs_context_enabled": True,
            "session_retention_hours": 1,
            "knowledge_base_enabled": False,
        }
    else:
        cfg = _row_to_dict(row)
    cfg["enabled"] = bool(cfg["enabled"]) and _platform_admin_enabled()
    return cfg


def get_logs_assistant_view(db: Session, tenant_id: int) -> dict:
    """B31-U1 -- the read-only slice of the tenant's AssistantTenantConfig the
    Logs / run-details assistant honours (ONE assistant config; the Logs chat
    no longer has settings of its own). Reuses ``get_config`` so the defaults
    (no row -> enabled, no model, no override, logs context on) and the
    platform kill-switch AND are identical to the Settings chat agent."""
    cfg = get_config(db, tenant_id)
    return {
        "enabled": bool(cfg["enabled"]),
        "model_id": (cfg.get("model_id") or "").strip(),
        "system_prompt_override": (cfg.get("system_prompt_override") or "").strip(),
        "logs_context_enabled": bool(cfg["logs_context_enabled"]),
    }


def get_history(db: Session, *, user_id: int, tenant_id: int) -> Dict[str, Any]:
    """Resolve the caller's most-recent ACTIVE session (within the tenant's
    retention window) + its scrubbed transcript, or the empty shape."""
    cfg = crud.get_tenant_config(db, tenant_id)
    # Disabled tenants get nothing (mirrors the launcher's enable gate).
    if cfg is not None and not cfg.enabled:
        return {"session_id": None, "messages": []}
    retention_hours = int(cfg.session_retention_hours) if cfg is not None else 1
    cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)

    sess = crud.get_recent_session(db, user_id=user_id, tenant_id=tenant_id, cutoff=cutoff)
    if sess is None:
        return {"session_id": None, "messages": []}

    rows = crud.list_session_messages(db, sess.id)
    messages = []
    for m in rows:
        content = _ACTIONS_BLOCK_RE.sub("", m.content or "").strip()
        actions = m.actions_json or []
        if content or actions:
            messages.append({"role": m.role, "content": content, "actions": actions})
    return {"session_id": str(sess.id), "messages": messages}


def update_config(
    db: Session, *, tenant_id: int, actor_id: int, updates: Dict[str, Any],
) -> dict:
    """Apply a partial update (``updates`` is already the router's
    ``_ConfigUpdate.model_dump(exclude_unset=True)``) to the tenant's config,
    audit the deltas, and return the new shape.

    Fields omitted from ``updates`` are not touched. An empty-string scalar
    normalizes to NULL (clear-the-override semantics). Raises
    ``ValidationError`` if ``action_allowlist`` contains an unknown action
    name, so a typo doesn't silently disable nothing or gate a future action.
    """
    action_allowlist = updates.get("action_allowlist")
    if action_allowlist is not None:
        for key in action_allowlist.keys():
            if key not in _ACTION_VOCABULARY:
                raise ValidationError(
                    f"unknown action {key!r} in action_allowlist; "
                    f"valid actions: {list(_ACTION_VOCABULARY)}"
                )

    row = crud.get_tenant_config(db, tenant_id)
    if row is None:
        # Defensive: backfill should have caught this, but if a tenant
        # slipped through, create the row inline so the save succeeds.
        row = models.AssistantTenantConfig(tenant_id=tenant_id)
        db.add(row)

    # Track deltas for the audit row so an operator can answer "what did
    # the tenant admin change".
    deltas: dict = {}
    for field, new_val in updates.items():
        if isinstance(new_val, str) and new_val == "":
            new_val = None
        old_val = getattr(row, field)
        if isinstance(old_val, str) and old_val == "":
            old_val = None
        if old_val != new_val:
            deltas[field] = {"from": old_val, "to": new_val}
            setattr(row, field, new_val)
    row.last_modified_by = actor_id
    row.last_modified_at = datetime.now(timezone.utc)

    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        user_id=actor_id,
        event_name="assistant.config.update",
        details={"deltas": deltas} if deltas else {"deltas": {}, "no_op": True},
    ))
    db.commit()
    db.refresh(row)
    return _row_to_dict(row)


def get_usage(db: Session, *, tenant_id: int, days: int) -> Dict[str, Any]:
    """Aggregate usage for the tenant over the last ``days`` days: total
    sessions/messages + by-model token spend. Raises ``ValidationError`` if
    ``days`` is out of the accepted ``1..365`` range."""
    if days <= 0 or days > 365:
        raise ValidationError("days must be in 1..365")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    total_sessions = crud.count_sessions_since(db, tenant_id=tenant_id, cutoff=cutoff)
    total_messages = crud.count_messages_since(db, tenant_id=tenant_id, cutoff=cutoff)

    by_model = [
        {
            "model_id": (row.model_id or "unknown"),
            "prompt_tokens": int(row.prompt or 0),
            "completion_tokens": int(row.completion or 0),
            "messages": int(row.messages or 0),
        }
        for row in crud.usage_by_model_since(db, tenant_id=tenant_id, cutoff=cutoff)
    ]

    return {
        "days": days,
        "total_sessions": int(total_sessions),
        "total_messages": int(total_messages),
        "by_model": by_model,
        # Top intents deferred — see the router handler's docstring.
        "top_intents": [],
    }

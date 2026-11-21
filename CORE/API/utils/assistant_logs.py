"""MOD Assistant — tenant-scoped, read-only log fetch.

Used by the orchestrator (lands in §5) to give the chat agent
troubleshooting context for "why is my run failing?"-style turns.
The helper is read-only and tenant-scoped *by construction* —
the tenant_id comes from the authenticated user passed in, not
from any caller-supplied parameter, so a buggy or malicious
caller can't cross tenants.

Three log surfaces are unioned:
  - "workflow"  — WorkflowRunEvent rows where source != 'worker'
                  (orchestrator / traffic-cop), joined to
                  workflow_runs.tenant_id for the gate.
  - "worker"    — WorkflowRunEvent rows where source == 'worker'
                  (same table, different source-tag), same join.
  - "api"       — AuditEvent rows where tenant_id matches.

WorkflowRunEvent has its own level column ('info'|'warn'|'error');
AuditEvent has no level column so we surface every audit row as
level='info' — `level='error'` filtered fetches simply skip the
api source. That's the right shape: a tenant asking "show me errors"
doesn't want their own audit trail mixed in.

Every invocation writes an AuditEvent row capturing the filter
params + rows_returned. If that audit write fails the helper
raises AuditWriteFailed — it does NOT return partial results
silently. Without the audit trail the access SHALL not happen.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

# Source / level enums. "all" means no filter on that axis.
VALID_SOURCES = frozenset({"api", "worker", "workflow", "all"})
VALID_LEVELS = frozenset({"info", "warn", "error", "all"})

DEFAULT_SINCE_SECONDS = 24 * 3600  # 24h
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

# Hard cap on the rendered text length per AuditEvent row so a
# massive details JSON can't blow the orchestrator's context window
# and so we never echo an entire request payload into a chat reply.
_API_MESSAGE_CAP = 500


class AuditWriteFailed(RuntimeError):
    """Audit-log insert for the helper invocation failed. Per
    mod-assistant-agent / specs / mod-assistant-logs-context: when
    the audit row can't persist, the helper refuses to return.
    Callers (the §5 orchestrator) catch this and emit a no-logs
    fallback to the user.
    """


def _audit_invocation(
    db: Session,
    current_user: models.User,
    params: Dict[str, Any],
    rows_returned: int,
) -> None:
    """Write a single AuditEvent row capturing this fetch. Raises
    AuditWriteFailed on any persistence error.

    The audit row's `details` JSONB carries the filter params plus
    `rows_returned`; that's enough for an operator answering
    "what did the assistant see on behalf of user X" without
    needing to replay the underlying SQL.
    """
    try:
        db.add(models.AuditEvent(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="assistant.logs.fetch",
            details={**params, "rows_returned": rows_returned, "via": "assistant"},
        ))
        db.commit()
    except Exception as e:
        logger.error("assistant_logs: audit write failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        raise AuditWriteFailed(str(e)) from e


def _fetch_workflow_or_worker(
    db: Session,
    tenant_id: int,
    cutoff: datetime,
    source_kind: str,            # 'workflow' | 'worker'
    level: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """One side of the WorkflowRunEvent split.

    `source_kind='worker'` returns rows where the event's source is
    literally 'worker'; `'workflow'` returns everything else
    (orchestrator + traffic-cop). The join through workflow_runs
    is the tenant gate — no row escapes without
    workflow_runs.tenant_id == tenant_id.
    """
    q = (
        db.query(models.WorkflowRunEvent, models.WorkflowRun.id.label("run_id"))
        .join(
            models.WorkflowRun,
            models.WorkflowRunEvent.workflow_run_id == models.WorkflowRun.id,
        )
        .filter(models.WorkflowRun.tenant_id == tenant_id)
        .filter(models.WorkflowRunEvent.ts >= cutoff)
    )
    if source_kind == "worker":
        q = q.filter(models.WorkflowRunEvent.source == "worker")
    else:
        q = q.filter(models.WorkflowRunEvent.source != "worker")
    if level != "all":
        q = q.filter(models.WorkflowRunEvent.level == level)
    q = q.order_by(desc(models.WorkflowRunEvent.ts)).limit(limit)

    out: List[Dict[str, Any]] = []
    for ev, run_id in q.all():
        out.append({
            "timestamp": ev.ts.isoformat() if ev.ts else None,
            "level": ev.level,
            "source": "worker" if source_kind == "worker" else "workflow",
            "message": ev.message,
            "related_workflow_run_id": run_id,
            "related_worker_id": None,  # not stored at row granularity in WorkflowRunEvent
        })
    return out


def _fetch_api(
    db: Session,
    tenant_id: int,
    cutoff: datetime,
    level: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """AuditEvent → 'api' source. AuditEvent rows are intrinsically
    informational; if the caller asked for `level='error'` or
    `'warn'` exclusively, return empty rather than fabricating
    misleading levels.
    """
    if level not in ("info", "all"):
        return []
    q = (
        db.query(models.AuditEvent)
        .filter(models.AuditEvent.tenant_id == tenant_id)
        .filter(models.AuditEvent.timestamp >= cutoff)
        .order_by(desc(models.AuditEvent.timestamp))
        .limit(limit)
    )
    out: List[Dict[str, Any]] = []
    for ev in q.all():
        details_text = json.dumps(ev.details or {}, default=str)
        msg = f"{ev.event_name}: {details_text}"
        if len(msg) > _API_MESSAGE_CAP:
            msg = msg[:_API_MESSAGE_CAP - 3] + "..."
        out.append({
            "timestamp": ev.timestamp.isoformat() if ev.timestamp else None,
            "level": "info",
            "source": "api",
            "message": msg,
            "related_workflow_run_id": None,
            "related_worker_id": None,
        })
    return out


def fetch(
    db: Session,
    current_user: models.User,
    since_seconds: int = DEFAULT_SINCE_SECONDS,
    level: str = "all",
    source: str = "all",
    limit: int = DEFAULT_LIMIT,
    *,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return up to `limit` recent log rows for `current_user`'s tenant.

    Args:
        db: SQLAlchemy session. The helper writes one AuditEvent row
            on success and commits.
        current_user: authenticated user. tenant_id is taken from
            current_user.tenant_id and is the ONLY source of truth
            for tenant scoping — any other caller-supplied tenant
            hint is ignored by construction (we never expose one).
        since_seconds: lookback window in seconds; default 24h.
        level: 'info' | 'warn' | 'error' | 'all'. Filters
            WorkflowRunEvent rows. AuditEvent rows are 'info' by
            definition, so a 'warn'-only or 'error'-only fetch
            simply skips the api source.
        source: 'api' | 'worker' | 'workflow' | 'all'.
        limit: per-query cap; output is also clamped to this after
            the union-sort step.
        session_id: passed through to the audit row so an operator
            can correlate fetches to a specific chat session.

    Returns:
        List of dicts with shape
        {timestamp, level, source, message, related_workflow_run_id,
         related_worker_id}, ordered by timestamp descending.

    Raises:
        ValueError: invalid `level` / `source` / `limit`.
        AuditWriteFailed: the audit-log insert failed; no rows
            returned. The orchestrator catches this and emits a
            "logs unavailable" message.
    """
    if source not in VALID_SOURCES:
        raise ValueError(
            f"invalid source: {source!r}; want one of {sorted(VALID_SOURCES)}"
        )
    if level not in VALID_LEVELS:
        raise ValueError(
            f"invalid level: {level!r}; want one of {sorted(VALID_LEVELS)}"
        )
    if limit <= 0 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be in 1..{MAX_LIMIT}; got {limit}")
    if since_seconds <= 0:
        raise ValueError(f"since_seconds must be > 0; got {since_seconds}")

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=since_seconds)
    tenant_id = current_user.tenant_id

    rows: List[Dict[str, Any]] = []
    if source in ("workflow", "all"):
        rows.extend(_fetch_workflow_or_worker(
            db, tenant_id, cutoff, "workflow", level, limit
        ))
    if source in ("worker", "all"):
        rows.extend(_fetch_workflow_or_worker(
            db, tenant_id, cutoff, "worker", level, limit
        ))
    if source in ("api", "all"):
        rows.extend(_fetch_api(db, tenant_id, cutoff, level, limit))

    # Union sort + final cap. The per-source limits above bound
    # how big the in-memory list ever gets (3*limit), so this
    # sort is bounded regardless of how chatty any one source is.
    rows.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    rows = rows[:limit]

    _audit_invocation(
        db,
        current_user,
        params={
            "since_seconds": since_seconds,
            "level": level,
            "source": source,
            "limit": limit,
            "session_id": session_id,
        },
        rows_returned=len(rows),
    )

    return rows

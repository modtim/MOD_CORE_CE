"""CRUD layer for ``routers/support_chat/_shared.py`` (Gate 2b migration).

``_shared.py`` is NOT a router — it has no ``@router``-decorated
handlers of its own. It hosts the helper functions, Pydantic DTOs and
constants that ``assistant_support.py`` / ``workflows.py`` /
``node_wizard.py`` / ``projects_agentic.py`` import (``from ._shared
import *``) and that ``routers/support_chat/__init__.py`` re-exports for
backwards compatibility (``from routers.support_chat import <name>``).

This module is the ONLY place ``db.query(models.X)`` lives for the
db.query sites that were inline in those _shared.py helpers. See
``services/_base.py`` for the layering contract and
``services/support_chat_shared_service.py`` for the policy that used to
sit next to these queries.

Every fetch-by-id here is caller-UNSCOPED by design (no tenant/user
filter baked in) — ownership/tenant policy is applied one layer up in
the service, so a legitimate 403 never collapses into a 404
(migrate_rules #5). None of the touched models
(``SupportChatSession``, ``SupportChatMessage``, ``PersistentStack``,
``WorkflowRun``, ``ApiTaskQueue``, ``WorkflowTemplate``, ``Tenant``,
``ContentBlob``, ``WorkflowNodeOutput``, ``Workflow``,
``WorkflowVersion``) get extra filters here beyond what the
pre-migration inline queries already had — filters are ported 1:1.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_session_by_id(db: Session, session_id: str) -> Optional[models.SupportChatSession]:
    """Fetch one support-chat session by id. Unscoped by design — the
    service layer applies the user_id/kind/task_id policy."""
    return (
        db.query(models.SupportChatSession)
          .filter(models.SupportChatSession.id == session_id)
          .first()
    )


def get_running_reasoner_stack(db: Session) -> Optional[models.PersistentStack]:
    """The ``managed-ai-reasoner`` PersistentStack row, only if its
    status is currently ``running`` — used by the wizard builder to
    prefer a live reasoner stack over the wizard tool's configured env."""
    return (
        db.query(models.PersistentStack)
          .filter(models.PersistentStack.name == "managed-ai-reasoner",
                  models.PersistentStack.status == "running")
          .first()
    )


def get_workflow_run_by_id(db: Session, run_id: int) -> Optional[models.WorkflowRun]:
    """Fetch a WorkflowRun by id. Unscoped — tenant policy is applied by
    the caller (logs-chat task context tenant-scopes non-admin askers)."""
    return (
        db.query(models.WorkflowRun)
          .filter(models.WorkflowRun.id == run_id)
          .first()
    )


def get_api_task_by_id(db: Session, task_id: int) -> Optional[models.ApiTaskQueue]:
    """Fetch an ApiTaskQueue row by id (system/build task namespace for
    logs-chat task context)."""
    return (
        db.query(models.ApiTaskQueue)
          .filter(models.ApiTaskQueue.id == task_id)
          .first()
    )


def list_session_messages_created_at_asc(
    db: Session, session_id: str,
) -> List[models.SupportChatMessage]:
    """All messages of a session, oldest first by ``created_at`` — the
    agentic-loop pipeline's prior-turn replay order."""
    return (
        db.query(models.SupportChatMessage)
          .filter(models.SupportChatMessage.session_id == session_id)
          .order_by(models.SupportChatMessage.created_at.asc())
          .all()
    )


def list_session_messages_id_asc(
    db: Session, session_id: str,
) -> List[models.SupportChatMessage]:
    """All messages of a session, oldest first by primary key ``id``.
    Used by the wizard's audit/generate paths and by the auto-escalation
    transcript builder — kept as a distinct function from the
    ``created_at``-ordered variant above because the pre-migration call
    sites ordered by different columns and migrate_rules says port 1:1,
    not unify."""
    return (
        db.query(models.SupportChatMessage)
          .filter(models.SupportChatMessage.session_id == session_id)
          .order_by(models.SupportChatMessage.id.asc())
          .all()
    )


def get_curated_workflow_templates(
    db: Session, tenant_id: Optional[int], limit: int = 8,
) -> List[models.WorkflowTemplate]:
    """Curated templates visible to a tenant: platform-global
    (``tenant_id IS NULL``) or owned by this tenant. Bounded to
    ``limit`` (matches the pre-migration inline ``.limit(8)``)."""
    return (
        db.query(models.WorkflowTemplate)
          .filter((models.WorkflowTemplate.tenant_id.is_(None))
                  | (models.WorkflowTemplate.tenant_id == tenant_id))
          .filter(models.WorkflowTemplate.is_curated.is_(True))
          .limit(limit)
          .all()
    )


def get_tenant_by_id(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    """Fetch a Tenant row by id — used to read the per-tenant
    ``wizard_agentic_loop_enabled`` flag."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


def get_content_blob_by_hash(
    db: Session, content_hash: str, tenant_id: int,
) -> Optional[models.ContentBlob]:
    """Fetch a tenant-owned ContentBlob by its content-hash id — the
    dedup check before minting a standin blob for a wizard sandbox
    test-run dispatch."""
    return (
        db.query(models.ContentBlob)
          .filter(models.ContentBlob.id == content_hash,
                  models.ContentBlob.tenant_id == tenant_id)
          .first()
    )


def get_terminal_node_output(
    db: Session, workflow_run_id: int,
) -> Optional[models.WorkflowNodeOutput]:
    """The terminal-output row for a completed sandbox WorkflowRun."""
    return (
        db.query(models.WorkflowNodeOutput)
          .filter(models.WorkflowNodeOutput.workflow_run_id == workflow_run_id,
                  models.WorkflowNodeOutput.is_terminal == True)  # noqa: E712
          .first()
    )


def get_workflow_by_id(db: Session, workflow_id: int) -> Optional[models.Workflow]:
    """Fetch a Workflow by id. Unscoped — the merge-apply service checks
    ``tenant_id`` ownership on top so a 403 doesn't collapse into 404."""
    return db.query(models.Workflow).filter(models.Workflow.id == workflow_id).first()


def get_workflow_version_by_id(
    db: Session, version_id: int, workflow_id: int,
) -> Optional[models.WorkflowVersion]:
    """Fetch a WorkflowVersion by id, scoped to the parent workflow (the
    pre-migration inline query already scoped this way)."""
    return (
        db.query(models.WorkflowVersion)
          .filter(models.WorkflowVersion.id == version_id,
                  models.WorkflowVersion.workflow_id == workflow_id)
          .first()
    )

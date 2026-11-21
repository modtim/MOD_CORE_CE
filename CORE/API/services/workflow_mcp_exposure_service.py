"""mcpt.a2/a3 (decomp-4.5 rows mcpt.a2-a6) — the EXPOSE side of MCP publishing.

``ensure_mcp_trigger`` (services/triggers/mcp_tool_trigger.py) provisions the
trigger for an exposure row, and ``WorkflowMcpExposure`` (models.py) is the
record — but nothing CREATED those rows. This module is that missing create
path: ``expose()`` writes the exposure, auto-provisions its MCP_TOOL_CALL
trigger, and audits the action; ``set_enabled()`` toggles it (disable is SOFT:
ISO 27001 — the row survives, ``enabled`` flips); ``list_for_workflow()`` reads
them back.

Tenant isolation lives here, not in the router: every lookup is scoped to the
caller's ``tenant_id`` (workflow via the tenant-scoped crud getter, exposures
via ``ix_workflow_mcp_exposure_tenant``), so a foreign workflow/exposure is
simply "not found" (404, never a leak). The tenant opt-in flag
``mcp_tools.workflow_publish_mcp_enabled`` is read through
``services/mcp_tools_service.publish_enabled`` — the SAME reader the list/call
side uses, so expose and read can never disagree.

Domain errors only (services/_base ServiceError family); the ROUTER maps them to
HTTP. The service commits (repo convention for write services);
``ensure_mcp_trigger`` flushes only, so the commit lands the pair atomically.
"""
from __future__ import annotations

from product_names import themed_logger
from typing import Optional

from sqlalchemy.orm import Session

import models
from services._base import ConflictError, NotFoundError, ServiceError
from services import mcp_tools_service as mcp_svc
from services.triggers import mcp_tool_trigger
from services.triggers.validator import _TOOL_NAME_RE  # same regex as the trigger config
from crud import workflow_cud as wf_crud

logger = themed_logger(__name__, 'workflows')

EXPOSED_EVENT = "mcp.workflow_exposed"
TOGGLED_EVENT = "mcp.workflow_exposure_toggled"


class BadRequestError(ServiceError):
    """Input is well-formed JSON but violates a domain rule (router -> 400)."""

    def __init__(self, message: str = "Bad request", *, details=None):
        super().__init__(message, code="bad_request", details=details)


class ForbiddenError(ServiceError):
    """Tenant opt-in flag is off (router -> 403)."""

    def __init__(self, message: str = "Forbidden", *, details=None):
        super().__init__(message, code="forbidden", details=details)


def refuse_system_workflow(db: Session, *, tenant_id: int, workflow_id: int) -> None:
    """1900BR4 — platform-internal (``is_system``) workflows are never publishable.

    The seeded system workflows (HarborScanFollowup hourly sweep, wizard build
    pipelines) are bookkeeping rows the scheduler drives; letting a tenant user
    expose one as an MCP tool or flip ``Workflow.publish_mcp`` on it registers a
    live MCP_TOOL_CALL trigger against platform machinery and lets the MCP panel
    mutate it. Nothing on this surface refused them, so the write paths happily
    wrote (and on a stack whose ``workflow_mcp_exposure`` table/column was not
    yet applied, the write blew up — the 500 1599b's browser run saw).

    Raises ConflictError (router -> 409) when the workflow is a system workflow.
    Callers that only READ (``list_for_workflow``) must NOT call this: the brief's
    contract is 200 with a list for any workflow the caller may see.
    """
    wf = wf_crud.get_workflow_by_id(db, workflow_id=int(workflow_id),
                                    tenant_id=int(tenant_id))
    if wf is not None and bool(getattr(wf, "is_system", False)):
        raise ConflictError(
            "System workflows cannot be exposed or published as MCP tools")


def _audit(db: Session, event_name: str, tenant_id, user_id, details) -> None:
    """AuditEvent row; never blocks the write it documents."""
    try:
        db.add(models.AuditEvent(
            tenant_id=tenant_id, user_id=user_id,
            event_name=event_name, details=details,
        ))
    except Exception as exc:  # noqa: BLE001 — audit must never fail the action
        logger.warning("workflow_mcp_exposure audit (%s) failed: %s", event_name, exc)


def list_for_workflow(db: Session, *, tenant_id: int, workflow_id: int):
    """Every exposure (enabled or not) for one workflow, scoped to ``tenant_id``.

    Raises NotFoundError when the workflow is missing / soft-deleted / in
    another tenant. Returns rows ordered by ``tool_name``.
    """
    tenant_id, workflow_id = int(tenant_id), int(workflow_id)
    if wf_crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=tenant_id) is None:
        raise NotFoundError("Workflow not found")
    return (
        db.query(models.WorkflowMcpExposure)
        .filter(models.WorkflowMcpExposure.tenant_id == tenant_id,
                models.WorkflowMcpExposure.workflow_id == workflow_id)
        .order_by(models.WorkflowMcpExposure.tool_name)
        .all()
    )


def set_enabled(db: Session, *, tenant_id: int, workflow_id: int, exposure_id: int,
                enabled: bool, actor_user_id: Optional[int] = None):
    """Enable / disable one exposure (disable is SOFT — the row always stays).

    NotFoundError when the exposure is missing, belongs to another tenant, or
    belongs to a DIFFERENT workflow than ``workflow_id`` — the caller's 'edit'
    grant was checked against ``workflow_id``, so an exposure that actually
    belongs to another workflow must never be reachable through this path
    (even within the same tenant). Idempotent: re-setting the same value
    writes no audit row. Returns the row.
    """
    refuse_system_workflow(db, tenant_id=tenant_id, workflow_id=workflow_id)
    row = (
        db.query(models.WorkflowMcpExposure)
        .filter(models.WorkflowMcpExposure.id == int(exposure_id),
                models.WorkflowMcpExposure.tenant_id == int(tenant_id),
                models.WorkflowMcpExposure.workflow_id == int(workflow_id))
        .first()
    )
    if row is None:
        raise NotFoundError("MCP exposure not found")
    if bool(row.enabled) == bool(enabled):
        return row
    row.enabled = bool(enabled)
    _audit(db, TOGGLED_EVENT, tenant_id, actor_user_id,
           {"exposure_id": int(row.id), "workflow_id": row.workflow_id,
            "tool_name": row.tool_name, "enabled": bool(enabled)})
    db.commit()
    return row


def expose(db: Session, *, tenant_id: int, workflow_id: int, tool_name: str,
           actor_user_id: Optional[int] = None):
    """Expose one workflow as an MCP tool: create the exposure + its trigger.

    Order of checks (all tenant-scoped, so a foreign workflow is 404, never a
    leak):
      1. ``tool_name`` must match the trigger validator's regex
         (``_TOOL_NAME_RE`` from services/triggers/validator.py — the SAME
         constant the MCP_TOOL_CALL trigger config is validated against, so a
         name that would be rejected downstream can never be stored here).
      2. the workflow must exist in THIS tenant and not be soft-deleted.
      3. the tenant opt-in flag must be on (fail-closed read via
         ``mcp_svc.publish_enabled``) -> 403-class domain error.
      4. ``tool_name`` must not already be taken by ANOTHER workflow of this
         tenant (``ix_workflow_mcp_exposure_tenant``) -> ConflictError.
      5. an existing (workflow, tool_name) row -> re-enable it (idempotent:
         no duplicate row, no duplicate audit, and ensure_mcp_trigger reuses
         the one trigger).

    Then ``ensure_mcp_trigger`` provisions the MCP_TOOL_CALL trigger (flushes
    only) and one commit lands exposure + trigger + audit together.
    Returns the exposure row.
    """
    tenant_id, workflow_id = int(tenant_id), int(workflow_id)

    if not isinstance(tool_name, str) or not _TOOL_NAME_RE.fullmatch(tool_name):
        raise BadRequestError(
            "tool_name must match ^[a-z][a-z0-9_-]{0,63}$")

    wf = wf_crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=tenant_id)
    if wf is None:
        raise NotFoundError("Workflow not found")

    refuse_system_workflow(db, tenant_id=tenant_id, workflow_id=workflow_id)

    if not mcp_svc.publish_enabled(db, tenant_id):
        raise ForbiddenError("MCP publishing is not enabled for this tenant")

    taken = (
        db.query(models.WorkflowMcpExposure)
        .filter(models.WorkflowMcpExposure.tenant_id == tenant_id,
                models.WorkflowMcpExposure.tool_name == tool_name)
        .all()
    )
    existing = None
    for row in taken:
        if row.workflow_id == workflow_id:
            existing = row
        else:
            raise ConflictError(
                f"tool_name '{tool_name}' is already exposed by another workflow "
                "in this tenant")

    if existing is not None:
        changed = bool(existing.enabled) is not True
        existing.enabled = True
        trigger = mcp_tool_trigger.ensure_mcp_trigger(
            db, tenant_id=tenant_id, exposure=existing)
        if changed:
            _audit(db, TOGGLED_EVENT, tenant_id, actor_user_id,
                   {"exposure_id": int(existing.id), "workflow_id": workflow_id,
                    "tool_name": tool_name, "enabled": True})
        else:
            _audit(db, EXPOSED_EVENT, tenant_id, actor_user_id,
                   {"exposure_id": int(existing.id), "workflow_id": workflow_id,
                    "tool_name": tool_name, "reused": True,
                    "trigger_id": int(trigger.id) if trigger is not None else None})
        db.commit()
        return existing

    exposure = models.WorkflowMcpExposure(
        workflow_id=workflow_id, tenant_id=tenant_id, tool_name=tool_name,
        enabled=True)
    db.add(exposure)
    db.flush()
    try:
        trigger = mcp_tool_trigger.ensure_mcp_trigger(
            db, tenant_id=tenant_id, exposure=exposure)
    except (LookupError, ValueError) as exc:
        db.rollback()
        logger.warning("workflow_mcp_exposure: trigger provisioning failed for "
                       "(tenant %s, workflow %s, tool %s): %s",
                       tenant_id, workflow_id, tool_name, exc)
        raise BadRequestError(
            "workflow has no version to pin the MCP trigger to") from exc
    _audit(db, EXPOSED_EVENT, tenant_id, actor_user_id,
           {"exposure_id": int(exposure.id), "workflow_id": workflow_id,
            "tool_name": tool_name,
            "trigger_id": int(trigger.id) if trigger is not None else None})
    db.commit()
    return exposure

"""node-code-edit-rebuild — Contract 2 (HTTP API).

Router: ``node_code_edit.router``, ``tags=["Node Code Edit"]``, NO prefix of
its own. Mounted by ``main.py``'s ``router_mapping`` under ``/node_definitions``,
so every route defined here (``/{node_id}/source``, ...) resolves to
``/node_definitions/{node_id}/source`` etc.

Lets any permitted user view/edit/copy/rebuild a node's underlying source
(Dockerfile/entrypoint/source files) instead of only editing it through the
wizard's generate-from-description flow. Source lives in
``node_source_revision`` (see ``services/node_source_revisions.py``, §1 —
already implemented) as an ordered, append-only, forever-retained chain.
Rebuilding a node re-dispatches the build DAG against the edited source (see
``services/node_build_dispatch.py``, Contract 1 — owned by Stream B).

This surface is EVERYONE-facing: it is no longer hidden behind the
``wizard_code_edit_enabled`` admin feature flag. That flag's field still
exists on ``routers/platform_feature_config.py``'s response model (kept
around harmlessly rather than ripped out here), but nothing in the codebase
reads it any more — it is a candidate for full retirement in a follow-up.
Access is gated purely by the user's own key/permissions — RBAC capability
on the node plus, for build-dispatching operations, their license/plan
entitlement:

Authorization model per endpoint:
  - ``view``/``edit``/``create``/``delete`` capability checks go through
    ``resource_permissions.authorize`` against ``ResourceType.NODE_DEFINITION``.
  - ``rebuild`` and ``copy`` additionally require the ``custom_nodes``
    entitlement (self-host license / SaaS plan gate) — see
    ``utils.entitlements``.
  - A system node (``tenant_id is None``) can never be edited/rebuilt/reverted
    in place — those operations 409 ``{"code": "must_fork"}`` so the client
    copies it into a tenant-owned node first (``POST .../copy``).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

import models
from crud.node_definition_cud import get_node_definition_by_id
from dependencies import get_db, require_active_and_onboarded_user
from resource_permissions import authorize
import services.node_source_revisions as nsr
import services.node_code_edit_service as node_code_edit_service
from services._base import ConflictError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Node Code Edit"])


# =============================================================================
# --- shared helpers ---
# =============================================================================

def _access_denied() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "node_access_denied"},
    )


def _entitlement_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={"code": "entitlement_required", "capability": "custom_nodes"},
    )


def _must_fork() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "must_fork"},
    )


def _get_node_or_404(db: Session, node_id: int, current_user: models.User) -> models.NodeDefinition:
    node = get_node_definition_by_id(db, node_id, current_user.tenant_id, user_id=current_user.id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    return node


def _require_cap(db: Session, current_user: models.User, node: models.NodeDefinition,
                  capability: str, *, resource_id: Optional[int] = None) -> None:
    rid = node.id if resource_id is None else resource_id
    if not authorize(db, current_user, models.ResourceType.NODE_DEFINITION, capability, rid):
        raise _access_denied()


# Tenant-of-record for platform-shared rows (system nodes have no tenant of
# their own). Matches the convention in routers/platform_feature_config.py,
# routers/harbor.py, routers/secrets.py.
PLATFORM_TENANT_ID = 1


# =============================================================================
# --- request bodies ---
# =============================================================================

class SourceEditRequest(BaseModel):
    hero: Optional[Dict[str, Any]] = None
    source_overrides_by_variant: Optional[Dict[str, List[Dict[str, str]]]] = None
    # Accepted for forward-compat with a future free-text revision message;
    # NodeSourceRevision (§1) has no column for it yet, so it is not persisted.
    message: Optional[str] = None


class CopyNodeRequest(BaseModel):
    new_name: Optional[str] = None


class RebuildRequest(BaseModel):
    scope: str  # "single_variant" | "full_fanout"
    variant: Optional[str] = None


class RevertRequest(BaseModel):
    target_revision_no: int = Field(..., ge=1)


# =============================================================================
# --- response models ---
# =============================================================================
# Each documents the existing payload shape; every key is Optional and extra
# keys are allowed, so the response is never rejected, stripped, or reshaped
# (FastAPI's `response_model` only *filters* keys that exist on the model, and
# here we enumerate every key every handler returns on every path). `rebuild`
# uses `exclude_unset=True` so that, when `dispatch_rebuild` returns a dict that
# omits `workflow_run_id`/`workflow_id`/`scope`, the JSON keeps exactly the keys
# the service produced (the pre-model shape) rather than injecting `null`.


class GetNodeSourceResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    node_id: Optional[int] = None
    name: Optional[str] = None
    is_system: Optional[bool] = None
    editable: Optional[bool] = None
    current_revision_no: Optional[int] = None
    revisions: Optional[List[Dict[str, Any]]] = None
    hero: Optional[Dict[str, Any]] = None
    variants: Optional[Dict[str, Any]] = None


class PostNodeSourceResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    revision_no: Optional[int] = None
    origin: Optional[str] = None


class CopyNodeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    node_id: Optional[int] = None
    name: Optional[str] = None


class RebuildNodeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    workflow_run_id: Optional[int] = None
    workflow_id: Optional[int] = None
    scope: Optional[str] = None
    critic_warnings: Optional[List[Dict[str, Any]]] = None


class ListNodeSourceRevisionsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    revisions: Optional[List[Dict[str, Any]]] = None


class DiffNodeSourceResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    diff: Optional[Dict[str, Any]] = None


class RevertNodeSourceResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    revision_no: Optional[int] = None
    origin: Optional[str] = None


class DeleteNodeSourceRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    deleted: Optional[bool] = None
    retained: Optional[bool] = None


# =============================================================================
# --- GET /{node_id}/source ---
# =============================================================================

@router.get("/{node_id}/source", response_model=GetNodeSourceResponse)
def get_node_source(
    node_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    node = _get_node_or_404(db, node_id, current_user)
    _require_cap(db, current_user, node, "view")

    rev = nsr.latest(db, node.id) or node_code_edit_service.materialize_initial_safe(db, node)
    revisions = nsr.list_revisions(db, node.id)
    is_system = node_code_edit_service.is_system(node)

    return {
        "node_id": node.id,
        "name": node.name,
        "is_system": is_system,
        "editable": not is_system,
        "current_revision_no": rev.revision_no,
        "revisions": [node_code_edit_service.revision_summary(r) for r in revisions],
        "hero": rev.hero or {},
        "variants": node_code_edit_service.variants_view(rev),
    }


# =============================================================================
# --- POST /{node_id}/source ---
# =============================================================================

@router.post("/{node_id}/source", status_code=status.HTTP_201_CREATED, response_model=PostNodeSourceResponse)
def post_node_source(
    node_id: int,
    body: SourceEditRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    node = _get_node_or_404(db, node_id, current_user)
    _require_cap(db, current_user, node, "edit")

    if node_code_edit_service.is_system(node):
        raise _must_fork()

    base = nsr.latest(db, node.id) or node_code_edit_service.materialize_initial_safe(db, node)
    hero = body.hero if body.hero is not None else dict(base.hero or {})
    overrides = (
        body.source_overrides_by_variant
        if body.source_overrides_by_variant is not None
        else dict(base.source_overrides_by_variant or {})
    )

    rev = nsr.create_revision(
        db, node,
        origin=nsr.ORIGIN_EDITED,
        author_user_id=current_user.id,
        hero=hero,
        source_overrides_by_variant=overrides,
        variants_snapshot=base.variants_snapshot,
    )
    return {"revision_no": rev.revision_no, "origin": rev.origin}


# =============================================================================
# --- POST /{node_id}/copy ---
# =============================================================================

@router.post("/{node_id}/copy", status_code=status.HTTP_201_CREATED, response_model=CopyNodeResponse)
def copy_node(
    node_id: int,
    body: CopyNodeRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    source = _get_node_or_404(db, node_id, current_user)
    _require_cap(db, current_user, source, "view")
    # Tenant-level "can create a node at all" check — not scoped to any
    # existing resource, so it is checked as a wildcard (resource_id=None),
    # the same pattern grant_owner_permissions/authorize use elsewhere for
    # creation-time checks that precede the new row's id existing.
    if not authorize(db, current_user, models.ResourceType.NODE_DEFINITION, "create", None):
        raise _access_denied()
    try:
        node_code_edit_service.require_custom_nodes(db, current_user)
    except node_code_edit_service.EntitlementRequiredError:
        raise _entitlement_required()

    src_rev = nsr.latest(db, source.id) or node_code_edit_service.materialize_initial_safe(db, source)

    base_name = (body.new_name or source.name or "node").strip() or "node"
    try:
        name = node_code_edit_service.next_node_copy_name(
            db, tenant_id=current_user.tenant_id, user_id=current_user.id, base_name=base_name,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message)

    new_node = models.NodeDefinition(
        name=name,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        node_role="user",
        type=source.type,
        description=source.description,
        input_schema=source.input_schema,
        output_schema=source.output_schema,
        configuration_schema=source.configuration_schema,
        gpu_variants=source.gpu_variants,
        # A copy is a template of a RUNNABLE node: inherit the runtime identity
        # too. Without node_kind / executable_path the copy was a skeleton (CE
        # palette row that cannot run, id 242 on the Dell), and the refresh heal
        # cannot repair it when the source is a tenant clone (its FK is not a
        # system row). A later rebuild replaces executable_path.
        node_kind=source.node_kind,
        executable_path=source.executable_path,
        process_type=source.process_type,
        batch_mode=source.batch_mode,
        accepted_formats=source.accepted_formats,
        cost_in_credits=source.cost_in_credits,
        forked_from_node_definition_id=source.id,
        source_origin="forked" if node_code_edit_service.is_system(source) else "copied",
    )
    db.add(new_node)
    db.commit()
    db.refresh(new_node)

    nsr.create_revision(
        db, new_node,
        origin=nsr.ORIGIN_COPIED,
        author_user_id=current_user.id,
        hero=dict(src_rev.hero or {}),
        source_overrides_by_variant=dict(src_rev.source_overrides_by_variant or {}),
        variants_snapshot=src_rev.variants_snapshot,
    )

    return {"node_id": new_node.id, "name": new_node.name}


# =============================================================================
# --- POST /{node_id}/rebuild ---
# =============================================================================

@router.post("/{node_id}/rebuild", response_model=RebuildNodeResponse)
def rebuild_node(
    node_id: int,
    body: RebuildRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    if body.scope not in ("single_variant", "full_fanout"):
        raise HTTPException(status_code=422, detail="scope must be 'single_variant' or 'full_fanout'")
    if body.scope == "single_variant" and not body.variant:
        raise HTTPException(status_code=422, detail="variant is required for scope=single_variant")

    node = _get_node_or_404(db, node_id, current_user)
    _require_cap(db, current_user, node, "edit")
    try:
        node_code_edit_service.require_custom_nodes(db, current_user)
    except node_code_edit_service.EntitlementRequiredError:
        raise _entitlement_required()

    rev = nsr.latest(db, node.id) or node_code_edit_service.materialize_initial_safe(db, node)

    import services.node_build_dispatch as node_build_dispatch

    warnings = node_build_dispatch.run_source_critics(rev)
    rev.critic_warnings = warnings
    flag_modified(rev, "critic_warnings")
    db.commit()

    from node_builder.build_dag.submit import DifferentiableDeclarationError

    try:
        result = node_build_dispatch.dispatch_rebuild(
            db, node=node, revision=rev, scope=body.scope,
            variant=body.variant, user_id=current_user.id,
        )
    except node_build_dispatch.RegistryIsolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "registry_not_isolated"},
        )
    except DifferentiableDeclarationError:
        # 622/EXPR 10.1 — defensive gate in create_build_workflow; never
        # expected in practice (dispatch_rebuild's generated graphs never
        # declare differentiable), but must not 500 if it ever does fire.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "differentiable_declaration_invalid"},
        )

    return {
        "workflow_run_id": result.get("workflow_run_id"),
        "workflow_id": result.get("workflow_id"),
        "scope": result.get("scope", body.scope),
        "critic_warnings": warnings,
    }


# =============================================================================
# --- GET /{node_id}/source/revisions ---
# =============================================================================

@router.get("/{node_id}/source/revisions", response_model=ListNodeSourceRevisionsResponse)
def list_node_source_revisions(
    node_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    node = _get_node_or_404(db, node_id, current_user)
    _require_cap(db, current_user, node, "view")
    revisions = nsr.list_revisions(db, node.id)
    return {"revisions": [node_code_edit_service.revision_summary(r) for r in revisions]}


# =============================================================================
# --- GET /{node_id}/source/diff ---
# =============================================================================

@router.get("/{node_id}/source/diff", response_model=DiffNodeSourceResponse)
def diff_node_source(
    node_id: int,
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    node = _get_node_or_404(db, node_id, current_user)
    _require_cap(db, current_user, node, "view")
    try:
        d = nsr.diff(db, node.id, from_, to)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="revision number not found")
    return {"diff": d}


# =============================================================================
# --- POST /{node_id}/source/revert ---
# =============================================================================

@router.post("/{node_id}/source/revert", response_model=RevertNodeSourceResponse)
def revert_node_source(
    node_id: int,
    body: RevertRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    node = _get_node_or_404(db, node_id, current_user)
    _require_cap(db, current_user, node, "edit")

    if node_code_edit_service.is_system(node):
        raise _must_fork()

    target = None
    for r in nsr.list_revisions(db, node.id, include_deleted=True):
        if r.revision_no == body.target_revision_no:
            target = r
            break
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="revision not found")

    rev = nsr.revert(db, node, target.id, author_user_id=current_user.id)
    return {"revision_no": rev.revision_no, "origin": rev.origin}


# =============================================================================
# --- DELETE /{node_id}/source/revisions/{revision_no} ---
# =============================================================================

@router.delete("/{node_id}/source/revisions/{revision_no}", response_model=DeleteNodeSourceRevisionResponse)
def delete_node_source_revision(
    node_id: int,
    revision_no: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    node = _get_node_or_404(db, node_id, current_user)
    _require_cap(db, current_user, node, "delete")

    target = None
    for r in nsr.list_revisions(db, node.id, include_deleted=True):
        if r.revision_no == revision_no:
            target = r
            break
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="revision not found")

    nsr.delete_revision(db, target.id, actor_user_id=current_user.id)

    try:
        db.add(models.AuditEvent(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="NODE_REVISION_DELETED",
            details={"node_id": node.id, "revision_no": revision_no},
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("failed to write NODE_REVISION_DELETED audit event")
        db.rollback()

    return {"deleted": True, "retained": True}

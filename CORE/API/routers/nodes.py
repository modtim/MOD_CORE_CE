# API/routers/nodes.py
"""Nodes — the clean `/v1/nodes` resource.

clean-v2-api / node-definitions-collapse wave (openspec: clean-v2-api,
design.md "Domain 2"). This router REPLACES the old `/v1/node-definitions`
grab-bag with the clean `/v1/nodes` resource:

    POST/GET     /v1/nodes                                   (create / list)
    GET/PATCH/DELETE /v1/nodes/{id}                           (get/update/delete)
    PATCH        /v1/nodes/{id}/price|role|sample
    POST         /v1/nodes/{id}/rescan|calibrate|try|share-with-tenant
    GET/POST     /v1/nodes/{id}/manifest[-metadata]|verify-manifest
    GET          /v1/nodes/{id}/scan-queue-context
    GET          /v1/nodes/public/{id}
    POST/GET/PATCH /v1/nodes/{id}/build-variant/{tag}|variant-verification[/...]|reference-variant
    PATCH        /v1/nodes/variants/status
    GET/POST/DELETE /v1/nodes/{id}/source[/revisions|/diff|/revert]
    POST         /v1/nodes/{id}/copy|rebuild
    POST/GET/DELETE /v1/nodes/{id}/calibration
    GET/POST/DELETE misc: base-images, new-count, status, refresh,
        suggest-category, wizard-preflight, wizard-llm-status, worker-llm-ping,
        curate-list[/tenant], stats

The build pipeline (`wizard/*`, `build-*`, `describe-*`, `check-*`,
`analyze-*`, `predict-variants`, `retry-with-corrections`,
`built-image-cache/*`, worker callbacks) lives in `routers/nodes_builds.py`
(`/v1/woodsman/*`); the marketplace collection + packages live in
`routers/nodes_marketplace.py` (`/v1/nodes/marketplace`, `/v1/nodes/packages`);
remediation lives in `routers/nodes_remediation.py` (`/v1/nodes/remediation`).

REUSE, NOT REWRITE: every route below re-registers the SAME endpoint
function object the old `/node-definitions` router used (imported from
`routers.node_definitions`, which still re-exports every domain module's
handlers + Pydantic response models unchanged) at its new path, via
`router.<verb>(path, **same kwargs as the old decorator)(function)`. Auth,
validation, ISO soft-delete and response shape are byte-identical to
before this wave; only the URL changed. The corresponding `@router.*`
decorators in `routers/node_definitions_domains/*.py` have been removed
(same pattern as Wave 1a's `admin_plans_pricing.py`) so the old
`/v1/node-definitions/*` paths no longer serve these handlers — the
functions themselves stay in place and importable.

Three small pieces of node/nodes-adjacent functionality live in files this
wave does NOT own (outside the `node_definitions*`/`nodes*` glob) — each
mounted at the old `/node-definitions` prefix in main.py alongside the
domain this wave DOES own — and were therefore left UNEDITED, just
re-mounted here by reference:

  * `routers/node_code_edit.py` (`router`, no baked-in prefix) — the
    `/{id}/source*`, `/{id}/copy`, `/{id}/rebuild` endpoints. Its router
    object is `include_router()`'d as-is below; its OLD mount in main.py
    at `/node-definitions` is untouched by this file (main.py is the
    coordinator's to edit) — ask the coordinator to drop that old mount
    once every caller is repointed.
  * `routers/variant_calibration.py` — the `/{id}/calibration` GET/POST/
    DELETE endpoints. That module's `APIRouter` bakes in its OWN
    `prefix="/node-definitions"`, so its router object can't be
    `include_router()`'d here without doubling the prefix
    (`/nodes/node-definitions/...`). Its three plain functions are
    imported and re-registered directly at `/nodes/{node_id}/calibration`
    instead — same reuse-not-rewrite approach, just via `add`-style
    decoration instead of `include_router`.
  * `routers/variant_patch_dispatch.py` — the self-healing variant patch
    loop, `/{id}/variants/{variant}/patch` (POST) and
    `/{id}/variants/{variant}/patch/status` (GET). Same baked-in-prefix
    situation as `variant_calibration.py`; its two plain functions are
    imported and re-registered directly at
    `/nodes/{node_id}/variants/{variant}/patch[/status]`.

See the wave's report for the full list of Worker/DockerContainers/
WorkflowBuilder/Dashboard callers that must be repointed from
`/v1/node-definitions/...` to `/v1/nodes/...` (this file does not, and
per the wave's charter must not, edit those clients).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

import schemas
import logging

logger = logging.getLogger(__name__)

# FX.91 p2c (tenant-admin one-time egress approval) — the two new endpoints
# below are the only handlers this file OWNS (everything else here is a
# reuse-not-rewrite re-registration of a node_definitions handler). Their
# logic lives in services/node_egress_approval.py; the router stays query-free
# (api_lint R5) and never leaks a raw exception (R6).
from dependencies import (  # noqa: F401
    get_db,
    require_active_onboarded_parent_user,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session
import crud
import models
from services import node_egress_approval as _node_egress_approval
from utils.http_errors import internal_http_error

from routers.node_definitions import (
    # CRUD
    create_or_update_node_definition,
    list_node_definitions_for_tenant,
    get_node_definition,
    update_node_definition,
    delete_node_definition,
    # item actions
    set_node_price,
    admin_list_system_nodes,
    set_node_role,
    set_node_sample,
    rescan_node_definition,
    calibrate_node,
    get_node_manifest,
    set_node_manifest_metadata,
    verify_node_manifest,
    try_node,
    share_node_with_tenant,
    get_scan_queue_context,
    public_node_detail,
    # marketplace (tenant-scoped node actions — the node's OWN listing)
    # variants
    build_variant,
    get_variant_verification,
    rule_on_drifted_variant,
    patch_reference_variant,
    patch_variant_status,
    # misc
    list_curated_base_images,
    marketplace_new_count,
    get_node_definitions_status,
    refresh_node_definitions,
    suggest_node_category,
    wizard_preflight,
    wizard_llm_status,
    worker_llm_ping,
    admin_curate_list,
    tenant_curate_list,
    admin_describe_stats,
    # response models (Pydantic; re-exported by routers.node_definitions
    # from routers/node_definitions_domains/_shared.py, unchanged)
    BaseImageCatalogEntry,
    WizardLLMStatusResponse,
    WorkerLLMPingResponse,
    _GetVariantVerificationResponse,
    _DriftRulingResponse,
    _PatchReferenceVariantResponse,
    _SuggestCategoryResponse,
    _CurateListResponse,
    _TenantCurateListResponse,
    _DescribeStatsResponse,
)

# Not owned by this wave (outside the node_definitions*/nodes* glob) —
# reused by reference only, see module docstring.
from routers.node_code_edit import router as _node_source_router
from routers.variant_calibration import (
    set_calibration as _set_calibration,
    get_calibration as _get_calibration,
    clear_calibration as _clear_calibration,
)
from routers.variant_patch_dispatch import (
    dispatch_patch as _dispatch_patch,
    get_patch_loop_status as _get_patch_loop_status,
)

router = APIRouter(
    prefix="/nodes",
    tags=["nodes"],
    responses={404: {"description": "Not found"}},
)

# ---------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------
router.post("/", response_model=schemas.NodeDefinition)(create_or_update_node_definition)
router.get("/", response_model=List[schemas.NodeDefinition])(list_node_definitions_for_tenant)

# ---------------------------------------------------------------------
# Literal single-segment GETs MUST be registered before the dynamic
# `GET /{node_definition_id}` catch-all below (same ordering constraint the
# pre-split monolith documented: a literal path like "/status" would
# otherwise first try to match "/{node_definition_id}" with
# node_definition_id="status" and 422 on int coercion). Do not reorder.
# ---------------------------------------------------------------------
router.get("/base-images", response_model=List[BaseImageCatalogEntry])(list_curated_base_images)
router.get("/new-count")(marketplace_new_count)
router.get("/status", response_model=Dict[str, Any])(get_node_definitions_status)
router.get("/wizard-preflight")(wizard_preflight)
router.get("/wizard-llm-status", response_model=WizardLLMStatusResponse)(wizard_llm_status)
router.get("/curate-list", response_model=_CurateListResponse)(admin_curate_list)
router.get("/curate-list/tenant", response_model=_TenantCurateListResponse)(tenant_curate_list)
router.get("/stats", response_model=_DescribeStatsResponse)(admin_describe_stats)
router.get("/public/{node_definition_id}")(public_node_detail)
router.get("/admin/system", response_model=List[schemas.NodeDefinition])(admin_list_system_nodes)

# Dynamic single-segment GET — must come after the literal GETs above.
router.get("/{node_definition_id}", response_model=schemas.NodeDefinition)(get_node_definition)

router.patch("/{node_definition_id}", response_model=schemas.NodeDefinition)(update_node_definition)
router.delete("/{node_definition_id}", status_code=status.HTTP_204_NO_CONTENT)(delete_node_definition)

# ---------------------------------------------------------------------
# Item actions
# ---------------------------------------------------------------------
router.patch("/{node_definition_id}/price", response_model=schemas.NodeDefinition)(set_node_price)
router.patch("/{node_definition_id}/role", response_model=schemas.NodeDefinition)(set_node_role)
router.patch("/{node_definition_id}/sample", response_model=schemas.NodeDefinition)(set_node_sample)
router.post("/{node_definition_id}/rescan", status_code=status.HTTP_202_ACCEPTED)(rescan_node_definition)
router.post("/{node_definition_id}/calibrate", status_code=status.HTTP_202_ACCEPTED)(calibrate_node)
router.get("/{node_definition_id}/manifest")(get_node_manifest)
router.post("/{node_definition_id}/manifest-metadata", status_code=200)(set_node_manifest_metadata)
router.post("/{node_definition_id}/verify-manifest")(verify_node_manifest)
router.post("/{node_definition_id}/try")(try_node)
router.post("/{node_definition_id}/share-with-tenant", response_model=schemas.NodeDefinition)(share_node_with_tenant)
router.get("/{node_definition_id}/scan-queue-context")(get_scan_queue_context)


# ---------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------
router.post(
    "/{node_definition_id}/build-variant/{hardware_tag}",
    status_code=status.HTTP_202_ACCEPTED,
)(build_variant)
router.get(
    "/{node_def_id}/variant-verification",
    response_model=_GetVariantVerificationResponse,
)(get_variant_verification)
router.post(
    "/{node_def_id}/variant-verification/{variant}/drift-ruling",
    response_model=_DriftRulingResponse,
)(rule_on_drifted_variant)
router.patch(
    "/{node_def_id}/reference-variant",
    response_model=_PatchReferenceVariantResponse,
)(patch_reference_variant)
# Renamed from "/variant-status/" (rule 6: no bare trailing-slash verb path).
router.patch("/variants/status", status_code=status.HTTP_200_OK)(patch_variant_status)

# ---------------------------------------------------------------------
# Source (reused unedited from routers/node_code_edit.py — see module
# docstring). No baked-in prefix on that router, so this is a plain
# include_router; paths keep their exact tails: {id}/source[/revisions|
# /diff|/revert|/revisions/{n}], {id}/copy, {id}/rebuild.
# ---------------------------------------------------------------------
router.include_router(_node_source_router)

# ---------------------------------------------------------------------
# Calibration (reused unedited from routers/variant_calibration.py — see
# module docstring; that router bakes its own prefix so we re-register the
# plain functions directly instead of include_router()).
# ---------------------------------------------------------------------
router.post("/{node_id}/calibration", status_code=status.HTTP_200_OK)(_set_calibration)
router.get("/{node_id}/calibration")(_get_calibration)
router.delete("/{node_id}/calibration", status_code=status.HTTP_200_OK)(_clear_calibration)

# Self-healing variant patch loop (reused unedited from
# routers/variant_patch_dispatch.py — same baked-in-prefix situation as
# variant_calibration.py above; not part of node_definitions_domains, not
# in design.md's Domain 2 table, discovered via its shared "/node-definitions"
# prefix and grep of tests/test_variant_patch_dispatch.py).
router.post("/{node_id}/variants/{variant}/patch")(_dispatch_patch)
router.get("/{node_id}/variants/{variant}/patch/status")(_get_patch_loop_status)

# ---------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------
router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)(refresh_node_definitions)
router.post("/suggest-category", response_model=_SuggestCategoryResponse)(suggest_node_category)
router.post(
    "/worker-llm-ping",
    response_model=WorkerLLMPingResponse,
    status_code=status.HTTP_202_ACCEPTED,
)(worker_llm_ping)

# ---------------------------------------------------------------------
# FX.91 p2c — tenant-admin one-time approval of a node's DECLARED egress
# hostnames (services/node_egress_approval.py). POST approves the exact set
# the palette showed (p2b); DELETE is a SOFT revoke (ISO 27001 — the record
# keeps its evidence, it is never hard-deleted). Tenant admin only
# (require_active_onboarded_parent_user = the codebase's parent-user admin
# predicate); a system node (tenant_id NULL) is approved install-wide by
# whichever admin reviewed it. Every change appends an AuditEvent.
# ---------------------------------------------------------------------


class _NodeEgressApprovalRequest(BaseModel):
    """The domain set the admin is approving (the palette's p2b render).

    Kept next to the endpoints rather than in schemas.py: this is the only
    request shape this router owns, and the grammar it validates against is
    services/node_egress_domains' (p2a), re-run by the service on write.
    """
    domains: Optional[List[str]] = None


class _EgressApprovalResult(BaseModel):
    """What the admin's decision recorded — identifiers + domain sets only."""
    approved: bool
    revoked: bool
    node_definition_id: int
    tenant_id: Optional[int] = None
    domains: Optional[List[str]] = None
    changed: bool = False
    approval_id: Optional[int] = None


@router.post("/{node_definition_id}/egress-approval",
             response_model=_EgressApprovalResult,
             status_code=status.HTTP_200_OK)
def approve_node_egress_domains(
    node_definition_id: int,
    req: _NodeEgressApprovalRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_onboarded_parent_user),
):
    """Approve a node's declared egress hostnames for this tenant, once.

    The body's ``domains`` must be the node's CURRENT declaration (the
    palette's p2b render), re-validated through the p2a grammar; a set that
    does not parse is a 400, never a stored approval. Approval is one-time:
    it stays valid until the node's declared set changes, and a changed set
    requires re-approval (services/node_egress_approval.is_approved compares
    the stored set against the live declaration).
    """
    nd = crud.get_node_definition_by_id(
        db=db, node_definition_id=node_definition_id,
        tenant_id=current_user.tenant_id,
    )
    if nd is None:
        raise HTTPException(status_code=404, detail="Node definition not found")
    try:
        result = _node_egress_approval.approve(
            db,
            tenant_id=nd.tenant_id,
            node_definition_id=node_definition_id,
            domains=req.domains,
            admin_user_id=current_user.id,
        )
    except _node_egress_approval.EgressApprovalError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
            headers={"X-Error-Code": "EGRESS_APPROVAL_DOMAINS_INVALID"},
        ) from None
    except Exception as exc:  # noqa: BLE001 — R6: never leak the raw text
        logger.exception("egress approval failed")
        raise internal_http_error(
            500, "Node egress approval failed", exc
        ) from exc
    return _EgressApprovalResult(
        approved=True,
        revoked=False,
        node_definition_id=node_definition_id,
        tenant_id=nd.tenant_id,
        domains=result["domains"],
        changed=result["changed"],
        approval_id=result["approval_id"],
    )


@router.delete("/{node_definition_id}/egress-approval",
               response_model=_EgressApprovalResult,
               status_code=status.HTTP_200_OK)
def revoke_node_egress_approval(
    node_definition_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_onboarded_parent_user),
):
    """SOFT-revoke a node's egress approval (never a hard delete).

    Stamps the tombstone + revoked_at/revoked_by_user_id on the approval row
    and appends the audit row, so the gate refuses again until re-approved.
    Idempotent: a node with no live approval reports changed=False.
    """
    nd = crud.get_node_definition_by_id(
        db=db, node_definition_id=node_definition_id,
        tenant_id=current_user.tenant_id,
    )
    if nd is None:
        raise HTTPException(status_code=404, detail="Node definition not found")
    try:
        result = _node_egress_approval.revoke(
            db,
            tenant_id=nd.tenant_id,
            node_definition_id=node_definition_id,
            admin_user_id=current_user.id,
        )
    except Exception as exc:  # noqa: BLE001 — R6: never leak the raw text
        logger.exception("egress approval revoke failed")
        raise internal_http_error(
            500, "Node egress approval revoke failed", exc
        ) from exc
    return _EgressApprovalResult(
        approved=False,
        revoked=True,
        node_definition_id=node_definition_id,
        tenant_id=nd.tenant_id,
        domains=result["domains"],
        changed=result["changed"],
        approval_id=result["approval_id"],
    )

# ---------------------------------------------------------------------
# FX.91 p2c — the READ half: what a tenant admin still has to decide.
# GET /nodes/egress-approval lists every node whose declared egress this
# tenant has not validly approved (never approved OR a declaration that
# changed since the approval = 'stale'), so the admin can walk the list
# once and the platform stops re-asking. GET /nodes/{id}/egress-approval
# reports one node's state (declared vs approved sets). Query-free: the
# reads live in services/node_egress_approval.pending / approval_state.
# ---------------------------------------------------------------------


class _EgressApprovalListResponse(BaseModel):
    """Nodes awaiting a tenant admin's decision (identifiers + sets only)."""
    tenant_id: Optional[int] = None
    pending: List[Dict[str, Any]] = []


class _EgressApprovalStateResponse(BaseModel):
    """One node's approval state: what the admin sees when reviewing it."""
    node_definition_id: int
    tenant_id: Optional[int] = None
    state: str
    declared: Optional[List[str]] = None
    approved: Optional[List[str]] = None
    approval_id: Optional[int] = None
    approved_at: Optional[datetime] = None
    approved_by_user_id: Optional[int] = None
    revoked_at: Optional[datetime] = None


@router.get("/egress-approval",
            response_model=_EgressApprovalListResponse,
            status_code=status.HTTP_200_OK)
def list_pending_node_egress_approvals(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_onboarded_parent_user),
):
    """List nodes whose declared egress this tenant has NOT validly approved.

    Includes 'stale' approvals (the node's declared set changed after the
    admin approved it, so the one-time approval no longer holds and the
    dispatch gate refuses). A node declaring nothing is default-deny and
    never appears — there is nothing to grant.
    """
    return _EgressApprovalListResponse(
        tenant_id=current_user.tenant_id,
        pending=_node_egress_approval.pending(
            db, tenant_id=current_user.tenant_id,
        ),
    )


@router.get("/{node_definition_id}/egress-approval",
            response_model=_EgressApprovalStateResponse,
            status_code=status.HTTP_200_OK)
def get_node_egress_approval_state(
    node_definition_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_onboarded_parent_user),
):
    """Report one node's egress-approval state (declared vs approved sets)."""
    nd = crud.get_node_definition_by_id(
        db=db, node_definition_id=node_definition_id,
        tenant_id=current_user.tenant_id,
    )
    if nd is None:
        raise HTTPException(status_code=404, detail="Node definition not found")
    st = _node_egress_approval.approval_state(
        db,
        tenant_id=nd.tenant_id,
        node_definition_id=node_definition_id,
    )
    return _EgressApprovalStateResponse(
        node_definition_id=node_definition_id,
        tenant_id=nd.tenant_id,
        state=st["state"],
        declared=st["declared"],
        approved=st["approved"],
        approval_id=st["approval_id"],
        approved_at=st["approved_at"],
        approved_by_user_id=st["approved_by_user_id"],
        revoked_at=st["revoked_at"],
    )

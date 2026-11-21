# API/routers/harbor.py

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, ConfigDict
import os
import logging
import crud
import models
from crud import harbor_cud
from dependencies import get_db, require_active_and_onboarded_user
from services import harbor_service
from services._base import ServiceError

# Setup logging
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode

router = APIRouter(
    prefix="/harbor",
    tags=["Harbor"],
    dependencies=[Depends(require_active_and_onboarded_user)]
)


class HarborStatusResponse(BaseModel):
    platform_configured: bool
    custom_registry_enabled: bool
    custom_registry_configured: bool
    can_use_custom_registry: bool  # Based on plan
    missing_platform_secrets: List[str]
    missing_custom_secrets: List[str]
    is_platform_admin: bool  # Whether the current user can configure platform secrets


class CustomRegistryConfig(BaseModel):
    enabled: bool
    registry_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


# Response models. Every field is Optional (default None) and extra keys are
# allowed, so these document the existing service payloads without being able
# to reject or strip one -- the router return shapes are dicts built by
# harbor_service and may carry enrichment fields (e.g. variant /
# verification_status / node_name on tenant-image rows, scan_overview on the
# listing). response_model=None is used where a handler can return the empty
# dict {} (tenant scan / quota) or where the raw dict shape is the contract.


class ConfigureRegistryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: Optional[str] = None


class GetRegistryConfigResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: Optional[bool] = None
    registry_url: Optional[str] = None
    username: Optional[str] = None


class TenantImageRow(BaseModel):
    model_config = ConfigDict(extra="allow")
    repository: Optional[str] = None
    tag: Optional[str] = None
    size: Optional[int] = None
    pushed_at: Optional[str] = None
    digest: Optional[str] = None
    scan_overview: Optional[dict] = None
    variant: Optional[str] = None
    verification_status: Optional[str] = None
    node_name: Optional[str] = None


class SoftDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Optional[int] = None
    repository: Optional[str] = None
    tag: Optional[str] = None
    deleted_at: Optional[str] = None
    retention_until: Optional[str] = None
    already_deleted: Optional[bool] = None


class ListSoftDeletedResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Optional[int] = None
    repository: Optional[str] = None
    tag: Optional[str] = None
    digest: Optional[str] = None
    size_bytes: Optional[int] = None
    deleted_at: Optional[str] = None
    deleted_by_user_id: Optional[int] = None
    retention_until: Optional[str] = None


class RestoreResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    restored: Optional[bool] = None
    already_active: Optional[bool] = None
    repository: Optional[str] = None
    tag: Optional[str] = None
    restored_from_deletion_id: Optional[int] = None


# ---------------------------------------------------------------------------
# ServiceError -> HTTPException mapping (api_conventions.install_error_handlers
# is scaffold-only / not wired into main.py yet -- see api_conventions.py
# docstring -- so handlers here map inline, same as the domain/reference
# example in services/_reference_domain_example.py).
# ---------------------------------------------------------------------------

_SERVICE_ERROR_STATUS = {
    "forbidden": status.HTTP_403_FORBIDDEN,
    "bad_request": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
    "gone": status.HTTP_410_GONE,
}


def _raise_http(exc: ServiceError) -> None:
    raise HTTPException(
        status_code=_SERVICE_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.message,
    )


@router.get("/status", response_model=HarborStatusResponse)
def check_harbor_status(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Checks Harbor configuration status for the current tenant.
    Includes both platform Harbor and custom registry settings.

    Security finding WS4.28: the NAMES of missing platform secrets leak
    platform detail to tenant users, so non-platform-admins get
    missing_platform_secrets=[]. platform_configured stays TRUTHFUL for
    everyone: the Workflow Builder (workflow-builder.tsx harborStatus.configured)
    and the Dashboard WB launcher (ui.js) gate on it, so forcing it false
    locks every tenant out of the Workflow Builder.
    """
    out = harbor_service.harbor_status(db, current_user)
    if not getattr(current_user, "is_platform_admin", False):
        out["missing_platform_secrets"] = []
    return out


@router.post("/custom-registry", status_code=status.HTTP_200_OK, response_model=ConfigureRegistryResponse)
def configure_custom_registry(
        config: CustomRegistryConfig,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Configure a user-supplied (bring-your-own) Docker registry.

    Free on self-host / CE (Cap.CUSTOM_REGISTRY is part of the baseline). On hosted
    SaaS the tenant's registry entitlement must include it (allow_custom_registry).
    """
    try:
        return harbor_service.configure_custom_registry(
            db, current_user,
            enabled=config.enabled,
            registry_url=config.registry_url,
            username=config.username,
            password=config.password,
        )
    except ServiceError as exc:
        _raise_http(exc)


@router.get("/custom-registry", response_model=GetRegistryConfigResponse)
def get_custom_registry_config(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get current custom registry configuration (without passwords).
    """
    return harbor_service.get_custom_registry_config(db, current_user)


# ---------------------------------------------------------------------------
# Tenant Registry (Dashboard → Tenant Registry tab) — Phase 10
# ---------------------------------------------------------------------------
# Three read-only endpoints that proxy Harbor's API, scoped to the current
# user's tenant_id (never accepts a tenant id from the client).
#
# Lives under the same /harbor/ prefix as the existing status + custom-
# registry routes; the `/tenant-*` path prefix makes them easy to find
# + distinguish from platform-Harbor configuration.

def _iso_retention_days() -> int:
    """ISO 27001 retention window before a soft-deleted tenant image
    is hard-deleted by the reaper. Configurable via env so platform
    admins can match their compliance policy without a code change.
    Default 30 days.

    Thin re-export of services.harbor_service.iso_retention_days --
    kept here (not inlined) because tests/test_tenant_image_soft_delete.py
    calls `harbor._iso_retention_days()` directly.
    """
    return harbor_service.iso_retention_days()


def _active_soft_delete_keys(db: Session, tenant_id: int) -> set[tuple[str, str]]:
    """Return the (repository, tag) set whose deletion is still inside
    the retention window — the listing endpoint filters these out so
    the user sees them as gone.

    Thin re-export of crud.harbor_cud.active_soft_delete_keys -- kept
    here (not inlined) because tests/test_tenant_image_soft_delete.py
    source-inspects tenant_harbor_images() for a literal call to it.
    """
    return harbor_cud.active_soft_delete_keys(db, tenant_id)


def _purge_nodes_referencing_image(
    db: Session, tenant_id: int, repository: str, tag: str,
) -> int:
    """After the reaper hard-deletes a tenant Harbor image, drop the
    matching gpu_variants entry from every NodeDefinition that pointed
    at it. If a node has no remaining variants we delete the row
    outright — it can't run anywhere and the Workflow Builder filter
    was already hiding it during the retention window.

    Matching is on the trailing "repo:tag" tail, same convention
    crud.node_definition_cud._hide_nodes_with_soft_deleted_images uses
    for the soft-delete-window hide. Returns the count of NodeDefinition
    rows mutated.

    Thin re-export of services.harbor_service.purge_nodes_referencing_image.
    """
    return harbor_service.purge_nodes_referencing_image(db, tenant_id, repository, tag)


@router.get("/tenant-images", response_model=List[TenantImageRow])
def tenant_harbor_images(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """List every image in the tenant's Harbor project, flattened into
    one row per (repository, tag) pair. Powers the Dashboard's
    Tenant Registry table.

    P9f.3 enrichment: each row carries `variant` + `verification_status`
    when it maps to a NodeDefinition's gpu_variants entry. Lets the
    Dashboard render a per-variant verified/pending/failed dot without
    a second round-trip to /node_definitions.
    """
    if not current_user.tenant_id:
        return []
    soft_deleted = _active_soft_delete_keys(db, current_user.tenant_id)
    return harbor_service.list_tenant_images(db, current_user.tenant_id, soft_deleted)


@router.post(
    "/tenant-images/soft-delete",
    status_code=status.HTTP_201_CREATED,
    response_model=SoftDeleteResponse,
)
def soft_delete_tenant_image(
    repository: str,
    tag: str,
    digest: Optional[str] = None,
    size_bytes: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """ISO-compliant soft delete of a tenant Harbor image (item #8).

    Marks the (repository, tag) deleted, hides it from the tenant's
    listing immediately, and records `retention_until = now() +
    ISO_RETENTION_DAYS`. The actual Harbor blob is hard-deleted by
    a background reaper after the retention window — no UI path
    triggers a hard delete.

    Permission: requires the user to belong to the tenant whose
    images they're deleting (the only tenant a non-platform-admin
    can soft-delete in is their own). Platform admins can act on
    any tenant's images via the same endpoint.
    """
    try:
        return harbor_service.soft_delete_tenant_image(
            db, current_user,
            repository=repository, tag=tag,
            digest=digest, size_bytes=size_bytes,
        )
    except ServiceError as exc:
        _raise_http(exc)


def reap_expired_tenant_image_deletions(db: Session, dry_run: bool = False) -> dict:
    """Background reaper: hard-deletes tenant Harbor blobs whose
    soft-delete retention has expired. Safe to call repeatedly —
    rows stay around with `reaped_at` set as audit history.

    Designed to be invoked from a scheduled background task or a
    one-shot ops command. Per `dry_run=True` it returns the planned
    actions without touching Harbor or the DB.

    Thin re-export of services.harbor_service.reap_expired_tenant_image_deletions
    -- kept here (not inlined) because tests/test_tenant_image_soft_delete.py
    inspects `harbor.reap_expired_tenant_image_deletions`'s signature.
    """
    return harbor_service.reap_expired_tenant_image_deletions(db, dry_run=dry_run)


@router.get("/tenant-images/soft-deleted", response_model=List[ListSoftDeletedResponse])
def list_soft_deleted_tenant_images(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """List images this tenant has soft-deleted and that are still
    inside the ISO retention window — i.e. restorable. Powers a
    "Recently deleted" / "Trash" view on the Tenant Registry page so
    users can undo a soft-delete before the reaper hard-deletes the
    Harbor blob.

    Once reaped (reaped_at IS NOT NULL) a row is excluded — the blob
    is gone from Harbor and the NodeDefinition has been purged; the
    row stays in the table as audit history only.
    """
    if not current_user.tenant_id:
        return []
    return harbor_service.list_soft_deleted_tenant_images(db, current_user)


@router.post(
    "/tenant-images/restore",
    status_code=status.HTTP_200_OK,
    response_model=RestoreResponse,
)
def restore_tenant_image(
    repository: str,
    tag: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Undo a soft-delete inside the ISO retention window. Deletes the
    tenant_image_deletions row so the image reappears in the registry
    listing and any NodeDefinition that references it returns to the
    Workflow Builder palette.

    Status semantics:
      * 200 with `restored: true`   — row existed and was removed.
      * 200 with `already_active: true` — idempotent re-call; no row to
        restore. The image is presumed not deleted.
      * 410 Gone — row exists but was already reaped (Harbor blob is
        gone, NodeDefinition has been purged). No way to restore short
        of re-pushing the image and re-creating the node.

    Permission: any active tenant user can restore their own tenant's
    deletions (mirrors soft-delete). Platform admins act on any tenant.
    """
    try:
        return harbor_service.restore_tenant_image(
            db, current_user, repository=repository, tag=tag,
        )
    except ServiceError as exc:
        _raise_http(exc)


@router.get("/tenant-scans/{digest:path}", response_model=None)
def tenant_harbor_scan(
    digest: str,
    repo: Optional[str] = None,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Full Trivy vulnerability detail for one digest in the tenant's
    project. Harbor needs both project+repo+digest to resolve it; the
    client passes repo via the `repo` query arg."""
    return harbor_service.get_tenant_scan(current_user.tenant_id, repo, digest)


@router.get("/tenant-quota", response_model=None)
def tenant_harbor_quota(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Return ``{used_bytes, hard_quota_bytes}`` for the tenant's Harbor
    project. Drives the storage bar + quota-warning banner on the
    Dashboard tab."""
    return harbor_service.get_tenant_quota(current_user.tenant_id)

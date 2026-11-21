# API/services/harbor_service.py
"""Business logic for the Harbor domain (routers/harbor.py).

Two sub-domains share this module because they share the router file
(Gate 2b migration -- one owner for the whole router):

  * platform / custom Docker-registry configuration (secrets-backed,
    entitlement-gated).
  * tenant Harbor image usage metering: list, ISO soft-delete, restore,
    background reap, per-digest scan detail, project quota.

Owns the transaction boundary and every Harbor-API / entitlement call;
`crud.harbor_cud` is the only place `db.query(models.X)` runs for this
domain. Routers stay HTTP-only (parse/validate input, map ServiceError
-> HTTPException, shape the response) and call into here.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

import crud
import models
from crud import harbor_cud
from services._base import ServiceError

logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data) -- mirrors
# the flag routers/harbor.py used before this migration.
from services.debug_mode import is_debug_mode


class ForbiddenError(ServiceError):
    """Caller lacks the entitlement/permission for the action (router
    maps -> 403). `services/_base.py` declares no forbidden error (see
    the 404-vs-403 trap note in the Gate-2 migration rules), so it's
    declared here instead."""

    def __init__(self, message: str = "Forbidden", *, details=None):
        super().__init__(message, code="forbidden", details=details)


def iso_retention_days() -> int:
    """ISO 27001 retention window (days) before a soft-deleted tenant
    image is hard-deleted by the reaper. Env-configurable so platform
    admins can match their compliance policy without a code change.
    Default 30 days.
    """
    raw = os.getenv("ISO_RETENTION_DAYS", "30")
    try:
        n = int(raw)
        return max(1, n)
    except (TypeError, ValueError):
        return 30


# ===========================================================================
# Platform / custom registry configuration
# ===========================================================================

def harbor_status(db: Session, current_user: "models.User") -> dict:
    """Harbor configuration status for the caller's tenant: platform
    Harbor secrets + optional custom-registry secrets."""
    from utils import entitlements

    tenant = harbor_cud.get_tenant(db, current_user.tenant_id)
    can_use_custom = entitlements.evaluate_feature(
        entitlements.Cap.CUSTOM_REGISTRY,
        entitlements.get_entitlements(db, tenant),
    ).allowed

    # Check platform Harbor credentials.
    # First check platform tenant (1), then fall back to the user's
    # tenant ONLY if their plan allows a custom registry.
    platform_tenant_id = 1
    platform_secrets = ["harbor_registry", "harbor_username", "harbor_password"]
    missing_platform: List[str] = []

    for secret_name in platform_secrets:
        decrypted = crud.get_decrypted_secret_by_name(db, secret_name, platform_tenant_id)

        if not decrypted or decrypted.strip() == '' or decrypted == 'PLEASE_CONFIGURE':
            if can_use_custom and current_user.tenant_id != platform_tenant_id:
                decrypted = crud.get_decrypted_secret_by_name(db, secret_name, current_user.tenant_id)
                if decrypted and decrypted.strip() != '' and decrypted != 'PLEASE_CONFIGURE':
                    if is_debug_mode():
                        logger.info(f"[Harbor Check] {secret_name}: using tenant's custom credentials (plan allows)")
                    else:
                        logger.info(f"[Harbor Check] {secret_name}: using tenant's custom credentials")

        if not decrypted or decrypted.strip() == '' or decrypted == 'PLEASE_CONFIGURE':
            logger.warning(f"[Harbor Check] {secret_name}: MISSING or INVALID")
            missing_platform.append(secret_name)
        else:
            if is_debug_mode():
                logger.info(f"[Harbor Check] {secret_name}: VALID (length: {len(decrypted)})")
            else:
                logger.info(f"[Harbor Check] {secret_name}: VALID")

    # Check custom registry configuration.
    custom_enabled_secret = crud.get_decrypted_secret_by_name(db, "custom_registry_enabled", current_user.tenant_id)
    custom_enabled = custom_enabled_secret == "true" if custom_enabled_secret else False

    custom_secrets = ["custom_registry_url", "custom_registry_username", "custom_registry_password"]
    missing_custom: List[str] = []
    if custom_enabled:
        for secret_name in custom_secrets:
            secret = crud.get_decrypted_secret_by_name(db, secret_name, current_user.tenant_id)
            if not secret or secret.strip() == '':
                missing_custom.append(secret_name)

    return {
        "platform_configured": len(missing_platform) == 0,
        "custom_registry_enabled": custom_enabled,
        "custom_registry_configured": custom_enabled and len(missing_custom) == 0,
        "can_use_custom_registry": can_use_custom,
        "missing_platform_secrets": missing_platform,
        "missing_custom_secrets": missing_custom,
        "is_platform_admin": current_user.is_platform_admin or False,
    }


def configure_custom_registry(
    db: Session,
    current_user: "models.User",
    *,
    enabled: bool,
    registry_url: Optional[str],
    username: Optional[str],
    password: Optional[str],
) -> dict:
    """Persist custom-registry config after checking the tenant's plan
    entitlement. Raises `ForbiddenError` (-> 403) or `ServiceError`
    code="bad_request" (-> 400) on policy violations."""
    from utils import entitlements

    tenant = harbor_cud.get_tenant(db, current_user.tenant_id)
    if not entitlements.evaluate_feature(
        entitlements.Cap.CUSTOM_REGISTRY,
        entitlements.get_entitlements(db, tenant),
    ).allowed:
        raise ForbiddenError(
            "Custom registry configuration is not enabled for this tenant."
        )

    # Update custom registry settings.
    crud.create_or_update_secret(
        db,
        secret_name="custom_registry_enabled",
        value="true" if enabled else "false",
        user=current_user,
    )

    if enabled:
        # Validate required fields.
        if not registry_url or not username or not password:
            raise ServiceError(
                "registry_url, username, and password are required when enabling custom registry",
                code="bad_request",
            )

        # Store credentials.
        crud.create_or_update_secret(db, "custom_registry_url", registry_url, current_user)
        crud.create_or_update_secret(db, "custom_registry_username", username, current_user)
        crud.create_or_update_secret(db, "custom_registry_password", password, current_user)

    return {"message": "Custom registry configuration updated successfully"}


def get_custom_registry_config(db: Session, current_user: "models.User") -> dict:
    """Current custom registry configuration (without passwords)."""
    enabled = crud.get_decrypted_secret_by_name(db, "custom_registry_enabled", current_user.tenant_id)

    if enabled != "true":
        return {
            "enabled": False,
            "registry_url": None,
            "username": None,
        }

    return {
        "enabled": True,
        "registry_url": crud.get_decrypted_secret_by_name(db, "custom_registry_url", current_user.tenant_id),
        "username": crud.get_decrypted_secret_by_name(db, "custom_registry_username", current_user.tenant_id),
    }


# ===========================================================================
# Tenant Registry (Dashboard -> Tenant Registry tab) -- Phase 10
# ===========================================================================

def purge_nodes_referencing_image(
    db: Session, tenant_id: int, repository: str, tag: str,
) -> int:
    """After the reaper hard-deletes a tenant Harbor image, drop the
    matching gpu_variants entry from every NodeDefinition that pointed
    at it. If a node has no remaining variants we delete the row
    outright -- it can't run anywhere and the Workflow Builder filter
    was already hiding it during the retention window.

    Matching is on the trailing "repo:tag" tail, same convention
    crud.node_definition_cud._hide_nodes_with_soft_deleted_images uses
    for the soft-delete-window hide. Returns the count of NodeDefinition
    rows mutated.
    """
    target_tail = f"{repository}:{tag}"
    nodes = harbor_cud.list_nodes_for_tenant(db, tenant_id)
    touched = 0
    for node in nodes:
        variants = node.gpu_variants or {}
        if not variants:
            continue
        kept = {
            variant: ref
            for variant, ref in variants.items()
            if not (isinstance(ref, str) and ref.split("/")[-1] == target_tail)
        }
        if kept == variants:
            continue
        if kept:
            harbor_cud.set_node_gpu_variants(db, node, kept)
        else:
            harbor_cud.delete_node(db, node)
            logger.info(
                "Deleted NodeDefinition id=%s name=%s after final image reap "
                "(tenant=%s repo=%s tag=%s).",
                node.id, node.name, tenant_id, repository, tag,
            )
        touched += 1
    return touched


def list_tenant_images(
    db: Session, tenant_id: int, soft_deleted: Set[Tuple[str, str]],
) -> List[dict]:
    """List every image in the tenant's Harbor project, flattened into
    one row per (repository, tag) pair. Powers the Dashboard's Tenant
    Registry table.

    `soft_deleted` (the (repository, tag) pairs still inside the ISO
    retention window) is computed by the caller so the router body that
    calls in here keeps a literal reference to the soft-delete lookup
    (test_tenant_image_soft_delete.py source-inspects the router handler
    for exactly this).

    P9f.3 enrichment: each row carries `variant` + `verification_status`
    when it maps to a NodeDefinition's gpu_variants entry. Lets the
    Dashboard render a per-variant verified/pending/failed dot without a
    second round-trip to /node_definitions.
    """
    try:
        from utils.tenant_harbor_client import (
            TenantHarborClient, TenantHarborError, project_name_for_tenant,
        )
        harbor = TenantHarborClient()
        repos = harbor.list_repositories(tenant_id)
    except Exception as e:
        logger.warning("tenant-images fetch failed for tenant %s: %s", tenant_id, e)
        return []

    # Reverse lookup: normalized image_ref tail -> (node_name, variant, status).
    # image_ref strings in NodeDefinition.gpu_variants carry the full
    # "harbor.host/project/repo:tag" form; Harbor's API returns
    # "project/repo" for name and tag separately. We match on the
    # "repo:tag" tail since the host+project prefix is the same for
    # every row and varies by deployment.
    variant_lookup: Dict[str, Tuple[str, str, str]] = {}
    try:
        nodes = harbor_cud.list_nodes_for_tenant(db, tenant_id)
        for node in nodes:
            gpu_variants = node.gpu_variants or {}
            vvs = node.variant_verification_status or {}
            for variant, image_ref in gpu_variants.items():
                if not isinstance(image_ref, str) or not image_ref:
                    continue
                tail = image_ref.split("/")[-1]
                if ":" not in tail:
                    continue
                status_val = vvs.get(variant) or "pending"
                variant_lookup[tail] = (node.name, variant, status_val)
    except Exception as e:
        logger.debug("tenant-images variant lookup skipped: %s", e)

    flat: List[dict] = []
    for repo in repos or []:
        repo_name = (repo.get("name") or "").split("/", 1)[-1]
        # Harbor v2 /projects/{p}/repositories returns repository
        # records WITHOUT tag info -- that lives on /artifacts. Fetch
        # per-repo so the dashboard sees real (tag, digest, push_time,
        # scan_overview) values instead of a hardcoded ("latest", null).
        #
        # with_scan_overview=True is REQUIRED to get per-artifact scan
        # state. Without it, Harbor returns each artifact with an empty
        # `scan_overview: {}` and the Tenant Registry UI renders that as
        # "Scanning..." forever (tenant-registry.js line 197 treats empty
        # status as in-progress). Stage repro: user saw their PDF->PNG
        # image stuck at "pending/scanning" in Data -> Tenant Registry
        # even though the API-side scan daemon had already written
        # scan_status=high_vulns on the NodeDefinition row.
        try:
            artifacts = harbor.list_repository_artifacts(
                tenant_id, repo_name,
                with_scan_overview=True,
            )
        except Exception as e:
            logger.warning(
                "tenant-images artifact fetch failed for %s/%s: %s",
                project_name_for_tenant(tenant_id), repo_name, e,
            )
            artifacts = []

        # Walk every (artifact, tag) pair. An artifact with no tags
        # (uncommon but possible -- dangling manifest) is skipped from
        # the listing entirely; the dashboard surfaces named tags only.
        rendered_any_row = False
        for artifact in artifacts:
            digest = artifact.get("digest")
            size_bytes = artifact.get("size") or 0
            scan_overview = artifact.get("scan_overview") or {}
            for tag in artifact.get("tags") or []:
                tag_name = tag.get("name") or "latest"
                if (repo_name, tag_name) in soft_deleted:
                    continue  # hidden during ISO retention window
                key = f"{repo_name}:{tag_name}"
                match = variant_lookup.get(key)
                flat.append({
                    "repository": repo_name,
                    "tag": tag_name,
                    "size": size_bytes,
                    "pushed_at": tag.get("push_time") or artifact.get("push_time"),
                    "digest": digest,
                    "scan_overview": scan_overview,
                    "variant": match[1] if match else None,
                    "verification_status": match[2] if match else None,
                    "node_name": match[0] if match else None,
                })
                rendered_any_row = True

        # Repo with no artifacts at all (empty after a hard delete or
        # mid-push). Fall back to the legacy synthetic "latest" row so
        # the user sees the repo exists, and still honour the
        # soft-delete filter for that synthetic key.
        if not rendered_any_row and not artifacts:
            if (repo_name, "latest") in soft_deleted:
                continue
            flat.append({
                "repository": repo_name,
                "tag": "latest",
                "size": repo.get("size") or 0,
                "pushed_at": repo.get("update_time") or repo.get("pushed_at"),
                "digest": None,
                "scan_overview": {},
                "variant": None,
                "verification_status": None,
                "node_name": None,
            })
    return flat


def soft_delete_tenant_image(
    db: Session,
    current_user: "models.User",
    *,
    repository: str,
    tag: str,
    digest: Optional[str] = None,
    size_bytes: Optional[int] = None,
) -> dict:
    """ISO-compliant soft delete of a tenant Harbor image (item #8).

    Marks the (repository, tag) deleted, hides it from the tenant's
    listing immediately, and records `retention_until = now() +
    ISO_RETENTION_DAYS`. The actual Harbor blob is hard-deleted by a
    background reaper after the retention window -- no UI path triggers
    a hard delete.

    Permission: requires the user to belong to the tenant whose images
    they're deleting (the only tenant a non-platform-admin can
    soft-delete in is their own). Platform admins can act on any
    tenant's images via the same endpoint.
    """
    if not current_user.tenant_id:
        raise ForbiddenError("No tenant context for this user")
    if not repository or not tag:
        raise ServiceError("repository and tag are required", code="bad_request")

    # Idempotency: if there's already an unreaped soft-delete for this
    # (tenant, repo, tag) inside the retention window, return the
    # existing row rather than failing.
    existing = harbor_cud.get_active_soft_delete(db, current_user.tenant_id, repository, tag)
    if existing:
        return {
            "id": existing.id,
            "repository": existing.repository,
            "tag": existing.tag,
            "deleted_at": existing.deleted_at.isoformat() if existing.deleted_at else None,
            "retention_until": existing.retention_until.isoformat() if existing.retention_until else None,
            "already_deleted": True,
        }

    retention_days = iso_retention_days()
    retention_until = datetime.now(timezone.utc) + timedelta(days=retention_days)

    row = harbor_cud.add_soft_delete(
        db,
        tenant_id=current_user.tenant_id,
        repository=repository,
        tag=tag,
        digest=digest,
        size_bytes=size_bytes,
        deleted_by_user_id=current_user.id,
        retention_until=retention_until,
    )

    # Audit (TPN CS-7, CMMC AU-2) -- re-using the freeform AuditEvent row
    # with a clear event_name; AuditAction enum has no tenant-image
    # entry today and adding one is out of launch scope.
    try:
        harbor_cud.add_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="TENANT_IMAGE_SOFT_DELETED",
            details={
                "repository": repository, "tag": tag, "digest": digest,
                "size_bytes": size_bytes,
                "retention_until": retention_until.isoformat(),
                "iso_retention_days": retention_days,
            },
        )
    except Exception:
        logger.exception("audit emission failed for tenant image soft-delete")

    db.commit()
    db.refresh(row)
    logger.info(
        "Tenant image soft-deleted: tenant=%s repo=%s tag=%s retention_until=%s",
        current_user.tenant_id, repository, tag, retention_until.isoformat(),
    )
    return {
        "id": row.id,
        "repository": row.repository,
        "tag": row.tag,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
        "retention_until": row.retention_until.isoformat(),
        "already_deleted": False,
    }


def reap_expired_tenant_image_deletions(db: Session, dry_run: bool = False) -> dict:
    """Background reaper: hard-deletes tenant Harbor blobs whose
    soft-delete retention has expired. Safe to call repeatedly -- rows
    stay around with `reaped_at` set as audit history.

    Designed to be invoked from a scheduled background task or a
    one-shot ops command. Per `dry_run=True` it returns the planned
    actions without touching Harbor or the DB.
    """
    now = datetime.now(timezone.utc)
    rows = harbor_cud.list_expired_deletions(db, now=now)
    plan = [{
        "id": r.id, "tenant_id": r.tenant_id,
        "repository": r.repository, "tag": r.tag,
        "retention_until": r.retention_until.isoformat() if r.retention_until else None,
    } for r in rows]
    if dry_run:
        return {"dry_run": True, "would_reap": plan, "count": len(plan)}

    if not rows:
        return {"reaped": [], "count": 0}

    from utils.paid_import import optional_paid
    _thc = optional_paid("utils.tenant_harbor_client")
    if _thc is None:
        return {"reaped": [], "count": 0}
    harbor = _thc.TenantHarborClient()
    reaped = []
    failed = []
    skipped_legal_hold = []
    # GY.C15 — legal hold: images of a held tenant are not reaped
    # (supports preservation requirements, e.g. DFARS 252.204-7012 90-day).
    # Fail closed: a hold-lookup error skips the whole batch.
    from utils.paid_import import optional_paid
    _lhold = optional_paid("services.legal_hold")
    try:
        if _lhold is None:  # FX.162.p5 — CE carve: no legal holds exist
            _lh_wide, _lh_ids = False, set()
        else:
            _lh_wide, _lh_ids = _lhold.held_tenants(db)
    except Exception as e:  # noqa: BLE001 — fail closed
        logger.error("harbor reaper: legal-hold lookup failed — skipping "
                     "batch (fail closed): %s", e)
        return {"reaped": [], "failed": failed, "skipped_legal_hold": [
            {"id": r.id, "repository": r.repository, "tag": r.tag}
            for r in rows], "count": 0}
    for r in rows:
        if _lh_wide or r.tenant_id in _lh_ids:
            logger.info(
                "Harbor reaper: SKIPPED tenant image (legal hold) tenant=%s "
                "repo=%s tag=%s — preserved", r.tenant_id, r.repository, r.tag,
            )
            skipped_legal_hold.append({
                "id": r.id, "repository": r.repository, "tag": r.tag,
            })
            continue
        try:
            harbor.delete_tag(r.tenant_id, r.repository, r.tag)
            harbor_cud.mark_reaped(r, reaped_at=now)
            db.add(r)
            purge_nodes_referencing_image(db, r.tenant_id, r.repository, r.tag)
            reaped.append({
                "id": r.id, "repository": r.repository, "tag": r.tag,
            })
            logger.info(
                "Reaped tenant image: tenant=%s repo=%s tag=%s",
                r.tenant_id, r.repository, r.tag,
            )
        except Exception as e:
            logger.exception(
                "Reaper failed for tenant=%s repo=%s tag=%s",
                r.tenant_id, r.repository, r.tag,
            )
            failed.append({
                "id": r.id, "repository": r.repository, "tag": r.tag,
                "error": str(e),
            })
    db.commit()
    return {"reaped": reaped, "failed": failed,
            "skipped_legal_hold": skipped_legal_hold, "count": len(reaped)}


def list_soft_deleted_tenant_images(db: Session, current_user: "models.User") -> List[dict]:
    """List images this tenant has soft-deleted and that are still
    inside the ISO retention window -- i.e. restorable. Powers a
    "Recently deleted" / "Trash" view on the Tenant Registry page so
    users can undo a soft-delete before the reaper hard-deletes the
    Harbor blob.

    Once reaped (reaped_at IS NOT NULL) a row is excluded -- the blob is
    gone from Harbor and the NodeDefinition has been purged; the row
    stays in the table as audit history only.
    """
    rows = harbor_cud.list_active_soft_deletes(db, current_user.tenant_id)
    return [{
        "id": r.id,
        "repository": r.repository,
        "tag": r.tag,
        "digest": r.digest,
        "size_bytes": r.size_bytes,
        "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
        "deleted_by_user_id": r.deleted_by_user_id,
        "retention_until": r.retention_until.isoformat() if r.retention_until else None,
    } for r in rows]


def restore_tenant_image(
    db: Session,
    current_user: "models.User",
    *,
    repository: str,
    tag: str,
) -> dict:
    """Undo a soft-delete inside the ISO retention window. Deletes the
    tenant_image_deletions row so the image reappears in the registry
    listing and any NodeDefinition that references it returns to the
    Workflow Builder palette.

    Result semantics:
      * `restored=True`               -- row existed and was removed.
      * `restored=False, already_active=True` -- idempotent re-call; no
        row to restore. The image is presumed not deleted.
      * raises ServiceError code="gone" (router maps -> 410) -- row
        exists but was already reaped (Harbor blob is gone,
        NodeDefinition has been purged). No way to restore short of
        re-pushing the image and re-creating the node.

    Permission: any active tenant user can restore their own tenant's
    deletions (mirrors soft-delete). Platform admins act on any tenant.
    """
    if not current_user.tenant_id:
        raise ForbiddenError("No tenant context for this user")
    if not repository or not tag:
        raise ServiceError("repository and tag are required", code="bad_request")

    row = harbor_cud.get_latest_deletion(db, current_user.tenant_id, repository, tag)
    if not row:
        return {
            "restored": False,
            "already_active": True,
            "repository": repository,
            "tag": tag,
        }
    if row.reaped_at is not None:
        raise ServiceError(
            f"Image {repository}:{tag} was reaped at "
            f"{row.reaped_at.isoformat()}; the Harbor blob is gone "
            "and the original node was deleted. Restore is no longer "
            "possible -- re-push the image and re-create the node.",
            code="gone",
        )

    deleted_id = row.id
    deleted_at = row.deleted_at.isoformat() if row.deleted_at else None
    harbor_cud.delete_deletion_row(db, row)

    try:
        harbor_cud.add_audit_event(
            db,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="TENANT_IMAGE_RESTORED",
            details={
                "repository": repository,
                "tag": tag,
                "restored_from_deletion_id": deleted_id,
                "original_deleted_at": deleted_at,
            },
        )
    except Exception:
        logger.exception("audit emission failed for tenant image restore")

    db.commit()
    logger.info(
        "Tenant image restored: tenant=%s repo=%s tag=%s (from deletion id=%s)",
        current_user.tenant_id, repository, tag, deleted_id,
    )
    return {
        "restored": True,
        "repository": repository,
        "tag": tag,
        "restored_from_deletion_id": deleted_id,
    }


def get_tenant_scan(tenant_id: Optional[int], repo: Optional[str], digest: str) -> dict:
    """Full Trivy vulnerability detail for one digest in the tenant's
    project. Harbor needs both project+repo+digest to resolve it; the
    client passes repo via the `repo` query arg. External-only (no db)."""
    if not tenant_id:
        return {}
    if not repo:
        # Fallback: try to find the digest by scanning repos.
        return {}
    try:
        from utils.tenant_harbor_client import TenantHarborClient
        return TenantHarborClient().get_vulnerabilities(tenant_id, repo, digest)
    except Exception as e:
        logger.warning("tenant-scans fetch failed: %s", e)
        return {}


def get_tenant_quota(tenant_id: Optional[int]) -> dict:
    """`{used_bytes, hard_quota_bytes}` for the tenant's Harbor project.
    Drives the storage bar + quota-warning banner on the Dashboard tab.
    External-only (no db)."""
    if not tenant_id:
        return {"used_bytes": 0, "hard_quota_bytes": -1}
    try:
        from utils.tenant_harbor_client import TenantHarborClient
        return TenantHarborClient().get_project_quota(tenant_id)
    except Exception as e:
        logger.warning("tenant-quota fetch failed: %s", e)
        return {"used_bytes": 0, "hard_quota_bytes": -1}

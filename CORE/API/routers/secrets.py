# In Main/routers/secrets.py

from typing import List, Optional, Union
from pydantic import BaseModel
from fastapi import APIRouter, Depends, status, HTTPException, BackgroundTasks, Body
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

import schemas, models
from services import secrets_service
from services import secret_expiry_service
from dependencies import get_db, require_active_and_onboarded_user, require_capability, require_bearer_or_api_key

router = APIRouter(
    tags=["Secrets"],
    dependencies=[Depends(require_active_and_onboarded_user)]
)

# Service router for API key access (no onboarding required)
service_router = APIRouter(
    tags=["Secrets"],
    prefix=""
)


def _has_global_secret_capability(db: Session, current_user: models.User, capability: str) -> bool:
    """True if the user can <capability> ANY secret via a custom role that holds
    the SECRET capability (the legacy role-based gate). No platform-admin bypass
    (ISO/DoD tenant isolation)."""
    return secrets_service.has_global_secret_capability(db, current_user, capability)


def _can_access_secret(db: Session, current_user: models.User, secret_id: int, capability: str) -> bool:
    """Per-secret authorization. Honors BOTH the per-secret grants made in the
    Permissions UI (owner via grant_owner_permissions, direct share, team/role on
    the resource) AND the legacy global role capability. Without this, sharing a
    secret to a user had no effect."""
    return secrets_service.can_access_secret(db, current_user, secret_id, capability)

@router.post("/", response_model=schemas.Secret, status_code=status.HTTP_201_CREATED)
def create_secret(
    secret: schemas.SecretCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_capability(models.ResourceType.SECRET, "create"))
):
    """
    Creates a new, encrypted secret for the tenant.
    The secret value is encrypted by the server and is write-only.
    """
    try:
        return secrets_service.create_secret(db, secret, current_user)
    except secrets_service.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None
    except secret_expiry_service.GovExpiryError as e:
        raise HTTPException(status_code=422, detail=e.message) from None
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A secret with this name already exists for your tenant.",
        )


@router.get("/", response_model=List[schemas.Secret])
def list_secrets(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    limit: int = 1000,
    offset: int = 0,
    sort_by: str = "name",
    order: str = "asc"
):
    """
    Lists the secrets the caller can see for the tenant, with pagination and
    sorting. The secret values are never returned.

    A user with the global SECRET 'view' role capability (or platform admin)
    sees every tenant secret; a user who only has per-secret grants sees just
    the ones shared with them.
    """
    return secrets_service.list_secrets(
        db, current_user, limit=limit, offset=offset, sort_by=sort_by, order=order
    )


# =========================================================================
# Platform secret sharing — admin shares secrets with tenants.
#
# clean-v2-api platform-services wave: collapsed off the grab-bag
# `/secrets/platform/secrets/*` stutter (mounted under `secrets` already,
# it doubled the resource name and stacked a `/platform` prefix-as-authz
# segment on top) onto the plain `secrets` resource. Auth was already a
# dependency-enforced check inside secrets_service (ForbiddenError -> 403),
# never the URL, so no auth behavior changes.
#
# `GET /shared` MUST be declared before `GET /{secret_id}` below: FastAPI/
# Starlette route matching is registration-order + first-structural-match
# (the `{secret_id}` path param has no `:int` converter), so if `/shared`
# were declared after `/{secret_id}` a request for `/secrets/shared` would
# match `/{secret_id}` first and 422 on the int conversion instead of
# reaching this handler.
# =========================================================================

class ShareSecretRequest(BaseModel):
    tenant_ids: Optional[List[int]] = None
    all_tenants: bool = False


class SharedSecretInfo(BaseModel):
    id: int
    name: str
    description: Optional[str]
    shared_with_all_tenants: bool
    shared_with_tenant_ids: Optional[List[str]]


class ShareSecretResponse(BaseModel):
    message: str
    secret_id: int
    secret_name: str
    shared_with_all_tenants: bool
    shared_with_tenant_ids: Optional[List[str]]


class UnshareSecretResponse(BaseModel):
    message: str
    secret_id: int


class RequestSecretAccessResponse(BaseModel):
    detail: str
    request_id: int
    secret_name: str


@router.get("/shared", response_model=List[SharedSecretInfo])
def list_shared_secrets(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """List all platform secrets that are shared with tenants. Platform admin only."""
    try:
        secrets = secrets_service.list_shared_secrets(db, current_user)
    except secrets_service.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None

    return [
        SharedSecretInfo(
            id=s.id,
            name=s.name,
            description=s.description,
            shared_with_all_tenants=s.shared_with_all_tenants,
            shared_with_tenant_ids=s.shared_with_tenant_ids,
        )
        for s in secrets
    ]


@router.post("/{secret_id}/share", response_model=ShareSecretResponse)
def share_secret_with_tenants(
    secret_id: int,
    request: ShareSecretRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Share a platform secret with specific tenants or all tenants. Platform admin only."""
    try:
        secret = secrets_service.share_secret_with_tenants(
            db, secret_id, request.tenant_ids, request.all_tenants, current_user
        )
    except secrets_service.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None
    except secrets_service.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    except secrets_service.BadRequestError as e:
        raise HTTPException(status_code=400, detail=e.message) from None

    return {
        "message": "Secret shared successfully",
        "secret_id": secret_id,
        "secret_name": secret.name,
        "shared_with_all_tenants": secret.shared_with_all_tenants,
        "shared_with_tenant_ids": secret.shared_with_tenant_ids,
    }


@router.delete("/{secret_id}/share", response_model=UnshareSecretResponse)
def unshare_secret(
    secret_id: int,
    tenant_ids: Optional[List[int]] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Revoke sharing of a platform secret. Platform admin only.

    If tenant_ids provided, removes only those tenants.
    If no tenant_ids, revokes all sharing.
    """
    try:
        secrets_service.unshare_secret(db, secret_id, tenant_ids, current_user)
    except secrets_service.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None
    except secrets_service.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None

    return {"message": "Secret sharing revoked", "secret_id": secret_id}


@router.post("/{secret_id}/request-access", status_code=status.HTTP_201_CREATED, response_model=RequestSecretAccessResponse)
def request_secret_access(
    secret_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    justification: Optional[str] = Body(None, embed=True),
):
    """Create a pending access request for a secret the caller can't use.
    The secret owner (created_by_user_id) reviews it in the Permission Requests
    inbox; approving grants the requester 'view'."""
    try:
        return secrets_service.request_secret_access(
            db, secret_id, current_user, justification, background_tasks
        )
    except secrets_service.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    except secrets_service.BadRequestError as e:
        raise HTTPException(status_code=400, detail=e.message) from None


@router.get("/{secret_id}", response_model=schemas.Secret)
def get_secret(
    secret_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Retrieves the metadata for a single secret.
    The secret value is never returned.
    """
    try:
        return secrets_service.get_secret(db, secret_id, current_user)
    except secrets_service.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    except secrets_service.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None


@router.patch("/{secret_id}", response_model=schemas.Secret)
def update_secret(
    secret_id: int,
    secret_update: schemas.SecretUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Update a secret's name, description, or value.
    """
    try:
        return secrets_service.update_secret(db, secret_id, secret_update, current_user)
    except secrets_service.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    except secrets_service.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None
    except secret_expiry_service.GovExpiryError as e:
        raise HTTPException(status_code=422, detail=e.message) from None
    except secrets_service.BadRequestError as e:
        raise HTTPException(status_code=400, detail=e.message) from None


@router.delete("/{secret_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_secret(
    secret_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Soft deletes a secret.
    """
    try:
        secrets_service.delete_secret(db, secret_id, current_user)
    except secrets_service.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    except secrets_service.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None
    return


# --- FX.117 p3b: per-secret allowed-workflows scope -------------------------
# The tenant comes from the token (crud.get_secret_by_id is tenant-scoped), so
# another tenant's secret is a 404, never a 403. The permission is the same
# per-secret gate the existing secrets routes use (services.secrets_service.
# can_access_secret "edit").
@router.get("/{secret_id}/workflow-scope",
            response_model=schemas.SecretWorkflowScope)
def get_secret_workflow_scope(
    secret_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """List the workflows a secret may be injected into.

    `workflow_ids` [] + `unrestricted` true = today's behaviour (every
    workflow of the tenant). Backed by the `secret_workflow_scopes` table
    (FX.117 p3b), one live row per allowed (secret, workflow) pair.
    """
    from services import secret_workflow_scope_service as sws
    try:
        ids = sws.get_scope_for_admin(db, secret_id, current_user)
    except sws.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    except sws.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None
    return schemas.SecretWorkflowScope(
        secret_id=secret_id,
        tenant_id=current_user.tenant_id,
        workflow_ids=ids,
        unrestricted=not ids,
    )


@router.put("/{secret_id}/workflow-scope",
            response_model=schemas.SecretWorkflowScopeResult)
def replace_secret_workflow_scope(
    secret_id: int,
    scope_update: schemas.SecretWorkflowScopeUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """REPLACE the secret's allowed-workflow list.

    Soft-deletes the rows for workflows no longer listed and inserts the new
    ones (ISO 27001: nothing is hard-deleted, the scope history stays). []
    clears the scope. Workflows must belong to this tenant. Audits one
    SECRET_SCOPE_UPDATED row — never a secret value.
    """
    from services import secret_workflow_scope_service as sws
    try:
        _secret, added, removed, new_ids = sws.replace_scope_for_admin(
            db, secret_id, list(scope_update.workflow_ids or []), current_user,
        )
    except sws.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    except sws.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None
    except sws.BadRequestError as e:
        raise HTTPException(status_code=400, detail=e.message) from None
    return schemas.SecretWorkflowScopeResult(
        secret_id=secret_id,
        workflow_ids=new_ids,
        added_workflow_ids=added,
        removed_workflow_ids=removed,
        unrestricted=not new_ids,
    )


# --- FX.117 p4a: per-secret expiry (table `secret_expiries`) ---------------
# The same per-secret gate the existing secrets routes use (services.
# secrets_service.can_access_secret) and the same tenant-from-the-token lookup,
# so another tenant's secret is a 404, never a 403. PUT null clears the expiry
# via a SOFT delete (the live row is stamped `deleted_at`, nothing is hard-
# deleted — ISO 27001), and each write audits one SECRET_EXPIRY_SET row.
@router.get("/{secret_id}/expiry",
            response_model=schemas.SecretExpiryRead)
def get_secret_expiry(
    secret_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Read a secret's expiry (`expires_at` null = no expiry).

    Backed by the `secret_expiries` table (FX.117 p4a), one live row per
    secret. `expired` is the same verdict the run-token gate applies: an
    expired secret's value is never injected (409 + audited
    SECRET_ACCESS_DENIED reason=expired while a run resolves it).
    """
    from services import secret_expiry_service as ses
    try:
        exp = ses.get_expiry_for_admin(db, secret_id, current_user)
    except ses.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    except ses.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None
    return schemas.SecretExpiryRead(
        secret_id=secret_id,
        tenant_id=current_user.tenant_id,
        expires_at=exp,
        expired=(exp is not None and exp <= ses._now()),
    )


@router.put("/{secret_id}/expiry",
            response_model=schemas.SecretExpiryResult)
def set_secret_expiry(
    secret_id: int,
    expiry_update: schemas.SecretExpiryUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """SET or CLEAR a secret's expiry.

    `expires_at` a timestamp sets it (soft-deleting the previous live row and
    inserting a fresh one, so the compliance trail keeps every expiry the
    secret had); `expires_at` null clears it. An expiry already in the past
    cuts the secret off from run-time injection immediately — the value is
    never hard-deleted (ISO 27001: expiry is a soft state). Audits one
    SECRET_EXPIRY_SET row — never a secret value.
    """
    from services import secret_expiry_service as ses
    try:
        _secret, new_exp, previous_exp, _added, _removed = ses.set_expiry_for_admin(
            db, secret_id, expiry_update.expires_at, current_user,
        )
    except ses.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    except ses.ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message) from None
    except ses.GovExpiryError as e:
        raise HTTPException(status_code=422, detail=e.message) from None
    except ses.BadRequestError as e:
        raise HTTPException(status_code=400, detail=e.message) from None
    return schemas.SecretExpiryResult(
        secret_id=secret_id,
        expires_at=new_exp,
        previous_expires_at=previous_exp,
        cleared=new_exp is None,
        expired=(new_exp is not None and new_exp <= ses._now()),
    )


class SecretValueRemoved(BaseModel):
    """FX.117 — response shape for the removed reveal route (always 410)."""
    detail: str


@router.get(
    "/{secret_id}/value",
    response_model=SecretValueRemoved,
    status_code=status.HTTP_410_GONE,
)
def get_decrypted_secret_value(
    secret_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    FX.117 (Q104=A) — REMOVED. Secret values are write-only: they can only
    be created, replaced (PATCH), or soft-deleted, never read back by a
    user. The route is kept to preserve API contract stability (v2 410
    convention) and always returns 410 Gone with guidance to use the
    Replace flow instead.

    The only remaining plaintext read path is the worker/run-token
    service endpoint below (/by-name/.../value), scoped to the acting
    run owner's tenant and the consuming node.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Secret values are write-only and can no longer be revealed. "
            "Create, replace (PATCH /secrets/{id}) or delete the secret "
            "instead. Run-time consumers receive the value through the "
            "worker run-token path, not this endpoint."
        ),
    )


@service_router.get("/by-name/{secret_name}/value", response_model=str)
def get_decrypted_secret_value_by_name(
    secret_name: str,
    workflow_run_id: Optional[int] = None,
    node_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key)
):
    """
    Retrieves the decrypted value of a single secret by name.

    This endpoint is used by worker containers during workflow execution to
    fetch user-specific credentials (API keys, passwords, tokens).

    FX.117 (Q104=A) — this is the ONLY remaining plaintext read path (the
    run-token injection point). It is scoped to the acting run owner's tenant
    by the by-name lookup (see service) and, when the worker supplies
    `workflow_run_id` / `node_id` query params, records one SECRET_INJECTED
    audit row per use (who/what run/node/secret id — NEVER the value).

    Lookup order for platform shared secrets (harbor_*):
    - If tenant's plan allows custom registry: check user's tenant first, then platform
    - If tenant's plan does NOT allow custom registry: only use platform tenant

    For all other secrets: only check user's tenant

    Authentication:
    - DAGs must first call /workflows/runs/{id}/exchange-token to get user's access_token
    - Then call this endpoint with that access_token as Bearer token
    - This ensures secrets are only accessible in the context of a running workflow
    - Full Keycloak audit trail of all secret access

    Returns:
        The decrypted secret value
    """
    try:
        _value = secrets_service.get_decrypted_secret_value_by_name(
            db, secret_name, current_user,
            workflow_run_id=workflow_run_id, node_id=node_id,
        )
    except secrets_service.ForbiddenError as e:
        # R-140 — the by-name path now enforces the same per-secret view
        # grant as the by-id path; a denial is a 403, audited in-service.
        # FX.117 PART 2 — a run-context refusal (workflow scope /
        # expiry) is the same 403, audited in-service.
        raise HTTPException(status_code=403, detail=e.message) from None
    except secrets_service.NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    # FX.117 (Q104=A) — when the caller identifies its run/node (the
    # worker run-token injection context), record one SECRET_INJECTED row
    # per use. Best-effort: a failure here must never mask the value.
    if workflow_run_id is not None:
        try:
            _secret_row = None
            try:
                import crud
                _secret_row = crud.get_secret_by_name(
                    db, secret_name, tenant_id=current_user.tenant_id
                )
            except Exception:  # noqa: BLE001
                _secret_row = None
            secrets_service.audit_secret_injection(
                db, current_user, _secret_row, secret_name,
                workflow_run_id=workflow_run_id, node_id=node_id,
            )
        except Exception:  # noqa: BLE001 — audit never masks the value
            pass
    return _value
"""Workflows-assets service layer (Gate 2b, workflows_domains/assets_cogs_files.py
migration).

Named `workflows_assets_service` (not `assets_cogs_files_service`) to avoid
colliding with the pre-existing, unrelated `services/assets_cogs_files_service.py`
(that one backs `routers/cogs/assets_cogs_files.py`).

Holds the *policy*/orchestration extracted out of
`routers/workflows_domains/assets_cogs_files.py` handlers that do more than a
single read: creating a cross-entity row + committing (`create_workflow_access_
request`), and a multi-step external-service call (`exchange_workflow_token`,
which talks to Keycloak).

Per SHARED RULES (wave 1): this is a body-only move. Each function below is a
verbatim extraction of logic that used to live inline in the router endpoint;
control flow, log messages and commit points are preserved exactly. This
module raises `HTTPException` directly where the original endpoint did (same
precedent as `services/managers_service.py` / `services/worker_management_
service.py` / `services/onboarding_service.py`) rather than routing every
branch through a new `ServiceError` taxonomy — several of these status
codes/detail shapes are load-bearing for the contract snapshot and existing
callers, so preserving them verbatim is safer than remapping them.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

import crud
import models
from crud import workflows_assets as workflows_assets_crud


class DuplicateAccessRequestError(Exception):
    """Raised when the requester already has a pending access request for this
    workflow (router maps this to the original HTTP 400)."""


# =============================================================================
# request_workflow_access
# =============================================================================

def create_workflow_access_request(
    db: Session,
    current_user: "models.User",
    db_workflow: "models.Workflow",
    justification: Optional[str] = None,
) -> Tuple["models.PermissionRequest", Optional["models.User"]]:
    """Create a pending PermissionRequest for `db_workflow`, or raise
    `DuplicateAccessRequestError` if the requester already has one pending.

    Returns `(request, owner)` — `owner` is the workflow's creator (for the
    router's best-effort notification email), or None if the workflow has no
    creator on record.
    """
    existing = workflows_assets_crud.get_pending_workflow_access_request(
        db, current_user.id, db_workflow.id
    )
    if existing:
        raise DuplicateAccessRequestError(
            "You already have a pending request for this workflow"
        )

    req = models.PermissionRequest(
        tenant_id=current_user.tenant_id,
        requester_id=current_user.id,
        cog_id=None,
        resource_type="workflow",
        resource_id=db_workflow.id,
        requested_level=models.PermissionLevel("viewer"),
        justification=justification,
        status=models.PermissionRequestStatus.PENDING,
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    owner = None
    if db_workflow.created_by_user_id is not None:
        owner = crud.get_user(db, db_workflow.created_by_user_id)

    return req, owner


# =============================================================================
# exchange_workflow_token
# =============================================================================

def _is_platform_service_principal(user) -> bool:
    """True when this request was authorised by the platform service API key
    (the credential worker/DAG containers are injected with). Mirrors
    `dependencies._resolve_platform_service_user`: the key authorising this
    request must hash-verify against PLATFORM_SERVICE_API_KEY."""
    try:
        from utils.request_principal import get_api_key
        from dependencies import verify_api_key
        import os
        svc_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")
        if not svc_key:
            return False
        row = get_api_key()
        return bool(row is not None and verify_api_key(svc_key, row.hashed_key))
    except Exception:
        return False


def exchange_workflow_token(db: Session, workflow_run_id: int,
                            current_user: "models.User") -> dict:
    """Exchange a workflow run's stored refresh token for a fresh Keycloak
    access token.

    GSEC R-001: `current_user` is the authenticated caller (route's
    `require_bearer_or_api_key` principal). The run's token belongs to the
    tenant that triggered the run, so a caller may only exchange for runs in
    their OWN tenant — unless they are a platform admin or the platform
    service principal (the credential worker/DAG containers are issued). A
    cross-tenant caller is refused with 403 and an audit log line.
    """
    import requests
    from utils.encryption import decrypt_value
    # 1788e CE carve: services.key_provider is paid/denied in CE; the CE-safe
    # provider is the shipped local-only module (same pattern as
    # crud/secret_cud.py:20), byte-compatible for CE-wrapped envelopes.
    from utils.paid_import import optional_paid
    from services import tenant_key_local
    key_provider = optional_paid("services.key_provider") or tenant_key_local

    # Fetch workflow run
    db_run = workflows_assets_crud.get_workflow_run_untenant(db, workflow_run_id)

    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found")

    # GSEC R-001 — tenant scoping: never mint a tenant's user token for a
    # caller from another tenant (the run's token is that tenant's credential).
    caller_tenant_id = getattr(current_user, "tenant_id", None)
    is_platform_admin = bool(getattr(current_user, "is_platform_admin", False))
    if not (is_platform_admin or _is_platform_service_principal(current_user)
            or caller_tenant_id == db_run.tenant_id):
        logging.warning(
            "GSEC R-001 exchange-token denied: user_id=%s user_tenant_id=%s "
            "run_id=%s run_tenant_id=%s is_platform_admin=%s",
            getattr(current_user, "id", None), caller_tenant_id, workflow_run_id,
            db_run.tenant_id, is_platform_admin,
        )
        raise HTTPException(
            status_code=403,
            detail="Cannot exchange token for a workflow run in another tenant.",
        )

    # Verify workflow is actually running (security check)
    if db_run.status not in [models.TaskStatus.QUEUED, models.TaskStatus.RUNNING]:
        raise HTTPException(
            status_code=400,
            detail=f"Workflow run is {db_run.status.value}, not active. Token exchange only allowed for running workflows."
        )

    # Check if refresh token exists
    if not db_run.encrypted_refresh_token:
        raise HTTPException(
            status_code=404,
            detail="No refresh token stored for this workflow run. Workflow may have been created before token storage was implemented."
        )

    # Decrypt refresh token
    tenant = workflows_assets_crud.get_tenant_by_id(db, db_run.tenant_id)
    if not tenant or not tenant.encryption_key:
        raise HTTPException(status_code=500, detail="Tenant encryption key not found")

    try:
        logging.info(f"🔓 Decrypting refresh token for workflow_run {workflow_run_id}")
        decrypted_tenant_key = key_provider.unwrap_tenant_key(tenant.encryption_key, tenant.id, db=db)
        # Convert bytes back to string for decryption
        encrypted_str = db_run.encrypted_refresh_token.decode('utf-8')
        logging.info(f"   Encrypted token length: {len(encrypted_str)} chars")
        refresh_token = decrypt_value(encrypted_str, decrypted_tenant_key)
        logging.info(f"   ✓ Decrypted successfully, token length: {len(refresh_token)} chars")
    except Exception as e:
        # logging audit L2: never interpolate exception text (it can carry
        # key material / upstream bodies) into a log line.
        logging.error(f"❌ Failed to decrypt refresh token for workflow_run {workflow_run_id}: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Failed to decrypt refresh token")

    # Exchange refresh token with Keycloak for fresh access token
    # Use INTERNAL URL for container-to-container communication
    keycloak_url = os.getenv("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
    keycloak_realm = os.getenv("KEYCLOAK_REALM_NAME", "modtex")
    token_url = f"{keycloak_url}/realms/{keycloak_realm}/protocol/openid-connect/token"

    # Get client secret for token exchange
    client_id = os.getenv("KEYCLOAK_DASHBOARD_CLIENT_ID", "dashboard-client")

    logging.info(f"🔄 Attempting Keycloak token exchange for workflow_run {workflow_run_id}")
    logging.info(f"   Keycloak URL: {token_url}")
    logging.info(f"   Client ID: {client_id}")
    logging.info(f"   Refresh token length: {len(refresh_token)} chars")

    try:
        response = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=10
        )

        logging.info(f"   Keycloak response status: {response.status_code}")

        if response.status_code != 200:
            error_detail = "Unknown error"
            _kc_error_code = "other"
            try:
                error_json = response.json()
                error_detail = error_json.get('error_description', error_json.get('error', 'Unknown error'))
                # logging audit L2: the upstream body may carry tokens/PII -
                # log only the status and a whitelisted Keycloak error code.
                _kc_error_code = error_json.get('error')
                if _kc_error_code not in ('invalid_grant', 'invalid_client',
                                          'invalid_request', 'unauthorized_client',
                                          'unsupported_grant_type'):
                    _kc_error_code = 'other'
                logging.error(f"Keycloak error response: status={response.status_code} error={_kc_error_code}")
            except:
                error_detail = response.text
                logging.error(f"Keycloak error response (raw): status={response.status_code}")

            logging.error(f"❌ Keycloak token exchange failed: {response.status_code} - {_kc_error_code}")
            raise HTTPException(
                status_code=502,
                detail=f"Keycloak token exchange failed: {error_detail}"
            )

        token_data = response.json()

        return {
            "access_token": token_data["access_token"],
            "expires_in": token_data.get("expires_in", 300),
            "token_type": "Bearer"
        }

    except requests.RequestException as e:
        # logging audit L3: exception text may echo URLs/params - log the type only.
        logging.error(f"Keycloak request failed for workflow_run {workflow_run_id}: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Failed to communicate with Keycloak")

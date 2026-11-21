"""Per-tenant HuggingFace token endpoints (GF.C0b.2).

A tenant admin stores/rotates/tests the tenant's own HF token. The token is a
plain tenant Secret named HF_TOKEN (GF.C0b.1) stored through the Secret/Vault
pattern and is NEVER returned by any read endpoint — only a masked preview
("hf_…abcd") plus metadata.

Auth: tenant-admin capabilities (`TENANT:view_settings` to read,
`TENANT:manage_settings` to mutate) via require_capability, mirroring
storage_connections.py. Platform admins bypass; everything is strictly
tenant-scoped (current_user.tenant_id is the only tenant ever touched).
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import models
import schemas
from crud import hf_token_cud as hf_crud
from dependencies import get_db, require_capability

logger = logging.getLogger(__name__)

# Prefix lives in the router (like the other "prefix in router" registrations
# in main.py) so the resource is namespaced at the source: /hf-tokens/status,
# /hf-tokens/test, ...
router = APIRouter(prefix="/hf-tokens", tags=["HF Tokens"])

# require_capability(...) returns a closure, so name the guards with the
# `require_` prefix: the R2 lint gate (and the auth dependency chain) keys on
# that name, and the plain `_view`/`_manage` naming used by
# storage_connections.py is only tolerated there because it is baselined.
require_view = require_capability(models.ResourceType.TENANT, "view_settings")
require_manage = require_capability(models.ResourceType.TENANT, "manage_settings")

_WHOAMI_URL = "https://huggingface.co/api/whoami-v2"
_WHOAMI_TIMEOUT = 10.0


def _audit(db: Session, current_user: models.User, event_name: str, details: dict) -> None:
    """Audit events the way the secrets router does: best-effort AuditEvent row,
    never breaks the request on audit failure."""
    try:
        db.add(
            models.AuditEvent(
                tenant_id=current_user.tenant_id,
                user_id=current_user.id,
                event_name=event_name,
                details=details,
            )
        )
        db.commit()
    except Exception:
        db.rollback()


def _mask(token: str) -> str:
    """Masked preview, e.g. hf_…abcd. Never more than prefix + last 4 chars;
    short tokens are masked entirely after the prefix."""
    value = (token or "").strip()
    if len(value) <= 4:
        return "…"
    head = value[:3] if value.startswith("hf_") else value[:2]
    return f"{head}…{value[-4:]}"


def _status_payload(db: Session, current_user: models.User) -> dict:
    secret = hf_crud.get_secret(db, tenant_id=current_user.tenant_id)
    if secret is None:
        return {"configured": False, "updated_at": None, "masked": None}
    value = hf_crud.get_decrypted_value(db, tenant_id=current_user.tenant_id)
    # The Secret row has no updated_at column (no model changes in GF.C0b.2);
    # created_at is the only timestamp available and is served as updated_at
    # in the status payload.
    return {
        "configured": True,
        "updated_at": secret.created_at,
        "masked": _mask(value) if value is not None else None,
    }


@router.get("/status", response_model=schemas.HfTokenStatus)
def get_hf_token_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_view),
):
    """Whether the tenant has an HF token configured and its masked preview.
    The value is never returned."""
    return _status_payload(db, current_user)


@router.put("/", response_model=schemas.HfTokenStatus)
def set_hf_token(
    payload: schemas.HfTokenSet,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_manage),
):
    """Create or rotate the tenant's HF token. Write-only: the value is
    encrypted (Fernet + Vault) and never echoed back."""
    hf_crud.create_or_rotate(
        db, tenant_id=current_user.tenant_id, user=current_user, value=payload.value
    )
    _audit(db, current_user, "HF_TOKEN_UPDATED", {"rotated": True})
    return _status_payload(db, current_user)


@router.delete("/", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_hf_token(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_manage),
):
    """Soft-delete the tenant's HF token (ISO: no hard delete). 404 if none."""
    secret = hf_crud.get_secret(db, tenant_id=current_user.tenant_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="No HF token is configured.")
    hf_crud.soft_delete(db, secret)
    _audit(db, current_user, "HF_TOKEN_DELETED", {"soft_deleted": True})
    return


@router.post("/test", response_model=schemas.HfTokenTestResult)
def test_hf_token(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_manage),
):
    """Validate the SAVED token against https://huggingface.co/api/whoami-v2.
    The value is read server-side from Vault and never appears in the request
    or the response."""
    # GSEC FX.19 R-042 / GY.C8 — HuggingFace egress refused (sealed mode
    # or egress.model_download_enabled False): no whoami call.
    from utils import egress_policy
    _r042 = egress_policy.refuse_reason(egress_policy.CATEGORY_MODEL_DOWNLOAD)
    if _r042:
        _audit(db, current_user, "HF_TOKEN_TESTED",
               {"ok": False, "egress_disabled": True})
        return schemas.HfTokenTestResult(
            ok=False, reason=f"HuggingFace is unreachable on this install "
                             f"({ _r042 }).")
    secret = hf_crud.get_secret(db, tenant_id=current_user.tenant_id)
    if secret is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No HF token is configured; save one first.",
        )
    token = hf_crud.get_decrypted_value(db, tenant_id=current_user.tenant_id)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="HF token is stored but could not be read; re-save it.",
        )
    try:
        resp = httpx.get(
            _WHOAMI_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_WHOAMI_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return schemas.HfTokenTestResult(ok=False, reason=f"Could not reach HuggingFace: {exc}")
    if resp.status_code == 200:
        try:
            name = (resp.json() or {}).get("name")
        except Exception:
            name = None
        reason = f"Valid — authenticated as {name}" if name else "Valid"
        _audit(db, current_user, "HF_TOKEN_TESTED", {"ok": True})
        return schemas.HfTokenTestResult(ok=True, reason=reason)
    if resp.status_code == 401:
        _audit(db, current_user, "HF_TOKEN_TESTED", {"ok": False, "http_status": 401})
        return schemas.HfTokenTestResult(ok=False, reason="HuggingFace rejected this token (401).")
    _audit(db, current_user, "HF_TOKEN_TESTED", {"ok": False, "http_status": resp.status_code})
    return schemas.HfTokenTestResult(
        ok=False, reason=f"HuggingFace returned {resp.status_code}."
    )

"""GX.S10a (launch-completion-megaplan Group X) — stack variables + secrets
routes. A thin HTTP layer over `services/stack_variables.py`.

Secret VALUES never cross this boundary: the write endpoints take a value and
hand it straight to Vault (never logged, never echoed), and every read
endpoint returns refs / metadata only. The single-segment routes
(`/compose/variables/detect`, `/secrets/catalog`) are registered on a
prefix-free sub-router that is included into the package `router` BEFORE the
`persistent_stacks` sub-router, so they are not shadowed by `/stacks/{stack_id}`
(in FastAPI, include order IS match order).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from ._shared import (
    Session,
    models,
    get_db,
    get_current_user,
    _get_owned,
    logger,
)

router = APIRouter()


# --- request/response models -------------------------------------------------

from pydantic import BaseModel  # noqa: E402


class StackComposeVariablesDetect(BaseModel):
    compose: str


class StackComposeVariablesDetectResponse(BaseModel):
    variables: List[dict]


class StackVariableItem(BaseModel):
    name: str
    kind: str
    services: List[str]
    has_value: bool
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    file: Optional[str] = None
    cog_id: Optional[int] = None


class StackVariablesListResponse(BaseModel):
    variables: List[StackVariableItem]


class StackSecretCatalogItem(BaseModel):
    path: str
    stack_name: str
    name: str
    updated_at: Optional[str] = None


class StackSecretCatalogResponse(BaseModel):
    secrets: List[StackSecretCatalogItem]


class StackVariableSet(BaseModel):
    kind: Optional[str] = None
    value: Optional[str] = None
    # GX.S10c — `b64: true` means `value` is base64 (binary secret_file uploads
    # only; the UI reads the file as bytes). Server decodes, caps and re-encodes.
    b64: Optional[bool] = None
    file: Optional[str] = None
    mode: Optional[str] = None
    services: Optional[List[str]] = None
    # storage_file / secret_file-from-cog selectors (request body only — the
    # cog id for storage_file IS stored in the spec, the from_cog snapshot is
    # not).
    cog_id: Optional[int] = None
    from_cog_id: Optional[int] = None
    pin_version: Optional[int] = None


# --- routes (declared in this order: static segments first) ------------------

@router.post("/compose/variables/detect",
             response_model=StackComposeVariablesDetectResponse)
def detect_stack_compose_variables(
        body: StackComposeVariablesDetect,
        user: models.User = Depends(get_current_user)):
    """POST /v1/stacks/compose/variables/detect — find `<NAME>` / `${NAME}` /
    `${NAME:-default}` placeholders in a compose document the UI is about to
    save. Pure parse + scan: no DB, no Vault. `suggested_kind` and `declared`
    are computed from the same document, so the dropdown pre-fills correctly."""
    from services import stack_variables as _sv
    spec = _sv.parse_compose_text(body.compose)
    return StackComposeVariablesDetectResponse(
        variables=_sv.detect_variables(spec))


@router.get("/secrets/catalog", response_model=StackSecretCatalogResponse)
def list_stack_secrets_catalog(
        db: Session = Depends(get_db),
        user: models.User = Depends(get_current_user)):
    """GET /v1/stacks/secrets/catalog — the tenant's existing Vault-backed
    secrets (from the DB index — Vault LIST is not granted to the approle) for
    the "use existing secret" dropdown. Tenant-scoped, soft-delete-aware, and
    values are never included."""
    from services import stack_variables as _sv
    return StackSecretCatalogResponse(secrets=_sv.secret_catalog(db, user.tenant_id))


@router.get("/{stack_id}/variables", response_model=StackVariablesListResponse)
def list_stack_variables_endpoint(
        stack_id: int,
        db: Session = Depends(get_db),
        user: models.User = Depends(get_current_user)):
    """GET /v1/stacks/{id}/variables — the declared variables with kind,
    services, has_value and audit metadata. NO values: plain values are the
    exception to "never echo" only because they are not secret — but they are
    omitted here too, and the UI re-reads them from the compose."""
    from services import stack_variables as _sv
    stack = _get_owned(db, stack_id, user)
    return StackVariablesListResponse(variables=_sv.list_stack_variables(db, stack))


@router.put("/{stack_id}/variables/{name}", response_model=StackVariablesListResponse)
def set_stack_variable_endpoint(
        stack_id: int,
        name: str,
        body: StackVariableSet,
        db: Session = Depends(get_db),
        user: models.User = Depends(get_current_user)):
    """PUT /v1/stacks/{id}/variables/{name} — set or rotate one variable's
    value. Writes Vault, sets `needs_redeploy` on the stack (a value change is
    a pending change until the operator redeploys) and audits
    STACK_VARIABLE_SET (the NAME only — never the value). Returns the
    refreshed variables list, still value-free."""
    from services import stack_variables as _sv
    stack = _get_owned(db, stack_id, user)
    declared = _sv.declared_variables(stack.compose_spec or {})
    if name not in declared:
        raise HTTPException(status_code=404,
                            detail=f"variable {name!r} is not declared on this stack")
    out = _sv.set_stack_variable(db, stack, name, body.model_dump(exclude_none=True), user)
    logger.info("stack %s variable %s set by user %s", stack.id, name, user.id)
    return StackVariablesListResponse(variables=out)


# Keep the star-re-export (`from .stack_variables import *` in the package
# `__init__`) from pulling this module's own `router` / re-exported names into
# the package namespace and shadowing the aggregator `router`.
__all__ = [
    'detect_stack_compose_variables',
    'list_stack_secrets_catalog',
    'list_stack_variables_endpoint',
    'set_stack_variable_endpoint',
]

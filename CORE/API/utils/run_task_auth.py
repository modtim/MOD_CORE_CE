"""Acceptance helpers for node-callback endpoints (worker-mediated-node-credentials, Group 4).

A running workflow node no longer holds a standing credential. The worker vends it
a short-lived, tenant+run-scoped ``run_task`` token on demand, and the node sends it
back to the API as the ``X-API-Key`` header (unchanged header name — existing node
code already uses ``X-API-Key``).

These dependencies let the node-callback endpoints accept that ``run_task`` token IN
ADDITION to their existing Bearer / API-key auth, WITHOUT weakening the existing path:

  * If ``X-API-Key`` is a valid ``run_task`` token, the caller is an authenticated node
    and the dependency returns the token's claims (a ``dict``). The handler MUST then
    pin the tenant with :func:`pin_run_task_tenant` — the addressed resource's
    ``tenant_id`` (and ``run_id`` where the endpoint has one) MUST match the token, else
    403. Tenant is taken from the verified token, NEVER trusted from the request body.
  * Otherwise the dependency falls through to the endpoint's existing auth
    (``require_bearer_or_api_key``, plus the onboarding gate for the workflows router)
    and 401s exactly as before if that also fails.

Mirrors the ``verify_vector_run_token`` acceptance shape in ``routers/vector_store.py``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Union

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_bearer_or_api_key
from utils.build_task_token import verify_run_task_token


def _run_task_claims(x_api_key: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the run_task claims if ``x_api_key`` is a valid run_task token, else None.
    Never raises (verify_run_task_token swallows all token errors)."""
    return verify_run_task_token(x_api_key) if x_api_key else None


def node_or_service_principal(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Union[Dict[str, Any], models.User]:
    """Accept a run_task node token OR the existing Bearer/API-key service auth.

    Returns the run_task claims ``dict`` for a node caller, or the authenticated
    ``models.User`` from ``require_bearer_or_api_key``. Raises 401 (via
    ``require_bearer_or_api_key``) if neither credential is valid.

    Used by endpoints whose existing gate is ``Depends(require_bearer_or_api_key)``.
    """
    claims = _run_task_claims(x_api_key)
    if claims is not None:
        return claims
    return require_bearer_or_api_key(authorization=authorization, x_api_key=x_api_key, db=db)


def node_or_onboarded_user_principal(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Union[Dict[str, Any], models.User]:
    """Accept a run_task node token OR the existing active+onboarded user auth.

    Returns the run_task claims ``dict`` for a node caller, or the authenticated
    active user (``require_bearer_or_api_key`` only returns ACTIVE users) after also
    enforcing the onboarding gate — preserving the workflows router's original
    ``require_active_and_onboarded_user`` semantics for the non-node path. Raises
    401/403 exactly as before if neither credential is valid.
    """
    claims = _run_task_claims(x_api_key)
    if claims is not None:
        return claims
    user = require_bearer_or_api_key(authorization=authorization, x_api_key=x_api_key, db=db)
    if not getattr(user, "onboarding_complete", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Onboarding must be completed to access this resource.",
        )
    return user


def pin_run_task_tenant(
    claims: Dict[str, Any],
    *,
    tenant_id: int,
    run_id: Optional[int] = None,
) -> None:
    """Enforce that a run_task token is scoped to the addressed resource.

    The token's ``tenant_id`` MUST equal ``tenant_id`` (the resolved resource's tenant),
    and — when the endpoint has a run and the token carries one — the token's ``run_id``
    MUST equal ``run_id``. Any mismatch raises 403. This is the cross-tenant guard: the
    tenant is taken from the verified token, never trusted from the request body.
    """
    if tenant_id is None or int(claims.get("tenant_id")) != int(tenant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="run_task token is not scoped to this resource's tenant.",
        )
    token_run = claims.get("run_id")
    if run_id is not None and token_run is not None and int(token_run) != int(run_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="run_task token is not scoped to this workflow run.",
        )

# API/routers/tenants_ce.py
"""
Tenants — the CE (open-core) read-only single-tenant `GET /v1/tenants`.

MT-CE-4 (11-05 owner decision Q-MT1 = B): CE is single-tenant, but the
Dashboard tenant picker stays — as a READ-ONLY single-tenant picker. The paid
`routers/tenants.py` CRUD (GET/POST/DELETE, multi-tenant) is manifest-DENIED in
the CE cut (open-core/manifest.yaml), so CE would serve 404 for `/v1/tenants`
and the ~9 Dashboard call sites (`await fetchApi('/tenants')`) would lose the
picker. This file ships ONLY the read half, scoped to the CALLER'S OWN tenant:

    GET /v1/tenants   ->  [ <the caller's own tenant row> ]   (1 element)

POST / PUT / PATCH / DELETE are NOT defined here: FastAPI answers 405 for a
known path with another method, so CE has no tenant write surface at all.

SHAPE: `response_model=List[schemas.Tenant]` — byte-for-byte the paid GET's
model, so every Dashboard caller (which reads only `tenant.id` / `tenant.name`
off a JSON array) keeps working with NO Dashboard edit. The other fields are
the caller's own tenant's values.

AUTH: the SAME dependency pair as the paid GET (`get_db`,
`require_active_and_onboarded_user`) plus the SAME platform-admin gate — the
paid GET refuses a non-platform-admin with 403 ("Only platform admins can list
all tenants", via `services/users_router_service.py` ForbiddenError ->
`_raise_http`). Behaviour parity is kept exactly: this route never widens
access to ordinary users, and `routers.tenants` is NOT imported (it is absent
in the CE cut), so the mapping is local.

LAYERING (router -> crud, no new crud, no new query site): the single-row
fetch re-homes `crud/node_definitions_workflows.get_tenant_by_id` verbatim —
the one allow-listed CE crud function whose predicate matches
`crud/users_router.list_tenants` (`Tenant.id == <id>` AND
`Tenant.deleted_at.is_(None)`). The id is ALWAYS `current_user.tenant_id`
(never a caller-supplied id/param), so the query is tenant-scoped by
construction and no `tenant_id`/`all`/`limit` query parameter is honoured.

FAIL CLOSED: a caller with no `tenant_id`, or whose tenant row is missing /
soft-deleted, gets 404 (never an empty list, which would silently hide a
broken tenant binding).
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import models
import schemas
from dependencies import get_db, require_active_and_onboarded_user
from crud.node_definitions_workflows import get_tenant_by_id

router = APIRouter(tags=["Tenants"], prefix="/tenants")


@router.get("", response_model=List[schemas.Tenant])
def list_own_tenants(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """The caller's own tenant, as a one-element list (Platform Admin only).

    CE single-tenant posture (MT-CE-4): the picker shows exactly one entry.
    Regular users get the same 403 any non-admin list call gets; an unknown tenant
    binding is a 404, never an empty list.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform admins can list all tenants",
        )

    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )

    tenant: "models.Tenant" = get_tenant_by_id(db, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    return [tenant]

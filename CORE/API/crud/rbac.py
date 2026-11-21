"""RBAC domain — query layer (Gate 2b: stack-training-and-prewarm).

The ONLY place `db.query(models.X)` lives for the two data-access needs that
were inline in `routers/rbac.py` before this migration:

  * a tenant row, by id.
  * a user row by id, tenant-UNSCOPED — role assignment must be able to
    resolve a target user in ANY tenant (a platform admin may assign a role
    cross-tenant); `services/rbac_service.py` applies tenant policy (and the
    404-vs-403 trap) AFTER this fetch, never before it and never here.

Role CRUD itself (create/get/list/update/delete `CustomRole`) and the user
mutation (`update_user`) already went through the existing generic `crud`
package (`crud/role_cud.py`, `crud/user_cud.py`, aggregated in
`crud/__init__.py`) even before this migration — that was already a proper
query layer, just organized under the older per-file-not-per-domain
convention. It is left as-is (and still called via `crud.xxx(...)` from
`services/rbac_service.py`) rather than duplicated here; this module only
adds what was genuinely inline in the router.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_tenant(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    """Fetch a tenant by id, for entitlement evaluation. A tenant IS the
    scope, so there is nothing further to scope this by."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


def get_user_by_id(db: Session, user_id: int) -> Optional[models.User]:
    """Fetch a user by id, tenant-UNSCOPED.

    Deliberately not tenant-filtered: role assignment must resolve a target
    user in ANY tenant before tenant policy is applied (a platform admin may
    assign cross-tenant; a non-admin may not). Scoping this query would
    collapse "wrong tenant" and "no such user" into the same outcome at the
    wrong layer. The 404-vs-403 trap that keeps them looking identical to
    the CALLER (without actually merging them internally) is enforced in
    `services/rbac_service.assign_role`, not here.
    """
    return db.query(models.User).filter(models.User.id == user_id).first()

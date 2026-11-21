"""Resource permission management — unified permission CRUD + role presets.

Split into per-domain sub-modules (iam_permissions, identity_tenancy, sso_scim).
The shared Pydantic models are DEFINED here so their ``__module__`` stays
``routers.resource_perms`` (identical to the pre-split single file); several of
these names (``GrantRequest``, ``UserPermissionSummary``) collide with models in
other routers, so keeping the module path stable keeps the OpenAPI ``$ref`` names
unchanged. The sub-module routers are re-assembled into the same aggregate
``router`` (same prefix/tags), so ``main.py`` is untouched.
"""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel
from fastapi import APIRouter


# =========================================================================
# Schemas — kept here (module = routers.resource_perms) so $ref names are stable
# =========================================================================

class GrantRequest(BaseModel):
    user_id: int
    resource_type: str
    capability_name: str
    resource_id: Optional[int] = None


class RolePresetRequest(BaseModel):
    user_id: int
    preset: str  # viewer, editor, admin


class ResourcePermissionResponse(BaseModel):
    id: int
    user_id: Optional[int]
    user_name: Optional[str]
    user_email: Optional[str]
    tenant_id: Optional[int] = None
    tenant_name: Optional[str] = None
    capability_name: str
    capability_display: str
    resource_type: str
    resource_id: Optional[int]
    is_active: bool
    granted_at: datetime


class RolePresetInfo(BaseModel):
    name: str
    display_name: str
    description: str
    resource_types: List[str]


class UserPermissionSummary(BaseModel):
    user_id: int
    user_name: str
    user_email: str
    preset: Optional[str]
    permission_count: int


class ShareableUser(BaseModel):
    id: int
    email: str
    name: str
    tenant_id: int
    tenant_name: Optional[str]


# =========================================================================
# Aggregate router — same prefix/tags as the pre-split file
# =========================================================================

router = APIRouter(prefix="/resource-permissions", tags=["resource-permissions"])


# DELETE /resource-permissions/{permission_id} lives in routers.capabilities
# (``delete_resource_permission``) — it was duplicated here and shadowed
# (dead code), so it was removed (WS1.40d).

from . import iam_permissions  # noqa: E402

router.include_router(iam_permissions.router)

# identity_tenancy (per-tenant user/role summary) and sso_scim (SSO/SCIM
# provisioning) are Paid multi-tenancy-management sub-modules: present in the
# full build, absent in CE. Import + mount each only if present so this package
# boots in CE. These are no-ops when the modules ARE present.
try:
    from . import identity_tenancy  # noqa: E402
except ImportError:  # CE build: Paid sub-module absent
    identity_tenancy = None

try:
    from . import sso_scim  # noqa: E402
except ImportError:  # CE build: Paid sub-module absent
    sso_scim = None

if identity_tenancy is not None:
    router.include_router(identity_tenancy.router)
if sso_scim is not None:
    router.include_router(sso_scim.router)

# Backward-compat re-exports (callers/tests import these off routers.resource_perms)
from .iam_permissions import (  # noqa: E402,F401
    list_role_presets,
    apply_preset,
    list_resource_permissions,
    grant_permission,
)
if identity_tenancy is not None:
    from .identity_tenancy import (  # noqa: E402,F401
        list_users_with_roles,
        _detect_preset,
    )
if sso_scim is not None:
    from .sso_scim import list_sharable_users  # noqa: E402,F401

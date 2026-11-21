"""CRUD (query layer) for `routers/auth.py`.

AUTH IS SECURITY-SENSITIVE (Gate 2b migration note): this router's token
exchange / refresh / JIT-provisioning / password-bypass-audit logic is
deliberately left IN PLACE in the router — see
`services/auth_service.py`'s module docstring for why. This module carries
ONLY the single inline `db.query` read the router had (a plain
`UserSession` lookup by Keycloak session id, used for multi-session
bookkeeping — it is not part of any auth/permission decision). `UserSession`
has no `tenant_id` and no `deleted_at` column (not in `database.py`'s
soft-delete listener), so this is a straight 1:1 port of the original
filter — migrate_rules #10.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_user_session_by_keycloak_session_id(
    db: Session, keycloak_session_id: str
) -> Optional[models.UserSession]:
    """Fetch a `UserSession` row by its Keycloak `session_state`, or None.
    Not tenant-scoped: `UserSession` carries no `tenant_id` column."""
    return (
        db.query(models.UserSession)
        .filter(models.UserSession.keycloak_session_id == keycloak_session_id)
        .first()
    )

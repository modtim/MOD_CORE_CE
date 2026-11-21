"""node_code_edit CRUD (Gate 2b migration of routers/node_code_edit.py).

Query layer only: the single copy-name uniqueness check used by
``/copy``'s auto-naming. `NodeDefinition` rows here are always tenant+user
scoped 1:1 with the original inline query (a private-draft namespace), so no
extra tenant policy is layered on top in the service.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

import models


def node_name_taken(db: Session, *, tenant_id: int, user_id: int, name: str) -> bool:
    """True if a NodeDefinition with this exact name already exists in the
    caller's own private-draft namespace (tenant_id + user_id)."""
    return db.query(models.NodeDefinition.id).filter(
        models.NodeDefinition.tenant_id == tenant_id,
        models.NodeDefinition.user_id == user_id,
        models.NodeDefinition.name == name,
    ).first() is not None

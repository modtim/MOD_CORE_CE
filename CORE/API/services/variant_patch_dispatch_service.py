"""Service layer for the variant patch dispatch router.

Data access for NodeDefinition and ApiTaskQueue rows. The router applies
the patch-loop logic to the returned ORM rows.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_node_definition(
    db: Session, *, node_id: int
) -> Optional[models.NodeDefinition]:
    """The node definition *node_id*, or None."""
    return db.query(models.NodeDefinition).filter(
        models.NodeDefinition.id == node_id
    ).first()


def list_recent_tenant_tasks(
    db: Session, *, tenant_id: int
) -> List[models.ApiTaskQueue]:
    """The 50 most recent ApiTaskQueue rows for *tenant_id*, newest first."""
    q = db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.tenant_id == tenant_id,
    )
    return (
        q.order_by(models.ApiTaskQueue.created_at.desc())
         .limit(50)
         .all()
    )

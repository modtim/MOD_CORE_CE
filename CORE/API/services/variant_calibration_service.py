"""Service layer for the variant_calibration router.

Data access for NodeDefinition rows carrying INT8 calibration config. The
router keeps the ownership check and custom_metadata shaping.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_node_definition(db: Session, node_id: int) -> Optional[models.NodeDefinition]:
    """The NodeDefinition with id *node_id*, or None."""
    return db.query(models.NodeDefinition).filter(
        models.NodeDefinition.id == node_id
    ).first()

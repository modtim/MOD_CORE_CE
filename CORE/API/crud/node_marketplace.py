"""CRUD for the `node_marketplace` domain (openspec: stack-training-and-prewarm, D9).

Gate 2b data-access layer for the external node-authoring marketplace surface
(routers/node_definitions_domains/node_marketplace.py). This is the ONLY place
`db.query(...)` runs for this domain — the router no longer queries inline.

Scope note: the lookup here is deliberately NOT tenant-scoped. The endpoint's
contract distinguishes "task does not exist" (404) from "task exists but belongs
to another tenant" (403); tenant-scoping the query would collapse the 403 into a
404 and change behaviour. Tenant policy therefore lives in the service layer,
which inspects `tenant_id` on the returned row. See services/node_marketplace_service.py.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from models import ApiTaskQueue


def get_build_task(db: Session, task_id: int) -> Optional[ApiTaskQueue]:
    """Fetch one build/task-queue row by primary key (un-scoped).

    Returns None when no such task exists. Tenant authorization is applied by
    the caller (the service), NOT here, so the 404-vs-403 distinction the
    external authoring contract requires is preserved.
    """
    return db.query(ApiTaskQueue).filter(ApiTaskQueue.id == task_id).first()

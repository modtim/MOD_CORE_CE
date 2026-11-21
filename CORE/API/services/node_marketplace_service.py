"""Service layer for the `node_marketplace` domain (openspec: stack-training-and-prewarm, D9).

Owns the policy for the external node-authoring marketplace surface backed by
routers/node_definitions_domains/node_marketplace.py:

    GET /v1/node-authoring/builds/{task_id}   tenant-scoped build-status poll
    GET /v1/node-authoring/contract           versioned node-contract reference

The router handlers are thin: they authenticate and translate this layer's
domain errors into `HTTPException`. All data access goes through
`crud/node_marketplace.py`; all tenancy / shaping policy lives here.

Behaviour-preserving MIGRATION only — the response dicts, status codes and
tenant rules are identical to the pre-migration inline router handlers.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict

from sqlalchemy.orm import Session

from crud import node_marketplace as marketplace_crud
from services._base import NotFoundError, ServiceError

#: The shipped node-contract reference doc. Anchored to the API app root
#: (`.../CORE/API/docs/node_contract.md`) so it resolves regardless of which
#: module loads it. NOTE: the shared `_shared._load_node_contract` helper this
#: replaces computes the path from its OWN `__file__`, which the Gate-2a split
#: moved one directory deeper (routers/node_definitions_domains/), breaking the
#: lookup to a non-existent `routers/docs/`. Owning the load here fixes that
#: regression for this domain without editing the shared module.
_NODE_CONTRACT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "docs",
    "node_contract.md",
)


class ForbiddenError(ServiceError):
    """Caller is authenticated but not permitted to see this resource
    (router maps -> 403). services/_base.py ships not_found/conflict but not a
    forbidden variant, so the domain declares its own here."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class NodeMarketplaceService:
    """Business logic for the external node-authoring marketplace endpoints.

    No HTTP concerns: raises `ServiceError` subclasses the router maps to status
    codes, so the same policy is reusable outside a request (e.g. the MCP tools
    these endpoints back)."""

    def __init__(self, crud=marketplace_crud):
        self._crud = crud

    def get_node_contract(self) -> Dict[str, Any]:
        """Load the shipped node-contract reference doc (version + markdown).

        Pure I/O of a bundled static doc — no DB, no tenant scope. Returns
        ``{"version": int|None, "content": str}`` exactly as the pre-split
        handler did; an unreadable doc yields ``{"version": None, "content": ""}``.
        """
        try:
            with open(_NODE_CONTRACT_PATH, "r") as f:
                content = f.read()
        except Exception:
            return {"version": None, "content": ""}
        m = re.search(r"node-contract-version:\s*(\d+)", content)
        return {"version": int(m.group(1)) if m else None, "content": content}

    def get_build_status(self, db: Session, *, task_id: int, tenant_id: int) -> Dict[str, Any]:
        """Return the build/task status view for a task the caller's tenant owns.

        Raises `NotFoundError` (-> 404) when the task does not exist, and
        `ForbiddenError` (-> 403) when it exists under a different tenant. The
        403's message intentionally matches the 404's ("Build task not found")
        so the endpoint does not confirm the existence of another tenant's task.
        """
        task = self._crud.get_build_task(db, task_id)
        if not task:
            raise NotFoundError("Build task not found")
        if task.tenant_id != tenant_id:
            raise ForbiddenError("Build task not found")
        return {
            "task_id": task.id,
            "status": task.status.value if hasattr(task.status, "value") else task.status,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "error_message": task.error_message,
            "result_payload": task.result_payload,
        }


node_marketplace_service = NodeMarketplaceService()

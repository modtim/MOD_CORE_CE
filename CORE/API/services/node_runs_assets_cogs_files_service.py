"""Business logic for the `assets_cogs_files` node_runs sub-module
(Gate 2b migration of `routers/node_runs_domains/assets_cogs_files.py`
— openspec: stack-training-and-prewarm, D9).

Companion to `crud/node_runs_assets_cogs_files.py` (the ONLY place this
slice's `db.query` lives). The router stays HTTP-only: parse input, call
a function here, map a `ServiceError` to `HTTPException`, shape the
response.

Extracted verbatim from the router during the Gate-2b migration —
behaviour-preserving MOVE. The logic below is the same logic that used
to live inline in `get_node_runs_for_workflow_run_service`; only the
data access now goes through `crud.node_runs_assets_cogs_files` and the
"not found" branch raises `NotFoundError` instead of a bare
`HTTPException`.

404-vs-403 (SHARED RULES #5): CRUD fetches stay tenant-UNSCOPED;
`_enforce_user_run_view` (from `routers.node_runs_domains._shared`,
pre-existing and NOT owned by this migration) applies the tenant/view
gate here, before any data is returned, so a legitimate 403 doesn't
collapse into a 404. That helper raises `HTTPException(404, ...)`
directly (it predates this migration and is shared with sibling
sub-modules) — it is allowed to propagate through this service
unchanged; FastAPI handles an `HTTPException` raised anywhere in the
call stack the same way.
"""
from __future__ import annotations

from typing import Any, List

from sqlalchemy.orm import Session

import models
from crud import node_runs_assets_cogs_files as crud
from services._base import NotFoundError
from utils.pii_redaction_surfaces import redact_node_run_view as _v4b_redact_node_run


# =============================================================================
# get_node_runs_for_workflow_run_service
# =============================================================================


def get_node_runs_for_workflow_run(
    db: Session, run_id: int, principal: Any
) -> List[models.NodeRun]:
    """All node runs for a workflow run, enriched with resolved
    `file_path` on each output (the worker only reports `content_hash` —
    the API resolves the full storage path so consumers such as the
    export container don't have to guess)."""
    from routers.node_runs_domains._shared import _enforce_user_run_view
    from utils.run_task_auth import pin_run_task_tenant

    db_workflow_run = crud.get_workflow_run(db, run_id)
    if not db_workflow_run:
        raise NotFoundError("Workflow run not found")

    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=db_workflow_run.tenant_id, run_id=run_id)
    _enforce_user_run_view(db, principal, db_workflow_run)

    node_runs = crud.get_node_runs_with_node_definition(db, run_id)

    # Enrich outputs with resolved storage_path from ContentBlob.
    # The worker only reports content_hash — the API resolves the full
    # MinIO path so consumers (export container, etc.) don't have to guess.
    all_hashes = []
    for nr in node_runs:
        if nr.outputs:
            all_hashes.extend(o.get("content_hash") for o in nr.outputs if o.get("content_hash"))

    if all_hashes:
        blobs = crud.get_content_blobs_by_hashes(db, all_hashes)
        blob_map = {b.id: b.storage_path for b in blobs}

        for nr in node_runs:
            if not nr.outputs:
                continue
            for output in nr.outputs:
                h = output.get("content_hash")
                if h and h in blob_map:
                    output["file_path"] = blob_map[h]

    # GY.V4b — PII/PHI redaction on READ only, for the node-run view this
    # service hands back (inputs values, outputs entries, error_message). The
    # stored NodeRun rows and their JSONB are never rewritten, keys are never
    # rewritten, and with the profile off each row is returned as the very ORM
    # row queried, so the response is byte-identical to today.
    return [_v4b_redact_node_run(nr) for nr in node_runs]

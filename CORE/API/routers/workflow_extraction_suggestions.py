"""expr-3.6 (workflow-expressiveness-primitives) — extraction-suggestions endpoint.

Route (mounted under /v1 in main.py):
    GET /v1/road/{workflow_id}/extraction-suggestions

Returns equivalence-class members for the caller's tenant so the
WorkflowBuilder banner can surface when the same-tenant equivalence
class size is >= 2.

Tenant isolation: the caller's tenant_id comes from the auth token;
cross-tenant and soft-deleted workflows always 404.

No per-tenant feature gate for this endpoint — the `control_flow_primitives_enabled`
flag gates control-flow nodes, not extraction banners (grep of services/
confirmed no expr-specific flag for 3.6).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as satext
from sqlalchemy.orm import Session

import models
from crud import workflow_cud as wf_crud
from dependencies import get_db, require_bearer_or_api_key
from utils.tenant_lifecycle import require_tenant_writable

logger = logging.getLogger(__name__)

TABLE = "subgraph_equivalence_index"

router = APIRouter(prefix="/road", tags=["The Road", "Workflow Extraction"])


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------

class _OtherWorkflowOut(BaseModel):
    id: int
    name: str


class _ExtractionSuggestionItem(BaseModel):
    canonical_hash: str
    hash_kind: str
    node_ids: List[str]
    node_count: int
    other_workflow_count: int
    other_workflows: List[_OtherWorkflowOut]


class _ExtractionSuggestionsOut(BaseModel):
    items: List[_ExtractionSuggestionItem]


class _CrossTenantMatchItem(BaseModel):
    canonical_hash: str
    hash_kind: str
    node_count: int
    other_tenant_count: int


class _CrossTenantMatchesOut(BaseModel):
    workflow_id: int
    matches: List[_CrossTenantMatchItem]


class _ExtractOut(BaseModel):
    workflow_id: int
    name: str
    node_count: int


# ---------------------------------------------------------------------------
# Helper: find this workflow's rows in the caller's equivalence classes
# ---------------------------------------------------------------------------

def _fetch_suggestions(
    db: Session,
    tenant_id: int,
    workflow_id: int,
) -> List[Dict[str, Any]]:
    """Return extraction-suggestion items for *this* workflow's equivalence
    classes.

    Strategy (two queries, all tenant-scoped):

    1. Fetch all LIVE canonical-hash groups for the tenant (equivalence
       classes of size >= 2) that INCLUDE the target workflow.

    2. For each such canonical_hash, fetch:
       - THIS workflow's node_ids from the index.
       - The other workflows' IDs + names (max 10) from the index,
         joined to workflows for the name, filtered to the same tenant.

    Returns list of dicts with keys:
        canonical_hash, hash_kind, node_ids, node_count,
        other_workflow_count, other_workflows.
    """
    # Step 1: find which canonical_hashes in this tenant have >= 2 distinct
    #         workflows, and whether our workflow is among them.
    class_hashes_rows = db.execute(satext(f"""
        SELECT DISTINCT ON (s1.canonical_hash) s1.canonical_hash
        FROM {TABLE} s1
        JOIN (
            SELECT canonical_hash
            FROM {TABLE}
            WHERE tenant_id = :t
              AND deleted_at IS NULL
            GROUP BY canonical_hash
            HAVING COUNT(DISTINCT workflow_id) >= 2
        ) s2 ON s1.canonical_hash = s2.canonical_hash
        WHERE s1.tenant_id = :t
          AND s1.deleted_at IS NULL
          AND s1.workflow_id = :wid
    """), {"t": tenant_id, "wid": workflow_id}).fetchall()

    if not class_hashes_rows:
        return []

    # Collect canonical_hashes that qualify
    hashes = [r[0] for r in class_hashes_rows]

    # Step 2a: this workflow's node_ids for each qualifying hash
    this_rows_rows = db.execute(satext(f"""
        SELECT canonical_hash, hash_kind, node_ids, node_count
        FROM {TABLE}
        WHERE tenant_id = :t
          AND deleted_at IS NULL
          AND workflow_id = :wid
          AND canonical_hash = ANY(:hashes)
    """), {"t": tenant_id, "wid": workflow_id, "hashes": hashes}).fetchall()

    this_rows = {r[0]: {"hash_kind": r[1], "node_ids": r[2] if r[2] else [],
                         "node_count": r[3]} for r in this_rows_rows}

    # Step 2b: other workflows in those classes (same tenant, live).
    # We need their id + name from the workflows table.
    # Per-hash window: rn <= 10 within each canonical_hash, plus the TRUE
    # per-hash count of other workflows (hash_total), so a class with more
    # than 10 members still reports its real count.
    other_rows = db.execute(satext(f"""
        SELECT canonical_hash, workflow_id, workflow_name, node_count, rn, hash_total
        FROM (
            SELECT canonical_hash, workflow_id, workflow_name, node_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY canonical_hash ORDER BY workflow_id
                   ) AS rn,
                   COUNT(*) OVER (PARTITION BY canonical_hash) AS hash_total
            FROM (
                SELECT DISTINCT ON (s.canonical_hash, w.id)
                    s.canonical_hash,
                    w.id AS workflow_id,
                    w.name AS workflow_name,
                    s.node_count
                FROM {TABLE} s
                JOIN workflows w ON w.id = s.workflow_id
                   AND w.tenant_id = s.tenant_id
                   AND w.deleted_at IS NULL
                WHERE s.tenant_id = :t
                  AND s.deleted_at IS NULL
                  AND s.workflow_id != :wid
                  AND s.canonical_hash = ANY(:hashes)
                ORDER BY s.canonical_hash, w.id
            ) dedup
        ) windowed
        WHERE rn <= 10
    """), {"t": tenant_id, "wid": workflow_id, "hashes": hashes}).fetchall()

    # Group other_workflows by canonical_hash (rn already caps each hash at 10)
    ow_by_hash: Dict[str, List[Dict[str, Any]]] = {}
    hash_total: Dict[str, int] = {}
    for c_hash, wid, wname, _nc, _rn, total in other_rows:
        ow_by_hash.setdefault(c_hash, []).append({"id": wid, "name": wname})
        hash_total[c_hash] = total

    # Build items
    items = []
    for c_hash, row_data in this_rows.items():
        ows = ow_by_hash.get(c_hash, [])
        items.append({
            "canonical_hash": c_hash,
            "hash_kind": row_data["hash_kind"],
            "node_ids": row_data["node_ids"],
            "node_count": row_data["node_count"],
            "other_workflow_count": hash_total.get(c_hash, len(ows)),
            "other_workflows": [_OtherWorkflowOut(**w).model_dump() for w in ows],
        })

    # Sort by node_count desc, then other_workflow_count desc
    items.sort(key=lambda x: (-x["node_count"], -x["other_workflow_count"]))

    # Max 20 items
    return items[:20]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/{workflow_id}/extraction-suggestions",
    response_model=_ExtractionSuggestionsOut,
)
def get_extraction_suggestions(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
) -> _ExtractionSuggestionsOut:
    """Return extraction suggestions for the given workflow.

    Returns equivalence-class members for the caller's tenant so the
    WorkflowBuilder can surface a banner when class size >= 2.
    """
    tenant_id = current_user.tenant_id

    # Validate the workflow exists, belongs to this tenant, and is not soft-deleted
    db_workflow = wf_crud.get_workflow_by_id(db, workflow_id=workflow_id,
                                              tenant_id=tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="not_found")

    items = _fetch_suggestions(db, tenant_id, workflow_id)
    return {"items": items}


# ---------------------------------------------------------------------------
# expr-3.7: cross-tenant equivalence matches for one workflow
# ---------------------------------------------------------------------------

@router.get(
    "/{workflow_id}/extraction-suggestions/cross-tenant",
    response_model=_CrossTenantMatchesOut,
)
def get_cross_tenant_matches(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
) -> _CrossTenantMatchesOut:
    """Cross-tenant equivalence matches that include *this* workflow.

    Same tenant-ownership check as the same-tenant route: a missing,
    soft-deleted, or other-tenant workflow is a 404.  The caller's tenant_id
    is the one handed to the service, so the service's both-sides opt-in and
    k-anonymity floor are evaluated for the caller.  A tenant that has not
    opted in simply gets an empty match list.

    Only the caller's own workflow is referenced: the service's
    ``own_workflow_ids`` (which can name this tenant's OTHER workflows) is
    used to filter and is never echoed back.
    """
    tenant_id = current_user.tenant_id

    db_workflow = wf_crud.get_workflow_by_id(db, workflow_id=workflow_id,
                                             tenant_id=tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="not_found")

    from services.cross_tenant_equivalence import cross_tenant_matches

    raw = cross_tenant_matches(db, tenant_id, limit=50)

    matches = [
        {"canonical_hash": item["canonical_hash"],
         "hash_kind": item["hash_kind"],
         "node_count": item["node_count"],
         "other_tenant_count": item["other_tenant_count"]}
        for item in raw
        if workflow_id in (item.get("own_workflow_ids") or [])
    ]
    return {"workflow_id": workflow_id, "matches": matches}


# ---------------------------------------------------------------------------
# expr-3.6b: one-click extract — new draft workflow for one equivalence class
# ---------------------------------------------------------------------------

@router.post(
    "/{workflow_id}/extraction-suggestions/{canonical_hash}/extract",
    response_model=_ExtractOut,
    status_code=201,
    # This writes a new workflow, unlike the GET routes above — gate it the
    # same way create_workflow does (423 while the caller's tenant is
    # read_only/suspended; platform admins bypass).
    dependencies=[Depends(require_tenant_writable)],
)
def extract_subgraph(
    workflow_id: int,
    canonical_hash: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
) -> _ExtractOut:
    """Create a NEW draft workflow holding only this workflow's nodes named by
    `canonical_hash` (plus the edges between them).

    Same ownership gate as the GET routes: a missing, soft-deleted, or other-
    tenant workflow is a 404. An unknown hash (no live index row for this
    tenant + workflow) is also a 404 — nothing to extract. The source workflow
    is never mutated.
    """
    tenant_id = current_user.tenant_id

    if wf_crud.get_workflow_by_id(db, workflow_id=workflow_id,
                                  tenant_id=tenant_id) is None:
        raise HTTPException(status_code=404, detail="not_found")

    from services.subgraph_extract import extract_subgraph_workflow

    try:
        return extract_subgraph_workflow(db, tenant_id=tenant_id,
                                         user=current_user,
                                         workflow_id=workflow_id,
                                         canonical_hash=canonical_hash)
    except LookupError:
        raise HTTPException(status_code=404, detail="not_found")

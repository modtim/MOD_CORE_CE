"""462 / expr-3.5 — subgraph equivalence analyzer (the daily tenant pass).

Consumer 3.5 of workflow-expressiveness-primitives: for ONE tenant, walk the
eligible workflows, enumerate their connected subgraphs
(services/subgraph_canonical.enumerate_subgraphs), canonicalise + hash each
one (byte kind) and REPLACE that workflow's live rows in
`subgraph_equivalence_index` atomically inside the CALLER's transaction:

  * live rows whose (hash_kind, canonical_hash, node_ids) key is no longer
    produced get soft-deleted (`deleted_at = NOW()`) -- ISO 27001, NEVER a
    hard delete (no DELETE statement anywhere in this module);
  * the new keys are inserted with `ON CONFLICT DO NOTHING` against the
    partial unique index uq_subgraph_equivalence_index_live
    (workflow_id, hash_kind, canonical_hash, md5(node_ids::text)) WHERE
    deleted_at IS NULL;
  * rows that are still current are left completely untouched (no tombstone
    churn, so `computed_at` keeps meaning "when this row was last confirmed").

Consumer 3.6 (the WorkflowBuilder extraction banner) reads
`equivalence_classes`: same-tenant equivalence classes of size >= 2. Cross-
tenant visibility (3.7) is deliberately NOT here -- equivalence_classes is
tenant-scoped by construction and 3.7 gates it on BOTH tenants opting in.

The lead registers the daily APScheduler job that calls analyze_tenant;
nothing in this module schedules itself. The per-pass ceiling is the
platform-settings registry key expr.subgraph_analyzer_max_workflows
(never an env var, never a new column).

Every workflow runs on its own SAVEPOINT (db.begin_nested()) so one
malformed graph_json can neither poison the tenant pass nor lose the
half-written rows of that workflow.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Tuple

from sqlalchemy import text as satext
from sqlalchemy.orm import Session

from services import platform_settings as psr
from services.subgraph_canonical import (
    canonical_hash,
    canonicalize_subgraph,
    enumerate_subgraphs,
)

logger = logging.getLogger(__name__)

TABLE = "subgraph_equivalence_index"
HASH_KIND = "byte"
MAX_WORKFLOWS_KEY = "expr.subgraph_analyzer_max_workflows"
DEFAULT_MAX_WORKFLOWS = 1000


def _validate_max_workflows(value) -> None:
    """Honest-range rule for the per-tenant ceiling: 1..100000.

    Zero would mean "never analyze", which is what an off-switch flag is for;
    a ceiling that can be set to 0 turns the analyzer off through the back
    door, so the floor is 1.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 1 or value > 100000:
        raise ValueError(f"must be 1..100000, got {value!r}")


psr.register(
    MAX_WORKFLOWS_KEY,
    int,
    DEFAULT_MAX_WORKFLOWS,
    validator=_validate_max_workflows,
    description="expr-3.5 (tasks.md 3.5) — subgraph equivalence analyzer "
                "ceiling: how many eligible workflows ONE tenant pass "
                "analyzes (workflow_id order, bounded). Default 1000; a "
                "tenant-scope row can raise/lower it. Min 1 -- disabling "
                "the analyzer is what not scheduling the daily job is for.",
    group="expr",
)


def _max_workflows(db: Session, tenant_id: int) -> int:
    """Resolve the per-tenant ceiling, fail-closed to the registered default."""
    try:
        value = psr.get_setting(MAX_WORKFLOWS_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — a broken settings layer must not stop the pass
        logger.warning("subgraph analyzer ceiling unreadable; using default")
        return DEFAULT_MAX_WORKFLOWS
    try:
        value = int(value)
        _validate_max_workflows(value)
        return value
    except (TypeError, ValueError):
        logger.warning("subgraph analyzer ceiling out of range; using default")
        return DEFAULT_MAX_WORKFLOWS


def _graph_json(db: Session, tenant_id: int, workflow_id: int):
    """Tenant-checked load of the workflow's latest version graph.

    The tenant filter is IN THE SQL (a workflow_id belonging to another
    tenant can never be read through this helper), and the version must be
    live (not soft-deleted) and owned by that same workflow. Returns
    (graph_json, version_id) or (None, None) when nothing eligible exists.
    """
    rows = db.execute(satext(f"""
        SELECT ver.graph_json, ver.id
        FROM workflows AS wf
        JOIN workflow_versions AS ver ON ver.id = wf.latest_version_id
        WHERE wf.id = :wid
          AND wf.tenant_id = :tid
          AND wf.deleted_at IS NULL
          AND ver.workflow_id = :wid
          AND ver.deleted_at IS NULL
    """), {"wid": workflow_id, "tid": tenant_id}).fetchall()
    if not rows:
        return None, None
    graph, version_id = rows[0]
    return graph, version_id


def _replace_live_rows(db: Session, tenant_id: int, workflow_id: int,
                       version_id, keys: List[Tuple[str, str, List[str]]]
                       ) -> Tuple[int, int]:
    """Atomically replace one workflow's live index rows (soft-delete only).

    Stale live rows (a (hash_kind, canonical_hash, node_ids) key the pass no
    longer produces) get `deleted_at = NOW()`; the new keys are inserted with
    ON CONFLICT DO NOTHING against uq_subgraph_equivalence_index_live, so a
    still-current row is never touched and keeps its original computed_at.
    Returns (inserted, soft_deleted).
    """
    keep_json = json.dumps([{"hash_kind": hk, "canonical_hash": h,
                             "node_ids": nids} for hk, h, nids in keys])
    stale = db.execute(satext(f"""
        UPDATE {TABLE}
        SET deleted_at = NOW()
        WHERE workflow_id = :wid
          AND deleted_at IS NULL
          AND NOT EXISTS (
              SELECT TRUE
              FROM jsonb_array_elements(CAST(:keep AS JSONB)) AS k
              WHERE (k->>'hash_kind') = hash_kind
                AND (k->>'canonical_hash') = canonical_hash
                AND (k->'node_ids') = node_ids
          )
    """), {"wid": workflow_id, "keep": keep_json})
    soft_deleted = stale.rowcount or 0

    inserted = 0
    chunk = 200
    for start in range(0, len(keys), chunk):
        batch = keys[start:start + chunk]
        values = ", ".join(
            f"(:tid{i}, :wid, :vid, :hk{i}, :hash{i}, CAST(:nid{i} AS JSONB), "
            f":nc{i})"
            for i in range(len(batch))
        )
        params = {"wid": workflow_id, "vid": version_id}
        for i, (hk, h, nids) in enumerate(batch):
            params.update({f"tid{i}": tenant_id, f"hk{i}": hk,
                           f"hash{i}": h, f"nid{i}": json.dumps(nids),
                           f"nc{i}": len(nids)})
        res = db.execute(satext(f"""
            INSERT INTO {TABLE} (tenant_id, workflow_id, workflow_version_id,
                                 hash_kind, canonical_hash, node_ids, node_count)
            VALUES {values}
            ON CONFLICT DO NOTHING
        """), params)
        inserted += res.rowcount or 0
    return inserted, soft_deleted


def analyze_workflow(db: Session, tenant_id: int, workflow_id: int) -> dict:
    """Analyze ONE workflow: enumerate subgraphs, hash them, replace live rows.

    Runs on its own SAVEPOINT so a malformed graph (or any other failure)
    rolls back only this workflow's index writes -- the tenant pass keeps
    going. Never raises: the exception CLASS NAME is logged (never the
    message, which could carry data) and returned as error_class.
    """
    savepoint = db.begin_nested()
    try:
        graph, version_id = _graph_json(db, tenant_id, workflow_id)
        if graph is None:
            savepoint.commit()
            return {"workflow_id": workflow_id, "subgraphs": 0,
                    "inserted": 0, "soft_deleted": 0,
                    "status": "skipped", "error_class": None}
        keys: List[Tuple[str, str, List[str]]] = []
        for node_ids in enumerate_subgraphs(graph):
            canon = canonicalize_subgraph(graph, node_ids)
            nids = sorted(node_ids)
            keys.append((HASH_KIND, canonical_hash(canon), nids))
        inserted, soft_deleted = _replace_live_rows(
            db, tenant_id, workflow_id, version_id, keys)
        savepoint.commit()
        return {"workflow_id": workflow_id, "subgraphs": len(keys),
                "inserted": inserted, "soft_deleted": soft_deleted,
                "status": "ok", "error_class": None}
    except Exception as exc:  # noqa: BLE001 — one bad graph must not poison the pass
        try:
            savepoint.rollback()
        except Exception:
            pass
        error_class = type(exc).__name__
        logger.warning("subgraph analyzer failed for workflow %s: %s",
                       workflow_id, error_class)
        return {"workflow_id": workflow_id, "subgraphs": 0,
                "inserted": 0, "soft_deleted": 0,
                "status": "error", "error_class": error_class}


def analyze_tenant(db: Session, tenant_id: int, *,
                   max_workflows: int | None = None) -> dict:
    """Daily pass over one tenant's eligible workflows (3.5).

    Eligible = live (not soft-deleted), not a platform system workflow, and
    has a latest_version_id. Analyzed in workflow_id order, bounded by
    `max_workflows` (default: the expr.subgraph_analyzer_max_workflows
    platform setting). Each workflow runs on its own savepoint, so an
    error_class result never aborts the rest of the pass.
    """
    limit = _max_workflows(db, tenant_id) if max_workflows is None else max_workflows
    limit = max(1, int(limit))
    ids = [row[0] for row in db.execute(satext(f"""
        SELECT id
        FROM workflows
        WHERE tenant_id = :t
          AND deleted_at IS NULL
          AND NOT is_system
          AND latest_version_id IS NOT NULL
        ORDER BY id
        LIMIT :limit
    """), {"t": tenant_id, "limit": limit}).fetchall()]
    results = [analyze_workflow(db, tenant_id, wid) for wid in ids]
    return {
        "tenant_id": tenant_id,
        "eligible": len(ids),
        "analyzed": sum(1 for r in results if r["status"] == "ok"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "subgraphs": sum(r["subgraphs"] for r in results),
        "inserted": sum(r["inserted"] for r in results),
        "soft_deleted": sum(r["soft_deleted"] for r in results),
        "workflows": results,
    }


def equivalence_classes(db: Session, tenant_id: int, *,
                        min_size: int = 2) -> List[Dict]:
    """Same-tenant equivalence classes (consumer 3.6's banner source).

    One GROUP BY canonical_hash over this tenant's LIVE rows, keeping classes
    with COUNT(DISTINCT workflow_id) >= min_size. Tenant-scoped by
    construction: cross-tenant visibility (3.7) is a separate, both-tenants
    opt-in gate and is NOT reachable through this function.
    """
    rows = db.execute(satext(f"""
        SELECT canonical_hash,
               array_agg(DISTINCT workflow_id) AS workflow_ids,
               MIN(node_count) AS node_count
        FROM {TABLE}
        WHERE tenant_id = :t
          AND deleted_at IS NULL
        GROUP BY canonical_hash
        HAVING COUNT(DISTINCT workflow_id) >= :min_size
    """), {"t": tenant_id, "min_size": int(min_size)}).fetchall()
    return [{"canonical_hash": h, "workflow_ids": sorted(wids),
            "node_count": nc} for h, wids, nc in rows]


# ---------------------------------------------------------------------------
# Scheduler integration — settings registration + scheduled_analyze
# ---------------------------------------------------------------------------

from services import platform_settings as _psr  # noqa: E402

_psr.register(
    "expr.subgraph_analyzer_job_enabled",
    bool, False, group="expr",
    description="expr 3.5 — enable the daily subgraph-equivalence "
                "analyzer scheduler job (default: off).",
)
_psr.register(
    "expr.subgraph_analyzer_max_tenants",
    int, 500, group="expr",
    description="expr 3.5 — upper bound on non-deleted tenants to "
                "scan per daily analyzer pass (1..100000).",
)


def scheduled_analyze(db: Session) -> dict:
    """Scheduler entry-point: gated by ``expr.subgraph_analyzer_job_enabled``.

    When the flag is off, returns ``{"status": "disabled"}`` and does NOT
    touch the database.  When on, iterates non-deleted tenants (bounded by
    ``expr.subgraph_analyzer_max_tenants``) and calls ``analyze_tenant`` for
    each one inside its own savepoint; a single error is logged by class name
    and the tenant is skipped.  Commits once at the end (the caller's
    transaction).  On exception the DB is rolled back and the error is
    re-raised so the tick wrapper can log it.
    """
    from services import platform_settings as psr

    enabled = psr.get_setting("expr.subgraph_analyzer_job_enabled", db=db)
    if not enabled:
        return {"status": "disabled"}

    max_tenants = psr.get_setting("expr.subgraph_analyzer_max_tenants",
                                  db=db)
    max_tenants = max(1, min(int(max_tenants), 100000))

    tenant_ids = [row[0] for row in db.execute(satext("""
        SELECT id FROM tenants
        WHERE deleted_at IS NULL
        ORDER BY id
        LIMIT :limit
    """), {"limit": max_tenants}).fetchall()]

    analyzed = 0
    errors = 0
    for tid in tenant_ids:
        try:
            with db.begin_nested():
                analyze_tenant(db, tid)
            analyzed += 1
        except Exception as exc:  # noqa: BLE001
            error_class = type(exc).__name__
            errors += 1
            logger.warning("subgraph analyzer failed for tenant %s: %s",
                           tid, error_class)

    db.commit()
    return {
        "tenant_ids": len(tenant_ids),
        "analyzed": analyzed,
        "errors": errors,
    }

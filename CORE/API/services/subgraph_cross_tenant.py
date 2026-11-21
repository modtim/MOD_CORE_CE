"""Cross-tenant subgraph equivalence visibility, gated by BOTH tenants opting in.

expr 3.7 (tasks.md 3.7): tenants can see which *other* tenants share
equivalent subgraph hashes — only when both the caller and the other tenant
have opted in.  Counts only; no ids are ever leaked.

Privacy: the return value contains {hash_kind, canonical_hash, own_workflow_count,
other_opted_in_tenant_count} — never other tenants' ids, workflow ids, or node ids.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from services import platform_settings as psr

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PSR registration
# ---------------------------------------------------------------------------

_OPT_IN_KEY = "expr.subgraph_cross_tenant_opt_in"
_MAX_CLASSES_KEY = "expr.subgraph_cross_tenant_max_classes"


def _validate_max_classes(value) -> None:
    """1..500 — zero would be a silent no-op, >500 risks unbounded memory."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 1 or value > 500:
        raise ValueError(f"must be 1..500, got {value!r}")


psr.register(
    _OPT_IN_KEY,
    bool,
    False,
    description="expr 3.7 — allow cross-tenant subgraph equivalence visibility "
                "for this tenant.  Both the caller AND the other tenant must "
                "opt in for a class to appear in results.  Default False.",
    group="expr",
)

psr.register(
    _MAX_CLASSES_KEY,
    int,
    50,
    validator=_validate_max_classes,
    description="expr 3.7 — max equivalence classes returned per cross_tenant_classes "
                "call.  Default 50.  Range 1..500.",
    group="expr",
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tenant_opted_in(db, tenant_id: int) -> bool:
    """Return True if *tenant_id* has opted in to cross-tenant visibility.

    Fail-closed: any error reading the setting returns False.
    """
    try:
        return bool(psr.get_setting(_OPT_IN_KEY, db=db, tenant_id=tenant_id))
    except Exception as exc:  # noqa: BLE001
        # Class name only — never log exc_info/traceback here, which can
        # embed SQL bind parameters (hashes, ids) via SQLAlchemy's repr.
        logger.error("tenant_opted_in: setting unreadable (%s)", type(exc).__name__)
        return False


def cross_tenant_classes(db, *, tenant_id: int) -> list[dict]:
    """Return cross-tenant equivalence classes visible to *tenant_id*.

    Returns a list of dicts keyed by {hash_kind, canonical_hash,
    own_workflow_count, other_opted_in_tenant_count}.  Only classes where at
    least one other opted-in tenant exists are included (other count > 0).

    Rules:
    - If caller has not opted in → [].
    - Soft-deleted rows (deleted_at IS NOT NULL) are ignored entirely.
    - SQL runs inside db.begin_nested(); on error, log class name only and [].
    - Bind params only — no string interpolation.
    - Cache opted-in status per call (up to 1000 distinct other tenants).
    - Order by other_opted_in_tenant_count DESC, capped by max_classes setting.
    """
    # Caller must be opted in
    if not tenant_opted_in(db, tenant_id):
        return []

    # Resolve max_classes
    try:
        max_classes = int(psr.get_setting(_MAX_CLASSES_KEY, db=None))
    except Exception as exc:  # noqa: BLE001
        logger.error("cross_tenant_classes: max_classes unreadable (%s), using default",
                     type(exc).__name__)
        max_classes = 50

    _TABLE = "subgraph_equivalence_index"

    # Hard cap on distinct (hash_kind, canonical_hash) pairs shipped into the
    # dynamic IN (VALUES ...) clause below, independent of max_classes — a
    # caller with thousands of live rows must not produce an unbounded query.
    _CLASS_PAIR_CAP = 1000

    try:
        with db.begin_nested():
            # Step 1: find distinct (hash_kind, canonical_hash) pairs the
            # caller has, bounded to _CLASS_PAIR_CAP.
            caller_rows = db.execute(text(
                f"SELECT DISTINCT hash_kind, canonical_hash "
                f"FROM {_TABLE} "
                f"WHERE tenant_id = :tid AND deleted_at IS NULL "
                f"LIMIT :cap"
            ), {"tid": tenant_id, "cap": _CLASS_PAIR_CAP}).fetchall()

            if not caller_rows:
                return []

            # Build a list of (hash_kind, canonical_hash) tuples for IN clause
            pairs = [(r[0], r[1]) for r in caller_rows]

            # Step 2: find distinct other tenants sharing those pairs
            # We use a VALUES list to pass multiple pairs as bind params
            # to avoid SQL injection (values are validated by caller context).
            # Build a safe VALUES clause from the pairs.
            if not pairs:
                return []

            # Use a subquery approach: build a table-valued parameter via
            # UNION ALL of bind parameters.
            pair_values = []
            pair_params = {}
            for idx, (hk, ch) in enumerate(pairs):
                pair_values.append(
                    f"(:hk_{idx}, :ch_{idx})"
                )
                pair_params[f"hk_{idx}"] = hk
                pair_params[f"ch_{idx}"] = ch

            values_clause = ", ".join(pair_values)

            # Find other tenants with matching hashes (excluding caller)
            # grouped by (hash_kind, canonical_hash)
            query = text(f"""
                SELECT eq.hash_kind, eq.canonical_hash,
                       COUNT(DISTINCT eq.tenant_id) AS other_tenant_count
                FROM {_TABLE} eq
                WHERE eq.tenant_id != :tid
                  AND eq.deleted_at IS NULL
                  AND (eq.hash_kind, eq.canonical_hash) IN ({values_clause})
                GROUP BY eq.hash_kind, eq.canonical_hash
                HAVING COUNT(DISTINCT eq.tenant_id) > 0
            """)
            query_params = {"tid": tenant_id}
            query_params.update(pair_params)

            other_tenants = db.execute(query, query_params).fetchall()

            if not other_tenants:
                return []

            # Step 3: filter to only opted-in other tenants
            # Collect distinct tenant ids
            distinct_other_tenant_ids = set()
            for row in other_tenants:
                # We need to check each tenant individually.
                # First, collect all distinct tenant_ids from the results.
                pass

            # We need per-(hash_kind, canonical_hash) counts of *opted-in* tenants.
            # To do that, we need the actual tenant_ids. Let's refine the query.

            # Refined: get (hash_kind, canonical_hash, tenant_id) for other tenants
            query2 = text(f"""
                SELECT eq.hash_kind, eq.canonical_hash, eq.tenant_id
                FROM {_TABLE} eq
                WHERE eq.tenant_id != :tid
                  AND eq.deleted_at IS NULL
                  AND (eq.hash_kind, eq.canonical_hash) IN ({values_clause})
                ORDER BY eq.hash_kind, eq.canonical_hash, eq.tenant_id
            """)
            raw_rows = db.execute(query2, query_params).fetchall()

            # Cache opted-in status for each distinct tenant we encounter (up to 1000)
            opted_in_cache: dict[int, bool] = {}
            MAX_TENANTS_TO_CHECK = 1000

            def _is_opted_in(tid: int) -> bool:
                if tid in opted_in_cache:
                    return opted_in_cache[tid]
                if len(opted_in_cache) >= MAX_TENANTS_TO_CHECK:
                    opted_in_cache[tid] = False
                    return False
                result = tenant_opted_in(db, tid)
                opted_in_cache[tid] = result
                return result

            # Build per-class dicts
            # { (hash_kind, canonical_hash): {"own_workflow_count": N, "other_opted_in": {tid}} }
            class_map: dict[tuple, dict] = {}

            for row in raw_rows:
                hk, ch, other_tid = row
                key = (hk, ch)
                if key not in class_map:
                    class_map[key] = {"own_workflow_count": 0, "other_opted_in": set()}
                if _is_opted_in(other_tid):
                    class_map[key]["other_opted_in"].add(other_tid)

            # Compute own_workflow_count per class from caller's rows
            own_query = text(f"""
                SELECT hash_kind, canonical_hash, COUNT(*) AS wf_count
                FROM {_TABLE}
                WHERE tenant_id = :tid AND deleted_at IS NULL
                GROUP BY hash_kind, canonical_hash
            """)
            own_rows = db.execute(own_query, {"tid": tenant_id}).fetchall()
            for r in own_rows:
                key = (r[0], r[1])
                if key in class_map:
                    class_map[key]["own_workflow_count"] = r[2]

            # Build result list
            results = []
            for (hk, ch), info in class_map.items():
                other_count = len(info["other_opted_in"])
                if other_count > 0:
                    results.append({
                        "hash_kind": hk,
                        "canonical_hash": ch,
                        "own_workflow_count": info["own_workflow_count"],
                        "other_opted_in_tenant_count": other_count,
                    })

            # Sort by other count desc, apply cap
            results.sort(key=lambda x: x["other_opted_in_tenant_count"], reverse=True)
            return results[:max_classes]

    except SQLAlchemyError as exc:
        # Class name only — SQLAlchemyError.__str__ / the traceback that
        # logger.exception would attach can embed the statement's bind
        # parameters ([SQL: ...] / [parameters: ...]), leaking hashes/ids.
        logger.error("subgraph_cross_tenant: SQL error (%s)", type(exc).__name__)
        return []

"""expr 3.7 (tasks.md 3.7) — cross-tenant equivalence visibility, gated by BOTH tenants opting in.

Consumer 3.7 of workflow-expressiveness-primitives.  services/
subgraph_equivalence_analyzer.py keeps `equivalence_classes` tenant-scoped by
construction and deliberately does NOT expose cross-tenant visibility; this
module is the only place it exists, and it is gated on BOTH sides: a class is
reported to the requesting tenant only when that tenant AND every counted
other tenant have opted in (`expr.cross_tenant_equivalence_opt_in`,
tenant-overridable, default off).

Privacy model (three layers, all enforced here):
  * both-sides opt-in — the requester's opt-in is checked BEFORE any index
    query (not opted in => [] with ZERO table queries), and each candidate
    tenant's own opt-in is resolved per tenant, so an opted-out tenant can
    never contribute to a count even though it holds the same shape;
  * k-anonymity floor — `expr.cross_tenant_equivalence_min_tenants` (default
    3, floor 2, clamped) means a class is reported only when at least that
    many DISTINCT other opted-in tenants hold it; a count of 1 or 2 would let
    a caller single out a specific tenant;
  * counts only — the item carries the requesting tenant's own workflow ids
    and a bare COUNT of the other side.  Other tenants' ids, workflow ids,
    names and node_ids never appear in the result, and no log line ever
    prints a hash together with a tenant id.

Intended caller: a LATER slice's router/banner (e.g. the WorkflowBuilder
extraction banner for 3.7) that renders these matches for the signed-in
tenant.  No route is registered here, and nothing in this module schedules
itself or writes anything: it never commits, never raises
(SQLAlchemyError -> [] with the exception's class name logged), and reads
through ONE savepoint so a malformed index can never poison the caller's
transaction.

Table `subgraph_equivalence_index` is migration-only (20261012j, not in
models.py) -> every statement is `sqlalchemy.text` with bind params only.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from sqlalchemy import text as satext
from sqlalchemy.exc import SQLAlchemyError

from services import platform_settings as psr

logger = logging.getLogger(__name__)

TABLE = "subgraph_equivalence_index"
OPT_IN_KEY = "expr.cross_tenant_equivalence_opt_in"
MIN_TENANTS_KEY = "expr.cross_tenant_equivalence_min_tenants"

_MIN_TENANTS_FLOOR = 2
_MIN_TENANTS_CEILING = 1000
_CANDIDATE_CAP = 500      # distinct other tenants considered per call
_HASH_SCAN_CAP = 200      # this tenant's hashes considered per call
_HASH_CHUNK = 50          # hashes per counting query (bind-param bound)


def _validate_min_tenants(value) -> None:
    """k-anonymity floor: an int >= 2 (a count of 1 names a single tenant)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < _MIN_TENANTS_FLOOR or value > _MIN_TENANTS_CEILING:
        raise ValueError(f"must be {_MIN_TENANTS_FLOOR}..{_MIN_TENANTS_CEILING}, "
                         f"got {value!r}")


psr.register(
    OPT_IN_KEY,
    bool,
    False,
    group="expr",
    description="expr 3.7 -- cross-tenant subgraph-equivalence visibility. "
                "A class is visible to a tenant only when BOTH that tenant and "
                "the other tenant have opted in; a tenant-scope row overrides "
                "the platform row. Default: off.",
)
psr.register(
    MIN_TENANTS_KEY,
    int,
    3,
    validator=_validate_min_tenants,
    group="expr",
    description="expr 3.7 -- k-anonymity floor: minimum number of DISTINCT "
                "other opted-in tenants that must hold a class before it is "
                "reported (2..1000, clamped). Default 3.",
)


def _opted_in(db, tenant_id: int) -> bool:
    """One tenant's opt-in, fail-closed (unreadable settings layer = off)."""
    try:
        return bool(psr.get_setting(OPT_IN_KEY, db=db, tenant_id=tenant_id))
    except Exception:  # noqa: BLE001 -- a broken settings layer means "off"
        logger.warning("cross_tenant_equivalence: opt-in unreadable, treated as off")
        return False


def _min_tenants(db) -> int:
    """k-anonymity floor, platform-level, clamped to 2..1000, fail-closed."""
    try:
        value = psr.get_setting(MIN_TENANTS_KEY, db=db)
    except Exception:  # noqa: BLE001 -- fall back to the strictest default
        return 3
    if not isinstance(value, int) or isinstance(value, bool):
        return 3
    return max(_MIN_TENANTS_FLOOR, min(int(value), _MIN_TENANTS_CEILING))


def _chunks(values: List[str]) -> List[List[str]]:
    """Hashes in _HASH_CHUNK-sized groups (keeps bind-param counts bounded)."""
    return [values[i:i + _HASH_CHUNK] for i in range(0, len(values), _HASH_CHUNK)]


def _in_params(prefix: str, count: int) -> str:
    """`:p_0, :p_1, ...` for an IN (...) clause (bind params only)."""
    return ", ".join(f":{prefix}_{i}" for i in range(count))


def cross_tenant_matches(db, tenant_id: int, *, limit: int = 20) -> List[Dict]:
    """Cross-tenant matches visible to `tenant_id` (both-sides opt-in + k-floor).

    Returns [] when the requesting tenant has not opted in -- WITHOUT touching
    the index table at all (zero table queries: the opt-in read is a
    platform-settings lookup, and we return before any index SELECT).

    Otherwise each item is
        {canonical_hash, hash_kind, node_count, own_workflow_ids,
         other_tenant_count}
    where `own_workflow_ids` are THIS tenant's workflow ids (the caller may
    show its own) and `other_tenant_count` is a bare COUNT of DISTINCT other
    opted-in tenants holding the same (canonical_hash, hash_kind) on LIVE
    rows.  Only classes whose count reaches the k-anonymity floor are
    returned.  Sorted node_count desc, then count desc; capped at `limit`.

    Candidate tenants are bounded (one DISTINCT query, LIMIT 500) and their
    opt-in is resolved per candidate tenant through psr.get_setting in
    Python -- never one query per index row.
    """
    tenant_id = int(tenant_id)
    limit = max(1, min(int(limit), 200))

    # Gate first: an opted-out requester gets nothing and the index is never
    # read (so a caller cannot probe the index through this entry-point).
    if not _opted_in(db, tenant_id):
        return []

    try:
        with db.begin_nested():
            min_tenants = _min_tenants(db)

            own_rows = db.execute(satext(f"""
                SELECT canonical_hash, hash_kind, node_count, workflow_id
                FROM {TABLE}
                WHERE tenant_id = :t
                  AND deleted_at IS NULL
            """), {"t": tenant_id}).fetchall()
            if not own_rows:
                return []

            own: Dict[Tuple[str, str], Dict] = {}
            for ch, kind, nc, wid in own_rows:
                slot = own.setdefault((str(ch), str(kind)),
                                      {"nc": int(nc), "wids": set()})
                slot["nc"] = min(slot["nc"], int(nc))
                slot["wids"].add(int(wid))

            hashes = sorted({h for h, _k in own})[:_HASH_SCAN_CAP]

            # Bounded candidate set: DISTINCT other tenants that hold any of
            # this tenant's hashes on live rows (LIMIT 500).  These ids are
            # used ONLY to resolve the other side's opt-in and to count --
            # they never reach the return value.
            candidates: set = set()
            for chunk in _chunks(hashes):
                binds = {f"h_{i}": h for i, h in enumerate(chunk)}
                cand_rows = db.execute(satext(f"""
                    SELECT DISTINCT tenant_id
                    FROM {TABLE}
                    WHERE deleted_at IS NULL
                      AND tenant_id != :t
                      AND canonical_hash IN ({_in_params("h", len(chunk))})
                    LIMIT {_CANDIDATE_CAP}
                """), {"t": tenant_id, **binds}).fetchall()
                candidates.update(int(r[0]) for r in cand_rows)
            candidates = sorted(candidates)[:_CANDIDATE_CAP]

            # Opt-in resolved per candidate tenant through psr (tenant row >
            # platform row > default) -- one settings lookup per CANDIDATE
            # (bounded by _CANDIDATE_CAP), never one per index row.
            opted_in = [tid for tid in candidates if _opted_in(db, tid)]
            if len(opted_in) < min_tenants:
                return []

            items = []
            for chunk in _chunks(hashes):
                binds = {f"h_{i}": h for i, h in enumerate(chunk)}
                tb = {f"t_{i}": tid for i, tid in enumerate(opted_in)}
                counts = db.execute(satext(f"""
                    SELECT canonical_hash, hash_kind,
                           COUNT(DISTINCT tenant_id) AS other_tenant_count
                    FROM {TABLE}
                    WHERE deleted_at IS NULL
                      AND canonical_hash IN ({_in_params("h", len(chunk))})
                      AND tenant_id IN ({_in_params("t", len(opted_in))})
                    GROUP BY canonical_hash, hash_kind
                    HAVING COUNT(DISTINCT tenant_id) >= :min_tenants
                """), {**binds, **tb, "min_tenants": min_tenants}).fetchall()
                for ch, kind, n in counts:
                    slot = own.get((str(ch), str(kind)))
                    if slot is None:
                        continue  # hash_kind must match on both sides
                    items.append({
                        "canonical_hash": str(ch),
                        "hash_kind": str(kind),
                        "node_count": int(slot["nc"]),
                        "own_workflow_ids": sorted(slot["wids"]),
                        "other_tenant_count": int(n),
                    })
            items.sort(key=lambda it: (-it["node_count"],
                                       -it["other_tenant_count"],
                                       it["canonical_hash"], it["hash_kind"]))
            return items[:limit]
    except SQLAlchemyError as exc:
        logger.error("cross_tenant_equivalence: SQL error (%s)", type(exc).__name__)
        return []

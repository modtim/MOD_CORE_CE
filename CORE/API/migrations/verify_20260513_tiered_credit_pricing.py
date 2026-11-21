"""Read-only verifier for the 20260513 tiered-credit-pricing backfill.

Re-samples node_definitions rows and re-derives the expected per-tier
credit values with the SAME formula the migration used
(_scaled_tiers: small=ceil(0.5c), medium=c, large=ceil(2.5c)) to flag
rows whose stored cost_credits_by_tier / cost_basis disagree.

Sampling choice: 5 lowest id + 5 highest id (deduped when the table
has fewer than n rows), excluding soft-deleted rows (deleted_at IS
NULL — node_definitions carries deleted_at per ISO 27001).

STRICTLY read-only: SELECT only, no --commit flag exists.
Exit code 1 if any mismatch is found, 0 otherwise.
"""
import math
import os
import sys


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def _scaled_tiers(cost_in_credits: int) -> dict[str, int]:
    """Same ratios as the migration: small=0.5x, medium=1x, large=2.5x.
    Free nodes (c<=0) get all zeros."""
    if cost_in_credits <= 0:
        return {"small": 0, "medium": 0, "large": 0}
    return {
        "small":  max(1, math.ceil(cost_in_credits * 0.5)),
        "medium": cost_in_credits,
        "large":  max(1, math.ceil(cost_in_credits * 2.5)),
    }


def _normalize_tiers(value):
    """JSONB may arrive as dict, None, or a string ('{}' / 'null')."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s in ("", "{}", "null"):
            return None
        try:
            import json as _json
            parsed = _json.loads(s)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def check_row(row) -> list[str]:
    """Return mismatch reasons for one row (dict or 4-tuple). Empty list = OK."""
    if isinstance(row, dict):
        r = row
    else:
        node_id, cost_in_credits, tier_raw, basis = row
        r = {"id": node_id, "cost_in_credits": cost_in_credits,
             "cost_credits_by_tier": tier_raw, "cost_basis": basis}

    reasons: list[str] = []
    c = int(r.get("cost_in_credits") or 0)
    basis = r.get("cost_basis") or "estimated"
    tiers = _normalize_tiers(r.get("cost_credits_by_tier"))

    if basis == "free":
        if c != 0:
            reasons.append("free basis but cost_in_credits != 0")
        if tiers is not None:
            nonzero = {k: v for k, v in tiers.items()
                       if k in ("small", "medium", "large") and (v or 0) != 0}
            if nonzero:
                reasons.append(f"free basis but nonzero tiers {nonzero}")
    else:
        if tiers is None or "medium" not in tiers:
            reasons.append("missing/NULL cost_credits_by_tier on non-free row")
        elif basis == "estimated":
            expected = _scaled_tiers(c)
            actual = {k: int(tiers.get(k) or 0) for k in expected}
            if actual != expected:
                reasons.append(
                    f"estimated basis but tiers {actual} != scaled {expected}"
                )
    return reasons


def sample_rows(conn, n: int = 10) -> list[dict]:
    """SELECT-only sample: n//2 lowest id + n//2 highest id, deduped,
    skipping soft-deleted rows."""
    half = max(1, n // 2)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, cost_in_credits, cost_credits_by_tier, cost_basis
          FROM node_definitions
         WHERE deleted_at IS NULL
         ORDER BY id ASC
        """
    )
    low = cur.fetchall()
    cur.execute(
        """
        SELECT id, cost_in_credits, cost_credits_by_tier, cost_basis
          FROM node_definitions
         WHERE deleted_at IS NULL
         ORDER BY id DESC
        """
    )
    high = cur.fetchall()

    seen: set[int] = set()
    rows: list[dict] = []
    for tup in list(low[:half]) + list(reversed(high[:half])):
        if tup[0] in seen:
            continue
        seen.add(tup[0])
        rows.append({
            "id": tup[0],
            "cost_in_credits": tup[1],
            "cost_credits_by_tier": tup[2],
            "cost_basis": tup[3],
        })
    return rows


def main() -> int:
    conn = _get_conn()
    rows = sample_rows(conn)
    print(f"sampled {len(rows)} node_definitions rows "
          f"(5 lowest id + 5 highest id, deleted_at IS NULL)")
    mismatches = 0
    for row in rows:
        reasons = check_row(row)
        tiers = _normalize_tiers(row["cost_credits_by_tier"])
        print(f"  id={row['id']} cost_in_credits={row['cost_in_credits']} "
              f"cost_credits_by_tier={tiers} cost_basis={row['cost_basis']}")
        if reasons:
            mismatches += 1
            print(f"    MISMATCH: {'; '.join(reasons)}")
    print(f"{mismatches} mismatches")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())

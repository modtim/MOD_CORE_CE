"""Tiered credit pricing — schema + retroactive backfill.

Three new columns on ``node_definitions`` and a CHECK constraint
that keeps the legacy single-int cost column consistent with the
new per-tier dict.

    cost_credits_by_tier    JSONB   {small, medium, large}  per-tier price
    cost_basis              VARCHAR pricing-source enum (estimated / calibrated / manual / free)
    regression_coefficients JSONB   per-node output_size + runtime linear-fit coefficients

The legacy ``cost_in_credits`` (int) column stays as the
fallback default; the migration adds a CHECK that it equals
``(cost_credits_by_tier->>'medium')::int`` whenever the jsonb is
populated.

Retroactive backfill rules (per the proposal's §1):

  1. cost_in_credits = 0 → cost_basis='free', tiers all 0.
     These are platform-internal source nodes that should never
     charge. The 'free' basis blocks all future recalibration.
  2. Everything else → cost_basis='estimated', tiers derived from
     a simple ratio of the existing cost_in_credits:
         small  = ceil(c * 0.5)
         medium = c
         large  = ceil(c * 2.5)
     The ratios reflect typical input-size scaling (small inputs
     do ~half the work, large inputs do ~2.5× the work). These
     get auto-revised to measured p95s once the calibration
     coordinator's recompute hook fires for each node (out of
     scope for this migration; see §5 in the openspec tasks).

Note this migration does NOT call the LLM estimator. Running 67+
LLM calls inside alembic is slow + flaky; the estimator runs
lazily via the wizard's analyze-script path (for newly-created
nodes) and via the manual `POST /node_definitions/{id}/recompute-cost`
endpoint (for fleet-wide refreshes).

The calibrated branch is NOT here — handled post-migration by
scheduler/pattern_updater.py ``_auto_calculate_credit_cost``.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260513_tiered_credit_pricing.py --commit
"""
from __future__ import annotations

import argparse
import json as _json
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


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _constraint_exists(cur, table: str, constraint: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_name = %s AND constraint_name = %s
        """,
        (table, constraint),
    )
    return cur.fetchone() is not None


def _scaled_tiers(cost_in_credits: int) -> dict[str, int]:
    """Derive per-tier credit values from a single existing
    cost_in_credits integer. Ratios: small=0.5x, medium=1x,
    large=2.5x — reflects typical input-size scaling. Free nodes
    (c=0) get all zeros."""
    if cost_in_credits <= 0:
        return {"small": 0, "medium": 0, "large": 0}
    return {
        "small":  max(1, math.ceil(cost_in_credits * 0.5)),
        "medium": cost_in_credits,
        "large":  max(1, math.ceil(cost_in_credits * 2.5)),
    }


def _write_audit_events(cur, summary: dict) -> None:
    """Write two append-only audit summary rows to audit_events.

    One row per basis with {basis, count} in details.
    Uses the platform audit tenant id from audit.py.
    """
    from audit import PLATFORM_AUDIT_TENANT_ID

    for basis in ("free", "estimated"):
        count = summary.get(basis, 0)
        cur.execute(
            """
            INSERT INTO audit_events (tenant_id, event_name, details)
            VALUES (%s, 'node_cost.backfill', %s::jsonb)
            """,
            (PLATFORM_AUDIT_TENANT_ID, _json.dumps(
                {"basis": basis, "count": count})),
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    steps: list[tuple[str, str, tuple]] = []
    backfill_summary = {"free": 0, "estimated": 0, "skipped_existing": 0}

    # ───────────────────────────────────────────────────────────
    # Step 1 — add the three columns.
    # ───────────────────────────────────────────────────────────
    if not _column_exists(cur, "node_definitions", "cost_credits_by_tier"):
        steps.append((
            "add cost_credits_by_tier jsonb",
            "ALTER TABLE node_definitions "
            "ADD COLUMN cost_credits_by_tier JSONB NOT NULL DEFAULT '{}'::jsonb",
            (),
        ))

    if not _column_exists(cur, "node_definitions", "cost_basis"):
        steps.append((
            "add cost_basis varchar",
            "ALTER TABLE node_definitions "
            "ADD COLUMN cost_basis VARCHAR(16) NOT NULL DEFAULT 'estimated'",
            (),
        ))

    if not _column_exists(cur, "node_definitions", "regression_coefficients"):
        steps.append((
            "add regression_coefficients jsonb",
            "ALTER TABLE node_definitions "
            "ADD COLUMN regression_coefficients JSONB NOT NULL DEFAULT '{}'::jsonb",
            (),
        ))

    # ───────────────────────────────────────────────────────────
    # Step 2 — apply the column adds before backfilling so the
    #          backfill UPDATEs see the new columns.
    # ───────────────────────────────────────────────────────────
    for label, stmt, params in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        print(f"        {stmt.strip()[:200]}")
        if args.commit:
            cur.execute(stmt, params)
    steps.clear()

    # ───────────────────────────────────────────────────────────
    # Step 3 — retroactive backfill. Per the proposal:
    #   cost_in_credits = 0 → free, tiers zero
    #   else              → estimated, tiers scaled from c
    # We only touch rows that still have the default cost_basis
    # so a re-run is idempotent (won't clobber already-migrated
    # rows that may have been recomputed in the meantime).
    #
    # Skip the backfill in dry-run mode (the columns don't exist
    # yet so the SELECT below would fail). Show a count of rows
    # that WOULD be touched instead so the operator can sanity-
    # check the size of the change.
    # ───────────────────────────────────────────────────────────
    if not args.commit:
        cur.execute(
            "SELECT count(*), "
            "       count(*) FILTER (WHERE cost_in_credits = 0), "
            "       count(*) FILTER (WHERE cost_in_credits > 0) "
            "  FROM node_definitions"
        )
        total, free_count, paid_count = cur.fetchone()
        print(
            f"\nDRY    backfill would touch {total} rows: "
            f"{free_count} → free, {paid_count} → estimated"
        )
        print("\n(dry run — re-run with --commit)")
        return 0

    cur.execute(
        """
        SELECT id, cost_in_credits, cost_credits_by_tier, cost_basis
          FROM node_definitions
         ORDER BY id ASC
        """
    )
    rows = cur.fetchall()
    print(f"\n{len(rows)} node_definitions rows to evaluate for backfill")

    for row in rows:
        node_id, cost_in_credits, current_tier_dict, current_basis = row
        # Skip rows that already have populated tier values.
        # current_tier_dict may be a Python dict (jsonb adapter)
        # or a string {} depending on driver — handle both.
        already_populated = (
            isinstance(current_tier_dict, dict)
            and "medium" in current_tier_dict
        )
        if already_populated:
            backfill_summary["skipped_existing"] += 1
            continue

        c = int(cost_in_credits or 0)
        tiers = _scaled_tiers(c)
        basis = "free" if c <= 0 else "estimated"

        if basis == "free":
            backfill_summary["free"] += 1
        else:
            backfill_summary["estimated"] += 1

        if args.commit:
            cur.execute(
                """
                UPDATE node_definitions
                   SET cost_credits_by_tier = %s::jsonb,
                       cost_basis           = %s
                 WHERE id = %s
                """,
                (_json.dumps(tiers), basis, node_id),
            )

    print(
        f"\nbackfill summary: free={backfill_summary['free']} "
        f"estimated={backfill_summary['estimated']} "
        f"skipped_existing={backfill_summary['skipped_existing']}"
    )

    # ───────────────────────────────────────────────────────────
    # Step 4 — Write audit summary rows (append-only, never
    #          modified). One row per basis:
    #          event_name="node_cost.backfill"
    #          details = {"basis": "<free|estimated>", "count": N}
    # ───────────────────────────────────────────────────────────
    _write_audit_events(cur, backfill_summary)

    # ───────────────────────────────────────────────────────────
    # Step 5 — CHECK constraint that the legacy int column stays
    #          consistent with cost_credits_by_tier.medium.
    #          Added LAST so backfill can finish without fighting
    #          the constraint.
    # ───────────────────────────────────────────────────────────
    if not _constraint_exists(
        cur, "node_definitions", "ck_node_def_cost_in_credits_matches_medium"
    ):
        constraint_sql = (
            "ALTER TABLE node_definitions "
            "ADD CONSTRAINT ck_node_def_cost_in_credits_matches_medium "
            "CHECK ("
            "  cost_credits_by_tier = '{}'::jsonb "
            "  OR cost_in_credits = (cost_credits_by_tier->>'medium')::int"
            ")"
        )
        print(f"{'WRITE' if args.commit else 'DRY  '}  add CHECK constraint")
        print(f"        {constraint_sql[:200]}")
        if args.commit:
            cur.execute(constraint_sql)

    print("\nApplied." if args.commit else "\n(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

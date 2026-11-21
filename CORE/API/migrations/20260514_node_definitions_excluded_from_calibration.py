"""NodeDefinition — add excluded_from_calibration + flag categorical-bad node types.

The calibration matrix is meant to learn the resource profile of user-
facing workflow nodes. Two classes of node legitimately don't belong:

  1. **System / admin-only one-shots** that aren't part of any user
     workflow — calibrating them produces no signal the scheduler will
     ever use to place a real workflow task.
       - LLM Generate Benchmark Strategy
       - ML Retrain

  2. **Trivially fast file-handler nodes** (sub-second I/O, no compute
     pressure). They run as part of every workflow but their resource
     consumption is below the noise floor of the matrix.
       - Image Input
       - Text Input
       - Resource Input

Real-compute nodes that *currently* fail calibration for an environment-
setup reason (NIM Bridge, ML System Power, Unity Export) are NOT in this
list — their containers grew CALIBRATION_RUN=true synthetic-work handlers
in companion commits so they can actually run.

This migration:
  1. Adds ``node_definitions.excluded_from_calibration`` (BOOL NOT NULL
     DEFAULT FALSE). Idempotent ALTER.
  2. Flips it TRUE for every row whose ``name`` is in the list above
     (system row + every tenant copy). Idempotent UPDATE.

Companion code change in services/calibration_coordinator.py filters
the flag out of both ``_count_eligible_nodes`` and ``_pick_next_node``,
so the next plan-create / next tick skips these rows.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260514_node_definitions_excluded_from_calibration.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


def _load_excluded_names():
    """1900Y3c — ONE source of truth for the exclusion name list.

    ``services/node_scanner.py`` owns ``CE_EXCLUDED_FROM_CALIBRATION_NAMES``
    because the node scan is the path that actually runs in CE (this
    migration never executes there — a fresh DB left the column at its FALSE
    default and system/admin one-shots entered the calibration matrix). The
    scan now seeds + self-heals the column itself; this migration imports the
    same tuple so the two can never drift.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from services.node_scanner import CE_EXCLUDED_FROM_CALIBRATION_NAMES

    return CE_EXCLUDED_FROM_CALIBRATION_NAMES


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    _EXCLUDED_NAMES = _load_excluded_names()

    conn = _get_conn()
    cur = conn.cursor()

    # 1. Column add (idempotent).
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'node_definitions'
           AND column_name = 'excluded_from_calibration'
        """
    )
    col_exists = cur.fetchone() is not None
    if col_exists:
        print("Column node_definitions.excluded_from_calibration already present.")
    else:
        if not args.commit:
            print("DRY  would: ALTER TABLE node_definitions ADD COLUMN "
                  "excluded_from_calibration BOOLEAN NOT NULL DEFAULT FALSE")
        else:
            cur.execute(
                "ALTER TABLE node_definitions "
                "ADD COLUMN excluded_from_calibration BOOLEAN NOT NULL "
                "DEFAULT FALSE"
            )
            print("WRITE ALTER TABLE node_definitions ADD COLUMN "
                  "excluded_from_calibration BOOLEAN NOT NULL DEFAULT FALSE")

    if not (col_exists or args.commit):
        print("\n(dry run — re-run with --commit)")
        return 0

    # 2. Preview rows that will be flipped.
    cur.execute(
        """
        SELECT id, name, tenant_id, excluded_from_calibration
          FROM node_definitions
         WHERE name = ANY(%s)
         ORDER BY name, tenant_id NULLS FIRST, id
        """,
        (list(_EXCLUDED_NAMES),),
    )
    rows = cur.fetchall()
    if not rows:
        print("\nNo matching node_definitions rows found.")
    else:
        print(f"\nMatched {len(rows)} row(s):")
        for nid, nname, tid, already in rows:
            tag = "(already true)" if already else ""
            print(f"  id={nid:>4d}  tenant={str(tid):>4s}  {nname!r}  {tag}")

    if args.commit and rows:
        cur.execute(
            """
            UPDATE node_definitions
               SET excluded_from_calibration = TRUE
             WHERE name = ANY(%s)
               AND excluded_from_calibration = FALSE
            """,
            (list(_EXCLUDED_NAMES),),
        )
        print(f"\nApplied. {cur.rowcount} row(s) updated.")

    if not args.commit:
        print("\n(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

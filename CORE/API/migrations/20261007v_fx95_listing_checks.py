"""GSEC FX.95 p2 — marketplace listing auto-check pipeline.

Adds the two columns that store the auto-check results (malware FX.90 /
image vuln / licence FX.73 / egress sandbox FX.91) on BOTH commerce
listing tables (services/listing_review_checks.py, wired into
services/listing_review.submit()):

workflow_listings / stack_listings (ADDITIVE, nullable columns):
    check_results   JSONB NULL      [{name, status:
                                     pass|fail|warn|skipped, detail}, ...]
                                     at the time of the last pipeline run
    checks_ran_at   TIMESTAMPTZ NULL last pipeline run timestamp

A failed check does NOT reject a listing — the row stays in the staff
queue flagged with check_results exposed by the review-queue endpoint
(rejection is a staff decision). NULL on every existing row = "the
pipeline has not run for this listing yet" (legacy/live rows keep
working unchanged; no backfill is possible — the checks have never
been run for them).

Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS (safe to re-run).
Dry run (default) PRINTS the SQL and exits 0 without touching the
database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007v_fx95_listing_checks.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007v_fx95_listing_checks.py --commit   # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007u and before any 20261008* step.

models.py is bind-mounted live into the API container, so the columns
must exist before any FX.95 p2 code path writes to them — apply
immediately after adding the columns to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "workflow_listings auto-check columns",
        """
ALTER TABLE workflow_listings
    ADD COLUMN IF NOT EXISTS check_results JSONB NULL,
    ADD COLUMN IF NOT EXISTS checks_ran_at TIMESTAMPTZ NULL;
""",
    ),
    (
        "stack_listings auto-check columns",
        """
ALTER TABLE stack_listings
    ADD COLUMN IF NOT EXISTS check_results JSONB NULL,
    ADD COLUMN IF NOT EXISTS checks_ran_at TIMESTAMPTZ NULL;
""",
    ),
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.95 p2 listing auto-check schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

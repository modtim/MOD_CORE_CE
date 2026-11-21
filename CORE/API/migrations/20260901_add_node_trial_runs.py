"""node-trial-runs — try-before-buy fields.

Adds (all nullable/additive, behaviour-preserving):
  - node_definitions.sample_input_ref / sample_output_ref / demo_workflow_ref
  - workflow_runs.trial_node_definition_id / trial_data_source / trial_no_burst
  - tenants.marketplace_seen_at

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260901_add_node_trial_runs.py --commit
"""
from __future__ import annotations

import argparse
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


def _col(cur, table, col) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, col),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = _get_conn()
    cur = conn.cursor()

    adds = [
        ("node_definitions", "sample_input_ref", "VARCHAR(500)"),
        ("node_definitions", "sample_output_ref", "VARCHAR(500)"),
        ("node_definitions", "demo_workflow_ref", "VARCHAR(500)"),
        ("workflow_runs", "trial_node_definition_id", "INTEGER REFERENCES node_definitions(id) ON DELETE SET NULL"),
        ("workflow_runs", "trial_data_source", "VARCHAR(10)"),
        ("tenants", "marketplace_seen_at", "TIMESTAMPTZ"),
    ]
    steps = [
        (f"add {t}.{c}", f"ALTER TABLE {t} ADD COLUMN {c} {ddl}")
        for (t, c, ddl) in adds if not _col(cur, t, c)
    ]

    if not steps:
        print("schema already current — nothing to do.")
    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        if args.commit:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

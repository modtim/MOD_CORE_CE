"""wizard-rebuild §9 — variant_test_pending table.

Speculative variant builds: the wizard builds every variant the
BuildPlan's `variants_for_mode` returns (every accelerator the
source can target), regardless of whether the current cluster has
matching hardware. The build itself only needs Docker — the
sample-test that VERIFIES the variant requires real GPU on
matching hardware.

When no matching worker is currently online for a variant, the
sample-test result endpoint stashes a pending row here:

  - `build_spec_id`           → spec the variant came from
  - `node_definition_id`      → populated when register fires
  - `variant`                 → cuda / rocm / intel / npu / cpu
  - `image_ref`               → the pushed Harbor tag
  - `created_at`              → row insert time
  - `claimed_by_worker_id`    → set when a heartbeat drains the row
  - `status`                  → pending | claimed | verified | failed | expired
  - `expires_at`              → now + 30d default; APScheduler hourly
                                sweep flips status to `expired`
                                when expires_at < now

State machine:
  pending → claimed (worker heartbeat picks it up)
  pending → expired (TTL hit, no matching hardware ever appeared)
  claimed → verified | failed (sample-test result lands)

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260525_variant_test_pending.py --commit

Idempotent — repeated runs are safe.
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


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if _table_exists(cur, "variant_test_pending"):
        print("Table variant_test_pending already present.")
        if not args.commit:
            print("\n(dry run — re-run with --commit)")
        return 0

    if not args.commit:
        print("DRY  would: CREATE TABLE variant_test_pending (...)")
        print("\n(dry run — re-run with --commit)")
        return 0

    cur.execute(
        """
        CREATE TABLE variant_test_pending (
            id SERIAL PRIMARY KEY,
            build_spec_id INTEGER REFERENCES build_specs(id)
                ON DELETE CASCADE,
            node_definition_id INTEGER REFERENCES node_definitions(id)
                ON DELETE CASCADE,
            variant VARCHAR(64) NOT NULL,
            image_ref VARCHAR(512) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            claimed_by_worker_id INTEGER REFERENCES worker_licenses(id)
                ON DELETE SET NULL,
            claimed_at TIMESTAMPTZ,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT vtp_status_chk
                CHECK (status IN ('pending', 'claimed', 'verified',
                                  'failed', 'expired'))
        )
        """
    )
    cur.execute(
        "CREATE INDEX idx_vtp_status_variant ON "
        "variant_test_pending(status, variant) "
        "WHERE status = 'pending'"
    )
    cur.execute(
        "CREATE INDEX idx_vtp_node_def ON "
        "variant_test_pending(node_definition_id) "
        "WHERE node_definition_id IS NOT NULL"
    )
    cur.execute(
        "CREATE INDEX idx_vtp_expires ON "
        "variant_test_pending(expires_at) "
        "WHERE status = 'pending'"
    )
    print("WRITE CREATE TABLE variant_test_pending + indexes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

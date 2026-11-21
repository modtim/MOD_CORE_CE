"""GC.MWL.C1 — manager-work-loans: checkpoint transfers between managers.

A loaned node run may hand its checkpoint to the borrower (direction
'export') and the borrower hands it back (direction 'import'). Each attempt
is its own row: `attempt` is the 1-based retry count for the (node_run_id,
direction) pair, `object_key` names the uploaded object once the transfer
has a payload, `byte_size` / `progress_at_transfer` track what was actually
moved, and `status` walks pending -> uploaded -> imported (or failed).

`work_loan_checkpoint_transfers`:
    id, work_loan_id (FK work_loans), node_run_id (FK node_runs),
    attempt INTEGER, direction TEXT CHECK ('export','import'),
    object_key VARCHAR(512) (nullable), byte_size BIGINT (nullable),
    progress_at_transfer DOUBLE PRECISION (nullable),
    status TEXT CHECK ('pending','uploaded','imported','failed')
    DEFAULT 'pending',
    created_at / updated_at, deleted_at (soft delete — ISO 27001: NO hard
    deletes).

Indexes: (work_loan_id) and (node_run_id, attempt).

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS, ALTER OWNER. Safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260930_work_loan_checkpoint_transfers.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


_TABLES = ("work_loan_checkpoint_transfers",)

CREATE_WORK_LOAN_CHECKPOINT_TRANSFERS_SQL = """
CREATE TABLE IF NOT EXISTS work_loan_checkpoint_transfers (
    id                     SERIAL PRIMARY KEY,
    work_loan_id           INTEGER NOT NULL REFERENCES work_loans(id),
    node_run_id            INTEGER NOT NULL REFERENCES node_runs(id),
    attempt                INTEGER NOT NULL DEFAULT 1,
    direction              VARCHAR(20) NOT NULL,
    object_key             VARCHAR(512),
    byte_size              BIGINT,
    progress_at_transfer   DOUBLE PRECISION,
    status                 VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at             TIMESTAMPTZ,
    CONSTRAINT ck_work_loan_checkpoint_transfers_direction
        CHECK (direction IN ('export', 'import')),
    CONSTRAINT ck_work_loan_checkpoint_transfers_status
        CHECK (status IN ('pending', 'uploaded', 'imported', 'failed'))
);
"""

WLC_T_LOAN_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_work_loan_checkpoint_transfers_work_loan_id
    ON work_loan_checkpoint_transfers (work_loan_id);
"""

WLC_T_RUN_ATTEMPT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_work_loan_checkpoint_transfers_node_run_id_attempt
    ON work_loan_checkpoint_transfers (node_run_id, attempt);
"""

STATEMENTS = [
    ("create table work_loan_checkpoint_transfers", CREATE_WORK_LOAN_CHECKPOINT_TRANSFERS_SQL),
    ("create index ix_work_loan_checkpoint_transfers_work_loan_id", WLC_T_LOAN_INDEX_SQL),
    ("create index ix_work_loan_checkpoint_transfers_node_run_id_attempt", WLC_T_RUN_ATTEMPT_INDEX_SQL),
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
        for label, sql in STATEMENTS:
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        for t in _TABLES:
            try:
                cur.execute(f'ALTER TABLE {t} OWNER TO {owner}')
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER TABLE {t} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("work_loan_checkpoint_transfers ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

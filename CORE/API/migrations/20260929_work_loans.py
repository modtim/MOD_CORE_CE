"""GC.MWL.5 — manager-work-loans: durable run work-loans between managers.

Replaces the shared-Redis fence in services/mesh_handoff_service.py: the
initiating manager stays the PERMANENT owner of a workflow run; a peer
(borrower) manager executes under a time-bounded lease.

`work_loans`:
    id, workflow_run_id (FK workflow_runs), tenant_id (FK tenants),
    owner_manager_id / borrower_manager_id (FK managers.manager_id),
    lease_expires_at TIMESTAMPTZ, status TEXT CHECK over the loan lifecycle,
    assertion_ref (nullable), created_at / updated_at, deleted_at (soft delete
    — ISO 27001: NO hard deletes).

    owner_manager_id is IMMUTABLE after insert: the ORM `@validates` guard in
    models.py raises on a persisted-row change, and the BEFORE UPDATE trigger
    below raises on the raw-SQL / other-session path the ORM can't see.

`mesh_loan_events`:
    append-only provenance for every loan state transition — mirrors
    mesh_ownership_events (who changed what, from/to which manager, which
    assertion, by which actor, why, when). NOTE: like mesh_ownership_events,
    it is an audit/ledger table (no soft-delete, never updated in practice);
    no BEFORE DELETE trigger is installed, matching the existing
    mesh_ownership_events table exactly.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS, CREATE OR REPLACE FUNCTION, DROP TRIGGER IF
EXISTS + CREATE TRIGGER, ALTER OWNER. Safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260929_work_loans.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


_TABLES = ("work_loans", "mesh_loan_events")

# --- work_loans ---------------------------------------------------------------

CREATE_WORK_LOANS_SQL = """
CREATE TABLE IF NOT EXISTS work_loans (
    id                  SERIAL PRIMARY KEY,
    workflow_run_id     INTEGER NOT NULL REFERENCES workflow_runs(id),
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id),
    owner_manager_id    VARCHAR(255) NOT NULL REFERENCES managers(manager_id),
    borrower_manager_id VARCHAR(255) NOT NULL REFERENCES managers(manager_id),
    lease_expires_at    TIMESTAMPTZ NOT NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'granted',
    assertion_ref       VARCHAR(512),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    CONSTRAINT ck_work_loans_status
        CHECK (status IN ('granted', 'running', 'claimed', 'collected',
                          'expired', 'cancelled', 'rolled_back'))
);
"""

WORK_LOANS_RUN_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_work_loans_workflow_run_id
    ON work_loans (workflow_run_id);
"""

WORK_LOANS_STATUS_LEASE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_work_loans_status_lease_expires_at
    ON work_loans (status, lease_expires_at);
"""

WORK_LOANS_TENANT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_work_loans_tenant_id
    ON work_loans (tenant_id);
"""

WORK_LOANS_STATUS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_work_loans_status
    ON work_loans (status);
"""

# owner_manager_id is set once at grant and never changes — the permanent
# owner of a run cannot be rewritten out from under the fencing model. The
# ORM guard (models.WorkLoan) covers API sessions; this trigger covers raw
# SQL and any other path.
OWNER_IMMUTABLE_FN_SQL = """
CREATE OR REPLACE FUNCTION work_loans_owner_immutable()
RETURNS trigger AS $$
BEGIN
    IF NEW.owner_manager_id IS DISTINCT FROM OLD.owner_manager_id THEN
        RAISE EXCEPTION 'work_loans.owner_manager_id is immutable (manager-work-loans: the initiating manager stays the owner)'
            USING ERRCODE = 'feature_not_supported';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_OWNER_IMMUTABLE_TRIGGERS = [
    ("drop trigger trg_no_update_owner_work_loans",
     "DROP TRIGGER IF EXISTS trg_no_update_owner_work_loans ON work_loans"),
    ("create trigger trg_no_update_owner_work_loans BEFORE UPDATE (row)",
     """
     CREATE TRIGGER trg_no_update_owner_work_loans
         BEFORE UPDATE ON work_loans
         FOR EACH ROW EXECUTE FUNCTION work_loans_owner_immutable()
     """),
]

# --- mesh_loan_events -----------------------------------------------------------

CREATE_MESH_LOAN_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS mesh_loan_events (
    id              SERIAL PRIMARY KEY,
    work_loan_id    INTEGER NOT NULL REFERENCES work_loans(id),
    workflow_run_id INTEGER REFERENCES workflow_runs(id),
    from_status     VARCHAR(20),
    to_status       VARCHAR(20) NOT NULL,
    from_manager    VARCHAR(255),
    to_manager      VARCHAR(255),
    assertion_ref   VARCHAR(512),
    actor_user_id   INTEGER,
    reason          VARCHAR(512),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

MESH_LOAN_EVENTS_LOAN_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_mesh_loan_events_work_loan_id
    ON mesh_loan_events (work_loan_id);
"""

MESH_LOAN_EVENTS_RUN_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_mesh_loan_events_workflow_run_id
    ON mesh_loan_events (workflow_run_id);
"""

MESH_LOAN_EVENTS_CREATED_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_mesh_loan_events_created_at
    ON mesh_loan_events (created_at);
"""

STATEMENTS = [
    ("create table work_loans", CREATE_WORK_LOANS_SQL),
    ("create index ix_work_loans_workflow_run_id", WORK_LOANS_RUN_INDEX_SQL),
    ("create index ix_work_loans_status_lease_expires_at", WORK_LOANS_STATUS_LEASE_INDEX_SQL),
    ("create index ix_work_loans_tenant_id", WORK_LOANS_TENANT_INDEX_SQL),
    ("create index ix_work_loans_status", WORK_LOANS_STATUS_INDEX_SQL),
    ("create or replace function work_loans_owner_immutable()", OWNER_IMMUTABLE_FN_SQL),
] + _OWNER_IMMUTABLE_TRIGGERS + [
    ("create table mesh_loan_events", CREATE_MESH_LOAN_EVENTS_SQL),
    ("create index ix_mesh_loan_events_work_loan_id", MESH_LOAN_EVENTS_LOAN_INDEX_SQL),
    ("create index ix_mesh_loan_events_workflow_run_id", MESH_LOAN_EVENTS_RUN_INDEX_SQL),
    ("create index ix_mesh_loan_events_created_at", MESH_LOAN_EVENTS_CREATED_INDEX_SQL),
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
    print("work_loans + mesh_loan_events ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

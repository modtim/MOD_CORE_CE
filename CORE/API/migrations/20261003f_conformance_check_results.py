"""GY.C12 (launch-completion-megaplan) — continuous conformance engine
results table (append-only).

Adds `conformance_check_results`, the append-only history for the
continuous conformance engine (services/conformance/): one row per
check per run, platform-scoped (`tenant_id` NULL).

ISO 27001 policy: no hard deletes — the table is append-only, guarded
exactly like the audit tables by the shared `audit_append_only_guard()`
function (BEFORE UPDATE OR DELETE + BEFORE TRUNCATE triggers, errcode
feature_not_supported so clients map it to 501). The DO block creates
the guard function only if it does not already exist (pg_proc check by
proname) so this file is self-contained on a fresh install regardless
of whether 20261003c_audit_append_only_triggers.py has run yet,
without ever overwriting a guard that has since changed elsewhere.

Also adds the `CONFORMANCE_DRIFT` value to the `auditaction` enum
(models.AuditAction; the drift event written by
services.conformance.engine._emit_drift). Postgres enum values must be
added one at a time outside a transaction block — the runner below
uses autocommit for it, mirroring
20260605_add_scim_audit_action_enum_values.py.

There is intentionally no downgrade: removing the triggers would
silently drop the append-only protection. Re-running is a no-op that
re-asserts everything (IF NOT EXISTS / CREATE OR REPLACE /
DROP TRIGGER IF EXISTS + CREATE TRIGGER).

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. The lead applies with --commit.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261003f_conformance_check_results.py            # dry run: prints SQL
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261003f_conformance_check_results.py --commit   # apply

Applied 2026-10-03.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "conformance_check_results"

ENUM_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'CONFORMANCE_DRIFT'"
)

TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER,
    check_id TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence JSONB,
    controls JSONB,
    trigger TEXT NOT NULL,
    run_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
"""

INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS ix_conformance_check_results_id ON {_TABLE} (id);
CREATE INDEX IF NOT EXISTS ix_conformance_check_results_tenant_id ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_conformance_check_results_check_id ON {_TABLE} (check_id);
CREATE INDEX IF NOT EXISTS ix_conformance_check_results_status ON {_TABLE} (status);
CREATE INDEX IF NOT EXISTS ix_conformance_check_results_run_id ON {_TABLE} (run_id);
CREATE INDEX IF NOT EXISTS ix_conformance_check_results_check_created
    ON {_TABLE} (check_id, created_at);
"""

TRIGGER_SQL = f"""
DO $$
BEGIN
    IF to_regclass('{_TABLE}') IS NULL THEN
        RAISE EXCEPTION '{_TABLE} does not exist; run the table DDL first';
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.proname = 'audit_append_only_guard'
    ) THEN
        CREATE FUNCTION audit_append_only_guard()
        RETURNS trigger AS $fn$
        BEGIN
            RAISE EXCEPTION 'Operation % is forbidden on append-only audit table % (ISO 27001: audit tables are append-only; UPDATE/DELETE/TRUNCATE are not allowed)',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'feature_not_supported';
        END;
        $fn$ LANGUAGE plpgsql;
    END IF;
    DROP TRIGGER IF EXISTS trg_no_row_mutation_{_TABLE} ON {_TABLE};
    CREATE TRIGGER trg_no_row_mutation_{_TABLE}
        BEFORE UPDATE OR DELETE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION audit_append_only_guard();
    DROP TRIGGER IF EXISTS trg_no_truncate_{_TABLE} ON {_TABLE};
    CREATE TRIGGER trg_no_truncate_{_TABLE}
        BEFORE TRUNCATE ON {_TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION audit_append_only_guard();
END
$$
"""

_STATEMENTS = [
    (f"{_TABLE} table", TABLE_SQL),
    (f"indexes on {_TABLE}", INDEX_SQL),
    (f"append-only triggers on {_TABLE}", TRIGGER_SQL),
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
        print(f"-- auditaction enum value\n{ENUM_SQL}")
        for label, sql in _STATEMENTS:
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: ALTER TYPE needs the owner role (CE has no modTech)
    set_role_ok = _owner_role.maybe_set_role(cur)
    cur.execute(ENUM_SQL)
    print(f"WRITE  auditaction enum value CONFORMANCE_DRIFT")
    for label, sql in _STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print(f"{_TABLE} (append-only) ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

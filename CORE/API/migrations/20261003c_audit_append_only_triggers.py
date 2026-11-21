"""GSEC R-054 (SEC-3-002) — enforce append-only on the audit tables.

ISO 27001 policy: no hard deletes; `audit_events` and
`permission_audit_logs` are append-only. The API's Vault dynamic DB
credentials could previously UPDATE/DELETE those tables (the REVOKE was
rejected: the dynamic role is a member of the table-owner role, making
REVOKE a no-op, and it breaks the first credential mint on a fresh
install before the tables exist).

This migration closes the gap at the database itself:

* ONE plpgsql function `audit_append_only_guard()` that RAISEs on any
  attempt to mutate a row of a protected table (errcode
  feature_not_supported, so clients can map it to 501);
* a `BEFORE UPDATE OR DELETE ... FOR EACH ROW` trigger and a
  `BEFORE TRUNCATE ... FOR EACH STATEMENT` trigger on BOTH
  `audit_events` and `permission_audit_logs`.

INSERT (the only legitimate operation — audit.py / audit_middleware.py
only ever append) is untouched.

Idempotent: CREATE OR REPLACE FUNCTION; each trigger pair is
DROP TRIGGER IF EXISTS + CREATE TRIGGER inside a DO block guarded by
`to_regclass(...) IS NOT NULL`, so a fresh DB that has not created the
tables yet still applies cleanly (and the mod-migrate runner's
fresh-install path never breaks).

There is intentionally no downgrade: removing these triggers would
silently drop the protection, which this task must not allow. Re-running
the file is a no-op that RE-ASSERTS the triggers.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. The lead applies with --commit.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261003c_audit_append_only_triggers.py            # dry run: prints SQL
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261003c_audit_append_only_triggers.py --commit   # apply

LEAD APPLIES — not applied by the task agent.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("audit_events", "permission_audit_logs")

GUARD_FN_SQL = """
CREATE OR REPLACE FUNCTION audit_append_only_guard()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Operation % is forbidden on append-only audit table % (ISO 27001: audit tables are append-only; UPDATE/DELETE/TRUNCATE are not allowed)',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END;
$$ LANGUAGE plpgsql
"""

_STATEMENTS = [("function audit_append_only_guard()", GUARD_FN_SQL)]
for _t in _TABLES:
    _STATEMENTS.append(
        (
            f"triggers on {_t} (row mutation + truncate)",
            f"""
            DO $$
            BEGIN
                IF to_regclass('{_t}') IS NULL THEN
                    RAISE NOTICE '{_t} does not exist yet; skipping {_t} triggers';
                    RETURN;
                END IF;
                DROP TRIGGER IF EXISTS trg_no_row_mutation_{_t} ON {_t};
                CREATE TRIGGER trg_no_row_mutation_{_t}
                    BEFORE UPDATE OR DELETE ON {_t}
                    FOR EACH ROW EXECUTE FUNCTION audit_append_only_guard();
                DROP TRIGGER IF EXISTS trg_no_truncate_{_t} ON {_t};
                CREATE TRIGGER trg_no_truncate_{_t}
                    BEFORE TRUNCATE ON {_t}
                    FOR EACH STATEMENT EXECUTE FUNCTION audit_append_only_guard();
            END
            $$
            """,
        )
    )


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
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("audit_events + permission_audit_logs append-only guard ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

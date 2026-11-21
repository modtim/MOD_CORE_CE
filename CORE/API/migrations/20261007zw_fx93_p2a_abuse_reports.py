# API/migrations/20261007zw_fx93_p2a_abuse_reports.py
"""FX.93 p2a [B] — abuse-report queue: the triage CLAIM column.

`abuse_reports` (EXISTS — created by 20261005t_fx93_abuse_reports.py,
SoftDeleteMixin) already carries the report row, the 1-business-day SLA
clock (`triage_due_at`) and the lifecycle status. FX.93 p2a adds the
queue's ownership column so a platform admin can claim a report before
triaging it (Airflow-style "assign this to me") and the queue page can
show the unclaimed pile:

    assigned_to  INTEGER NULL  (FK users.id; NULL = unclaimed)

Nullable, so every pre-existing report row keeps working unchanged.
p2c owns the transitions that set/clear it — this part ships the
column + the enum only, so the DTO and later parts can see it.

Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS + CREATE INDEX
IF NOT EXISTS. The table itself is NOT re-declared here: the repo's
runner (tools/mod_migrate.py) walks this directory in filename order,
so 20261005t (the CREATE) always runs first on a fresh DB.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zw_fx93_p2a_abuse_reports.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zw_fx93_p2a_abuse_reports.py --commit   # apply

Additive only: nothing is dropped or truncated (ISO 27001 — the queue
is soft-delete only; removal stamps `deleted_at`, the row stays).
NOT applied by this task — the lead applies with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "abuse_reports"

_STATEMENTS = [
    (
        f"{_TABLE} triage-claim column (FX.93 p2a)",
        f"""
ALTER TABLE {_TABLE}
    ADD COLUMN IF NOT EXISTS assigned_to INTEGER NULL
        REFERENCES users (id);
""",
    ),
    (
        f"indexes on {_TABLE} (claim + claim/SLA)",
        f"""
CREATE INDEX IF NOT EXISTS ix_abuse_reports_assigned_to
    ON {_TABLE} (assigned_to);
CREATE INDEX IF NOT EXISTS ix_abuse_reports_assigned_due
    ON {_TABLE} (assigned_to, triage_due_at);
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
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print(f"{_TABLE} claim column ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""FX.97 part 1 (owner decision Q83=A) — provenance report audit action.

Adds the audit-action enum value that backs
`AuditAction.PROVENANCE_REPORT_EXPORTED`:

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    PROVENANCE_REPORT_EXPORTED

No schema columns are added in part 1: the report is built from existing
rows (workflow_runs / node_runs / workflow_node_outputs /
workflow_run_files) and the fields that do not exist yet are emitted as
null under `gaps`.

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction
block, so the whole script runs on an autocommit connection (same
pattern as 20261006t_fx90_malware_scan_audit_actions.py /
20261006u_fx89_security_contact.py). Idempotent: ADD VALUE IF NOT
EXISTS.

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order, recorded in the append-only
release_steps ledger) — this file sorts after 20261006v.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007a_fx97_provenance_audit_actions.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007a_fx97_provenance_audit_actions.py --commit   # apply

models.py is bind-mounted live into the API container, so the enum value
must exist in the live `auditaction` type before any FX.97 export writes
its audit row — apply immediately after adding the member to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "auditaction.PROVENANCE_REPORT_EXPORTED value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'PROVENANCE_REPORT_EXPORTED';
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
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.97 provenance report audit action ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

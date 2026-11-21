"""FX.156 part 2 (owner decision Q143=B) — security alert ack audit
action.

Adds the audit-action enum value that backs
`AuditAction.SECURITY_ALERT_ACKED` (one row per real acknowledgement of
a platform security alert; re-acks are no-ops — no second row):

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    SECURITY_ALERT_ACKED

No schema columns are added in part 2: the ack stamps
``security_alerts.acked_at``/``acked_by`` (already created by
20261007c) and the metadata carries alert id / rule key / severity /
subject — identifiers only, never credentials/tokens/keys.

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction
block, so the whole script runs on an autocommit connection (same
pattern as 20261007a_fx97_provenance_audit_actions.py). Idempotent:
ADD VALUE IF NOT EXISTS.

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order, recorded in the append-only
release_steps ledger) — this file sorts after 20261007d.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007e_fx156_alert_ack_action.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007e_fx156_alert_ack_action.py --commit   # apply

models.py is bind-mounted live into the API container, so the enum value
must exist in the live `auditaction` type before any ack endpoint writes
its audit row — apply immediately after adding the member to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "auditaction.SECURITY_ALERT_ACKED value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_ALERT_ACKED';
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
    print("FX.156 security alert ack audit action ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

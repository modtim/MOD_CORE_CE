"""GSEC FX.151 part 1b — SSRF allowlist-fetch audit action.

Adds the audit-action enum value that backs
`models.AuditAction.SSRF_ALLOWLIST_FETCH`:

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    SSRF_ALLOWLIST_FETCH

One row is written by utils/safe_http.py whenever an outbound
server-side fetch proceeds to a target matched by the
`security.ssrf_allowlist` platform setting (the SSRF guard would
otherwise have refused the private/loopback/link-local/CGNAT address).

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction
block, so the whole script runs on an autocommit connection (same
pattern as 20261007a_fx97_provenance_audit_actions.py). Idempotent:
ADD VALUE IF NOT EXISTS.

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order, recorded in the append-only
release_steps ledger) — this file sorts after 20261007c.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007d_fx151_ssrf_audit_action.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007d_fx151_ssrf_audit_action.py --commit   # apply

models.py is bind-mounted live into the API container, so the enum value
must exist in the live `auditaction` type before any allowlisted fetch
writes its audit row — apply immediately after adding the member to
models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "auditaction.SSRF_ALLOWLIST_FETCH value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SSRF_ALLOWLIST_FETCH';
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
    print("FX.151 SSRF allowlist fetch audit action ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

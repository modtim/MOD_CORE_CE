"""GSEC FX.91 p3b — audit-action enum value for a BLOCKED node-egress attempt.

Adds the audit-action enum label that backs
`models.AuditAction.NODE_EGRESS_BLOCKED` (value `node_egress_blocked`):

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    NODE_EGRESS_BLOCKED

One row is written by services/node_egress_approval.record_blocked_egress
(reached from routers/worker_tasks.py's two FX.91 dispatch gates) whenever a
node's declared external egress is refused at the enqueue line — the gov
allowlist block (nodes.egress_gov_allowlist_enforced / an active gov profile)
or the tenant-admin approval gate (nodes.egress_approval_required, hard
refusal or the report-only WOULD-block finding). Metadata carries tenant,
node id/name, gate, profile, denied/declared/allowlist sets, the enforced +
report-only posture and the run ids — hostnames and ids only, never headers,
tokens or payload content (ISO 27001 AU-2/AU-3, append-only).

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction block, so
the whole script runs on an autocommit connection (same pattern as
20261007a_fx97_provenance_audit_actions.py / 20261007d_fx151_ssrf_audit_action.py).
Idempotent: ADD VALUE IF NOT EXISTS.

Migration runner is tools/mod_migrate.py (steps discovered as *.py in this
directory, run in filename order, recorded in the append-only release_steps
ledger) — this file sorts after 20261007zzz_fx115_p3_node_lockdown_exceptions.py.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzz_fx91_p3b_egress_block_audit.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzz_fx91_p3b_egress_block_audit.py --commit   # apply

models.py is bind-mounted live into the API container, so the enum value must
exist in the live `auditaction` type before any blocked-egress audit row
writes it — apply immediately after adding the member to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "auditaction.NODE_EGRESS_BLOCKED value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'NODE_EGRESS_BLOCKED';
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
    print("FX.91 p3b blocked-egress audit action ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

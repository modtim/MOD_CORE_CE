"""GSEC FX.119 p2a — audit-action enum labels for on-call paging outcomes.

Adds the audit-action enum labels that back
`models.AuditAction.ONCALL_PAGE_SENT` (value `oncall_page_sent`) and
`models.AuditAction.ONCALL_PAGE_FAILED` (value `oncall_page_failed`):

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    ONCALL_PAGE_SENT
    ONCALL_PAGE_FAILED

One row is written by services/paging_service.py._audit_page for every page
OUTCOME: SENT when the self-hosted ntfy push stack accepted the POST, FAILED
when paging is disabled, no channel is configured, the Vault publisher token
is missing, the POST times out, or ntfy answers 4xx/5xx. Metadata carries the
page title excerpt, topic, ntfy priority, the monitor's reason and the HTTP
status — identifiers and outcomes only, never the publisher token and never a
full page body (ISO 27001 AU-2/AU-3, append-only; no hard deletes).

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction block, so
the whole script runs on an autocommit connection (same pattern as
20261007zn_fx58_p2b_restore_evidence_audit_action.py /
20261007zzz_fx91_p3b_egress_block_audit.py). Idempotent: ADD VALUE IF NOT
EXISTS. Statements stay literal one-liners so
tests/test_audit_action_enum_sync.py can discover them by regex.

Migration runner is tools/mod_migrate.py (steps discovered as *.py in this
directory, run in filename order, recorded in the append-only release_steps
ledger) — this file sorts after 20261007zzza_fx112_p3a_tenant_security_alert_rules.py.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzb_fx119_p2a_oncall_page_audit_actions.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzb_fx119_p2a_oncall_page_audit_actions.py --commit   # apply

models.py is bind-mounted live into the API container, so the enum values must
exist in the live `auditaction` type before any paging audit row writes them —
apply immediately after adding the members to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

SYNC_STATEMENTS = [
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'ONCALL_PAGE_SENT'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'ONCALL_PAGE_FAILED'",
]

_STATEMENTS = [
    (
        "auditaction ONCALL_PAGE_SENT + ONCALL_PAGE_FAILED values",
        ";\n".join(SYNC_STATEMENTS) + ";",
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
        for stmt in (s.strip() for s in sql.split(";") if s.strip()):
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.119 p2a on-call paging audit actions ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

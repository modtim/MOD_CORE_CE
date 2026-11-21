"""GSEC FX.58 p2b — add the restore-test audit-evidence action to the
`auditaction` Postgres enum.

models.AuditAction gains BACKUP_RESTORE_TEST_EVIDENCE (services/restore_test_job
.py writes one append-only PermissionAuditLog row per restore-test attempt,
ISO 27001 AU-2/AU-3). PermissionAuditLog.action is `Enum(AuditAction)`, which
stores the enum NAME, so the Python member needs the matching Postgres label —
without it every evidence insert raises `invalid input value for enum
auditaction: "BACKUP_RESTORE_TEST_EVIDENCE"`.

Convention copied from 20261004b_audit_action_enum_sync.py and the SSO/SCIM
migrations: one literal `ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '<NAME>'`
per value, executed on an autocommit connection (ADD VALUE cannot run inside a
transaction block). Idempotent — a re-run is a no-op. Additive only; nothing is
dropped and no audit row is deleted (ISO 27001: append-only).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261007zn_fx58_p2b_restore_evidence_audit_action.py          # dry-run
    docker exec my_fastapi_app python3 \\
        migrations/20261007zn_fx58_p2b_restore_evidence_audit_action.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

ENUM_NAME = "auditaction"

# Literal statements on ONE line each: tests/test_audit_action_enum_sync.py
# discovers them by regex, so they must stay literal (no implicit
# concatenation, no variable-built template).
SYNC_STATEMENTS = [
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'BACKUP_RESTORE_TEST_EVIDENCE'",
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        for stmt in SYNC_STATEMENTS:
            print(stmt + ";")
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    for stmt in SYNC_STATEMENTS:
        cur.execute(stmt)
    cur.close()
    conn.close()
    print(f"Added {len(SYNC_STATEMENTS)} FX.58 p2b value(s) to the "
          f"{ENUM_NAME} enum.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

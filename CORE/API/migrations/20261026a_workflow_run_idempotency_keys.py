#!/usr/bin/env python3
"""2400GX-n8 (30.4.8) -- NEW table `workflow_run_idempotency_keys`: maps
(tenant_id, workflow_version_id, idem_key) -> workflow_run_id so a replayed
POST /workflows/workflow-versions/{id}/run returns the same run.

revision      : 20261026a_workflow_run_idempotency_keys
down_revision : 20261013k_workflow_differentiable_column

No ORM model (raw-SQL table, queried via services/run_idempotency.py), so no
models.py edit is needed. Additive only; no data rewrite. Idempotent:
CREATE ... IF NOT EXISTS. Reversible: `--downgrade --commit` runs
DROP TABLE IF EXISTS (key rows are dev-infra metadata; runs are untouched).

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane -- the lead applies it:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261026a_workflow_run_idempotency_keys.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "workflow_run_idempotency_keys table",
        """
CREATE TABLE IF NOT EXISTS workflow_run_idempotency_keys (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    workflow_version_id INTEGER NOT NULL,
    idem_key VARCHAR(255) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    workflow_run_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    ),
    (
        "unique scope index (tenant_id, workflow_version_id, idem_key)",
        """
CREATE UNIQUE INDEX IF NOT EXISTS uq_wf_run_idem_scope
    ON workflow_run_idempotency_keys (tenant_id, workflow_version_id, idem_key);
""",
    ),
    (
        "created_at index (retention purge)",
        """
CREATE INDEX IF NOT EXISTS ix_wf_run_idem_created_at
    ON workflow_run_idempotency_keys (created_at);
""",
    ),
]


_DOWNGRADE = "DROP TABLE IF EXISTS workflow_run_idempotency_keys;"


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the DDL. Without this flag, dry-run only.")
    p.add_argument("--downgrade", action="store_true",
                   help="Drop the table (dry-run unless --commit).")
    args = p.parse_args()

    if args.downgrade:
        if not args.commit:
            print("DRY  would: " + _DOWNGRADE)
            return 0
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(_DOWNGRADE)
        cur.close()
        conn.close()
        print("OK   downgrade: workflow_run_idempotency_keys dropped.")
        return 0

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade available: --downgrade [--commit]")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print("2400GX-n8 ready: workflow_run_idempotency_keys exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

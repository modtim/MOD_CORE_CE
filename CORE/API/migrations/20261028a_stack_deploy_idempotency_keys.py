#!/usr/bin/env python3
"""2400GX-c3-J4c (30.6.3, S11 MUST) -- NEW table `stack_deploy_idempotency_keys`:
maps (tenant_id, stack_id, idem_key) -> (request_hash, revision_id) so a replayed
POST /v1/stacks/{id}/deploy with the same key + same normalized body returns the
stack instead of queueing a second deploy task.

revision      : 20261028a_stack_deploy_idempotency_keys
down_revision : 20261027d_workflow_slug_graph_hash

Also ALTERs the forever-retained `persistent_stack_revision` rows with the two
additive credential columns (30.6.3 c3-J4c design :44): `idempotency_key` (the
raw deploy credential, so /revert can name+undo a credential-driven deploy) and
`key_id` (the deploy credential's ``ApiKey.id``, same value
services/stack_deploy_token.py:132 records as ``api_key_id``; a PLAIN integer,
NOT a ForeignKey, so a revoked/soft-deleted key can never null or reap a
forever-retained revision row).

No ORM model for the ledger table (raw-SQL, queried with plain SQL from
services/persistent_stacks_domain_service.py, exactly like
``workflow_run_idempotency_keys`` / services/run_idempotency.py). Additive only:
no existing column or index is changed, every step is IF NOT EXISTS, no backfill,
no UPDATE. Idempotent: re-running is a no-op. Reversible: `--downgrade --commit`
drops the ledger table + the two columns (key rows are dev-infra metadata;
revision rows themselves are never touched).

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the lane
-- the lead applies it:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261028a_stack_deploy_idempotency_keys.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "persistent_stack_revision.idempotency_key column",
        """
ALTER TABLE persistent_stack_revision
    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64);
""",
    ),
    (
        "persistent_stack_revision.key_id column (plain integer, NOT a FK)",
        """
ALTER TABLE persistent_stack_revision
    ADD COLUMN IF NOT EXISTS key_id INTEGER;
""",
    ),
    (
        "stack_deploy_idempotency_keys table",
        """
CREATE TABLE IF NOT EXISTS stack_deploy_idempotency_keys (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    stack_id INTEGER NOT NULL,
    idem_key VARCHAR(64) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    revision_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    ),
    (
        "unique scope index (tenant_id, stack_id, idem_key)",
        """
CREATE UNIQUE INDEX IF NOT EXISTS uq_stack_deploy_idem_scope
    ON stack_deploy_idempotency_keys (tenant_id, stack_id, idem_key);
""",
    ),
    (
        "created_at index (retention purge)",
        """
CREATE INDEX IF NOT EXISTS ix_stack_deploy_idem_created_at
    ON stack_deploy_idempotency_keys (created_at);
""",
    ),
    (
        "idempotency_key index on the revision table",
        """
CREATE INDEX IF NOT EXISTS ix_persistent_stack_revision_idempotency_key
    ON persistent_stack_revision (idempotency_key);
""",
    ),
]


_DOWNGRADE = """
DROP TABLE IF EXISTS stack_deploy_idempotency_keys;
ALTER TABLE persistent_stack_revision
    DROP COLUMN IF EXISTS idempotency_key;
ALTER TABLE persistent_stack_revision
    DROP COLUMN IF EXISTS key_id;
"""


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
                   help="Drop the table + columns (dry-run unless --commit).")
    args = p.parse_args()

    if args.downgrade:
        if not args.commit:
            print("DRY  would: " + _DOWNGRADE)
            return 0
        conn = _get_conn()
        cur = conn.cursor()
        for stmt in [s.strip() for s in _DOWNGRADE.split(";") if s.strip()]:
            try:
                cur.execute(stmt + ";")
            except Exception as e:  # noqa: BLE001
                print(f"FAIL downgrade: {e}")
                return 1
        cur.close()
        conn.close()
        print("OK   downgrade: stack_deploy_idempotency_keys + revision columns dropped.")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())

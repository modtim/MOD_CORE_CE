#!/usr/bin/env python3
"""sz-nice-to-haves.4 (sz-nice-to-haves decomp) — the ONE new table:
PERSISTENT STACK BACKUP CONFIG (Service Wizard backup posture per
persistent stack).

revision      : 20261010f_persistent_stack_backup_config
down_revision : 20261010e_workflow_budgets

Purpose per the decomp row: records a persistent stack's backup
posture — logical-dump vs raw-volume vs none — with a `requires_ack`
flag for destructive/lossy postures (e.g. "raw_volume" or "none" may
need an explicit human ack before the stack can go live). This
migration only creates storage; no `services/service_backup.py`
writer/reader logic is wired here — that is a separate, later task.

Columns, per the sz-nice-to-haves.4 decomp row:
    id                   BIGSERIAL PRIMARY KEY
    persistent_stack_id  INTEGER NOT NULL REFERENCES persistent_stacks (id)
                         ON DELETE CASCADE — a backup posture row has no
                         meaning once its stack is hard-deleted.
    backup_mode          VARCHAR(20) NOT NULL DEFAULT 'logical_dump'
                         (values: logical_dump, raw_volume, none)
    requires_ack         BOOLEAN NOT NULL DEFAULT false
    acked_by_user_id     INTEGER REFERENCES users (id) ON DELETE SET NULL
    acked_at             TIMESTAMPTZ
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()

Unique constraint on (persistent_stack_id): one backup-posture row per
stack.

No secret values are ever stored here. If a future backup target needs
credentials, those belong in Vault and this table would hold only a
path reference (matching `persistent_stacks.secret_refs`), never a
plaintext key/secret column. Nothing of that sort is added in this
migration.

Indexes: none beyond the unique constraint (one row per stack, looked
up by persistent_stack_id which the unique constraint already indexes).

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.

Idempotent: CREATE TABLE IF NOT EXISTS; safe to re-run. Additive only:
no drops, no ALTER on existing tables. The matching models.py ORM
class (`PersistentStackBackupConfig`) is added alongside this
migration in the same task.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB
by the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010f_persistent_stack_backup_config.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "persistent_stack_backup_config"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-nice-to-haves.4, per-persistent-stack "
        "backup posture: logical_dump/raw_volume/none + requires_ack)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    persistent_stack_id INTEGER NOT NULL REFERENCES persistent_stacks (id)
        ON DELETE CASCADE,
    backup_mode VARCHAR(20) NOT NULL DEFAULT 'logical_dump',
    requires_ack BOOLEAN NOT NULL DEFAULT false,
    acked_by_user_id INTEGER REFERENCES users (id) ON DELETE SET NULL,
    acked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_{_TABLE}_stack UNIQUE (persistent_stack_id),
    CONSTRAINT ck_{_TABLE}_mode CHECK (
        backup_mode IN ('logical_dump', 'raw_volume', 'none')
    )
);
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
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the CREATE TABLE step. Without this flag, "
                        "dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (this migration only creates "
              "storage; a later task owns any teardown)")
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
    print(
        "sz-nice-to-haves.4 ready: persistent_stack_backup_config "
        "exists; a later task wires services/service_backup.py "
        "read/write logic against it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

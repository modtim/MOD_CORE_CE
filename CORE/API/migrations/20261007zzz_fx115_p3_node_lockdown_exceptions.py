#!/usr/bin/env python3
"""FX.115 p3 (owner decision Q102=A follow-through) — per-node lockdown
exceptions: the NEW table.

models.NodeLockdownException (services/node_lockdown_report.py derives the
worker's effective exempt list from it, routers/node_lockdown_exceptions.py
creates/revokes rows) is the auditable replacement for the blunt
``worker.node_lockdown_exempt_nodes`` allow-list: ONE live row per
(node_kind, flag) saying "this node kind relaxes THIS lockdown flag, for
THIS reason, decided by THIS admin, until THEN". The allow-list carries no
reason, no author, and no expiry — which is exactly what a compliance audit
needs to see — so the exception becomes a record, and the legacy setting
stays as a union fallback for one release.

    node_lockdown_exceptions
      id                    BIGSERIAL PRIMARY KEY
      node_kind             VARCHAR(250) NOT NULL   TaskPayload.node_kind
      flag                  VARCHAR(32)  NOT NULL   nonroot|capdrop|noprivs|
                                                  seccomp|rootfs|scratch_tmpfs|
                                                  limits_pids|limits_memory|
                                                  limits_cpu
      reason                TEXT         NOT NULL   REQUIRED justification
      created_by_user_id    INTEGER      NULL       FK users.id (the admin)
      created_at            TIMESTAMPTZ  NOT NULL   server_default now()
      expires_at            TIMESTAMPTZ  NULL       NULL = permanent; a live
                                                  row past its expiry is
                                                  IGNORED, not deleted
      deleted_by_user_id    INTEGER      NULL       FK users.id (revoker)
      deleted_at            TIMESTAMPTZ  NULL       soft-delete tombstone

Latest-wins per (node_kind, flag) via a PARTIAL unique index on
``deleted_at IS NULL`` (the node_egress_approvals idiom), so a revoke
tombstone never squats the live key and re-creating refreshes in place.
ISO 27001: NO hard delete — revoke stamps deleted_at + deleted_by_user_id.

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE ... IF NOT EXISTS on every
index. Nothing existing is altered, dropped, or truncated — additive only.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzz_fx115_p3_node_lockdown_exceptions.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzz_fx115_p3_node_lockdown_exceptions.py --commit   # apply

NOT applied by the p3 task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "node_lockdown_exceptions"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.115 p3)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    node_kind VARCHAR(250) NOT NULL,
    flag VARCHAR(32) NOT NULL,
    reason TEXT NOT NULL,
    created_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NULL,
    deleted_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    deleted_at TIMESTAMPTZ NULL
);
""",
    ),
    (
        f"{_TABLE} node_kind index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_node_kind
    ON {_TABLE} (node_kind);
""",
    ),
    (
        f"{_TABLE} flag index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_flag
    ON {_TABLE} (flag);
""",
    ),
    (
        f"{_TABLE} created_at index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_created_at
    ON {_TABLE} (created_at);
""",
    ),
    (
        f"{_TABLE} expires_at index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_expires_at
    ON {_TABLE} (expires_at);
""",
    ),
    (
        f"{_TABLE} tombstone index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at
    ON {_TABLE} (deleted_at);
""",
    ),
    (
        f"{_TABLE} one live exception per (node_kind, flag)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live_kind_flag
    ON {_TABLE} (node_kind, flag)
    WHERE deleted_at IS NULL;
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
        print(f"DID  {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

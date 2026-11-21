#!/usr/bin/env python3
"""hx.15 (worker-host-exec decomp, D2): per-worker access policy +
explicit user grants for host-exec jobs.

revision      : 20261010j_worker_host_exec_access
down_revision : 20261010i_worker_host_exec_jobs

Purpose per `the internal design notes` D2: this
migration only creates the two storage tables for "who may submit
host-exec jobs against a given worker" (policy) and "which specific
users were granted/revoked access" (grants). Wiring into
`services/worker_host_exec_service.py` (hx.5, owned by another agent)
is a separate follow-up -- see models.py docstrings on
`WorkerHostExecPolicy` / `WorkerHostExecGrant` for the stub name.

Tables:
    worker_host_exec_policies
        id                  BIGSERIAL PRIMARY KEY
        worker_id           INTEGER NOT NULL REFERENCES worker_licenses
                            (id) (plain FK, no ON DELETE action)
        access              VARCHAR(16) NOT NULL DEFAULT 'admins',
                            CHECK IN ('admins','grants','all_members')
        updated_by_user_id  INTEGER REFERENCES users (id) (plain FK,
                            no ON DELETE action, nullable)
        deleted_at          TIMESTAMPTZ (soft-delete, ISO 27001: never
                            a hard DELETE)
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    Partial unique index on worker_id WHERE deleted_at IS NULL (one
    live policy per worker; a soft-deleted policy frees the worker up
    for a fresh row).

    worker_host_exec_grants
        id                   BIGSERIAL PRIMARY KEY
        worker_id            INTEGER NOT NULL REFERENCES
                             worker_licenses (id) (plain FK, no ON
                             DELETE action)
        grantee_user_id      INTEGER NOT NULL REFERENCES users (id)
                             (plain FK, no ON DELETE action)
        granted_by_user_id   INTEGER NOT NULL REFERENCES users (id)
                             (plain FK, no ON DELETE action)
        allow_root           BOOLEAN NOT NULL DEFAULT false
        created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        revoked_at           TIMESTAMPTZ (null = still active; revoke
                             = set this, never delete the row)
        revoked_by_user_id   INTEGER REFERENCES users (id) (plain FK,
                             no ON DELETE action, nullable)
    Partial unique index on (worker_id, grantee_user_id) WHERE
    revoked_at IS NULL (one active grant per worker+grantee; a revoked
    grantee can be re-granted later without a uniqueness clash).

`id` is BIGSERIAL PRIMARY KEY on both tables, never a max()-subquery
DEFAULT.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. Append-only (ISO
27001): revoke/soft-delete columns only, no DELETE statements.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010j_worker_host_exec_access.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_T_POLICIES = "worker_host_exec_policies"
_T_GRANTS = "worker_host_exec_grants"

_STATEMENTS = [
    (
        f"{_T_POLICIES} table (hx.15, one live policy row per worker)",
        f"""
CREATE TABLE IF NOT EXISTS {_T_POLICIES} (
    id BIGSERIAL PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES worker_licenses (id),
    access VARCHAR(16) NOT NULL DEFAULT 'admins'
        CHECK (access IN ('admins','grants','all_members')),
    updated_by_user_id INTEGER REFERENCES users (id),
    deleted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ),
    (
        f"{_T_POLICIES} partial unique index on worker_id (live rows)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS ux_{_T_POLICIES}_worker
    ON {_T_POLICIES} (worker_id)
    WHERE deleted_at IS NULL;
""",
    ),
    (
        f"{_T_GRANTS} table (hx.15, explicit per-user access grants)",
        f"""
CREATE TABLE IF NOT EXISTS {_T_GRANTS} (
    id BIGSERIAL PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES worker_licenses (id),
    grantee_user_id INTEGER NOT NULL REFERENCES users (id),
    granted_by_user_id INTEGER NOT NULL REFERENCES users (id),
    allow_root BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    revoked_by_user_id INTEGER REFERENCES users (id)
);
""",
    ),
    (
        f"{_T_GRANTS} partial unique index on (worker_id, grantee_user_id) "
        "(active grants)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS ux_{_T_GRANTS}_worker_grantee
    ON {_T_GRANTS} (worker_id, grantee_user_id)
    WHERE revoked_at IS NULL;
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
                   help="Apply the CREATE TABLE + index steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (append-only per ISO 27001; "
              "policies soft-delete and grants revoke, never a DELETE)")
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
        "hx.15 ready: worker_host_exec_policies + worker_host_exec_grants "
        "exist; services/worker_host_exec_service.py's "
        "_worker_access_policy stub can now be replaced with real reads "
        "of these tables (separate hx.5 follow-up)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""hx.1 (worker-host-exec decomp, D1-D4) -- the ONE new table:
WORKER HOST EXEC JOBS (one row per host-exec request dispatched to a
worker's host-helper socket).

revision      : 20261010i_worker_host_exec_jobs
down_revision : 20261010h_fortress_22_hsm_key_metadata

NOTE on filename: the task brief named this
`20261010h_worker_host_exec_jobs.py`, but `20261010h` was already taken
by `20261010h_fortress_22_hsm_key_metadata.py` by the time this ran. This
migration is `20261010i_*` and chains after `20261010h_fortress_22_*`
instead, to avoid clobbering that file.

Purpose per `the internal design notes`
D1-D4: this migration only creates storage for the signed host-exec job
record; the host-helper socket service and API endpoints that write/read
it are separate (later) hx tasks.

Columns:
    id                  BIGSERIAL PRIMARY KEY
    tenant_id           INTEGER REFERENCES tenants (id) (nullable, plain
                        FK -- no ON DELETE action; this is an
                        append-only ISO 27001 audit record and must
                        never be anonymised/cascaded away) -- platform
                        workers have no owning tenant.
    worker_id           INTEGER NOT NULL REFERENCES worker_licenses (id)
                        (plain FK -- no ON DELETE action; workers are
                        soft-deleted, so RESTRICT-by-default is safe and
                        preserves the audit trail) -- matches the table
                        the Worker model (`WorkerLicense`, table
                        `worker_licenses`) already uses elsewhere (see
                        `claimed_by_worker_id` on ApiTaskQueue).
    actor_user_id       INTEGER REFERENCES users (id) (nullable, plain
                        FK -- no ON DELETE action; users are
                        soft-deleted, so the actor reference must stay
                        intact for audit, never SET NULL).
    argv_sha256         VARCHAR(64) NOT NULL -- integrity hash of the
                        real argv sent to the worker.
    argv_redacted       JSONB NOT NULL -- secret-redacted argv, for
                        audit/display only (D3: no command allowlist).
    cwd                 TEXT
    run_as              VARCHAR(64) NOT NULL -- no DB default: "the
                        worker's service user" is worker-specific and
                        resolved by the API before insert; 'root' is
                        allowed (D4).
    timeout_s           INTEGER
    status              VARCHAR(16) NOT NULL DEFAULT 'queued', CHECK IN
                        ('queued','running','succeeded','failed',
                        'timeout','cancelled')
    exit_code           INTEGER
    started_at          TIMESTAMPTZ
    finished_at         TIMESTAMPTZ
    output_object_key   TEXT
    output_truncated    BOOLEAN NOT NULL DEFAULT false
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()

Indexes:
    ix_worker_host_exec_jobs_worker_created on (worker_id, created_at)
    ix_worker_host_exec_jobs_actor on (actor_user_id)

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. Append-only (ISO
27001): no delete path here -- jobs age out via retention tooling only.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010i_worker_host_exec_jobs.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "worker_host_exec_jobs"

_STATEMENTS = [
    (
        f"{_TABLE} table (hx.1, one row per host-exec request)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants (id),
    worker_id INTEGER NOT NULL REFERENCES worker_licenses (id),
    actor_user_id INTEGER REFERENCES users (id),
    argv_sha256 VARCHAR(64) NOT NULL,
    argv_redacted JSONB NOT NULL,
    cwd TEXT,
    run_as VARCHAR(64) NOT NULL,
    timeout_s INTEGER,
    status VARCHAR(16) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','succeeded','failed','timeout','cancelled')),
    exit_code INTEGER,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    output_object_key TEXT,
    output_truncated BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ),
    (
        f"{_TABLE} index (worker_id, created_at)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_worker_created
    ON {_TABLE} (worker_id, created_at);
""",
    ),
    (
        f"{_TABLE} index (actor_user_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_actor
    ON {_TABLE} (actor_user_id);
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
        print("DRY  downgrade: no-op (append-only per ISO 27001; jobs "
              "age out via retention tooling, never a DELETE)")
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
        "hx.1 ready: worker_host_exec_jobs exists; later hx tasks "
        "(host-helper socket + API endpoints) can write and read "
        "signed host-exec job rows."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

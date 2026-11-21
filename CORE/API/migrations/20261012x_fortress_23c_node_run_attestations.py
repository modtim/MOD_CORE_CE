#!/usr/bin/env python3
"""fortress-2.3c S2a — node_run_attestations (worker per-step signatures, append-only).

revision      : 20261012x_fortress_23c_node_run_attestations
down_revision : 20261012w_fortress_55_submission_packages

Fortress 2.3c ("worker signs the per-step digest; the platform signs the
bundle that carries those signatures"). This slice is the TABLE only: the
API ingest (routers/worker_tasks.py) and the models.py ORM class are other
slices / lead-only, so nothing here declares or imports a model.

``node_run_attestations`` — one row per worker attestation (per node-run
attempt). The row is the durable evidence that a specific worker signed a
specific step digest with a specific public key half; ``contribution`` is
exactly what that signature covers. ``key_id`` is a 16-hex LABEL, never a
key; ``public_key_b64`` is the EPHEMERAL PUBLIC half only — no private
material is ever stored here.

FK column types are INTEGER, not BIGINT: models.py (read-only) declares
``tenants.id`` (models.py:346), ``workflow_runs.id`` (models.py:2285),
``node_runs.id`` (models.py:3641) and ``api_task_queue.id`` (models.py:8214)
all as ``Column(Integer, primary_key=True)``. A BIGINT FK into an INTEGER
PK is rejected by Postgres, so every FK here is INTEGER.

Every FK is NOT NULL, so every FK carries ``ON DELETE RESTRICT`` explicitly:
a hard delete of a parent (tenant / run / node_run / task) must NOT silently
drop the durable attestation. ``worker_name`` is TEXT NOT NULL with NO FK
(worker_licenses.worker_name is not a PK target; the name is free text).

ISO 27001 — append-only: NO UPDATE, NO DELETE, NO TRUNCATE. The guard is the
SHARED generic trigger function from 20261012q_fortress_13_access_denial_logs
(``fortress_13_append_only_guard``): it takes no arguments, reports TG_OP +
TG_TABLE_NAME and RAISEs with errcode feature_not_supported, so it is
re-usable verbatim. We re-declare it with CREATE OR REPLACE (identical body,
so a re-run is a no-op and the fortress-1.3 tables keep the same guard) and
attach the same trigger PAIR: BEFORE UPDATE OR DELETE FOR EACH ROW plus
BEFORE TRUNCATE FOR EACH STATEMENT. Both triggers are ``ENABLE ALWAYS`` so
they fire even under the test harness's session_replication_role=replica
(same posture as 20261012v_fortress_59_workflow_certifications.py).

``id`` is ``BIGSERIAL PRIMARY KEY`` — never a max()-subquery DEFAULT (Postgres
rejects subqueries in a DEFAULT clause).

Fully idempotent: CREATE TABLE / CREATE ... INDEX all IF NOT EXISTS, CREATE
OR REPLACE FUNCTION, DROP TRIGGER IF EXISTS + CREATE TRIGGER inside a
to_regclass existence check, so a re-run is a no-op. Additive only.

DRY-RUN by default; ``--commit`` applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012x_fortress_23c_node_run_attestations.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "node_run_attestations"
_GUARD_FN = "fortress_13_append_only_guard"
_UQ = "uq_node_run_attestations_run_task"
_IX_RUN = "ix_node_run_attestations_run_node"
_IX_TENANT = "ix_node_run_attestations_tenant_created"


def _sql_statements() -> list[tuple[str, str]]:
    """Ordered (label, sql) steps; idempotent."""
    steps = []
    steps.append((
        f"{_TABLE} table (fortress 2.3c, worker attestations)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    workflow_run_id INTEGER NOT NULL REFERENCES workflow_runs (id) ON DELETE RESTRICT,
    node_run_id INTEGER NOT NULL REFERENCES node_runs (id) ON DELETE RESTRICT,
    task_id INTEGER NOT NULL REFERENCES api_task_queue (id) ON DELETE RESTRICT,
    worker_name TEXT NOT NULL,
    alg TEXT NOT NULL,
    key_id TEXT NOT NULL,
    public_key_b64 TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    signature_b64 TEXT NOT NULL,
    contribution JSONB NOT NULL,
    signed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_node_run_attestations_alg
        CHECK (alg = 'ed25519'),
    CONSTRAINT ck_node_run_attestations_sha256_length
        CHECK (LENGTH (payload_sha256) = 64)
);
""",
    ))
    steps.append((
        f"{_TABLE} UNIQUE {_UQ} (node_run_id, task_id) — one attestation per attempt",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_UQ}
    ON {_TABLE} (node_run_id, task_id);
""",
    ))
    steps.append((
        f"{_TABLE} index {_IX_RUN} (workflow_run_id, node_run_id)",
        f"""
CREATE INDEX IF NOT EXISTS {_IX_RUN}
    ON {_TABLE} (workflow_run_id, node_run_id);
""",
    ))
    steps.append((
        f"{_TABLE} index {_IX_TENANT} (tenant_id, created_at DESC)",
        f"""
CREATE INDEX IF NOT EXISTS {_IX_TENANT}
    ON {_TABLE} (tenant_id, created_at DESC);
""",
    ))
    steps.append((
        f"function {_GUARD_FN}() (shared append-only guard, reused from fortress 1.3)",
        f"""
CREATE OR REPLACE FUNCTION {_GUARD_FN}()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Operation % is forbidden on append-only audit table % (ISO 27001: audit tables are append-only; UPDATE/DELETE/TRUNCATE are not allowed)',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END;
$$ LANGUAGE plpgsql
""",
    ))
    steps.append((
        f"append-only triggers on {_TABLE} (row mutation + truncate, ENABLE ALWAYS)",
        f"""
DO $$
BEGIN
    IF to_regclass('{_TABLE}') IS NULL THEN
        RAISE NOTICE '{_TABLE} does not exist yet; skipping {_TABLE} triggers';
        RETURN;
    END IF;
    DROP TRIGGER IF EXISTS trg_no_row_mutation_{_TABLE} ON {_TABLE};
    CREATE TRIGGER trg_no_row_mutation_{_TABLE}
        BEFORE UPDATE OR DELETE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {_GUARD_FN}();
    ALTER TABLE {_TABLE}
        ENABLE ALWAYS TRIGGER trg_no_row_mutation_{_TABLE};
    DROP TRIGGER IF EXISTS trg_no_truncate_{_TABLE} ON {_TABLE};
    CREATE TRIGGER trg_no_truncate_{_TABLE}
        BEFORE TRUNCATE ON {_TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION {_GUARD_FN}();
    ALTER TABLE {_TABLE}
        ENABLE ALWAYS TRIGGER trg_no_truncate_{_TABLE};
END
$$
""",
    ))
    return steps


# Module-level alias so the test can replay the upgrade without calling the
# builder (idiom: 20261012q_fortress_13_access_denial_logs.py).
_STATEMENTS: list[tuple[str, str]] = _sql_statements()


def _apply(execute) -> list[str]:
    """Run every step through ``execute(sql)``; return the labels applied.

    Kept separate from ``main()`` so the test can drive it with the conftest
    ``db_session`` (never the live DB engine).
    """
    applied = []
    for label, sql in _STATEMENTS:
        # ONE whole statement per entry: the plpgsql bodies and the DO block
        # contain semicolons, so this must not be split on ';'.
        execute(sql)
        applied.append(label)
    return applied


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
                   help="Apply the CREATE TABLE + index + trigger steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _sql_statements():
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (ISO 27001 — attestations are never dropped; "
              "the guard refuses UPDATE/DELETE/TRUNCATE)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    try:
        _apply(cur.execute)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL apply: {e}")
        return 1
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "Fortress 2.3c ready: node_run_attestations exists, append-only "
        "(UPDATE/DELETE/TRUNCATE RAISE via "
        f"{_GUARD_FN}), one attestation per (node_run_id, task_id), "
        "alg locked to ed25519."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

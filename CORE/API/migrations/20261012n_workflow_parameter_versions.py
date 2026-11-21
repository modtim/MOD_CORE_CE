#!/usr/bin/env python3
"""expr 10.4 (workflow-expressiveness-primitives decomp, §10.4) -- NEW table:
WORKFLOW PARAMETER VERSIONS (one row per parameter set a training step produced,
or that a rollback / manual edit created).

revision      : 20261012n_workflow_parameter_versions
down_revision : 20261012m_adversary_findings_run_unique
                (task 496 is writing that migration RIGHT NOW as
                 migrations/20261012m_adversary_findings_run_unique.py; this
                 chain slot depends on it -- apply m before n.)

Purpose: the differentiable-programs spec ("workflow_parameter_versions write
per training step") says every training step SHALL persist the resulting
per-node parameter values as a new versioned parameter set keyed by
(workflow_version_id, training_step_run_id), the tenant SHALL be able to roll
back to any prior parameter version, and non-training runs SHALL use the LATEST
parameter version. This migration lands exactly that storage. No writer/reader
code is wired here -- same posture as its sibling migrations (storage first,
service later).

Columns:
    workflow_parameter_versions
        id                  BIGSERIAL PRIMARY KEY (never a max()-subquery
                            DEFAULT -- Postgres rejects subqueries in DEFAULT)
        tenant_id           INTEGER NOT NULL REFERENCES tenants (id) -- the
                            owning tenant; every query filters on it.
        workflow_id         INTEGER NOT NULL REFERENCES workflows (id)
        workflow_version_id INTEGER NOT NULL REFERENCES workflow_versions (id)
                            -- the graph this parameter set belongs to (the
                            spec's key, first half).
        training_step_run_id INTEGER NULL REFERENCES workflow_runs (id) ON
                            DELETE SET NULL -- the training-step run that
                            produced this set (the spec's key, second half);
                            NULL = a rollback-created or manually-edited
                            version, which no run created.
        version_number      INTEGER NOT NULL -- monotonic per workflow_version
                            (rollback/manual sets keep counting forward so a
                            rollback never re-uses a number).
        parameters          JSONB NOT NULL -- the per-node parameter values
                            (the gradient-applied snapshot).
        parent_version_id   BIGINT NULL FK workflow_parameter_versions (id) ON
                            DELETE SET NULL -- which set this one was derived
                            from (the rollback / edit lineage); NULL = the
                            first set for this workflow_version.
        source              VARCHAR(16) NOT NULL CHECK in ('training_step',
                            'rollback', 'manual') -- how the row came to exist.
        created_by          INTEGER NULL REFERENCES users (id) ON DELETE SET
                            NULL -- the user who triggered a rollback/manual
                            set (NULL = created by a training step).
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        deleted_at          TIMESTAMPTZ NULL -- soft delete only (ISO 27001).

NO `ON DELETE CASCADE` anywhere: parameter history is training history, so
deleting a workflow / version / run / user must never drop the rows that record
what its parameters were (the FKs that could fire carry ON DELETE SET NULL).

Indexes:
    uq_workflow_parameter_versions_version_number UNIQUE on
        (workflow_version_id, version_number) -- one row per number per graph.
    uq_workflow_parameter_versions_step partial UNIQUE on
        (workflow_version_id, training_step_run_id) WHERE
        training_step_run_id IS NOT NULL -- the spec's key: at most one
        parameter set per (graph, training step).
    ix_workflow_parameter_versions_tenant_workflow_created on
        (tenant_id, workflow_id, created_at DESC) -- the tenant's parameter
        timeline per workflow (latest-version lookup for non-training runs).

The table ALREADY EXISTS on any DB that ran 20261010d_workflow_parameter_
versions (the earlier copy: id/workflow_id[ON DELETE CASCADE]/
version_id UUID/parameters/training_step_id UUID/created_at) and models.py
already carries that shape, so the CREATE above is a no-op there and the ALTER
below adds the spec's key + lineage columns beside it (ADD COLUMN IF NOT EXISTS
x8, one ALTER statement; the NOT NULLs are safe because nothing writes the
table yet -- the orchestrator write path is a later task). The earlier shape's
UUID version_id/training_step_id columns stay: they are the not-yet-built
training loop's own ids, untouched.

Idempotent: CREATE TABLE / ALTER ADD COLUMN / CREATE [UNIQUE] INDEX IF NOT
EXISTS;
safe to re-run. Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012n_workflow_parameter_versions.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflow_parameter_versions"

_STATEMENTS = [
    (
        f"{_TABLE} table (expr 10.4, one row per parameter set a training "
        "step / rollback / manual edit produced)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    workflow_id INTEGER NOT NULL REFERENCES workflows (id),
    workflow_version_id INTEGER NOT NULL REFERENCES workflow_versions (id),
    training_step_run_id INTEGER REFERENCES workflow_runs (id) ON DELETE SET NULL,
    version_number INTEGER NOT NULL,
    parameters JSONB NOT NULL,
    parent_version_id BIGINT REFERENCES {_TABLE} (id) ON DELETE SET NULL,
    source VARCHAR(16) NOT NULL
        CONSTRAINT ck_{_TABLE}_source
        CHECK (source IN ('training_step', 'rollback', 'manual')),
    created_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
""",
    ),
    (
        f"{_TABLE} columns for a DB that already carries the earlier sz-expr3"
        "5 copy (id/workflow_id/version_id/parameters/training_step_id/"
        "created_at): the spec's key + lineage columns are added, never "
        "re-placed (bind-mount hazard: models.py already has a class for this "
        "table, so a fresh CREATE is a no-op there)",
        f"""
ALTER TABLE {_TABLE}
    ADD COLUMN IF NOT EXISTS tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    ADD COLUMN IF NOT EXISTS workflow_version_id INTEGER NOT NULL
        REFERENCES workflow_versions (id),
    ADD COLUMN IF NOT EXISTS training_step_run_id INTEGER
        REFERENCES workflow_runs (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS version_number INTEGER NOT NULL,
    ADD COLUMN IF NOT EXISTS parent_version_id BIGINT
        REFERENCES {_TABLE} (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL
        CONSTRAINT ck_{_TABLE}_source
        CHECK (source IN ('training_step', 'rollback', 'manual')),
    ADD COLUMN IF NOT EXISTS created_by INTEGER
        REFERENCES users (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
""",
    ),
    (
        f"{_TABLE} UNIQUE (workflow_version_id, version_number)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_version_number
    ON {_TABLE} (workflow_version_id, version_number);
""",
    ),
    (
        f"{_TABLE} partial UNIQUE (workflow_version_id, training_step_run_id) "
        "WHERE training_step_run_id IS NOT NULL (the spec's key)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_step
    ON {_TABLE} (workflow_version_id, training_step_run_id)
    WHERE training_step_run_id IS NOT NULL;
""",
    ),
    (
        f"{_TABLE} index (tenant_id, workflow_id, created_at DESC)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_workflow_created
    ON {_TABLE} (tenant_id, workflow_id, created_at DESC);
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
        print("DRY  downgrade: no-op (parameter versions are kept as "
              "historical training data; the table is append-only with "
              "soft deletes only, so nothing is dropped on rollback)")
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
        "expr 10.4 ready: workflow_parameter_versions exists; training steps "
        "(10.1/10.2), rollback (10.5) and non-training latest-version reads "
        "(10.3) can write and read it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

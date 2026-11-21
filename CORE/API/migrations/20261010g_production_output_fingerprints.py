#!/usr/bin/env python3
"""sz-self-improving.1a (sz-self-improving decomp, §1.1) — the ONE new
table: PRODUCTION OUTPUT FINGERPRINTS (one row per sampled
production-output fingerprint, drift-surveillance input).

revision      : 20261010g_production_output_fingerprints
down_revision : 20261010f_persistent_stack_backup_config

Purpose per `the internal design notes
tasks.md` §1.1 and design.md Decision 3: a later drift sweep
(sz-self-improving.4/.5/.6, NOT this task) reads this table to detect
production drift against a baseline. This migration only creates the
storage; no extractor/writer/reader code is wired here. The extractor
itself (`services/drift_fingerprint.py`, `_EXTRACTORS`) already exists
(committed in 52e698fd6) and is untouched by this migration.

Columns, per the sz-self-improving.1a decomp row:
    id                  BIGSERIAL PRIMARY KEY
    tenant_id           INTEGER NOT NULL REFERENCES tenants (id)
                        ON DELETE CASCADE
    workflow_id         INTEGER REFERENCES workflows (id)
                        ON DELETE SET NULL (nullable) — a fingerprint for
                        a since-deleted workflow is still useful history.
    workflow_run_id     INTEGER REFERENCES workflow_runs (id)
                        ON DELETE SET NULL (nullable), same reasoning.
    node_run_id         INTEGER REFERENCES node_runs (id)
                        ON DELETE SET NULL (nullable).
    fingerprint_kind    VARCHAR(40) NOT NULL — free-text
                        (image_embedding, text_embedding, tensor_stats,
                        schema_hash, binary_hash, ...), matches
                        services/drift_fingerprint.py's _EXTRACTORS
                        kinds; no enum/CHECK here, the extractor owns
                        valid kinds.
    fingerprint_payload JSONB NOT NULL — the extractor's full return
                        dict (vector/stats/hash + a `degenerate` flag).
    degenerate          BOOLEAN NOT NULL DEFAULT false — denormalized
                        top-level copy of
                        fingerprint_payload["degenerate"] so a drift
                        sweep can filter/index without parsing JSONB;
                        matches the reviewer note on 52e698fd6 that a
                        degenerate fingerprint must never silently
                        count as real signal.
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()

Indexes:
    ix_production_output_fingerprints_workflow_kind_created on
        (workflow_id, fingerprint_kind, created_at) — per the
        sz-self-improving.1 decomp's own spec for this row.
    ix_production_output_fingerprints_tenant on (tenant_id).

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. No reader/writer
code is wired by this migration — table existence + columns are what
sz-self-improving.4/.5/.6 depend on, verified by plain SQL
introspection.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010g_production_output_fingerprints.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "production_output_fingerprints"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-self-improving.1a, one row per sampled "
        "production-output fingerprint)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_id INTEGER REFERENCES workflows (id) ON DELETE SET NULL,
    workflow_run_id INTEGER REFERENCES workflow_runs (id) ON DELETE SET NULL,
    node_run_id INTEGER REFERENCES node_runs (id) ON DELETE SET NULL,
    fingerprint_kind VARCHAR(40) NOT NULL,
    fingerprint_payload JSONB NOT NULL,
    degenerate BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ),
    (
        f"{_TABLE} index (workflow_id, fingerprint_kind, created_at)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_workflow_kind_created
    ON {_TABLE} (workflow_id, fingerprint_kind, created_at);
""",
    ),
    (
        f"{_TABLE} index (tenant_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant
    ON {_TABLE} (tenant_id);
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
        print("DRY  downgrade: no-op (fingerprint rows are kept as "
              "historical drift-surveillance data even after their "
              "workflow/run/node run is deleted)")
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
        "sz-self-improving.1a ready: production_output_fingerprints "
        "exists; the drift sweep (sz-self-improving.4/.5/.6) can write "
        "and read per-(workflow_id, fingerprint_kind, created_at) "
        "fingerprint rows."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

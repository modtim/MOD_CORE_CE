#!/usr/bin/env python3
"""Fortress 3.7a — per-tenant / per-workflow scanner config store.

revision      : 20261012r_output_scan_configs
down_revision : 20261012h_workflow_memory_tables

Design Decision: per-tenant scanner enablement with optional per-workflow
override. The UI slices later; this is the backend store.

SOFT DELETE ONLY (ISO 27001): ``deleted_at TIMESTAMPTZ`` — the clear path
stamps it, re-set creates a new live row, history is kept.

FK ``ON DELETE CASCADE`` (tenants, workflows): a hard DELETE of the parent
tenant/workflow row would physically remove these config rows, bypassing the
soft-delete history above. That is acceptable ONLY because tenants and
workflows are themselves never hard-deleted in this codebase (both use
``deleted_at`` soft-delete) -- the CASCADE path is dead code under normal
operation, kept purely so the FK can still be declared NOT NULL / validated
without orphaning rows if a hard delete is ever run by hand.

``id`` is BIGSERIAL PRIMARY KEY (never a max()-subquery DEFAULT).
Fully idempotent: CREATE TABLE / CREATE INDEX all IF NOT EXISTS; safe to
re-run. Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; ``--commit`` applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012r_output_scan_configs.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "output_scan_configs"


def _sql_statements() -> list[tuple[str, str]]:
    """Ordered (label, sql) steps; idempotent."""
    steps = []
    steps.append((
        f"{_TABLE} table (fortress 3.7a, scanner config store)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_id INTEGER NULL REFERENCES workflows (id) ON DELETE CASCADE,
    scanner VARCHAR(64) NOT NULL,
    enabled BOOLEAN NOT NULL,
    updated_by INTEGER NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
""",
    ))
    steps.append((
        f"{_TABLE} partial UNIQUE (tenant_id, workflow_id, scanner) live-only",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live
    ON {_TABLE} (tenant_id, COALESCE(workflow_id, 0), scanner)
    WHERE deleted_at IS NULL;
""",
    ))
    steps.append((
        f"{_TABLE} index (tenant_id, workflow_id) for effective lookup",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_wf
    ON {_TABLE} (tenant_id, workflow_id);
""",
    ))
    return steps


def _apply(execute, ) -> list[str]:
    """Run every step through `execute(sql)`; return the labels applied.

    Kept separate from `main()` so the test can drive it with the conftest
    `db_session` (never the live DB engine).
    """
    applied = []
    for label, sql in _sql_statements():
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
                   help="Apply the CREATE TABLE + index steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _sql_statements():
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (soft deletes only; the clear path "
              "stamps deleted_at, re-set inserts a new row)")
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
    print(f"{_TABLE} ready: per-tenant / per-workflow scanner config store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

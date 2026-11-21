"""named-artifact-registry — per-tenant named handles for RAG corpora + LoRAs.

Adds the ``named_artifacts`` table: a small, tenant-scoped registry so workflow
nodes can refer to a produced artifact BY NAME instead of by an opaque
collection id / filename the user has to copy around.

  * type == "rag"  — ref = vector-store collection/namespace; metadata = counts.
  * type == "lora" — ref = resolvable location of the .safetensors (the filename
    the brand-sdxl server finds under its mounted /loras dir); metadata = base
    model / trigger word / steps.

Uniqueness is per (tenant_id, type, name) among LIVE rows only (partial unique
index on deleted_at IS NULL) so a soft-deleted tombstone doesn't squat the name
(ISO 27001: soft-delete only). Additive + idempotent — inert on existing data.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260908_add_named_artifacts.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_NAMED_ARTIFACTS_SQL = """
CREATE TABLE IF NOT EXISTS named_artifacts (
    id          SERIAL PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
    type        VARCHAR(16) NOT NULL,
    name        VARCHAR(200) NOT NULL,
    ref         TEXT NOT NULL,
    description TEXT,
    metadata    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);
"""

INDEX_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_named_artifacts_tenant_id
    ON named_artifacts (tenant_id);
"""

INDEX_DELETED_AT_SQL = """
CREATE INDEX IF NOT EXISTS ix_named_artifacts_deleted_at
    ON named_artifacts (deleted_at);
"""

# Per-tenant uniqueness on (type, name) among LIVE rows only.
UNIQUE_LIVE_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_named_artifact_live_tenant_type_name
    ON named_artifacts (tenant_id, type, name)
    WHERE deleted_at IS NULL;
"""

STATEMENTS = [
    CREATE_NAMED_ARTIFACTS_SQL,
    INDEX_TENANT_SQL,
    INDEX_DELETED_AT_SQL,
    UNIQUE_LIVE_SQL,
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        for s in STATEMENTS:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    # Create objects owned by modTech (not the transient Vault dynamic role that
    # runs this migration) so the table matches every other table's ownership
    # and the API's startup ALTER-OWNER reconcile is a no-op instead of failing
    # with "must be owner". The app's dynamic role is a member of modTech, so
    # SET ROLE succeeds; fall back to plain execution if it can't (dev/sqlite).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for s in STATEMENTS:
        cur.execute(s)
    # Ensure ownership even if the table pre-existed under a transient role.
    if set_role_ok:
        try:
            cur.execute(f'ALTER TABLE named_artifacts OWNER TO {owner}')
        except Exception as e:  # noqa: BLE001
            print(f"(ALTER OWNER skipped: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("named_artifacts table ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

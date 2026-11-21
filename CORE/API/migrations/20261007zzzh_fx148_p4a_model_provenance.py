#!/usr/bin/env python3
"""GSEC FX.148 p4a (owner decision Q135=C) — MODEL PROVENANCE RECORD.

FX.148 (model supply-chain hardening) requires a model's provenance —
source repo/URL, revision/commit, per-file hashes, the importing actor, the
timestamp and the pickle-scan verdict — to live IN THE INVENTORY, because
"which build of which repo is this model, and did anything dangerous in it
run" is the question a compliance reviewer asks while looking at the model
list. Until now the only answer was the model's own manifest, which records
nothing about where a model came from or what gated it.

NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane:
NO column was added to any existing table).

APPEND-ONLY (ISO 27001 A.18.1.3 — records shall not be destroyed): there is
NO `deleted_at` column, nothing UPDATEs a row, and a re-import of the same
model writes a NEW row. The newest row is the current provenance; every
prior import stays readable. There is no delete path at all.

    model_provenance
      id                     BIGSERIAL PRIMARY KEY
      model_name             VARCHAR(200) NOT NULL   inventory handle
      model_type             VARCHAR(16)  NOT NULL   embedding | llm
      source_url             TEXT NULL               declared source claim
      source_revision        TEXT NULL               commit / tag
      file_sha256            JSONB NULL              {filename: sha256}
      file_count             INTEGER  NOT NULL DEFAULT 0
      total_bytes            BIGINT   NOT NULL DEFAULT 0
      scan_status            VARCHAR(16) NULL        clean|unsafe|error|...
      scan_result            JSONB NULL              verdict snapshot
      imported_by_user_id    INTEGER NULL            FK users(id) (approver)
      imported_by_tenant_id  INTEGER NULL            FK tenants(id)
      import_kind            VARCHAR(24) NOT NULL DEFAULT 'upload'
      created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()

The FKs carry NO `ON DELETE CASCADE` / `ON DELETE SET NULL` clause: deleting
a user or tenant must never erase the record of what they once imported
(ISO 27001 A.18.1.3, protection of records).

Indexes: (model_name, model_type) — the inventory read; created_at — the
newest-first listing.

Dry run (default, no --commit) PRINTS the SQL and exits 0 WITHOUT touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzh_fx148_p4a_model_provenance.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzh_fx148_p4a_model_provenance.py --commit   # apply

NOT applied by the p4a task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "model_provenance"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.148 p4a)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    model_name VARCHAR(200) NOT NULL,
    model_type VARCHAR(16) NOT NULL,
    source_url TEXT NULL,
    source_revision TEXT NULL,
    file_sha256 JSONB NULL,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes BIGINT NOT NULL DEFAULT 0,
    scan_status VARCHAR(16) NULL,
    scan_result JSONB NULL,
    imported_by_user_id INTEGER NULL REFERENCES users(id),
    imported_by_tenant_id INTEGER NULL REFERENCES tenants(id),
    import_kind VARCHAR(24) NOT NULL DEFAULT 'upload',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_name
    ON {_TABLE} (model_name, model_type);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_created
    ON {_TABLE} (created_at);
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
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
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
        for stmt in (s.strip() for s in sql.split(";") if s.strip()):
            try:
                cur.execute(stmt)
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {label}: {e}")
                return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "FX.148 p4a ready: model_provenance exists, append-only (no deleted_at, "
        "no UPDATE path); re-imports add rows."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""wizard-knowledge-pack Phase 1 (S) -- library_profiles.source/pack_version
+ wizard_knowledge_pack_versions table.

revision      : 20261013h_wizard_knowledge_pack_schema
down_revision : 20261013g_fortress_59_certification_signature_columns

Closes the internal design notes Phase 1 items 1.1, 1.2,
1.3, 1.5. Schema only -- no apply-logic (that is Phase 2 brief 986d, which
targets only the live LibraryProfile columns gpu_required/evidence/
install_docs_excerpt; install_recipe_by_accel/successful_build_count_by_accel
are LEGACY/dead since GB.LEAK.5 and stay untouched here).

Columns added to library_profiles (additive):

  - source VARCHAR(32) NOT NULL DEFAULT 'local_capture' -- mirrors the plain-
    String style of LibraryInstallRecipe.provenance (models.py:11254); not a
    Postgres enum type, so this migration stays simple. Existing rows get the
    server default (they were all locally captured before this column
    existed).
  - pack_version VARCHAR(16) NULL -- which wizard-knowledge-pack version (if
    any) supplied this row; NULL for locally captured rows.

New table wizard_knowledge_pack_versions -- one row per pack version
install/attempt:

  - id BIGSERIAL PRIMARY KEY (never a max()-subquery DEFAULT -- Postgres
    rejects subqueries in a DEFAULT clause).
  - version VARCHAR(16) NOT NULL
  - applied_at TIMESTAMPTZ NULL
  - applied_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL
    (two words; ONDELETE is a syntax error)
  - status VARCHAR(16) NOT NULL DEFAULT 'pending' (applied/rollback/pending)
  - integrity_check_payload JSONB NULL
  - source_url VARCHAR(500) NULL
  - signature_status VARCHAR(16) NOT NULL DEFAULT 'unverified'
    (valid/invalid/unverified)
  - created_at TIMESTAMPTZ NOT NULL DEFAULT now()

No CHECK constraints added for status/signature_status in this brief -- kept
plain VARCHAR with documented value sets, matching the LibraryInstallRecipe.
provenance precedent (plain String, no enum/CHECK) this migration is asked to
mirror.

Idempotent: ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS throughout;
safe to re-run.

Downgrade (DEV-ONLY path): drops the table and the two columns. Prod never
runs downgrade -- ISO 27001's no-hard-delete rule governs ROWS, not schema;
dropping an additive, still-unused column/table set on a dev/test DB is not a
data-destruction event the way deleting a row would be.

DRY-RUN by default; --commit applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261013h_wizard_knowledge_pack_schema.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_PROFILES_TABLE = "library_profiles"
_PACK_TABLE = "wizard_knowledge_pack_versions"

_UPGRADE_STATEMENTS = [
    (
        f"{_PROFILES_TABLE}.source VARCHAR(32) NOT NULL DEFAULT 'local_capture'",
        f"""
ALTER TABLE {_PROFILES_TABLE} ADD COLUMN IF NOT EXISTS
    source VARCHAR(32) NOT NULL DEFAULT 'local_capture';
""",
    ),
    (
        f"{_PROFILES_TABLE}.pack_version VARCHAR(16) NULL",
        f"""
ALTER TABLE {_PROFILES_TABLE} ADD COLUMN IF NOT EXISTS
    pack_version VARCHAR(16);
""",
    ),
    (
        f"{_PACK_TABLE} table (wizard-knowledge-pack version ledger)",
        f"""
CREATE TABLE IF NOT EXISTS {_PACK_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    version VARCHAR(16) NOT NULL,
    applied_at TIMESTAMPTZ NULL,
    applied_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    integrity_check_payload JSONB NULL,
    source_url VARCHAR(500) NULL,
    signature_status VARCHAR(16) NOT NULL DEFAULT 'unverified',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ),
]

_DOWNGRADE_STATEMENTS = [
    (
        f"drop table {_PACK_TABLE}",
        f"DROP TABLE IF EXISTS {_PACK_TABLE};",
    ),
    (
        f"drop column {_PROFILES_TABLE}.pack_version",
        f"ALTER TABLE {_PROFILES_TABLE} DROP COLUMN IF EXISTS pack_version;",
    ),
    (
        f"drop column {_PROFILES_TABLE}.source",
        f"ALTER TABLE {_PROFILES_TABLE} DROP COLUMN IF EXISTS source;",
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
                   help="Apply the ALTER/CREATE steps. Without this flag, dry-run only.")
    p.add_argument("--downgrade", action="store_true",
                   help="DEV-ONLY: drop the table/columns instead of adding them.")
    args = p.parse_args()

    statements = _DOWNGRADE_STATEMENTS if args.downgrade else _UPGRADE_STATEMENTS

    if not args.commit:
        for label, sql in statements:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        if not args.downgrade:
            print("DRY  downgrade: dev-only, drops wizard_knowledge_pack_versions "
                  "and the two library_profiles columns added here (prod never "
                  "runs downgrade -- ISO 27001's no-hard-delete rule governs "
                  "rows, not schema)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in statements:
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
    if args.downgrade:
        print("wizard-knowledge-pack schema downgrade applied: "
              "wizard_knowledge_pack_versions dropped, library_profiles "
              "source/pack_version columns dropped (dev-only).")
    else:
        print(
            "wizard-knowledge-pack schema ready: library_profiles carries "
            "source (VARCHAR(32) NOT NULL DEFAULT 'local_capture') and "
            "pack_version (VARCHAR(16) NULL); wizard_knowledge_pack_versions "
            "table created."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

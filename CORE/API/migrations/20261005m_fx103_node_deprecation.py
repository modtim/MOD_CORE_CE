"""FX.103 (owner decision Q89=A) — node deprecation/retirement lifecycle.

node_versions (node-version-pinning) keeps every build addressable, but there
was no way to mark a NODE as deprecated (notice period) or past its final
runnable date (retirement). The lifecycle needs three columns on
node_definitions:

    status          VARCHAR(16) NOT NULL DEFAULT 'active'
                    -- 'active' | 'deprecated' | 'retired'
    deprecated_at   TIMESTAMPTZ  NULL
    retire_after    TIMESTAMPTZ  NULL
                    -- a deprecated/retired node stays RUNNABLE for pinned
                    -- workflows until this date; >= deprecated_at +
                    -- deprecation.min_runnable_months (12) is enforced by the
                    -- validator (services/node_deprecation.py), not the DB.

No model-catalog table exists (models are served by persistent_stacks /
nim_catalog's coarse is_active), so the lifecycle lands on node definitions
only this pass.

Additive + nullable (status is NOT NULL with a DEFAULT so existing rows read
'active' without a backfill): every pre-existing node behaves exactly as
before — 'active' means no badge, no run gate.

ISO 27001: no deletes; status flips are audited by the caller (AuditEvent
NODE_DEPRECATION_* rows).

Migration runner is tools/mod_migrate.py (steps discovered as *.py, run in
filename order, recorded in the release_steps ledger) — this file sorts after
20261005l.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261005m_fx103_node_deprecation.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261005m_fx103_node_deprecation.py --commit   # apply

models.py is bind-mounted live into the API container, so the columns must
exist before any FX.103 code path touches them — apply immediately after
adding the columns to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "node_definitions.status column",
        """
ALTER TABLE node_definitions
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'active';
""",
    ),
    (
        "node_definitions.deprecated_at column",
        """
ALTER TABLE node_definitions
    ADD COLUMN IF NOT EXISTS deprecated_at TIMESTAMP WITH TIME ZONE;
""",
    ),
    (
        "node_definitions.retire_after column",
        """
ALTER TABLE node_definitions
    ADD COLUMN IF NOT EXISTS retire_after TIMESTAMP WITH TIME ZONE;
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
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.103 node deprecation columns ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

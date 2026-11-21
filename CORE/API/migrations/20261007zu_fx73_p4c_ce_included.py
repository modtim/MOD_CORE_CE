"""GSEC FX.73 part 4c (owner decision Q56=A) — CE-included source of truth per node.

Adds ONE authoritative column answering "is this node included in the CE
(open-source) edition?":

    node_definitions.ce_included  BOOLEAN NULL

NULL = not stamped yet (every pre-FX.73-p4c row). The node scanner
(services/node_scanner.py) stamps TRUE on CREATE for a node whose name is in
the CE node-seed keep-list (open-core/node-seed-keeplist.yaml), which is the
only place a CE install decides what it ships; the palette API
(GET /v1/nodes) then returns `ce_included` so the WorkflowBuilder can badge
"CE" on the node row. Nothing here deletes or hides a node — the tenant
system-node copies still populate the palette unchanged.

Nullable + server default FALSE keeps the live bind-mounted models.py safe:
the column is `deferred()` in models.py, so it is absent from default SELECTs
until the migration is applied (the palette enrichment checks column
availability before reading it, so an un-migrated DB degrades to the
keep-list derivation instead of 500ing the palette).

Idempotent: ALTER TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
DRY RUN (default) PRINTS the SQL and exits 0 without touching the database;
apply with --commit (the lead applies it):

    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zu_fx73_p4c_ce_included.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zu_fx73_p4c_ce_included.py --commit   # apply

Sorts after 20261007s in tools/mod_migrate.py's filename-order discovery.
Same SET ROLE "modTech" pattern as the other 20261007* migrations.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "node_definitions.ce_included column",
        """
ALTER TABLE node_definitions
    ADD COLUMN IF NOT EXISTS ce_included BOOLEAN NULL;
""",
    ),
    (
        "node_definitions.ce_included index",
        """
CREATE INDEX IF NOT EXISTS ix_node_definitions_ce_included
    ON node_definitions (ce_included);
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
            print(f"-- {label}")
            print(sql.strip())
        print("\ndry run: nothing executed. Re-run with --commit to apply.")
        return 0

    dbapi = _get_conn()
    cur = dbapi.cursor()
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
        set_role_ok = _owner_role.maybe_set_role(cur)
        for label, sql in _STATEMENTS:
            cur.execute(sql)
            print(f"applied: {label}")
    finally:
        cur.close()
        dbapi.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

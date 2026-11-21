# API/migrations/20261007zx_fx91_p2a_node_egress_domains.py
"""FX.91 p2a [B] — node manifest egress_domains: the DECLARED-host column.

`node_definitions` (models.NodeDefinition) already carries `allowed_egress`
(the dispatch-time {host,ports,protocol} shape that services/egress_allowlist.py
compiles). FX.91 p2a adds the manifest half of the same contract: the plain
external hostnames a node's manifest declares it needs, shown to a human in the
palette (p2b) and approved once by a tenant admin (p2c):

    egress_domains  JSONB NULL  (list of hostname strings; NULL = declared
                                 nothing = default-deny)

Nullable, so every pre-existing node row keeps working unchanged — a NULL row
reads as default-deny, exactly the posture the worker already enforces
(mod-user-internal, Internal: true). models.py declares the column deferred(),
so a DB where this step has not run yet still loads and reads NULL.

Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS. No new table is created
here and nothing is dropped or truncated (ISO 27001 — additive only).

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching the
database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zx_fx91_p2a_node_egress_domains.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zx_fx91_p2a_node_egress_domains.py --commit   # apply

NOT applied by the p2a task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "node_definitions"

_STATEMENTS = [
    (
        f"{_TABLE} declared-egress column (FX.91 p2a)",
        f"""
ALTER TABLE {_TABLE}
    ADD COLUMN IF NOT EXISTS egress_domains JSONB NULL DEFAULT NULL;
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
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
    print(f"{_TABLE} declared-egress column ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""FX.103 part 2 (owner decision Q89=A) — node retirement: notice-sent stamp.

Part 1 (20261005m_fx103) added node_definitions.status / deprecated_at /
retire_after. Part 2 adds the daily retirement scheduler, which must send
the 6-month deprecation notice email exactly ONCE per node. The "already
sent" state is stamped on the row itself:

    deprecation_notice_sent_at  TIMESTAMPTZ  NULL
                                -- NULL = notice not sent yet (or nothing to
                                   send will be stamped too, to stop the
                                   daily re-scan); set when the notice is
                                   fully delivered (or there were no
                                   affected tenant admins). The scheduler
                                   never re-sends once this is set.

Additive + nullable: pre-existing rows read NULL = "notice not sent" and
the scheduler treats them exactly like today's rows. ISO 27001: no deletes.

Migration runner is tools/mod_migrate.py (steps discovered as *.py, run in
filename order, recorded in the release_steps ledger) — this file sorts
after 20261005q.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec my_fastapi_app python3 \
        /app/migrations/20261005r_fx103_node_retirement.py --commit

models.py is bind-mounted live into the API container, so the column must
exist before the models.py edit (which adds the mapped attribute) is in
place — apply this migration, then land the models.py hunk.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "node_definitions.deprecation_notice_sent_at column",
        """
ALTER TABLE node_definitions
    ADD COLUMN IF NOT EXISTS deprecation_notice_sent_at
    TIMESTAMP WITH TIME ZONE;
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
    print("FX.103 part 2 deprecation_notice_sent_at ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

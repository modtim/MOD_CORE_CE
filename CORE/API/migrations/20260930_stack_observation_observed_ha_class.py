"""GR.FEN.1b — persist the worker-reported HA class on stack observations.

Adds one nullable String column, `observed_ha_class`, to
`stack_observations`.

The worker already reads the `com.example.ha-class` container label off
every managed project and reports it in the heartbeat (Go field `HAClass`,
json key `ha_class`), but `services/stack_observation.record_observations`
discarded it. Persisting it lets the platform compare what the containers on
a stack's host ACTUALLY carry against `persistent_stacks.ha_class` and
surface a mismatch: editing the class (PUT /stacks/{id}/ha-class) only
reaches the running containers at the next redeploy, and the worker's fence
decision reads the LABEL — so until a redeploy the stored class is not in
effect, and the stacks row says "redeploy needed to apply".

NULL means "no worker that reports it has observed this project yet" (older
binary or pre-label deploy) — it is NOT "label absent", and the mismatch
logic treats it as "nothing observed" rather than a drift.

Purely additive — no existing data touched (every existing row gets
observed_ha_class=NULL).

Idempotent: `ADD COLUMN IF NOT EXISTS`; safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260930_stack_observation_observed_ha_class.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


_TABLE = "stack_observations"

ADD_OBSERVED_HA_CLASS_SQL = """
ALTER TABLE stack_observations
    ADD COLUMN IF NOT EXISTS observed_ha_class VARCHAR(50);
"""

STATEMENTS = [
    ("add column stack_observations.observed_ha_class", ADD_OBSERVED_HA_CLASS_SQL),
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
        for label, sql in STATEMENTS:
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        try:
            cur.execute(f'ALTER TABLE {_TABLE} OWNER TO {owner}')
        except Exception as e:  # noqa: BLE001
            print(f"(ALTER TABLE {_TABLE} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("stack_observations.observed_ha_class ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

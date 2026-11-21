"""GT.C1 (launch-completion-megaplan) — release_steps ledger + append-only guard.

`release_steps` records every step the one-shot `tools/mod_migrate.py`
runner has successfully applied (installer/updater path):

    id, step_name (UNIQUE — the migration file stem or `seed:<name>`),
    checksum (sha256 of the step file at apply time), applied_at,
    release_version (nullable), duration_ms, runner_host

The runner skips a step whose name is already recorded (no-op) and skips
with a WARNING if the file's checksum changed since it was recorded — a
changed step file must never be silently re-run against a live install.
Rows are the release history: audit-grade, so (ISO 27001, same style as
platform_settings_audit / wizard_runs / wizard_event_tables) the table is
ENFORCED append-only: BEFORE UPDATE OR DELETE row triggers RAISE.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/FUNCTION IF NOT EXISTS, DROP TRIGGER IF EXISTS + CREATE TRIGGER,
ALTER OWNER. Safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step). After applying, EXISTING
installs must baseline their history:

    docker compose --profile migrate run --rm mod-migrate --baseline

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261002f_release_steps.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("release_steps",)

CREATE_RELEASE_STEPS_SQL = """
CREATE TABLE IF NOT EXISTS release_steps (
    id              SERIAL PRIMARY KEY,
    step_name       VARCHAR(255) NOT NULL UNIQUE,
    checksum        VARCHAR(64) NOT NULL,
    applied_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    release_version VARCHAR(64),
    duration_ms     INTEGER,
    runner_host     VARCHAR(255)
);
"""

GUARD_FN_SQL = """
CREATE OR REPLACE FUNCTION release_steps_no_mutate()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Operation % is forbidden on append-only table % (ISO no-hard-delete)',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END;
$$ LANGUAGE plpgsql
"""

_STATEMENTS = [
    ("release_steps table", CREATE_RELEASE_STEPS_SQL),
    ("function release_steps_no_mutate()", GUARD_FN_SQL),
]
for _op in ("UPDATE", "DELETE"):
    _trig = f"trg_no_{_op.lower()}_release_steps"
    _STATEMENTS.append(
        (f"drop trigger {_trig} if exists",
         f"DROP TRIGGER IF EXISTS {_trig} ON release_steps")
    )
    _STATEMENTS.append(
        (f"create trigger {_trig} BEFORE {_op} (row)",
         f"""
         CREATE TRIGGER {_trig}
             BEFORE {_op} ON release_steps
             FOR EACH ROW EXECUTE FUNCTION release_steps_no_mutate()
         """)
    )


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
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        for t in _TABLES:
            try:
                cur.execute(f'ALTER TABLE {t} OWNER TO {owner}')
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER TABLE {t} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("release_steps ledger + append-only guard ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

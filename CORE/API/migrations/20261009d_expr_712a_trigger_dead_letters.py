#!/usr/bin/env python3
"""Expressiveness 7.12 — the ONE new table: TRIGGER DEAD-LETTER SINK.

revision      : 20261009d_expr_712a_trigger_dead_letters
down_revision : 20261009c_fortress_21b_workflow_run_proof_bundles

7.12 requires an `event_dispatch_dead_letter` sink: when a trigger fire
fails and its retry budget is exhausted (services/triggers/pipeline.py), the
event is parked here instead of being dropped — the row carries the payload
as received, the trigger it belongs to, the last error text, the attempt
count, and the first/last failure timestamps. A later replay stamps
`replayed_at` / `replayed_run_id` and never removes the row, so the failure
evidence is permanent (ISO 27001 A.18.1.3, protection of records).

Structure copied from the newest new-table migrations
(20261009b_fx145_p2a_admin_device_records.py,
 20261009a_cui_phones_mobile_devices.py): Class A argparse, `--commit` to
apply, dry-run (prints SQL) by default.

    trigger_dead_letters
      id              BIGSERIAL PRIMARY KEY
      tenant_id       INTEGER NULL      FK tenants(id) (no ON DELETE)
      trigger_id      INTEGER NOT NULL  FK workflow_triggers(id) (no ON DELETE)
      payload         JSONB NULL        event context as received
      error           TEXT NULL         last error text
      attempts        INTEGER NOT NULL DEFAULT 0
      first_failed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      last_failed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
      replayed_at     TIMESTAMPTZ NULL  set by a replay, never cleared
      replayed_run_id INTEGER NULL      no FK: the run may be tombstoned

`id` is `BIGSERIAL PRIMARY KEY` — never a max()-subquery DEFAULT (Postgres
rejects subqueries in a DEFAULT clause).

`tenant_id` is NULLABLE (NULL = platform-scope trigger) with NO ON DELETE
clause; `trigger_id` likewise carries no ON DELETE — a trigger is never
hard-deleted (ISO soft delete), and its dead-letter history must survive it.

Append-only: this migration contains no DELETE, no UPDATE and no DROP of any
kind; downgrade is a NO-OP (ISO 27001 A.18.1.3).

Idempotent: CREATE TABLE / CREATE ... INDEX IF NOT EXISTS, so a re-run is a
no-op.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane — the lead applies it.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "trigger_dead_letters"


_STATEMENTS = [
    (
        f"{_TABLE} table (expressiveness 7.12, dead-letter sink)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NULL REFERENCES tenants (id),
    trigger_id INTEGER NOT NULL REFERENCES workflow_triggers (id),
    payload JSONB NULL,
    error TEXT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    first_failed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_failed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    replayed_at TIMESTAMPTZ NULL,
    replayed_run_id INTEGER NULL,
    CHECK (attempts >= 0)
);
""",
    ),
    (
        f"{_TABLE} indexes",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_id
    ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_trigger_id
    ON {_TABLE} (trigger_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_first_failed_at
    ON {_TABLE} (first_failed_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_replayed_at
    ON {_TABLE} (replayed_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_trigger
    ON {_TABLE} (tenant_id, trigger_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_unreplayed
    ON {_TABLE} (trigger_id, replayed_at);
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
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the CREATE + index steps. Without this flag, "
                        "dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (ISO 27001 — never drop the records)")
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
        "Expressiveness 7.12 ready: trigger_dead_letters exists, append-only "
        "(a replay stamps replayed_at/replayed_run_id, never deletes)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

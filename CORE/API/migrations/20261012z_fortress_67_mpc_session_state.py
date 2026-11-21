#!/usr/bin/env python3
"""fortress-6.7 S1a — mpc_session_state table (MPC session state machine).

revision      : 20261012z_fortress_67_mpc_session_state
down_revision : 20261012y_fortress_63_dp_budget_ledger

Fortress 6.7 ("multi-party compute sessions") slice 1 is storage only: models.py
is lead-only, so nothing here declares or imports an MPCSessionState ORM class.
One new table, mpc_session_state — the CURRENT-STATE snapshot of one MPC
session (design_fortress_66.md §3): proposed -> ready -> running ->
completed|failed, with rejected|cancelled as the reject/cancel terminals.
Transition history lives in the existing append-only audit trail; this table
only ever holds "now".

FK actions: workflow_id and initiator_tenant_id are ON DELETE RESTRICT — the
session row IS the compliance record of who jointly computed what, so a hard
delete of the owning workflow/tenant must be refused, not a silent CASCADE.
requested_by_user_id and deleted_by_user_id are nullable user FKs with the
usual ON DELETE SET NULL (two words; ONDELETE is a syntax error).

status is VARCHAR(16) CHECKed to the seven design values; participants_json is
the per-tenant invite/accept/ack snapshot (jsonb_path_ops GIN so "is tenant N
invited?" is an index lookup); security_props_json is frozen at creation
(§4) and never edited.

``id`` is BIGSERIAL PRIMARY KEY — never a max()-subquery DEFAULT (Postgres
rejects subqueries in a DEFAULT clause).

No-hard-delete guard (ISO 27001 soft delete only): the 20261012u
output_scan_exemptions_no_hard_delete idiom — RAISE with errcode
feature_not_supported attached BEFORE DELETE FOR EACH ROW plus BEFORE
TRUNCATE FOR EACH STATEMENT. UPDATE stays allowed: the accept/dispatch
transitions ARE UPDATEs of this row.

Fully idempotent: CREATE TABLE / CREATE ... INDEX all IF NOT EXISTS, CREATE
OR REPLACE FUNCTION, DROP TRIGGER IF EXISTS + CREATE TRIGGER inside a to_regclass
existence check, so a re-run is a no-op. Additive only.

DRY-RUN by default; ``--commit`` applies. NEVER applied to the live DB by the
lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012z_fortress_67_mpc_session_state.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "mpc_session_state"
_GUARD_FN = "mpc_session_state_no_hard_delete"
_IX_INIT = "ix_mpc_session_state_initiator_status"
_IX_GIN = "ix_mpc_session_state_participants_gin"


def _sql_statements() -> list[tuple[str, str]]:
    """Ordered (label, sql) steps; every step idempotent."""
    steps = []
    steps.append((
        f"{_TABLE} table (fortress 6.7, MPC session state machine)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    workflow_id INTEGER NOT NULL REFERENCES workflows (id) ON DELETE RESTRICT,
    initiator_tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    status VARCHAR(16) NOT NULL DEFAULT 'proposed',
    participants_json JSONB NOT NULL DEFAULT '[]',
    security_props_json JSONB NOT NULL,
    aggregate_result_ref TEXT NULL,
    error_code VARCHAR(64) NULL,
    requested_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    deleted_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT ck_mpc_session_state_status
        CHECK (status IN ('proposed', 'ready', 'running', 'completed',
                          'failed', 'rejected', 'cancelled'))
);
""",
    ))
    steps.append((
        f"{_TABLE} index {_IX_INIT} (initiator_tenant_id, status) live-only",
        f"""
CREATE INDEX IF NOT EXISTS {_IX_INIT}
    ON {_TABLE} (initiator_tenant_id, status)
    WHERE deleted_at IS NULL;
""",
    ))
    steps.append((
        f"{_TABLE} GIN index {_IX_GIN} (participants_json, jsonb_path_ops)",
        f"""
CREATE INDEX IF NOT EXISTS {_IX_GIN}
    ON {_TABLE} USING gin (participants_json jsonb_path_ops);
""",
    ))
    steps.append((
        f"function {_GUARD_FN}() (no hard delete; UPDATE stays allowed)",
        f"""
CREATE OR REPLACE FUNCTION {_GUARD_FN}()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'Hard delete/truncate is forbidden on % (ISO 27001 no-hard-delete); '
        'revoke by soft-deleting (set deleted_at + deleted_by_user_id) instead',
        TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END;
$$ LANGUAGE plpgsql
""",
    ))
    steps.append((
        f"no-hard-delete triggers on {_TABLE} (DELETE + TRUNCATE only)",
        f"""
DO $$
BEGIN
    IF to_regclass('{_TABLE}') IS NULL THEN
        RAISE NOTICE '{_TABLE} does not exist yet; skipping {_TABLE} triggers';
        RETURN;
    END IF;
    DROP TRIGGER IF EXISTS trg_no_delete_{_TABLE} ON {_TABLE};
    CREATE TRIGGER trg_no_delete_{_TABLE}
        BEFORE DELETE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {_GUARD_FN}();
    DROP TRIGGER IF EXISTS trg_no_truncate_{_TABLE} ON {_TABLE};
    CREATE TRIGGER trg_no_truncate_{_TABLE}
        BEFORE TRUNCATE ON {_TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION {_GUARD_FN}();
END
$$
""",
    ))
    return steps


_STATEMENTS: list[tuple[str, str]] = _sql_statements()


def _apply(execute) -> list[str]:
    """Run every step through ``execute(sql)``; return the labels applied.

    Kept separate from ``main()`` so the test can drive it with the conftest
    ``db_session`` (never the live DB engine).
    """
    applied = []
    for label, sql in _STATEMENTS:
        # ONE whole statement per entry: the plpgsql bodies and the DO block
        # contain semicolons, so this must not be split on ';'.
        execute(sql)
        applied.append(label)
    return applied


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
                   help="Apply the CREATE + index + trigger steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _sql_statements():
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (ISO 27001 — session rows are never "
              "dropped; a cancelled session is status='cancelled')")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    try:
        _apply(cur.execute)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL apply: {e}")
        return 1
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "Fortress 6.7 ready: mpc_session_state exists, status CHECKed to the "
        "seven design values, participants GIN'd for invitee lookup, "
        "FK-protected (RESTRICT) against workflow/tenant hard deletes, "
        "UPDATE allowed (state transitions), DELETE/TRUNCATE RAISE via "
        f"{_GUARD_FN}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

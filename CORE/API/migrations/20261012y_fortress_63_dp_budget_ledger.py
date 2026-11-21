#!/usr/bin/env python3
"""fortress-6.3 S1 — differential-privacy budget LEDGER + CAPS (tables only).

revision      : 20261012y_fortress_63_dp_budget_ledger
down_revision : 20261012x_fortress_23c_node_run_attestations

Fortress 6.3 ("DP budget accounting") slice 1 is storage only: models.py is
lead-only, so nothing here declares or imports an ORM class.  Two new tables:

  dp_budget_ledger  one row per dispatched node: how much (epsilon, delta)
    this run's use of that node consumed in the YYYY-MM period.  APPEND-ONLY
    (ISO 27001: the ledger IS the evidence that a cap was respected — it is
    never mutated, never deleted).

  dp_budget_caps    the caps the ledger is checked against: one row per
    (tenant, workflow), workflow_id NULL = the tenant-wide default row
    (exactly the 20261010e_workflow_budgets.py shape).  Mutable (updated_at).

FK actions (LEAD OVERRIDE, not the design text): every FK here is
``ON DELETE RESTRICT`` — including caps.workflow_id.  Workflows are soft-
deleted, so a CASCADE would silently drop the cap row that IS the compliance
record; RESTRICT refuses the parent hard delete instead.  ``ON DELETE SET
NULL`` is two words; ``ONDELETE`` is a syntax error.

Caps uniqueness: a plain UNIQUE(tenant_id, workflow_id) would let duplicate
tenant-wide rows in (NULLs are distinct in Postgres), so the cap is a UNIQUE
INDEX on (tenant_id, COALESCE(workflow_id, 0)) — one tenant-wide row plus one
row per workflow, per tenant.

CHECKs: epsilon_spent >= 0 AND delta_spent >= 0 AND delta_spent < 1 (delta is
the failure probability, always < 1); epsilon_cap_per_period IS NULL (unlimited
/ on-prem) OR > 0; period matches ^[0-9]{4}-[0-9]{2}$ (credit-manager style).

Append-only guard: 20261012q's function is fortress_13_-prefixed (that slice
owns its guard), so this slice creates its own equivalent
``fortress_63_append_only_guard`` — RAISE with errcode feature_not_supported
(clients map it to 501) — attached BEFORE UPDATE OR DELETE FOR EACH ROW plus
BEFORE TRUNCATE FOR EACH STATEMENT, each ENABLE ALWAYS so the guard also
fires under the test harness's session_replication_role=replica (the posture
20261012v already ships).

``id`` is BIGSERIAL PRIMARY KEY — never a max()-subquery DEFAULT (Postgres
rejects subqueries in a DEFAULT clause).

Fully idempotent: CREATE TABLE / CREATE ... INDEX all IF NOT EXISTS, CREATE
OR REPLACE FUNCTION, DROP TRIGGER IF EXISTS + CREATE TRIGGER + ALTER TABLE
ENABLE ALWAYS inside a to_regclass existence check, so a re-run is a no-op.
Additive only.

DRY-RUN by default; ``--commit`` applies. NEVER applied to the live DB by the
lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012y_fortress_63_dp_budget_ledger.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_LEDGER = "dp_budget_ledger"
_CAPS = "dp_budget_caps"
_GUARD_FN = "fortress_63_append_only_guard"
_LEDGER_IX = "ix_dp_budget_ledger_period"
_CAPS_UQ = "uq_dp_budget_caps_tenant_workflow"


def _sql_statements() -> list[tuple[str, str]]:
    """Ordered (label, sql) steps; every step idempotent."""
    steps = []
    steps.append((
        f"{_LEDGER} table (fortress 6.3, per-period DP spend ledger)",
        f"""
CREATE TABLE IF NOT EXISTS {_LEDGER} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    workflow_id INTEGER NOT NULL REFERENCES workflows (id) ON DELETE RESTRICT,
    node_id VARCHAR(128) NOT NULL,
    run_id INTEGER NOT NULL REFERENCES workflow_runs (id) ON DELETE RESTRICT,
    epsilon_spent NUMERIC(12,6) NOT NULL,
    delta_spent NUMERIC(20,18) NOT NULL,
    period VARCHAR(10) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_dp_budget_ledger_spend_nonneg
        CHECK (epsilon_spent >= 0 AND delta_spent >= 0 AND delta_spent < 1),
    CONSTRAINT ck_dp_budget_ledger_period_shape
        CHECK (period ~ '^[0-9]{{4}}-[0-9]{{2}}$')
);
""",
    ))
    steps.append((
        f"{_LEDGER} index {_LEDGER_IX} (tenant_id, workflow_id, period)",
        f"""
CREATE INDEX IF NOT EXISTS {_LEDGER_IX}
    ON {_LEDGER} (tenant_id, workflow_id, period);
""",
    ))
    steps.append((
        f"{_CAPS} table (fortress 6.3, per-workflow/tenant-wide caps)",
        f"""
CREATE TABLE IF NOT EXISTS {_CAPS} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    workflow_id INTEGER NULL REFERENCES workflows (id) ON DELETE RESTRICT,
    epsilon_cap_per_period NUMERIC(12,6) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_dp_budget_caps_cap_positive
        CHECK (epsilon_cap_per_period IS NULL OR epsilon_cap_per_period > 0)
);
""",
    ))
    steps.append((
        f"{_CAPS} UNIQUE index {_CAPS_UQ} (tenant_id, COALESCE(workflow_id, 0))",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_CAPS_UQ}
    ON {_CAPS} (tenant_id, COALESCE(workflow_id, 0));
""",
    ))
    steps.append((
        f"function {_GUARD_FN}() (shared append-only guard)",
        f"""
CREATE OR REPLACE FUNCTION {_GUARD_FN}()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Operation % is forbidden on append-only audit table % (ISO 27001: audit tables are append-only; UPDATE/DELETE/TRUNCATE are not allowed)',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END;
$$ LANGUAGE plpgsql
""",
    ))
    steps.append((
        f"append-only triggers on {_LEDGER} (row mutation + truncate)",
        f"""
DO $$
BEGIN
    IF to_regclass('{_LEDGER}') IS NULL THEN
        RAISE NOTICE '{_LEDGER} does not exist yet; skipping {_LEDGER} triggers';
        RETURN;
    END IF;
    DROP TRIGGER IF EXISTS trg_no_row_mutation_{_LEDGER} ON {_LEDGER};
    CREATE TRIGGER trg_no_row_mutation_{_LEDGER}
        BEFORE UPDATE OR DELETE ON {_LEDGER}
        FOR EACH ROW EXECUTE FUNCTION {_GUARD_FN}();
    ALTER TABLE {_LEDGER}
        ENABLE ALWAYS TRIGGER trg_no_row_mutation_{_LEDGER};
    DROP TRIGGER IF EXISTS trg_no_truncate_{_LEDGER} ON {_LEDGER};
    CREATE TRIGGER trg_no_truncate_{_LEDGER}
        BEFORE TRUNCATE ON {_LEDGER}
        FOR EACH STATEMENT EXECUTE FUNCTION {_GUARD_FN}();
    ALTER TABLE {_LEDGER}
        ENABLE ALWAYS TRIGGER trg_no_truncate_{_LEDGER};
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
        print("DRY  downgrade: no-op (ISO 27001 — ledger rows are never dropped; "
              "caps are revoked by NULLing epsilon_cap_per_period)")
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
        "Fortress 6.3 ready: dp_budget_ledger + dp_budget_caps exist, ledger "
        "append-only (UPDATE/DELETE/TRUNCATE RAISE via "
        f"{_GUARD_FN}, ENABLE ALWAYS), one cap row per "
        "(tenant, workflow-or-tenant-wide)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""burst-to-saas slice 3 — per-workflow consent and the tenant's brakes.

Adds:

  workflows.burst_eligible            BOOLEAN NOT NULL DEFAULT false
  workflows.burst_enabled_by_user_id  INTEGER REFERENCES users(id)
  workflows.burst_enabled_at          TIMESTAMPTZ
  tenant_burst_settings               (one row per tenant)

**Every existing workflow becomes ineligible**, which is the point. Consent
lives on the workflow because that is where someone knows what data flows
through it, and a migration that opted anything in would be making that
decision on their behalf — for data that then leaves their premises.

`burst_enabled_by/at` replace a second approver (design D10): requiring two
makes the feature unusable for a two-person shop, so accountability comes from
recording who decided rather than from blocking the decision.

`tenant_burst_settings.enabled` is a KILL SWITCH. Off disables burst
everywhere at once; on enables nothing by itself. Rows are created lazily on
first write, so absence means "never configured" and reads as disabled.

Run inside the API container:

    docker exec my_fastapi_app python3 migrations/20260811_burst_policy.py --commit

Idempotent — repeated runs are safe. Note the Vault dynamic credential means a
later run is a DIFFERENT Postgres role, so existence is tested before every
create rather than relying on IF NOT EXISTS, which checks ownership first.
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (table,))
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def _index_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (name,))
    return cur.fetchone() is not None


_WORKFLOW_COLUMNS = [
    ("burst_eligible", "BOOLEAN NOT NULL DEFAULT false"),
    ("burst_enabled_by_user_id", "INTEGER REFERENCES users(id)"),
    ("burst_enabled_at", "TIMESTAMPTZ"),
]

_SETTINGS_TABLE = """
CREATE TABLE tenant_burst_settings (
    id                      SERIAL PRIMARY KEY,
    tenant_id               INTEGER NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
    enabled                 BOOLEAN NOT NULL DEFAULT false,
    spill_threshold_minutes INTEGER NOT NULL DEFAULT 30,
    spend_ceiling           NUMERIC(12,2),
    spend_period_days       INTEGER NOT NULL DEFAULT 30,
    updated_by_user_id      INTEGER REFERENCES users(id),
    updated_at              TIMESTAMPTZ DEFAULT now(),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not _table_exists(cur, "workflows"):
        print("Table workflows absent — refusing to run.")
        return 1

    for name, ddl in _WORKFLOW_COLUMNS:
        if _column_exists(cur, "workflows", name):
            print(f"Column workflows.{name} already present.")
            continue
        if not args.commit:
            print(f"DRY RUN: would add workflows.{name}")
            continue
        cur.execute(f"ALTER TABLE workflows ADD COLUMN {name} {ddl}")
        print(f"Added workflows.{name}.")

    if _table_exists(cur, "tenant_burst_settings"):
        print("Table tenant_burst_settings already present.")
    elif not args.commit:
        print("DRY RUN: would CREATE TABLE tenant_burst_settings")
    else:
        cur.execute(_SETTINGS_TABLE)
        print("Created tenant_burst_settings.")

    idx = "ix_workflows_burst_eligible"
    if _index_exists(cur, idx):
        print(f"Index {idx} already present.")
    elif not args.commit:
        print(f"DRY RUN: would create index {idx}")
    else:
        # Partial: the scheduler only ever asks "which of these may spill",
        # and the overwhelming majority are false.
        cur.execute(
            f"CREATE INDEX {idx} ON workflows (tenant_id) WHERE burst_eligible"
        )
        print(f"Created index {idx}.")

    if args.commit:
        cur.execute("SELECT count(*) FROM workflows WHERE burst_eligible")
        eligible = cur.fetchone()[0]
        print(f"\nWorkflows currently eligible to spill: {eligible} "
              "(expected 0 immediately after this migration).")
    else:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

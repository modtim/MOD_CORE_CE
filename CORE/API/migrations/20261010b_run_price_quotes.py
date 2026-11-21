#!/usr/bin/env python3
"""sz-tcp.22 (tiered-credit-pricing decomp, owner decision 10-10) — the ONE
new table: RUN PRICE QUOTES (locked-quote persistence).

revision      : 20261010b_run_price_quotes
down_revision : 20261010a_wizard_rationale

Owner decision 10-10: users are charged the LOCKED QUOTE shown before Run,
never a re-estimate computed after the fact. This table is where that
locked number lives, one row per run — `services/run_price_quote.py`
writes it once (lock_quote, idempotent per run) and the eventual billing
charge path (sz-tcp.22 follow-up, NOT wired here) reads it back instead
of recomputing.

Columns, per the sz-tcp.22 decomp row + models.py RunPriceQuote class:
    id               BIGSERIAL PRIMARY KEY
    run_id           INTEGER NOT NULL UNIQUE FK workflow_runs(id)
                     (one quote per run, ever; no ON DELETE action — a
                     quote is kept as evidence even if the run row were
                     ever pruned, matching the no-hard-delete posture of
                     run_price_quotes itself)
    tenant_id        INTEGER NOT NULL FK tenants(id)
                     (indexed: every read is one tenant's quotes)
    quoted_credits   INTEGER NOT NULL      the locked total
    breakdown        JSONB NOT NULL DEFAULT '[]'::jsonb
                     per-node [{node_id, credits, pricing_source,
                     predicted_runtime_s}]
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()

Indexes: ix_run_price_quotes_tenant_created (tenant's quote history, most
recent first reads use this ordering).

FK refs use ON DELETE SET NULL would be wrong here (run_id/tenant_id are
NOT NULL) — plain REFERENCES, no ON DELETE clause, matches the "locked
quote outlives everything" intent. `id` is BIGSERIAL PRIMARY KEY, never a
max()-subquery DEFAULT.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010b_run_price_quotes.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "run_price_quotes"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-tcp.22, locked quote per run)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs (id),
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    quoted_credits INTEGER NOT NULL,
    breakdown JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ),
    (
        f"{_TABLE} index (tenant_id, created_at)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_created
    ON {_TABLE} (tenant_id, created_at);
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
                   help="Apply the CREATE TABLE + index steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (a locked quote is kept forever "
              "once written)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "sz-tcp.22 ready: run_price_quotes exists, tenant-scoped; "
        "services/run_price_quote.py can lock_quote/get_locked_quote."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""the ONE
new table: WIZARD RATIONALE (self-explanation persistence).

revision      : 20261010a_wizard_rationale
down_revision : 20261009l_tiered_pricing_calibrated_backfill

workflow-wizard-self-explanation §5.5/§5.6 — one row per decision point the
wizard captured during a compose turn: what the agent had to choose
(`step`), and why it chose what it did (`rationale`). The collector
(services/wizard_rationale.py) writes these throughout the agentic loop;
the final wizard response ships them as the `rationale` field
([{decision_point, chosen_option, alternatives_considered, reason}]) and
the panel renders them under "Why this workflow?".

Columns, per tasks.md §5.6 + the models.py WizardRationale class:
    id               BIGSERIAL PRIMARY KEY
    tenant_id        INTEGER NOT NULL FK tenants(id) ON DELETE CASCADE
                     (indexed: every read is one tenant's history; cross-
                     tenant leakage of design reasoning is the leak this
                     table exists to prevent)
    user_id          INTEGER FK users(id) ON DELETE SET NULL, nullable
                     (platform-initiated builds have no acting user)
    wizard_session_id VARCHAR(36) FK support_chat_sessions(id) SET NULL,
                     nullable (node-wizard builds have no chat session;
                     the FK target is a uuid4 string, so VARCHAR not INT)
    build_spec_id    INTEGER FK build_specs(id) SET NULL, nullable
                     (the node build this decision came from)
    step             VARCHAR(64) NOT NULL  decision-point label
    rationale        TEXT NOT NULL         human-readable reason
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
    deleted_at       TIMESTAMPTZ NULL      soft-delete tombstone (ISO 27001
                     no-hard-delete: a rationale row is evidence, never
                     un-recorded)

Indexes: ix_wizard_rationale_tenant_created (panel/response read: one
tenant's decisions in build order) and ix_wizard_rationale_tenant_session
("Why this workflow?" read: one tenant's decisions for one wizard
conversation). Plus the ix_<table>_live partial index the soft-delete
migration installs on hot-path tables, so the default
`deleted_at IS NULL` SELECT stays cheap after years of churn.

FK refs use ON DELETE SET NULL (two words) — never ONDELETE. `id` is
BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT (Postgres rejects
subqueries in a DEFAULT clause).

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010a_wizard_rationale.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "wizard_rationale"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-expr2.4, per-decision wizard self-explanation)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users (id) ON DELETE SET NULL,
    wizard_session_id VARCHAR(36) REFERENCES support_chat_sessions (id)
        ON DELETE SET NULL,
    build_spec_id INTEGER REFERENCES build_specs (id) ON DELETE SET NULL,
    step VARCHAR(64) NOT NULL,
    rationale TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL
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
    (
        f"{_TABLE} index (tenant_id, wizard_session_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_session
    ON {_TABLE} (tenant_id, wizard_session_id);
""",
    ),
    (
        f"{_TABLE} partial live index (deleted_at IS NULL)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_live
    ON {_TABLE} (id) WHERE deleted_at IS NULL;
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
        print("DRY  downgrade: no-op (ISO 27001 — never drop rationale "
              "evidence)")
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
        "sz-expr2.4 ready: wizard_rationale exists, tenant-scoped, "
        "soft-deletable; services/wizard_rationale.py can record/list."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

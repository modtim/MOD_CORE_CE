#!/usr/bin/env python3
"""wsip1.6 (workflow-self-improving-production decomp, §1.6) -- the ONE
new table: WIZARD DEBUG SESSIONS (one row per conversational-debug
session opened from a completed run's "Why did this run produce this
output?" button).

revision      : 20261012f_wizard_debug_sessions
down_revision : 20261012e_adversary_tables

Purpose: sz-self-improving.1 (Phase-1 DB migrations row) landed the
storage side of the WSIP family; this migration lands the debug-session
table that consumers 4.1 (mode=debug + bound_run_id on
/support/workflow-wizard/stream), 4.3 (the debug SSE vocabulary) and
4.4 (wizard_quality_signal event_type=debug_fix_applied) read and write.
Like its sibling 20261012b_production_drift_events.py, this migration
only creates the storage: no debug pipeline / writer / reader code is
wired here. The pure service already in tree, services/wizard_debug.py
(build_context_bundle, EVT_* constants), owns the event vocabulary this
table's `status` mirrors.

Columns, per the wsip1.6 decomp row:
    id                  BIGSERIAL PRIMARY KEY (never a max()-subquery
                        DEFAULT -- Postgres rejects subqueries in DEFAULT)
    tenant_id           INTEGER NOT NULL REFERENCES tenants (id)
                        ON DELETE CASCADE -- tenant deletion is a real
                        teardown, a user's debug history goes with it.
    user_id             INTEGER REFERENCES users (id)
                        ON DELETE SET NULL (three words; nullable) --
                        users.id is the INTEGER PK; a session whose user
                        has since been deleted stays readable as
                        history (ISO 27001: no hard delete).
    workflow_id         INTEGER REFERENCES workflows (id)
                        ON DELETE SET NULL (nullable) -- the debugged
                        workflow may be deleted later; the session row
                        still records what the user learned.
    bound_run_id        INTEGER REFERENCES workflow_runs (id)
                        ON DELETE SET NULL (nullable) -- the run the
                        session is anchored to (tasks.md 4.1
                        `bound_run_id`); SET NULL so the session
                        survives the run it interrogates.
    status              VARCHAR(32) NOT NULL DEFAULT 'debug_started'
                        with a CHECK over the closed session
                        vocabulary (see STATUS VOCABULARY note below).
    context_bundle_summary JSONB NULL -- sizes/ids ONLY (bundle row ids,
                        byte counts, node ids), never artifact content:
                        nothing here re-materializes a run artifact.
    root_cause          JSONB NULL -- the design.md Decision 6 shape
                        ({root_cause_node_id, ...}) plus the causal-graph
                        cross-check verdict.
    proposed_fix        JSONB NULL -- the LLM's structured fix proposal
                        (diff/param change + expected_outcome +
                        confidence), not yet applied.
    verification_run_id INTEGER REFERENCES workflow_runs (id)
                        ON DELETE SET NULL (nullable) -- the re-run
                        spawned to verify the proposed fix; SET NULL so
                        the session keeps its verification history after
                        that run is deleted.
    created_at / updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    applied_at          TIMESTAMPTZ NULL -- set once when status reaches
                        'fix_applied' (tasks.md 4.4 debug_fix_applied).
    deleted_at          TIMESTAMPTZ NULL -- soft delete only (ISO 27001,
                        no hard delete); no delete helper here or below.

Indexes:
    ix_wizard_debug_sessions_tenant_created on (tenant_id,
        created_at DESC) -- the per-tenant debug-session timeline.
    ix_wizard_debug_sessions_bound_run on (bound_run_id) -- "what have
        users already asked about this run?" lookup.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012f_wizard_debug_sessions.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "wizard_debug_sessions"

_STATUS_CHECK = (
    "'debug_started', 'context_bundle_assembled', "
    "'root_cause_identified', 'fix_proposed', "
    "'fix_verification_started', 'fix_verification_succeeded', "
    "'fix_verification_failed', 'fix_applied', "
    "'abandoned', 'failed'"
)

_STATEMENTS = [
    (
        f"{_TABLE} table (wsip1.6, one row per conversational-debug "
        "session)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users (id) ON DELETE SET NULL,
    workflow_id INTEGER REFERENCES workflows (id) ON DELETE SET NULL,
    bound_run_id INTEGER REFERENCES workflow_runs (id) ON DELETE SET NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'debug_started' CHECK
        (status IN ({_STATUS_CHECK})),
    context_bundle_summary JSONB,
    root_cause JSONB,
    proposed_fix JSONB,
    verification_run_id INTEGER REFERENCES workflow_runs (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);
""",
    ),
    (
        f"{_TABLE} index (tenant_id, created_at DESC)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_created
    ON {_TABLE} (tenant_id, created_at DESC);
""",
    ),
    (
        f"{_TABLE} index (bound_run_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_bound_run
    ON {_TABLE} (bound_run_id);
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
        print("DRY  downgrade: no-op (debug sessions are kept as "
              "historical user-surveillance data; the table is append-only "
              "with soft deletes only, so nothing is dropped on "
              "rollback)")
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
        "wsip1.6 ready: wizard_debug_sessions exists; the conversational-debug "
        "pipeline (tasks.md 4.1 mode=debug + bound_run_id, 4.3 SSE "
        "vocabulary, 4.4 debug_fix_applied) can write and read per-"
        "(tenant_id, created_at) debug rows."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""wsip1.5 (workflow-self-improving-production tasks.md 1.5, sz-self-improving.1)
-- the TWO new tables: ADVERSARY WORKFLOWS + ADVERSARY FINDINGS.

revision      : 20261012e_adversary_tables
down_revision : 20261012d_causal_graph_edges

Purpose: Phase-3 "adversarial co-evolution" (design.md line 147). For every
production workflow a paired adversary workflow runs on the per-tenant
background-job pool: it generates candidate inputs (genetic algorithm over
the input space, mutation operators per input type), dispatches the
production workflow against each candidate in sandbox mode
(workflow_runs.purpose='adversary_dispatch'), and records the candidates
that produced failures. This migration lands the STORAGE only -- no
registrar, no GA driver, no dispatch loop, no event emission -- the same
posture as its sibling 20261012b_production_drift_events.py.

adversary_workflows: one row per (production workflow, adversary pairing).
    id                  BIGSERIAL PRIMARY KEY -- never a max()-subquery
                        DEFAULT: Postgres rejects subqueries in DEFAULT.
    tenant_id           FK tenants (id) ON DELETE CASCADE NOT NULL -- an
                        adversary pairing is tenant-owned; when the tenant
                        is torn down its adversary goes with it.
    target_workflow_id  FK workflows (id) ON DELETE CASCADE NOT NULL -- the
                        production workflow this adversary attacks. CASCADE
                        (not SET NULL): the pairing is meaningless without
                        its target, so it is torn down with it.
    status              VARCHAR(16) NOT NULL DEFAULT 'active' with a CHECK
                        over exactly 'active' | 'paused' | 'torn_down' --
                        the registrar's lifecycle states (sz-self-improving.26
                        create/teardown). 'torn_down' is the explicit
                        teardown state, distinct from the ISO 27001
                        deleted_at tombstone.
    current_generation  INTEGER NOT NULL DEFAULT 0 CHECK (>= 0) -- the GA
                        generation this adversary has advanced to
                        (services/adversary_ga.next_generation() bumps it);
                        stored here so a resumed run continues where it
                        left off.
    config              JSONB NULL -- the pairing's knobs (population size,
                        GA seed, per-run budget, input_type). Tunables live
                        in the platform-settings registry, never env vars;
                        this row only records what THIS adversary was
                        created with, so a historical row stays
                        interpretable after the settings move.
    created_at/updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    torn_down_at        TIMESTAMPTZ NULL -- when status moved to
                        'torn_down' (registrar teardown timestamp).
    deleted_at          TIMESTAMPTZ NULL -- soft delete only (ISO 27001,
                        no hard delete); no delete helper here or below.

    PARTIAL UNIQUE uix_adversary_workflows_live_target on
        (target_workflow_id) WHERE deleted_at IS NULL
                           AND status <> 'torn_down'
        -- one LIVE adversary per production workflow, as a database fact:
        a second registrar create for the same target is a hard
        IntegrityError, while a torn-down pairing (or a soft-deleted one)
        falls outside the index, so re-arming a workflow after teardown
        works.
    ix_adversary_workflows_tenant on (tenant_id) -- the per-tenant
        adversary pool the background-job scheduler iterates.

adversary_findings: one row per persisted failure finding
    (sz-self-improving.30, services/adversary_classification.classify_failure
    -> Finding(failure_mode, severity, emit_critical_event, summary)).
    id                    BIGSERIAL PRIMARY KEY
    tenant_id             FK tenants (id) ON DELETE CASCADE NOT NULL
    adversary_workflow_id FK adversary_workflows (id) ON DELETE CASCADE
                          NOT NULL -- the finding belongs to the adversary
                          that produced it; when that pairing is torn down
                          its findings go with it.
    target_workflow_id    FK workflows (id) ON DELETE SET NULL, nullable --
                          the production workflow that broke. SET NULL
                          (three words, never ONDELETE): a finding for a
                          since-deleted workflow is still the record of a
                          real defect, so the row outlives its parent.
    workflow_run_id       FK workflow_runs (id) ON DELETE SET NULL,
                          nullable -- the sandbox dispatch
                          (purpose='adversary_dispatch') that produced the
                          outcome; SET NULL for the same reason.
    generation            INTEGER NOT NULL -- which GA generation this
                          candidate came from (Candidate.generation).
    severity              VARCHAR(16) NOT NULL, CHECK over EXACTLY the
                          SEVERITY_* values in services/adversary_
                          classification.py:
                            'informational' | 'low' | 'medium' | 'high'
                            | 'critical'
                          (SEVERITY_RANK orders them; the CHECK makes the
                          DB agree with the service, so a finding the
                          operator agent ranks 'critical' really is one).
    failure_class         VARCHAR(64) NULL -- see FIELD MAPPING note below.
    input_ref             JSONB NULL -- candidate INPUT REFERENCES/IDS ONLY
                          (Candidate.id e.g. 's13g0i4', input_type,
                          parent_ids, operators), NEVER the candidate
                          payload: a candidate may be image/tensor bytes,
                          and nothing here re-materializes one.
    detail                JSONB NULL -- finding-side context: the
                          truncated Finding.summary text (already payload-
                          free per adversary_classification._summary), the
                          emit_critical_event flag, run/outcome ids.
    found_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
    deleted_at            TIMESTAMPTZ NULL -- soft delete only (ISO 27001).

    ix_adversary_findings_target_severity_found on
        (target_workflow_id, severity, found_at DESC) -- the operator-
        agent / dashboard query: newest findings for one workflow,
        worst-first severity.
    ix_adversary_findings_tenant on (tenant_id).

FIELD MAPPING (services -> DDL, the one deviation from the brief's field
names, kept as the brief spells it and explained here rather than renamed):
    services/adversary_classification.py names its finding field
    `failure_mode` (FAILURE_MODE_* = 'timeout' | 'error' |
    'safety_scan_failure' | 'critic_needs_fix'). The brief's column is
    `failure_class`; the writer maps failure_mode -> failure_class 1:1.
    VARCHAR(64) is wide enough for any current or future mode name and
    carries no CHECK: the service owns the mode vocabulary (same free-text
    posture as `metric` on production_drift_events), and a CHECK here would
    make a new failure mode a migration.
    Finding.emit_critical_event is not a column -- it selects which event
    fires (adversary_found_failure_case vs adversary_found_critical_failure)
    and is mirrored into `detail` for historical rows.

Idempotent: CREATE TABLE / CREATE INDEX / CREATE UNIQUE INDEX IF NOT EXISTS;
safe to re-run. Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012e_adversary_tables.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_AW_TABLE = "adversary_workflows"
_AF_TABLE = "adversary_findings"

# Exactly services/adversary_classification.py's SEVERITY_* constants
# (SEVERITIES tuple, lines 72-77). The DB CHECK must agree with the
# service's SEVERITY_RANK or the operator agent can rank a row the
# classifier would never have emitted.
_SEVERITIES = ("informational", "low", "medium", "high", "critical")
# The registrar's lifecycle states (sz-self-improving.26 create/teardown).
_STATUSES = ("active", "paused", "torn_down")


def _sql_in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


_STATEMENTS = [
    (
        f"{_AW_TABLE} table (wsip1.5, one row per production-workflow "
        "adversary pairing)",
        f"""
CREATE TABLE IF NOT EXISTS {_AW_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    target_workflow_id INTEGER NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    current_generation INTEGER NOT NULL DEFAULT 0,
    config JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    torn_down_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    CONSTRAINT ck_{_AW_TABLE}_status CHECK (status IN {_sql_in(_STATUSES)}),
    CONSTRAINT ck_{_AW_TABLE}_generation_nonneg CHECK (current_generation >= 0)
);
""",
    ),
    (
        f"{_AW_TABLE} partial UNIQUE (target_workflow_id) WHERE live "
        "(deleted_at IS NULL AND status <> 'torn_down')",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uix_{_AW_TABLE}_live_target
    ON {_AW_TABLE} (target_workflow_id)
    WHERE deleted_at IS NULL AND status <> 'torn_down';
""",
    ),
    (
        f"{_AW_TABLE} index (tenant_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_AW_TABLE}_tenant
    ON {_AW_TABLE} (tenant_id);
""",
    ),
    (
        f"{_AF_TABLE} table (wsip1.5, one row per persisted adversary "
        "failure finding)",
        f"""
CREATE TABLE IF NOT EXISTS {_AF_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    adversary_workflow_id INTEGER NOT NULL REFERENCES {_AW_TABLE} (id) ON DELETE CASCADE,
    target_workflow_id INTEGER REFERENCES workflows (id) ON DELETE SET NULL,
    workflow_run_id INTEGER REFERENCES workflow_runs (id) ON DELETE SET NULL,
    generation INTEGER NOT NULL,
    severity VARCHAR(16) NOT NULL,
    failure_class VARCHAR(64),
    input_ref JSONB,
    detail JSONB,
    found_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    CONSTRAINT ck_{_AF_TABLE}_severity CHECK (severity IN {_sql_in(_SEVERITIES)})
);
""",
    ),
    (
        f"{_AF_TABLE} index (target_workflow_id, severity, found_at DESC)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_AF_TABLE}_target_severity_found
    ON {_AF_TABLE} (target_workflow_id, severity, found_at DESC);
""",
    ),
    (
        f"{_AF_TABLE} index (tenant_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_AF_TABLE}_tenant
    ON {_AF_TABLE} (tenant_id);
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
        print("DRY  downgrade: no-op (adversary pairings and their "
              "findings are kept as historical surveillance data; both "
              "tables are append-only with soft deletes only, so "
              "nothing is dropped on rollback)")
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
        "wsip1.5 ready: adversary_workflows + adversary_findings exist; "
        "the registrar (sz-self-improving.26) can create/teardown "
        "adversary pairings and the persist step (sz-self-improving.30) "
        "can write findings."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

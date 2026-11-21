#!/usr/bin/env python3
"""wsip 1.8 (workflow-self-improving-production decomp, §1.8) -- the TWO
new tables: AUTONOMOUS OPERATOR PROPOSALS + AUTONOMOUS OPERATOR DEPLOYMENTS
(the proposal the operator agent files, and the canary deployment that
carries a proposed fix into production).

revision      : 20261012g_autonomous_operator_tables
down_revision : 20261012f_wizard_debug_sessions

Purpose: the autonomous operator's propose/monitor/rollback loop needs
somewhere to put what it found and what it did. The PURE services already
landed -- services/autonomous_degradation.py (score + evidence shape),
services/autonomous_canary.py (canary routing + promote/rollback verdicts),
services/autonomous_propose_fix.py (fix-prompt synthesis + wizard-event
outcomes) -- compute everything and return dataclasses; nothing persists.
This migration lands the storage only, same posture as its sibling
20261012b_production_drift_events.py: no writer/reader code is wired here.

Consumers (later tasks, not this one): 7.5 persists proposals with full
evidence + wizard test-run lineage refs; 7.6 is the Operator Proposals
dashboard; 7.7 caps fix attempts at 3 per (workflow, day) -- that is the
(workflow_id, created_at DESC) index right here; 8.x runs the canary
deployments against autonomous_operator_deployments.

Columns, per the wsip 1.8 decomp row:
    autonomous_operator_proposals
        id                  BIGSERIAL PRIMARY KEY (never a max()-subquery
                            DEFAULT -- Postgres rejects subqueries in DEFAULT)
        tenant_id           INTEGER NOT NULL REFERENCES tenants (id)
                            ON DELETE CASCADE -- a tenant teardown really
                            does discard its proposal history.
        workflow_id         INTEGER NOT NULL REFERENCES workflows (id)
                            ON DELETE CASCADE -- the proposal is a fix FOR
                            this workflow; 7.7 counts them per (workflow,
                            day) and 7.6 lists them per workflow, so the
                            row is meaningless without its parent.
        status              VARCHAR(24) NOT NULL DEFAULT 'proposed' with a
                            CHECK over exactly:
                              proposed | blocked_on_user | approved |
                              rejected | deployed | superseded | failed
                            'blocked_on_user' is the propose-only phase-4
                            state (design.md Phase 4: the operator proposes,
                            a human approves); 'deployed' means a deployment
                            row exists; 'superseded' is the soft-replacement
                            state (no hard delete, ISO 27001).
        degradation_score   DOUBLE PRECISION NULL -- DegradationScore.score
                            from services/autonomous_degradation.py (a float
                            composite, so DOUBLE, never FLOAT/numeric).
        evidence            JSONB NULL -- DegradationEvidence-shaped ids/
                            metrics ONLY (drift_status, error rates,
                            predictor Brier scores, adversary severities):
                            nothing here re-materializes an output artifact.
        fix_prompt          TEXT NULL -- the synthesized propose-fix prompt
                            (services/autonomous_propose_fix.py, capped by
                            the autonomous_operator.fix_prompt_max_chars
                            platform setting, not an env var).
        proposed_graph_json JSONB NULL -- the candidate graph the operator
                            wants to swap in, kept as data so 7.6 can diff
                            it against the live graph.
        wizard_lineage      JSONB NULL -- test-run ids + wizard session /
                            proposal ids: the debug-session lineage that
                            proves the candidate was actually exercised
                            before it was proposed (7.5).
        base_version_id     INTEGER REFERENCES workflow_versions (id)
                            ON DELETE SET NULL (three words; nullable) --
                            the version the proposal was cut against; a
                            since-deleted version stays resolvable as
                            history, so the row outlives its parent.
        reviewed_by         INTEGER REFERENCES users (id)
                            ON DELETE SET NULL (nullable) -- the human who
                            approved/rejected it (phase 4); a deleted
                            reviewer must not orphan the decision record.
        reviewed_at         TIMESTAMPTZ NULL
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        deleted_at          TIMESTAMPTZ NULL -- soft delete only (ISO 27001,
                            no hard delete); no delete helper here or below.

    autonomous_operator_deployments
        id                  BIGSERIAL PRIMARY KEY
        tenant_id           INTEGER NOT NULL REFERENCES tenants (id)
                            ON DELETE CASCADE
        proposal_id         INTEGER NOT NULL REFERENCES
                            autonomous_operator_proposals (id)
                            ON DELETE CASCADE -- a deployment is the
                            execution OF a proposal; the FK is what 8.x
                            reads to find a proposal's canary history.
        workflow_id         INTEGER NOT NULL REFERENCES workflows (id)
                            ON DELETE CASCADE -- repeated from the
                            proposal so the (workflow_id, started_at DESC)
                            timeline needs no join.
        new_version_id      INTEGER REFERENCES workflow_versions (id)
                            ON DELETE SET NULL (nullable) -- the version
                            created when the candidate was deployed; SET
                            NULL so a rolled-back/deleted version still
                            names what was tried.
        stage               VARCHAR(24) NOT NULL, CHECK over the canary
                            vocabulary actually used in
                            services/autonomous_canary.py (see the
                            STAGE mapping below):
                              canary | continue | promote | rollback |
                              production
        traffic_pct         DOUBLE PRECISION NULL -- the canary split in
                            force for this window (wsip.autonomous_operator_
                            canary_pct, 0..100; kept per row so a historical
                            window stays interpretable after the setting
                            moves).
        metrics             JSONB NULL -- CanaryStats-shaped window
                            evidence (canary_run_count, quality_signal_delta,
                            error_rate_delta, safety_scan_failures) plus the
                            CanaryThresholds snapshot the verdict used.
        started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        finished_at         TIMESTAMPTZ NULL
        deleted_at          TIMESTAMPTZ NULL -- soft delete only.

STAGE mapping (services/autonomous_canary.py, grep of its literals):
    route_variant() -> Literal["canary", "production"]
    CanaryDecision.action -> Literal["rollback", "promote", "continue"]
    Those five strings are the whole vocabulary the service emits, so the
    CHECK accepts exactly them:
        'canary'    -- window is live: the candidate is receiving its
                       route_variant() canary share of traffic.
        'continue'  -- evaluate_canary() said continue: keep the window
                       open (also covers 'insufficient_runs').
        'promote'   -- evaluate_canary() said promote: widen toward 100%.
        'rollback'  -- evaluate_canary() said rollback: kill the candidate.
        'production'-- terminal: route_variant() production arm, i.e. the
                       candidate now owns 100% of the traffic (promoted).
    No other stage name is invented here.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012g_autonomous_operator_tables.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_PROPOSALS_TABLE = "autonomous_operator_proposals"
_DEPLOYMENTS_TABLE = "autonomous_operator_deployments"

_STATEMENTS = [
    (
        f"{_PROPOSALS_TABLE} table (wsip 1.8, one row per operator fix "
        "proposal)",
        f"""
CREATE TABLE IF NOT EXISTS {_PROPOSALS_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_id INTEGER NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    status VARCHAR(24) NOT NULL DEFAULT 'proposed'
        CONSTRAINT ck_autonomous_operator_proposals_status CHECK (
        status IN ('proposed', 'blocked_on_user', 'approved', 'rejected',
                   'deployed', 'superseded', 'failed')
    ),
    degradation_score DOUBLE PRECISION,
    evidence JSONB,
    fix_prompt TEXT,
    proposed_graph_json JSONB,
    wizard_lineage JSONB,
    base_version_id INTEGER REFERENCES workflow_versions (id)
        ON DELETE SET NULL,
    reviewed_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
""",
    ),
    (
        f"{_PROPOSALS_TABLE} index (workflow_id, created_at DESC) "
        "[7.7 per-workflow daily fix-attempt cap]",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_PROPOSALS_TABLE}_workflow_created
    ON {_PROPOSALS_TABLE} (workflow_id, created_at DESC);
""",
    ),
    (
        f"{_PROPOSALS_TABLE} index (tenant_id, status) "
        "[7.6 Operator Proposals dashboard]",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_PROPOSALS_TABLE}_tenant_status
    ON {_PROPOSALS_TABLE} (tenant_id, status);
""",
    ),
    (
        f"{_DEPLOYMENTS_TABLE} table (wsip 1.8, one row per canary "
        "deployment of a proposal)",
        f"""
CREATE TABLE IF NOT EXISTS {_DEPLOYMENTS_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    proposal_id INTEGER NOT NULL REFERENCES {_PROPOSALS_TABLE} (id)
        ON DELETE CASCADE,
    workflow_id INTEGER NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    new_version_id INTEGER REFERENCES workflow_versions (id)
        ON DELETE SET NULL,
    stage VARCHAR(24) NOT NULL
        CONSTRAINT ck_autonomous_operator_deployments_stage CHECK (
        stage IN ('canary', 'continue', 'promote', 'rollback',
                  'production')
    ),
    traffic_pct DOUBLE PRECISION,
    metrics JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);
""",
    ),
    (
        f"{_DEPLOYMENTS_TABLE} index (proposal_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_DEPLOYMENTS_TABLE}_proposal
    ON {_DEPLOYMENTS_TABLE} (proposal_id);
""",
    ),
    (
        f"{_DEPLOYMENTS_TABLE} index (workflow_id, started_at DESC)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_DEPLOYMENTS_TABLE}_workflow_started
    ON {_DEPLOYMENTS_TABLE} (workflow_id, started_at DESC);
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
        print("DRY  downgrade: no-op (proposals and canary deployments are "
              "kept as historical surveillance data; both tables are "
              "append-only with soft deletes only, so nothing is dropped "
              "on rollback)")
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
        "wsip 1.8 ready: autonomous_operator_proposals + "
        "autonomous_operator_deployments exist; 7.5/7.6/7.7 can write and "
        "read per-(workflow_id, created_at) proposal rows and 8.x can run "
        "canary deployments against them."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

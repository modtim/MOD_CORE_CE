#!/usr/bin/env python3
"""wsip1.c+d (workflow-self-improving-production decomp, §1.c/§1.d) -- the ONE
new table: PRODUCTION DRIFT EVENTS (one row per DETECTED production-drift
event, the append-only output of the drift sweep that reads
production_output_fingerprints).

revision      : 20261012b_production_drift_events
down_revision : 20261011j_workflow_parameters_column

Purpose: sz-self-improving.1a (commit 596b76e5c,
migrations/20261010g_production_output_fingerprints.py) landed the
SAMPLED side -- one fingerprint row per production output. This migration
lands the DETECTED side: when a later drift sweep (sz-self-improving.4/.5/.6,
NOT this task) decides a workflow's recent outputs have drifted from its
baseline, it writes one row here. This migration only creates the storage;
no sweep/writer/reader code is wired here, same posture as its sibling.

Columns, per the wsip1.c+d decomp row:
    id                  BIGSERIAL PRIMARY KEY (never a max()-subquery
                        DEFAULT -- Postgres rejects subqueries in DEFAULT)
    tenant_id           INTEGER NOT NULL REFERENCES tenants (id)
                        ON DELETE CASCADE -- tenant deletion is a real
                        teardown, its drift history goes with it.
    workflow_id         INTEGER REFERENCES workflows (id)
                        ON DELETE SET NULL (three words; nullable) -- a
                        drift event for a since-deleted workflow is still
                        useful history, so the row outlives its parent.
    fingerprint_kind    VARCHAR(40) NOT NULL -- free text, exactly as on
                        the sibling fingerprints table
                        (image_embedding, text_embedding, tensor_stats,
                        schema_hash, binary_hash, ...);
                        services/drift_fingerprint.py owns valid kinds.
    metric              VARCHAR(16) NOT NULL -- free text, no CHECK; the
                        sweep owns valid values. Today it emits exactly
                        two:
                          'mmd'    -- moving-mean difference of the
                                      candidate window against the
                                      reference window.
                          'zscore' -- reference-window standard
                                      deviations the candidate sample
                                      sits out at.
    value               DOUBLE PRECISION NOT NULL -- the computed metric
                        score.
    threshold           DOUBLE PRECISION NOT NULL -- the cut the sweep
                        applied when it called this drift.
    reference_window_days INTEGER NOT NULL DEFAULT 30
    candidate_window_days INTEGER NOT NULL DEFAULT 7
                        -- the windows this detection compared, stored
                        per row so a historical detection stays
                        interpretable after the sweep's tunables
                        (platform-settings registry, not env vars) move.
    sample_examples     JSONB NULL -- fingerprint ROW IDS only
                        (production_output_fingerprints.id), never output
                        content: nothing here re-materializes a
                        production artifact.
    detail              JSONB NULL -- sweep-side context
                        (baseline stats, sample counts, settings snapshot).
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
    deleted_at          TIMESTAMPTZ NULL -- soft delete only (ISO 27001,
                        no hard delete); no delete helper here or below.

Indexes:
    ix_production_drift_events_workflow_detected on
        (workflow_id, detected_at) -- the per-workflow drift timeline.
    ix_production_drift_events_tenant on (tenant_id).

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012b_production_drift_events.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "production_drift_events"

_STATEMENTS = [
    (
        f"{_TABLE} table (wsip1.c+d, one row per detected production-drift "
        "event)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_id INTEGER REFERENCES workflows (id) ON DELETE SET NULL,
    fingerprint_kind VARCHAR(40) NOT NULL,
    metric VARCHAR(16) NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    threshold DOUBLE PRECISION NOT NULL,
    reference_window_days INTEGER NOT NULL DEFAULT 30,
    candidate_window_days INTEGER NOT NULL DEFAULT 7,
    sample_examples JSONB,
    detail JSONB,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
""",
    ),
    (
        f"{_TABLE} index (workflow_id, detected_at)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_workflow_detected
    ON {_TABLE} (workflow_id, detected_at);
""",
    ),
    (
        f"{_TABLE} index (tenant_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant
    ON {_TABLE} (tenant_id);
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
        print("DRY  downgrade: no-op (drift detections are kept as "
              "historical surveillance data; the table is append-only "
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
        "wsip1.c+d ready: production_drift_events exists; the drift sweep "
        "(sz-self-improving.4/.5/.6) can write and read per-(workflow_id, "
        "detected_at) drift rows."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

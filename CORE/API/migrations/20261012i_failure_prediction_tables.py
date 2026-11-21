#!/usr/bin/env python3
"""wsip1.3+1.4 (workflow-self-improving-production decomp, §1.3/§1.4) -- the TWO
new tables: FAILURE PREDICTION MODELS + FAILURE PREDICTION FEATURES (one row per
dispatch-time prediction, the append-only training/calibration log).

revision      : 20261012i_failure_prediction_tables
down_revision : 20261012h_workflow_memory_tables

Purpose: services/failure_predictor.py is already landed and is PURE (it never
touches a DB): extract_features() hands the caller a numeric-only feature dict,
dump_model() hands the caller an HMAC-SHA256-signed BYTES blob, shadow_record()
hands the caller a {predicted_failure_probability, actual_outcome, mode} dict,
and CalibrationTracker measures the spec's "when the predictor returns 0.8, ~80%
SHALL actually fail" rule. This migration lands the storage those four need:
the model table (nightly training, WSIP 3.2/3.3 global fallback) and the
feature/prediction table (pre-dispatch persist, WSIP 3.4; shadow record, WSIP
3.5; calibration, WSIP 3.7). No writer/reader code is wired here, same posture
as its sibling migrations.

failure_prediction_models is created FIRST: failure_prediction_features.model_id
FKs it, so the target must exist before the referencing CREATE runs.

Columns, per the wsip1.3+1.4 decomp row:
    failure_prediction_models
        id                  BIGSERIAL PRIMARY KEY (never a max()-subquery
                            DEFAULT -- Postgres rejects subqueries in DEFAULT)
        tenant_id           INTEGER NULL REFERENCES tenants (id) ON DELETE
                            CASCADE -- NULL = the GLOBAL fallback model (WSIP
                            3.3), which is not owned by any tenant and so has
                            no teardown to follow.
        workflow_id         INTEGER NULL REFERENCES workflows (id) ON DELETE
                            CASCADE -- NULL = global-scope model. A model for
                            a since-deleted workflow is dropped with it: the
                            features rows keep pointing at it via their own
                            SET NULL FK, and training history is re-derivable.
        scope               VARCHAR(16) NOT NULL CHECK in ('workflow','global')
        model_blob          BYTEA NULL -- exactly dump_model() output: MAGIC
                            || joblib payload || HMAC-SHA256 digest. load_model
                            verifies the signature before unpickling, so a
                            tampered row never reaches the unpickler.
        blob_ref            JSONB NULL -- S3 pointer alternative for blobs too
                            big to keep inline (bucket/key JSON).
        training_rows       INTEGER NOT NULL DEFAULT 0 -- how many feature
                            rows this model was fit on (the spec's >=100 gate
                            is visible per model, not just in the trainer).
        metrics             JSONB NULL -- CalibrationTracker output
                            (count, brier, buckets) plus AUC.
        trained_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        is_active           BOOLEAN NOT NULL DEFAULT FALSE -- exactly one live
                            active model per workflow (and one global); the
                            nightly trainer stages a new model, then swaps.
        deleted_at          TIMESTAMPTZ NULL -- soft delete only (ISO 27001).
    failure_prediction_features
        id                  BIGSERIAL PRIMARY KEY
        tenant_id           INTEGER NOT NULL REFERENCES tenants (id) ON DELETE
                            CASCADE
        workflow_id         INTEGER NULL REFERENCES workflows (id) ON DELETE
                            SET NULL -- a prediction for a since-deleted
                            workflow stays as training history.
        workflow_run_id     INTEGER NULL REFERENCES workflow_runs (id) ON DELETE
                            SET NULL -- the run this prediction was made FOR;
                            NULL for synthetic/backfill rows.
        features            JSONB NOT NULL -- the extract_features() dict:
                            exactly FEATURE_ORDER keys, all floats. NO payload
                            content, no file names, no user data -- numeric
                            signals only (node/edge/input counts, byte sizes,
                            hour/day, recent failure rates, accel-kind one-hot).
        predicted_failure_probability DOUBLE PRECISION NULL -- predict() output
                            = P(FAILURE) under the module-wide convention.
        mode                VARCHAR(16) NOT NULL CHECK in ('off','shadow','enforce')
                            -- the WSIP mode this prediction ran under; only
                            'shadow' produces a record (shadow_record()).
        model_id            BIGINT NULL FK failure_prediction_models ON DELETE
                            SET NULL -- which model scored this row (NULL =
                            scored before any model existed).
        actual_outcome      SMALLINT NULL CHECK in (0,1) -- 1 = FAILURE
                            (module-wide convention), filled POST-RUN; NULL
                            until the outcome is recorded.
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        outcome_recorded_at TIMESTAMPTZ NULL
        deleted_at          TIMESTAMPTZ NULL -- soft delete only.

Indexes:
    ix_failure_prediction_features_workflow_created on (workflow_id, created_at
        DESC) -- the per-workflow training timeline.
    ix_failure_prediction_features_tenant on (tenant_id).
    ix_failure_prediction_features_unrecorded partial on (created_at) WHERE
        actual_outcome IS NULL -- the post-run outcome backfill sweep.
    uq_failure_prediction_models_active_workflow partial UNIQUE on (workflow_id)
        WHERE is_active AND deleted_at IS NULL AND workflow_id IS NOT NULL.
    uq_failure_prediction_models_active_global partial UNIQUE on ((1)) WHERE
        is_active AND scope = 'global' AND deleted_at IS NULL.

Idempotent: CREATE TABLE / CREATE INDEX / CREATE UNIQUE INDEX IF NOT EXISTS;
safe to re-run. Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012i_failure_prediction_tables.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_MODELS_TABLE = "failure_prediction_models"
_FEATURES_TABLE = "failure_prediction_features"

_STATEMENTS = [
    (
        f"{_MODELS_TABLE} table (wsip1.3, the predictor's model store; created "
        "FIRST because failure_prediction_features.model_id FKs it)",
        f"""
CREATE TABLE IF NOT EXISTS {_MODELS_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_id INTEGER REFERENCES workflows (id) ON DELETE CASCADE,
    scope VARCHAR(16) NOT NULL
        CONSTRAINT ck_failure_prediction_models_scope
        CHECK (scope IN ('workflow', 'global')),
    model_blob BYTEA,
    blob_ref JSONB,
    training_rows INTEGER NOT NULL DEFAULT 0,
    metrics JSONB,
    trained_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at TIMESTAMPTZ,
    CONSTRAINT ck_failure_prediction_models_blob_or_ref
        CHECK ((model_blob IS NOT NULL) OR (blob_ref IS NOT NULL))
);
""",
    ),
    (
        f"{_FEATURES_TABLE} table (wsip1.4, one row per dispatch-time "
        "prediction; features = the numeric-only extract_features() dict)",
        f"""
CREATE TABLE IF NOT EXISTS {_FEATURES_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_id INTEGER REFERENCES workflows (id) ON DELETE SET NULL,
    workflow_run_id INTEGER REFERENCES workflow_runs (id) ON DELETE SET NULL,
    features JSONB NOT NULL,
    predicted_failure_probability DOUBLE PRECISION,
    mode VARCHAR(16) NOT NULL
        CONSTRAINT ck_failure_prediction_features_mode
        CHECK (mode IN ('off', 'shadow', 'enforce')),
    model_id BIGINT REFERENCES {_MODELS_TABLE} (id) ON DELETE SET NULL,
    actual_outcome SMALLINT
        CONSTRAINT ck_failure_prediction_features_outcome
        CHECK (actual_outcome IN (0, 1)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    outcome_recorded_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);
""",
    ),
    (
        f"{_FEATURES_TABLE} index (workflow_id, created_at DESC)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_FEATURES_TABLE}_workflow_created
    ON {_FEATURES_TABLE} (workflow_id, created_at DESC);
""",
    ),
    (
        f"{_FEATURES_TABLE} index (tenant_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_FEATURES_TABLE}_tenant
    ON {_FEATURES_TABLE} (tenant_id);
""",
    ),
    (
        f"{_FEATURES_TABLE} partial index (created_at) WHERE actual_outcome "
        "IS NULL (the post-run outcome backfill sweep)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_FEATURES_TABLE}_unrecorded
    ON {_FEATURES_TABLE} (created_at) WHERE actual_outcome IS NULL;
""",
    ),
    (
        f"{_MODELS_TABLE} partial UNIQUE: one active model per workflow",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_MODELS_TABLE}_active_workflow
    ON {_MODELS_TABLE} (workflow_id)
    WHERE is_active AND deleted_at IS NULL AND workflow_id IS NOT NULL;
""",
    ),
    (
        f"{_MODELS_TABLE} partial UNIQUE: one active global fallback model",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_MODELS_TABLE}_active_global
    ON {_MODELS_TABLE} ((1))
    WHERE is_active AND scope = 'global' AND deleted_at IS NULL;
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
        print("DRY  downgrade: no-op (predictions and models are kept as "
              "historical training data; both tables are append-only with "
              "soft deletes only, so nothing is dropped on rollback)")
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
        "wsip1.3+1.4 ready: failure_prediction_models + failure_prediction_"
        "features exist; nightly training (3.2), pre-dispatch persist (3.4), "
        "shadow record (3.5) and calibration (3.7) can write and read them."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

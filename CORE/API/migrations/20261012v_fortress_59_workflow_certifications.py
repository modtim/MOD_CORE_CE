#!/usr/bin/env python3
"""Fortress 5.9 — workflow_certifications + certifiers tables.

revision      : 20261012v_fortress_59_workflow_certifications
down_revision : 20261012u_fortress_36c_output_scan_exemptions

Design: per-workflow certification store with a certifier registry.
Soft-delete everywhere (ISO 27001). Both tables carry a BEFORE DELETE
trigger that raises (RESTRICT hard delete).

SOFT DELETE ONLY (ISO 27001): ``deleted_at TIMESTAMPTZ`` — the clear path
stamps it, re-set creates a new live row, history is kept.

FKs to tenants/workflows/users:
  - tenant_id, workflow_id: NOT NULL -> ON DELETE RESTRICT (can't leave
    orphan, and the parent itself is soft-deleted).
  - certifier_id, created_by_user_id: NULL -> ON DELETE SET NULL
    (the certifier/user may be soft-deleted without breaking the cert).

id columns are BIGSERIAL PRIMARY KEY. FK columns match their parent PK
type: tenant_id/workflow_id/created_by_user_id/revoked_by_user_id are
INTEGER (tenants.id/workflows.id/users.id are Integer); certifier_id is
BIGINT (certifiers.id is BIGSERIAL).

Fully idempotent: CREATE TABLE / CREATE INDEX all IF NOT EXISTS; safe to
re-run. Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; ``--commit`` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012v_fortress_59_workflow_certifications.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_CERTIFIERS = "certifiers"
_CERTS = "workflow_certifications"
_NO_DELETE_FN = "cert_no_hard_delete"


def _sql_statements() -> list[tuple[str, str]]:
    """Ordered (label, sql) steps; idempotent."""
    steps: list[tuple[str, str]] = []

    # --- certifiers table ------------------------------------------------
    steps.append((
        f"{_CERTIFIERS} table (fortress 5.9, certifier registry)",
        f"""
CREATE TABLE IF NOT EXISTS {_CERTIFIERS} (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    public_key_b64 TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
""",
    ))

    # --- certifiers unique index (partial on name, live-only) -------------
    steps.append((
        f"{_CERTIFIERS} partial UNIQUE (name) live-only",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_CERTIFIERS}_name
    ON {_CERTIFIERS} (name)
    WHERE deleted_at IS NULL;
""",
    ))

    # --- workflow_certifications table ------------------------------------
    steps.append((
        f"{_CERTS} table (fortress 5.9, certification store)",
        f"""
CREATE TABLE IF NOT EXISTS {_CERTS} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    workflow_id INTEGER NOT NULL REFERENCES workflows (id) ON DELETE RESTRICT,
    certifier_id BIGINT NULL REFERENCES certifiers (id) ON DELETE SET NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked', 'expired')),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    created_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    revoked_at TIMESTAMPTZ,
    revoked_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    revoke_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    CHECK (expires_at > issued_at)
);
""",
    ))

    # --- workflow_certifications index (tenant_id, workflow_id, live-only)
    steps.append((
        f"{_CERTS} partial index ix_{_CERTS}_tenant_wf (live)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_CERTS}_tenant_wf
    ON {_CERTS} (tenant_id, workflow_id)
    WHERE deleted_at IS NULL;
""",
    ))

    # --- workflow_certifications status index ----------------------------
    steps.append((
        f"{_CERTS} index on status for active lookup",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_CERTS}_status
    ON {_CERTS} (status);
""",
    ))

    # --- workflow_certifications expires index ---------------------------
    steps.append((
        f"{_CERTS} index on expires_at for expiry scan",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_CERTS}_expires
    ON {_CERTS} (expires_at);
""",
    ))

    # --- Guard function: no hard delete (allow UPDATE) -------------------
    steps.append((
        f"function {_NO_DELETE_FN}() (no hard delete, allow soft-delete via UPDATE)",
        f"""
CREATE OR REPLACE FUNCTION {_NO_DELETE_FN}()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Hard delete is forbidden on % (ISO 27001: use soft-delete via UPDATE with deleted_at)',
        TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END;
$$ LANGUAGE plpgsql
""",
    ))

    # --- Guard triggers (BEFORE DELETE only; ENABLE ALWAYS for test compat)
    # The test harness uses session_replication_role=replica, which suppresses
    # ordinary (ENABLE REPLICATED) triggers.  ENABLE ALWAYS ensures the guard
    # fires in both production and test environments.
    steps.append((
        f"append-only guard triggers on {_CERTIFIERS} (DELETE only, UPDATE ok)",
        f"""
DO $$
BEGIN
    IF to_regclass('{_CERTIFIERS}') IS NULL THEN
        RAISE NOTICE '{_CERTIFIERS} does not exist yet; skipping {_CERTIFIERS} guard trigger';
        RETURN;
    END IF;
    DROP TRIGGER IF EXISTS trg_no_delete_{_CERTIFIERS} ON {_CERTIFIERS};
    CREATE TRIGGER trg_no_delete_{_CERTIFIERS}
        BEFORE DELETE ON {_CERTIFIERS}
        FOR EACH ROW EXECUTE FUNCTION {_NO_DELETE_FN}();
    ALTER TABLE {_CERTIFIERS}
        ENABLE ALWAYS TRIGGER trg_no_delete_{_CERTIFIERS};
END
$$
""",
    ))

    steps.append((
        f"append-only guard triggers on {_CERTS} (DELETE only, UPDATE ok)",
        f"""
DO $$
BEGIN
    IF to_regclass('{_CERTS}') IS NULL THEN
        RAISE NOTICE '{_CERTS} does not exist yet; skipping {_CERTS} guard trigger';
        RETURN;
    END IF;
    DROP TRIGGER IF EXISTS trg_no_delete_{_CERTS} ON {_CERTS};
    CREATE TRIGGER trg_no_delete_{_CERTS}
        BEFORE DELETE ON {_CERTS}
        FOR EACH ROW EXECUTE FUNCTION {_NO_DELETE_FN}();
    ALTER TABLE {_CERTS}
        ENABLE ALWAYS TRIGGER trg_no_delete_{_CERTS};
END
$$
""",
    ))

    return steps


def _apply(execute) -> list[str]:
    """Run every step through ``execute(sql)``; return the labels applied.

    Kept separate from ``main()`` so the test can drive it with the conftest
    ``db_session`` (never the live DB engine).
    """
    applied = []
    for label, sql in _sql_statements():
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
                   help="Apply the CREATE TABLE + index + trigger steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _sql_statements():
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (soft deletes only; the clear path "
              "stamps deleted_at, re-set creates a new live row)")
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
    print(f"{_CERTS} + {_CERTIFIERS} ready: fortress 5.9 workflow certifications.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

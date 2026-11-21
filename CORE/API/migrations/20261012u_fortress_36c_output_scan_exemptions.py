#!/usr/bin/env python3
"""fortress-3.6c S1 — pre-authorised output-safety exemption store.

revision      : 20261012u_fortress_36c_output_scan_exemptions
down_revision : 20261012t_fortress_51_subscription_fk_restrict

Design Decision (design_fortress_36c.md sections 4-5, S1): a block that a
platform admin has ALREADY authorised must not re-block. The store is a new
table, not an ALTER of ``output_scan_configs`` (no reason / approver / expiry /
finding-code dimension there, and its live UNIQUE would collide; ALTERing an
existing table is the bind-mount/migration hazard).

``output_scan_exemptions`` (SoftDeleteWithActorMixin idiom):
    id BIGSERIAL PRIMARY KEY
    tenant_id INTEGER NOT NULL   FK tenants(id) ON DELETE RESTRICT
    workflow_id INTEGER NULL     FK workflows(id) ON DELETE SET NULL (NULL = tenant-wide)
    node_kind VARCHAR(250) NULL  same key spelling as node_lockdown_exceptions
    scanner VARCHAR(64) NULL     NULL = any scanner
    finding_code VARCHAR(64) NULL NULL = any finding
    reason TEXT NOT NULL         DB-enforced non-blank (router + service also refuse)
    created_by_user_id / approved_by_user_id FK users(id) ON DELETE SET NULL
    approved_at / expires_at TIMESTAMPTZ NULL
    self_approved BOOLEAN NOT NULL DEFAULT FALSE   (solo-mode maker-checker escape)
    created_at / updated_at / deleted_at / deleted_by_user_id

FK actions: ``tenants`` is ON DELETE RESTRICT — a hard delete of the owning
tenant must NOT silently drop the durable compliance record (the exemption IS
the audit evidence that a block was authorised). Every other FK is
``ON DELETE SET NULL`` (two words), never CASCADE, so a parent hard delete
orphans nothing.

ISO 27001 — NO hard delete: revoke stamps ``deleted_at`` (+ actor). A guard
trigger pair refuses DELETE and TRUNCATE of exemption rows; UPDATE stays
ALLOWED (revoke = deleted_at set; approval = approved_* set), which is why
this is NOT the 20261009f append-only pair (that one also refuses UPDATE).

``id`` is BIGSERIAL PRIMARY KEY — never a max()-subquery DEFAULT (Postgres
rejects subqueries in a DEFAULT clause).

Fully idempotent: CREATE TABLE / CREATE ... INDEX all IF NOT EXISTS, CREATE
OR REPLACE FUNCTION, DROP TRIGGER IF EXISTS + CREATE TRIGGER inside a
to_regclass existence check, so a re-run is a no-op. Additive only.

DRY-RUN by default; ``--commit`` applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012u_fortress_36c_output_scan_exemptions.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "output_scan_exemptions"
_GUARD_FN = "output_scan_exemptions_no_hard_delete"
_UQ = "uq_output_scan_exemptions_live_key"
_IX = "ix_output_scan_exemptions_tenant_wf"


def _sql_statements() -> list[tuple[str, str]]:
    """Ordered (label, sql) steps; idempotent."""
    steps = []
    steps.append((
        f"{_TABLE} table (fortress 3.6c, exemption store)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE RESTRICT,
    workflow_id INTEGER NULL REFERENCES workflows (id) ON DELETE SET NULL,
    node_kind VARCHAR(250) NULL,
    scanner VARCHAR(64) NULL,
    finding_code VARCHAR(64) NULL,
    reason TEXT NOT NULL,
    created_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    approved_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    approved_at TIMESTAMPTZ NULL,
    self_approved BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    deleted_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT ck_output_scan_exemptions_reason_required
        CHECK (LENGTH (BTRIM (reason)) > 0),
    CONSTRAINT ck_output_scan_exemptions_dual_control
        CHECK (approved_by_user_id IS NULL OR created_by_user_id IS NULL
               OR approved_by_user_id <> created_by_user_id OR self_approved)
);
""",
    ))
    steps.append((
        f"{_TABLE} partial UNIQUE {_UQ} (live key only)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_UQ}
    ON {_TABLE} (tenant_id, COALESCE(workflow_id, 0),
                 COALESCE(node_kind, ''), COALESCE(scanner, ''),
                 COALESCE(finding_code, ''))
    WHERE deleted_at IS NULL;
""",
    ))
    steps.append((
        f"{_TABLE} index {_IX} (tenant_id, workflow_id) live-only",
        f"""
CREATE INDEX IF NOT EXISTS {_IX}
    ON {_TABLE} (tenant_id, workflow_id)
    WHERE deleted_at IS NULL;
""",
    ))
    steps.append((
        f"function {_GUARD_FN}() (no hard delete; UPDATE stays allowed)",
        f"""
CREATE OR REPLACE FUNCTION {_GUARD_FN}()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'Hard delete/truncate is forbidden on % (ISO 27001 no-hard-delete); '
        'revoke by soft-deleting (set deleted_at + deleted_by_user_id) instead',
        TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END;
$$ LANGUAGE plpgsql
""",
    ))
    steps.append((
        f"no-hard-delete triggers on {_TABLE} (DELETE + TRUNCATE only)",
        f"""
DO $$
BEGIN
    IF to_regclass('{_TABLE}') IS NULL THEN
        RAISE NOTICE '{_TABLE} does not exist yet; skipping {_TABLE} triggers';
        RETURN;
    END IF;
    DROP TRIGGER IF EXISTS trg_no_delete_{_TABLE} ON {_TABLE};
    CREATE TRIGGER trg_no_delete_{_TABLE}
        BEFORE DELETE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {_GUARD_FN}();
    DROP TRIGGER IF EXISTS trg_no_truncate_{_TABLE} ON {_TABLE};
    CREATE TRIGGER trg_no_truncate_{_TABLE}
        BEFORE TRUNCATE ON {_TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION {_GUARD_FN}();
END
$$
""",
    ))
    return steps


def _apply(execute, ) -> list[str]:
    """Run every step through `execute(sql)`; return the labels applied.

    Kept separate from `main()` so the test can drive it with the conftest
    `db_session` (never the live DB engine).
    """
    applied = []
    for label, sql in _sql_statements():
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
                   help="Apply the CREATE TABLE + index + trigger steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _sql_statements():
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (ISO 27001 — exemptions are never dropped; "
              "revoke stamps deleted_at)")
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
        "Fortress 3.6c ready: output_scan_exemptions exists, soft-delete only "
        "(DELETE/TRUNCATE RAISE; UPDATE allowed for approve/revoke), one live "
        "row per (tenant, workflow, node_kind, scanner, finding_code)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

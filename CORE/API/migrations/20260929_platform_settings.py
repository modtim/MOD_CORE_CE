"""GS.PSR.1 — DB-backed platform-settings registry.

Tunables and feature flags move out of env into the DB
([[settings-not-env-files]]). This migration creates the two backing
tables; the typed registry + resolution order live in
`services/platform_settings.py` (GS.PSR.2) and readers are migrated over
later (GS.PSR.6).

`platform_settings`:
    key          TEXT            the registry key (e.g. 'model_cache_enabled')
    scope        TEXT            CHECK in ('platform', 'tenant')
    tenant_id    INTEGER NULL    FK tenants(id) — only for scope='tenant'
    value        JSONB NOT NULL  the setting, coerced to its Python type on read
    updated_by   INTEGER NULL    FK users.id (actor who last changed it)
    updated_at   TIMESTAMPTZ     DEFAULT now()
    deleted_at   TIMESTAMPTZ     soft-delete — ISO 27001: NO hard deletes.
                                 set_setting soft-supersedes the old row
                                 (stamps deleted_at) and inserts a fresh one,
                                 so the full history is retained.

    Live-row uniqueness is a PARTIAL UNIQUE index over
    (key, scope, COALESCE(tenant_id, 0)) WHERE deleted_at IS NULL — at most
    one live row per (key, scope, tenant); superseded rows fall outside the
    index. COALESCE(tenant_id, 0) gives a stable bucket for platform rows
    (tenant_id is INTEGER here, matching tenants.id — no UUID adaptation
    needed).

`platform_settings_audit`:
    append-only. id, key, scope, tenant_id, old_value JSONB, new_value
    JSONB, actor_user_id, changed_at. One row per set_setting mutation,
    written in the SAME transaction as the soft-supersede. ENFORCED
    append-only by BEFORE UPDATE OR DELETE row triggers that RAISE
    (same style as the wizard_event / wizard_runs triggers — see
    migrations/20260508_wizard_event_tables_append_only.py and
    migrations/20260907_add_wizard_runs.py).

    NOTE: `Base.metadata.create_all` does NOT install trigger functions,
    so the sibling pytest DB (built from metadata) lacks the guard until
    the tests install it from the SQL constants below.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX/FUNCTION IF NOT EXISTS, DROP TRIGGER IF EXISTS + CREATE
TRIGGER, ALTER OWNER. Safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260929_platform_settings.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


_TABLES = ("platform_settings", "platform_settings_audit")

# --- platform_settings -------------------------------------------------------

CREATE_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS platform_settings (
    id          SERIAL PRIMARY KEY,
    key         TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'platform',
    tenant_id   INTEGER REFERENCES tenants(id),
    value       JSONB NOT NULL,
    updated_by  INTEGER REFERENCES users(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ,
    CONSTRAINT ck_platform_settings_scope
        CHECK (scope IN ('platform', 'tenant')),
    CONSTRAINT ck_platform_settings_scope_tenant
        CHECK ((scope = 'tenant') = (tenant_id IS NOT NULL))
);
"""

# Live-row uniqueness: one live row per (key, scope, tenant). Partial, so
# soft-deleted (superseded) history rows do not collide.
UNIQUE_LIVE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_settings_live
    ON platform_settings (key, scope, COALESCE(tenant_id, 0))
    WHERE deleted_at IS NULL;
"""

LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_platform_settings_live_lookup
    ON platform_settings (key, scope, tenant_id)
    WHERE deleted_at IS NULL;
"""

# --- platform_settings_audit --------------------------------------------------

CREATE_AUDIT_SQL = """
CREATE TABLE IF NOT EXISTS platform_settings_audit (
    id            SERIAL PRIMARY KEY,
    key           TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT 'platform',
    tenant_id     INTEGER REFERENCES tenants(id),
    old_value     JSONB,
    new_value     JSONB NOT NULL,
    actor_user_id INTEGER REFERENCES users(id),
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_platform_settings_audit_scope
        CHECK (scope IN ('platform', 'tenant'))
);
"""

AUDIT_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_platform_settings_audit_lookup
    ON platform_settings_audit (key, scope, tenant_id);
"""

# Append-only guard — same RAISE style as the other append-only audit
# tables in this repo (wizard_event_no_mutate / wizard_run_no_hard_delete).
AUDIT_NO_MUTATE_FN_SQL = """
CREATE OR REPLACE FUNCTION platform_settings_audit_no_mutate()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Operation % is forbidden on append-only table % (ISO no-hard-delete)',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END;
$$ LANGUAGE plpgsql
"""

# (drop, create) pairs — FOR EACH ROW on UPDATE + DELETE so any single-row
# mutation of the ledger raises, not just bulk ops.
_AUDIT_TRIGGERS = []
for _op in ("UPDATE", "DELETE"):
    _trig = f"trg_no_{_op.lower()}_platform_settings_audit"
    _AUDIT_TRIGGERS.append(
        (f"drop trigger {_trig} if exists",
         f"DROP TRIGGER IF EXISTS {_trig} ON platform_settings_audit")
    )
    _AUDIT_TRIGGERS.append(
        (f"create trigger {_trig} BEFORE {_op} (row)",
         f"""
         CREATE TRIGGER {_trig}
             BEFORE {_op} ON platform_settings_audit
             FOR EACH ROW EXECUTE FUNCTION platform_settings_audit_no_mutate()
         """)
    )

STATEMENTS = [
    ("create table platform_settings", CREATE_SETTINGS_SQL),
    ("create unique index uq_platform_settings_live (partial)", UNIQUE_LIVE_INDEX_SQL),
    ("create index ix_platform_settings_live_lookup (partial)", LOOKUP_INDEX_SQL),
    ("create table platform_settings_audit", CREATE_AUDIT_SQL),
    ("create index ix_platform_settings_audit_lookup", AUDIT_LOOKUP_INDEX_SQL),
    ("create or replace function platform_settings_audit_no_mutate()",
     AUDIT_NO_MUTATE_FN_SQL),
] + _AUDIT_TRIGGERS


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        for label, sql in STATEMENTS:
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        for t in _TABLES:
            try:
                cur.execute(f'ALTER TABLE {t} OWNER TO {owner}')
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER TABLE {t} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("platform_settings + platform_settings_audit ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

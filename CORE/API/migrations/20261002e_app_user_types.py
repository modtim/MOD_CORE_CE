"""GX.S1b (launch-completion-megaplan Group X) — app user types + app users.

An app declares its own user TYPES (e.g. `consignor`): users who
authenticate through the platform IdP via the APP's KC client (GX.A2, same
realm, KC group `app:<slug>:<type>`) but are ADDITIVE and outside MOD — no
MOD tenant role/entitlements, no Dashboard/WB/API access (the central auth
dependency refuses them: 403 APP_USER_NO_PLATFORM_ACCESS).

Adds:
  * `app_user_types` — the types an app defines (name/label, self_signup
    toggle, soft-delete).
  * `app_users` — one row per (app, KC sub): the admitted identity, with
    status active/disabled (SOFT disable — ISO 27001, never hard-deleted;
    the KC user itself is never deleted, only de-grouped).
  * three new values on the Postgres `auditaction` enum backing
    `models.AuditAction` (UPPERCASE, matching the existing values):
    APP_USER_TYPE_SET / APP_USER_INVITED / APP_USER_DISABLED.

Additive only; idempotent: CREATE TABLE IF NOT EXISTS /
ALTER TYPE ... ADD VALUE IF NOT EXISTS. Safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261002e_app_user_types.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("app_user_types", "app_users")

CREATE_APP_USER_TYPES_SQL = """
CREATE TABLE IF NOT EXISTS app_user_types (
    id SERIAL PRIMARY KEY,
    app_id INTEGER NOT NULL REFERENCES apps(id),
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    name VARCHAR(63) NOT NULL,
    label VARCHAR(255),
    self_signup BOOLEAN NOT NULL DEFAULT false,
    created_by_user_id INTEGER REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    deleted_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_app_user_types_app_name UNIQUE (app_id, name),
    CONSTRAINT ck_app_user_types_name CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$')
);
"""

CREATE_APP_USERS_SQL = """
CREATE TABLE IF NOT EXISTS app_users (
    id SERIAL PRIMARY KEY,
    app_id INTEGER NOT NULL REFERENCES apps(id),
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    app_user_type_id INTEGER NOT NULL REFERENCES app_user_types(id),
    kc_sub VARCHAR(255) NOT NULL,
    email VARCHAR(320) NOT NULL,
    user_type VARCHAR(63) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    invited_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    disabled_at TIMESTAMP WITH TIME ZONE,
    deleted_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_app_users_app_sub UNIQUE (app_id, kc_sub),
    CONSTRAINT ck_app_users_status CHECK (status IN ('active', 'disabled'))
);
"""

_STATEMENTS = [
    ("app_user_types table", CREATE_APP_USER_TYPES_SQL),
    ("app_users table", CREATE_APP_USERS_SQL),
    ("index app_users.app_id",
     "CREATE INDEX IF NOT EXISTS ix_app_users_app_id ON app_users (app_id)"),
    ("index app_users.tenant_id",
     "CREATE INDEX IF NOT EXISTS ix_app_users_tenant_id ON app_users (tenant_id)"),
    ("index app_users.app_user_type_id",
     "CREATE INDEX IF NOT EXISTS ix_app_users_app_user_type_id "
     "ON app_users (app_user_type_id)"),
    ("index app_users.kc_sub",
     "CREATE INDEX IF NOT EXISTS ix_app_users_kc_sub ON app_users (kc_sub)"),
    ("index app_user_types.app_id",
     "CREATE INDEX IF NOT EXISTS ix_app_user_types_app_id "
     "ON app_user_types (app_id)"),
    ("index app_user_types.tenant_id",
     "CREATE INDEX IF NOT EXISTS ix_app_user_types_tenant_id "
     "ON app_user_types (tenant_id)"),
    ("auditaction += APP_USER_TYPE_SET",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'APP_USER_TYPE_SET'"),
    ("auditaction += APP_USER_INVITED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'APP_USER_INVITED'"),
    ("auditaction += APP_USER_DISABLED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'APP_USER_DISABLED'"),
]


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
        for label, sql in _STATEMENTS:
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
    for label, sql in _STATEMENTS:
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
    print("app user types + app users ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

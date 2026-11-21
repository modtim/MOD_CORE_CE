"""add-self-service-sso Phase E — users-table JIT columns.

Adds four columns to `users` needed by the JIT provisioning path:

  provisioning_source VARCHAR(16) NOT NULL DEFAULT 'manual'
    Values: 'manual' (signup), 'invite' (invited), 'sso' (JIT).
    Drives §7.7 — JIT users skip the MOD-side verify-email gate
    because the IdP assertion is the trust anchor.

  is_email_verified BOOLEAN NOT NULL DEFAULT false
    MOD-side mirror of Keycloak's emailVerified flag. JIT users
    land with TRUE because the IdP assertion is authoritative.

  email_verified_at TIMESTAMPTZ NULL
    When the email was last verified. Backbone for the deferred
    §6.5 90-day staleness gate.

  last_sso_idp_id INTEGER NULL REFERENCES tenant_idp_configs(id)
    ON DELETE SET NULL. Used by §3.5 — when an IdP is deleted, we
    revoke sessions for users last logged in via that IdP. Pre-
    Phase-E the §3.5 path is a no-op; this column lights it up.

All four columns are idempotent (IF NOT EXISTS).

Vault role-rotation note: every migration in this codebase
re-asserts table OWNER TO "modTech" after the DDL because each
migration runs as a fresh ephemeral Vault role. Without the
ownership reset, the next migration that touches a FK to this
table fails with "permission denied".

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260605_add_jit_provisioning_columns.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ADD_COLUMNS_SQL = """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS provisioning_source VARCHAR(16) NOT NULL
        DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS is_email_verified BOOLEAN NOT NULL
        DEFAULT false,
    ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS last_sso_idp_id INTEGER NULL;
"""

ADD_FK_SQL = """
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_users_last_sso_idp_id'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT fk_users_last_sso_idp_id
            FOREIGN KEY (last_sso_idp_id)
            REFERENCES tenant_idp_configs(id) ON DELETE SET NULL;
    END IF;
END $$;
"""

ADD_INDEX_SQL = """
-- Speeds up the §3.5 revoke-on-IdP-delete sweep: scan rows where
-- last_sso_idp_id = <idp id>. Partial index keeps it small.
CREATE INDEX IF NOT EXISTS ix_users_last_sso_idp_id
    ON users (last_sso_idp_id)
    WHERE last_sso_idp_id IS NOT NULL;
"""

# Vault role-rotation guard — keep `users` ownership pinned to the
# canonical role so subsequent migrations can still touch it.
RESET_OWNER_SQL = 'ALTER TABLE users OWNER TO {owner};'


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
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        print(ADD_COLUMNS_SQL)
        print(ADD_FK_SQL)
        print(ADD_INDEX_SQL)
        print(RESET_OWNER_SQL.format(owner="{owner}"))
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    cur.execute(ADD_COLUMNS_SQL)
    cur.execute("SELECT to_regclass('public.tenant_idp_configs')")
    if cur.fetchone()[0] is not None:
        cur.execute(ADD_FK_SQL)
    else:
        # CE: SSO/SCIM tables are not shipped, so the FK target is absent.
        print("WARNING: tenant_idp_configs absent; skipping fk_users_last_sso_idp_id")
    cur.execute(ADD_INDEX_SQL)
    try:
        cur.execute(RESET_OWNER_SQL.format(owner=owner))
    except Exception as e:  # noqa: BLE001
        # Non-fatal — the current role may already be modTech.
        print(f"OWNER reset skipped: {e}")
    cur.close()
    print("users JIT columns + FK + index applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

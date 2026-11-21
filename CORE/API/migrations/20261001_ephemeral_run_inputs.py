"""GX.R4 (launch-completion-megaplan Group X) — ephemeral run inputs.

Adds ONE table so a run can feed an input node a short-TTL file (audio/text/
any) that does NOT land in the user's asset library and does NOT 409 on a
duplicate filename.

  app_ephemeral_inputs      per-tenant namespace; TTL 24h -> 7d grace -> bytes
                            purged in ALL modes (owner decision OQ-5(a)); the
                            row is NEVER hard-deleted (ISO 27001). Each row
                            backs a hidden Cog (cog_id) the run's input node
                            consumes via COG_IDS.

`app_id` is a plain nullable integer — NO FK yet. The `apps` table (GX.A2) does
not exist yet; GX.A2 adds `REFERENCES apps(id)` + the index in its own
migration. Keeping it FK-less here keeps this migration self-contained and
re-runnable.

TTL + grace are DB platform settings (services.platform_settings, keys
`ephemeral_inputs.ttl_hours` / `ephemeral_inputs.purge_grace_days`), NOT env —
so this migration seeds no tunable values; the registry defaults (24h / 7d)
apply until an operator overrides them in the admin UI.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS. Safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted live;
applying belongs to the deploy step — see the bind-mount models hazard).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261001_ephemeral_run_inputs.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("app_ephemeral_inputs",)

CREATE_EPHEMERAL_SQL = """
CREATE TABLE IF NOT EXISTS app_ephemeral_inputs (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- GX.A2 adds: REFERENCES apps(id). Plain nullable integer until the
    -- `apps` table exists.
    app_id          INTEGER,
    owner_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name            VARCHAR(255) NOT NULL,
    file_extension  VARCHAR(20),
    mime_type       VARCHAR(255),
    size_bytes      BIGINT NOT NULL DEFAULT 0,
    content_hash    VARCHAR(64) NOT NULL,
    storage_path    VARCHAR(1024),
    cog_id          INTEGER REFERENCES cogs(id) ON DELETE SET NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'active',
    expired_at      TIMESTAMPTZ,
    bytes_purged_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
"""

INDEX_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_app_ephemeral_inputs_tenant_id
    ON app_ephemeral_inputs (tenant_id);
"""
INDEX_STATUS_EXPIRES_SQL = """
CREATE INDEX IF NOT EXISTS ix_app_ephemeral_inputs_status_expires
    ON app_ephemeral_inputs (status, expires_at);
"""
INDEX_EXPIRED_AT_SQL = """
CREATE INDEX IF NOT EXISTS ix_app_ephemeral_inputs_expired_at
    ON app_ephemeral_inputs (expired_at) WHERE status = 'expired';
"""
INDEX_CONTENT_HASH_SQL = """
CREATE INDEX IF NOT EXISTS ix_app_ephemeral_inputs_content_hash
    ON app_ephemeral_inputs (tenant_id, content_hash);
"""

_STATEMENTS = [
    ("create table app_ephemeral_inputs", CREATE_EPHEMERAL_SQL),
    ("index app_ephemeral_inputs.tenant_id", INDEX_TENANT_SQL),
    ("index app_ephemeral_inputs(status, expires_at)", INDEX_STATUS_EXPIRES_SQL),
    ("index app_ephemeral_inputs.expired_at (expired)", INDEX_EXPIRED_AT_SQL),
    ("index app_ephemeral_inputs(tenant_id, content_hash)", INDEX_CONTENT_HASH_SQL),
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
    print("app_ephemeral_inputs ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

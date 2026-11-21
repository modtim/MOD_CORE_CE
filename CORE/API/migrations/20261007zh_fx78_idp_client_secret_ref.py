"""GSEC FX.78.p4 (BYO IdP) — per-tenant IdP client-credential reference columns.

tenant_idp_configs (EXISTS): the existing self-service SSO IdP table
(Phase B raw-SQL only, no ORM class). FX.78.p4 extends THAT feature
with bring-your-own OIDC client credentials. The client_secret VALUE
never lives in this table — it is written to Vault through the
established tenant-secret KV helper (crud/secret_cud._vault_put_tenant_
secret, path tenants/{id}/secrets/sso_idp_client_secret_{idp_id}) and
only the reference + presence flag land here. Two nullable columns,
added at the END of the table:

    client_id           VARCHAR(512) NULL  (public OIDC client identifier;
                      safe to echo in the response)
    client_secret_ref   VARCHAR(512) NULL  (Vault KV path; NULL = no
                      secret stored. The API exposes has_secret: bool
                      only, never the secret)

Both are nullable so every pre-existing IdP row (and every test-schema
row created without the columns) keeps working unchanged: the CRUD
SELECT/INSERT default them to NULL.

Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS. Dry run (default)
PRINTS the SQL and exits 0 without touching the database. Apply with
--commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zh_fx78_idp_client_secret_ref.py           # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zh_fx78_idp_client_secret_ref.py --commit  # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007zg (FX.85 p2b objections). NOT applied by this task (model/
migration/service/router/tests only) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "tenant_idp_configs BYO-IdP credential reference columns",
        """
ALTER TABLE tenant_idp_configs
    ADD COLUMN IF NOT EXISTS client_id VARCHAR(512) NULL;
ALTER TABLE tenant_idp_configs
    ADD COLUMN IF NOT EXISTS client_secret_ref VARCHAR(512) NULL;
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
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    # CE-SSO-SCIM-1 (owner 11-15: SSO/SCIM is OUT of CE — the 12 tables are
    # removed from CE). This step's ONLY subject is `tenant_idp_configs`, which
    # the CE cut never creates (its own CREATE migration is denied in CE and
    # db_bootstrap_guards.py drops the block), so the bare ALTERs below die with
    # "table tenant_idp_configs does not exist" -> rc 1 -> tools/mod_migrate.py
    # stops at the FIRST failing step and the API never starts
    # (proven: results-1110/FIX-CE-MIGRATE-SKIP-LIST-IDP-...-P1, commit
    # 688e6e35e3). tools/ce_migrate_skip.txt may not list a step that does DDL,
    # so the guard lives HERE, inside the step: no table -> print one line and
    # return 0. mod_migrate then records the step like any other rc-0 step, so
    # the filename-order contract holds and a later run never re-runs it.
    # Paid is unchanged: its table exists, so the probe is true and the ALTERs
    # run exactly as before. Shared code, shipped to paid unchanged.
    cur.execute("SELECT to_regclass('public.tenant_idp_configs') IS NULL;")
    if cur.fetchone()[0]:
        print("SKIP  tenant_idp_configs absent on this DB (CE ships no SSO/SCIM "
              "tables) — nothing to add; step recorded as applied.")
        cur.close()
        return 0
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        # one statement per semicolon — the batch may hold several
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

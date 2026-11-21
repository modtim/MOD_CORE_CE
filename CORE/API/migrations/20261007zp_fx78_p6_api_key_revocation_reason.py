"""GSEC FX.78.p6 (SCIM 2.0 Users) — why an API key was soft-revoked.

api_keys (EXISTS, SoftDeleteMixin): SCIM deprovisioning soft-revokes a
user's live keys (stamps deleted_at — ISO 27001 A.9.2.6 access removal,
never a row delete). FX.78.p6 makes soft-disable REVERSIBLE: a later
PATCH/PUT with active=true re-provisions the SAME user and must restore
EXACTLY the keys SCIM revoked at that deprovision time — keys the user
revoked themselves (manual revoke, account deletion) stay revoked. The
revoke/restore pair in crud/user_cud.py filters on `deleted_at_reason`,
so the reason needs its own column; today only the audit row carried it.

    deleted_at_reason  VARCHAR(64) NULL   ("scim_deprovision" | "account_deletion"
                      | "deactivation" | ... ; NULL = revoked by a path that
                      predates this column)

Nullable, so every pre-existing key row keeps working unchanged.

Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS. Dry run (default)
PRINTS the SQL and exits 0 without touching the database. Apply with
--commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zp_fx78_p6_api_key_revocation_reason.py           # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zp_fx78_p6_api_key_revocation_reason.py --commit  # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007zo (FX.158 p2a advisories). NOT applied by this task (model /
migration/service/router/tests only) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "api_keys revocation-reason column (SCIM soft-disable reversibility)",
        """
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS deleted_at_reason VARCHAR(64) NULL;
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
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
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

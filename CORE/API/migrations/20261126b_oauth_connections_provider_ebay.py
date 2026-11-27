#!/usr/bin/env python3
"""EBAY-CHECK-MIGRATION — widen the oauth_connections provider CHECK for
'ebay' and 'ebay_sandbox' (owner Tim 11-25: yes).

The LIVE databases still carry the OLD provider value list from
20261011e_oauth_connections.py: a live box CANNOT store an 'ebay' row until
this migration widens the CHECK (models.py was widened for the test DB only
— see results-1110/EBAY-OAUTH-PROVIDER-BUILD.md FINDINGS F-1).

Constraint naming: 20261011e created the CHECK INLINE on the column, which
Postgres auto-names `oauth_connections_provider_check`; models.py names it
`ck_oauth_connections_provider`. Both names are dropped IF EXISTS so the
migration is correct against a live DB (auto-name) and a test DB (model
name) alike, and the result is the single model-named constraint.

One transaction:
    ALTER TABLE oauth_connections DROP CONSTRAINT IF EXISTS
        ck_oauth_connections_provider;
    ALTER TABLE oauth_connections DROP CONSTRAINT IF EXISTS
        oauth_connections_provider_check;
    ALTER TABLE oauth_connections ADD CONSTRAINT
        ck_oauth_connections_provider CHECK (provider IN (<full current
        models.py list>)) NOT VALID;
    ALTER TABLE oauth_connections VALIDATE CONSTRAINT
        ck_oauth_connections_provider;
(NOT VALID + VALIDATE keeps the ACCESS-EXCLUSIVE window short; existing
rows are still checked.)

Idempotent: safe to run twice. The second run detects that
ck_oauth_connections_provider already admits 'ebay' (via
information_schema.check_constraints) and skips — a no-op.

No data changes. The provider list is HARD-CODED here (no migration in this
repo reads the model at runtime); CORE/API/tests/
test_oauth_ebay_provider_check_migration.py fails if this list and the
models.py CHECK ever differ.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it (the API rebuild runs it at boot).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261126b_oauth_connections_provider_ebay.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "oauth_connections"
# Keep EXACTLY in sync with models.OAuthConnection ck_oauth_connections_provider
# (the guard test asserts set equality both ways).
_PROVIDERS = ("google", "github", "notion", "linear", "slack",
              "airtable", "microsoft365", "ebay", "ebay_sandbox")
_MODEL_CONSTRAINT = "ck_oauth_connections_provider"
_LIVE_INLINE_CONSTRAINT = "oauth_connections_provider_check"

_CHECK_SQL = (
    f"CHECK (provider IN ({', '.join(repr(p) for p in _PROVIDERS)}))"
)

# Second-run detection: the named constraint exists AND its clause already
# admits the ebay pair -> nothing to do. Postgres normalises `IN (...)` to
# `provider = ANY('{google,...,ebay,ebay_sandbox}'::text[])`, so the stored
# clause carries the values UNQUOTED: match on the bare token `ebay_sandbox`
# (absent from the old list, present in every widened list).
_WIDENED_MARKER = "ebay_sandbox"
_DETECT_SQL = f"""
SELECT check_clause FROM information_schema.check_constraints
WHERE constraint_schema = 'public'
  AND constraint_name = '{_MODEL_CONSTRAINT}'
"""

_STATEMENTS = [
    (
        f"{_TABLE} drop old provider CHECK (model name)",
        f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "
        f"{_MODEL_CONSTRAINT}",
    ),
    (
        f"{_TABLE} drop old provider CHECK (live inline auto-name)",
        f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "
        f"{_LIVE_INLINE_CONSTRAINT}",
    ),
    (
        f"{_TABLE} add widened provider CHECK (NOT VALID)",
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT "
        f"{_MODEL_CONSTRAINT} {_CHECK_SQL} NOT VALID",
    ),
    (
        f"{_TABLE} validate widened provider CHECK",
        f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT "
        f"{_MODEL_CONSTRAINT}",
    ),
]


def _get_conn():
    """Return a transactional (NOT autocommit) connection to the DB: the
    four DDL statements run inside ONE transaction."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = False
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--commit", action="store_true",
                   help="Apply the CHECK widen (default = dry-run prints SQL)")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(f"{sql};")
        print(f"(dry-run; {len(_STATEMENTS)} statement(s), nothing executed; "
              "pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: ALTER TABLE needs the owner role (CE has no modTech)
    set_role_ok = _owner_role.maybe_set_role(cur)

    # Idempotency guard: already widened -> no-op.
    try:
        cur.execute(_DETECT_SQL)
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - detection is best-effort
        conn.rollback()
        rows = []
    if rows and "'ebay'" in (rows[0][0] or ""):
        if set_role_ok:
            cur.execute("RESET ROLE")
        conn.commit()
        cur.close()
        conn.close()
        print(f"{_TABLE} provider CHECK: already contains 'ebay' — "
              "no-op (second run).")
        return 0

    try:
        for label, sql in _STATEMENTS:
            cur.execute(sql)
            print(f"OK   {label}")
        if set_role_ok:
            cur.execute("RESET ROLE")
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        print(f"FAIL provider CHECK widen: {e}")
        return 1
    cur.close()
    conn.close()
    print(f"{_TABLE} provider CHECK widened to {len(_PROVIDERS)} provider(s) "
          "including 'ebay' and 'ebay_sandbox'; no data changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""GSEC FX.56 p1 (owner decision Q39=A) — audited backfill of a finite
``expires_at`` on every live credential that never carried one.

Scope: ``api_keys`` rows (user API keys, service-account keys and app keys
all live in this table — ``app_id``/``service_account_id`` just tag the
kind) with ``expires_at IS NULL AND deleted_at IS NULL``. Each such key
gets ``expires_at = now() + credentials.default_lifetime_days`` (platform
setting, default 90 — read live from the ``platform_settings`` table so a
retuned default is honoured). NOTHING is revoked or deleted (ISO 27001
soft-state only): existing finite expiries, disabled keys and soft-deleted
rows are untouched.

Audit: one ``permission_audit_logs`` row per tenant summarising the count
(``CREDENTIAL_EXPIRY_BACKFILL`` on the native ``auditaction`` enum;
``audit_metadata`` carries tenant_id + keys_updated + lifetime_days —
never key values). The same backfill + audit is callable as
``services.credential_lifetime.backfill_missing_expiries`` (tests cover
that path; the SQL here is the equivalent raw form).

Idempotent: a re-run finds no ``expires_at IS NULL`` rows, updates
nothing and writes no audit rows.

Dry run (default) PRINTS the SQL and exits 0 without touching the
database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007t_fx56_credential_expiry_backfill.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007t_fx56_credential_expiry_backfill.py --commit   # apply

``ALTER TYPE auditaction ADD VALUE`` cannot run inside a transaction
block, so the whole script runs on an autocommit connection (same
pattern as 20261007k_fx50_security_incidents.py). Migration runner is
tools/mod_migrate.py (steps discovered as *.py in this directory, run
in filename order) — this file sorts after 20261007q.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "auditaction backfill value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'CREDENTIAL_EXPIRY_BACKFILL';
""",
    ),
    (
        "one audit row per tenant summarising the backfill",
        """
INSERT INTO permission_audit_logs (
    tenant_id, action, actor_user_id, resource_type, resource_id,
    audit_metadata
)
SELECT
    c.tenant_id,
    'CREDENTIAL_EXPIRY_BACKFILL'::auditaction,
    0,
    'security',
    0,
    jsonb_build_object(
        'operation', 'credential_expiry_backfill',
        'tenant_id', c.tenant_id,
        'keys_updated', c.n,
        'lifetime_days', lt.days
    )
FROM (
    SELECT COALESCE(u.tenant_id, 1) AS tenant_id, COUNT(*) AS n
    FROM api_keys k
    LEFT JOIN users u ON u.id = k.user_id
    WHERE k.expires_at IS NULL
      AND k.deleted_at IS NULL
    GROUP BY COALESCE(u.tenant_id, 1)
) c
CROSS JOIN (
    SELECT COALESCE((ps.value::text)::int, 90) AS days
    FROM platform_settings ps
    WHERE ps.key = 'credentials.default_lifetime_days'
      AND ps.scope = 'platform'
      AND ps.tenant_id IS NULL
      AND ps.deleted_at IS NULL
    ORDER BY ps.updated_at DESC LIMIT 1
) lt;
""",
    ),
    (
        "backfill expires_at on never-expiring live credentials",
        """
UPDATE api_keys k
SET expires_at = (
    SELECT NOW() + make_interval(days => (SELECT COALESCE((ps.value::text)::int, 90)
        FROM platform_settings ps
        WHERE ps.key = 'credentials.default_lifetime_days'
          AND ps.scope = 'platform'
          AND ps.tenant_id IS NULL
          AND ps.deleted_at IS NULL
        ORDER BY ps.updated_at DESC LIMIT 1))
)
WHERE k.expires_at IS NULL
  AND k.deleted_at IS NULL;
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
    print("FX.56 credential expiry backfill ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

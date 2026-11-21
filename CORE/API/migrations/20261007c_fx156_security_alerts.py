"""GSEC FX.156 part 1 (owner decision Q143=B) — platform security
alert-rules engine core: add the `security_alerts` +
`security_alert_rule_state` tables.

One alert-rules engine over audit/log events (owner decision): sign-in
anomalies, admin + privileged actions, unusual downloads, cert/key
expiry, backup failures, malware hits — thresholds from DB platform
settings (services/platform_settings.py `security_alerts.*` block).
Email alerts + daily digest + ack endpoint + monthly review report land
in later parts; this part ships the tables + the engine core
(services/security_alert_engine.py) with TWO rules: signin_failures and
admin_role_change.

`security_alerts` mirrors models.SecurityAlert exactly:
* deduped by (rule_key, tenant_id, subject) — repeat findings increment
  count + last_seen_at instead of creating rows (composite index);
* tenant_id NULLABLE = platform-level alert;
* ISO 27001 — NO hard delete: removal stamps deleted_at/deleted_by;
  acknowledgement (part 2) stamps acked_at/acked_by.

`security_alert_rule_state` mirrors models.SecurityAlertRuleState: the
per-rule cursor (last_run_at) so a restart never re-scans
already-consumed audit rows.

The repo's migration runner is `tools/mod_migrate.py`, not Alembic:
steps are discovered as `*.py` files in this directory and run in
filename order, each recorded in the append-only `release_steps`
ledger. Naming convention `YYYYMMDD[_letter].slug.py` — this file sorts
after 20261007b.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit.

Run inside the API container:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007c_fx156_security_alerts.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007c_fx156_security_alerts.py --commit   # apply

Additive only: CREATE TABLE/INDEX IF NOT EXISTS — nothing is dropped
or truncated.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("security_alerts", "security_alert_rule_state")

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS security_alerts (
    id BIGSERIAL PRIMARY KEY,
    rule_key VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'high',
    tenant_id INTEGER REFERENCES tenants(id),
    subject VARCHAR(255) NOT NULL,
    details JSONB,
    first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    count INTEGER NOT NULL DEFAULT 1,
    acked_at TIMESTAMP WITH TIME ZONE,
    acked_by INTEGER REFERENCES users(id),
    deleted_at TIMESTAMP WITH TIME ZONE,
    deleted_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT ck_security_alerts_severity
        CHECK (severity IN ('low', 'medium', 'high', 'critical'))
);
CREATE TABLE IF NOT EXISTS security_alert_rule_state (
    rule_key VARCHAR(64) PRIMARY KEY,
    last_run_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_security_alerts_rule_key
    ON security_alerts (rule_key);
CREATE INDEX IF NOT EXISTS ix_security_alerts_tenant_id
    ON security_alerts (tenant_id);
CREATE INDEX IF NOT EXISTS ix_security_alerts_first_seen_at
    ON security_alerts (first_seen_at);
CREATE INDEX IF NOT EXISTS ix_security_alerts_last_seen_at
    ON security_alerts (last_seen_at);
CREATE INDEX IF NOT EXISTS ix_security_alerts_dedupe
    ON security_alerts (rule_key, tenant_id, subject);
"""

_STATEMENTS = [
    ("security_alerts + security_alert_rule_state tables", TABLE_SQL),
    ("indexes on security_alerts", INDEX_SQL),
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
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("security_alerts ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

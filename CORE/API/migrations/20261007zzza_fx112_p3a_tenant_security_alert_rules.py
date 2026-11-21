#!/usr/bin/env python3
"""GSEC FX.112 p3a — tenant-admin alert rules + thresholds: the NEW
`tenant_security_alert_rule_configs` table + the auditaction enum value.

models.TenantSecurityAlertRuleConfig (services/tenant_alert_rules.py)
is the per-tenant override of the FX.156 built-in alert rules: a tenant
admin enables/disables a built-in rule for their tenant and/or sets its
threshold within the platform-set min/max bounds
(``security_alerts.tenant_*`` platform settings). ONE live row per
(tenant_id, rule_key) (unique), updated in place — ISO 27001: no hard
delete. ``threshold_override`` NULL = "use the platform default".

    tenant_security_alert_rule_configs
      id                    BIGSERIAL PRIMARY KEY
      tenant_id             INTEGER      NOT NULL   FK tenants.id
      rule_key              VARCHAR(64)  NOT NULL   FX.156 rule key
      enabled               BOOLEAN      NOT NULL   default true
      threshold_override    INTEGER      NULL       NULL = platform default
      updated_by            INTEGER      NULL       FK users.id ON DELETE
                                                  SET NULL (the admin)
      updated_at            TIMESTAMPTZ  NULL       ORM onupdate
      created_at            TIMESTAMPTZ  NOT NULL   server_default now()

Also adds the audit action that every rule change writes (one row per
change, models.AuditAction.TENANT_SECURITY_ALERT_RULE_UPDATED):
``ALTER TYPE auditaction ADD VALUE IF NOT EXISTS
'TENANT_SECURITY_ALERT_RULE_UPDATED';`` — literal form so
tests/test_audit_action_enum_sync.py can discover it (the live type is
built once and values are appended one migration at a time).

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block, so
the whole script runs on an autocommit connection (same pattern as
20261007e_fx156_alert_ack_action.py). Idempotent.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzza_fx112_p3a_tenant_security_alert_rules.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzza_fx112_p3a_tenant_security_alert_rules.py --commit   # apply

NOT applied by the p3a task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "tenant_security_alert_rule_configs"

_STATEMENTS = [
    (
        f"{_TABLE} table (FX.112 p3a)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    rule_key VARCHAR(64) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    threshold_override INTEGER NULL,
    updated_by INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""",
    ),
    (
        f"{_TABLE} tenant_id index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_id
    ON {_TABLE} (tenant_id);
""",
    ),
    (
        f"{_TABLE} rule_key index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_rule_key
    ON {_TABLE} (rule_key);
""",
    ),
    (
        f"{_TABLE} one live config per (tenant_id, rule_key)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_tenant_rule
    ON {_TABLE} (tenant_id, rule_key);
""",
    ),
    (
        "auditaction.TENANT_SECURITY_ALERT_RULE_UPDATED value (FX.112 p3a)",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'TENANT_SECURITY_ALERT_RULE_UPDATED';
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
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
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
        cur.execute(sql)
        print(f"DID  {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

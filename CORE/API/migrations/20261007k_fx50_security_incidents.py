"""GSEC FX.50 part 2 (owner decision Q33=A) — breach-notification
incident tracker.

Adds the incident tracker tables the admin incident workflow reads and
writes (models.SecurityIncident / models.IncidentFollowUp,
services/security_incidents.py), plus the audit-action enum values that
back one audit row per incident state change.

Q33=A: affected customers are told within 72 hours of CONFIRMING a
breach; tenants on a gov/DoD compliance profile (cmmc /
fedramp_high_il5) get an initial heads-up within 24 hours; follow-ups as
facts firm up. The 72h/24h clocks are stamped at confirm time from the
incident.* platform settings and stored on the row so the overdue
report never re-derives them.

security_incidents (NEW):
    id                  VARCHAR(36) PK        uuid4 string
    tenant_id           INTEGER NULL          tenants.id FK; NULL =
                                              platform-wide incident
    severity            VARCHAR(16) NOT NULL  low|medium|high|critical
    summary             VARCHAR(500) NULL
    status              VARCHAR(32) NOT NULL  open|confirmed|notified|closed
    detected_at         TIMESTAMPTZ NOT NULL
    confirmed_at        TIMESTAMPTZ NULL
    gov_headsup_due_at  TIMESTAMPTZ NULL      confirmed_at + 24h, gov
                                              profile installs ONLY
    notify_due_at       TIMESTAMPTZ NULL      confirmed_at + 72h
    notified_at         TIMESTAMPTZ NULL
    closed_at           TIMESTAMPTZ NULL
    created_by / confirmed_by INTEGER NULL    users.id
    created_at / updated_at / deleted_at      (deleted_at = soft-delete
                                              marker, ISO 27001 — NO
                                              hard deletes)

incident_follow_ups (NEW):
    id / incident_id (VARCHAR(36) FK, NOT NULL) / owner (VARCHAR(255)
    NOT NULL, free-form) / note / due_at TIMESTAMPTZ NOT NULL /
    done_at TIMESTAMPTZ NULL / created_by / created_at / updated_at /
    deleted_at (soft-delete only)

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    SECURITY_INCIDENT_CREATED / SECURITY_INCIDENT_CONFIRMED /
    SECURITY_INCIDENT_FOLLOWUP_ADDED /
    SECURITY_INCIDENT_FOLLOWUP_COMPLETED / SECURITY_INCIDENT_NOTIFIED /
    SECURITY_INCIDENT_CLOSED

Metadata on those rows carries identifiers/timestamps only — never
breach content or credentials.

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS +
ALTER TYPE ADD VALUE IF NOT EXISTS. Dry run (default) PRINTS the SQL
and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007k_fx50_security_incidents.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007k_fx50_security_incidents.py --commit   # apply

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction
block, so the whole script runs on an autocommit connection (same
pattern as 20261007e_fx156_alert_ack_action.py). Migration runner is
tools/mod_migrate.py (steps discovered as *.py in this directory, run
in filename order) — this file sorts after 20261007j.

models.py is bind-mounted live into the API container, so the tables +
enum values must exist before any FX.50 part-2 code path writes to them
— apply immediately after adding the models to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "security_incidents table",
        """
CREATE TABLE IF NOT EXISTS security_incidents (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants(id),
    severity VARCHAR(16) NOT NULL,
    summary VARCHAR(500) NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'open',
    detected_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ NULL,
    gov_headsup_due_at TIMESTAMPTZ NULL,
    notify_due_at TIMESTAMPTZ NULL,
    notified_at TIMESTAMPTZ NULL,
    closed_at TIMESTAMPTZ NULL,
    created_by INTEGER NULL,
    confirmed_by INTEGER NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_security_incidents_severity
        CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    CONSTRAINT ck_security_incidents_status
        CHECK (status IN ('OPEN', 'CONFIRMED', 'NOTIFIED', 'CLOSED'))
);
""",
    ),
    (
        "security_incidents indexes",
        """
CREATE INDEX IF NOT EXISTS ix_security_incidents_tenant_id
    ON security_incidents (tenant_id);
CREATE INDEX IF NOT EXISTS ix_security_incidents_severity
    ON security_incidents (severity);
CREATE INDEX IF NOT EXISTS ix_security_incidents_status
    ON security_incidents (status);
CREATE INDEX IF NOT EXISTS ix_security_incidents_tenant_detected
    ON security_incidents (tenant_id, detected_at);
CREATE INDEX IF NOT EXISTS ix_security_incidents_notify_due
    ON security_incidents (notify_due_at);
""",
    ),
    (
        "incident_follow_ups table",
        """
CREATE TABLE IF NOT EXISTS incident_follow_ups (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id VARCHAR(36) NOT NULL
        REFERENCES security_incidents(id),
    owner VARCHAR(255) NOT NULL,
    note TEXT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    done_at TIMESTAMPTZ NULL,
    created_by INTEGER NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL
);
""",
    ),
    (
        "incident_follow_ups indexes",
        """
CREATE INDEX IF NOT EXISTS ix_incident_follow_ups_incident_id
    ON incident_follow_ups (incident_id);
CREATE INDEX IF NOT EXISTS ix_incident_follow_ups_due_at
    ON incident_follow_ups (due_at);
""",
    ),
    (
        "auditaction incident values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_INCIDENT_CREATED';
""",
    ),
    (
        "auditaction incident values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_INCIDENT_CONFIRMED';
""",
    ),
    (
        "auditaction incident values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_INCIDENT_FOLLOWUP_ADDED';
""",
    ),
    (
        "auditaction incident values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_INCIDENT_FOLLOWUP_COMPLETED';
""",
    ),
    (
        "auditaction incident values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_INCIDENT_NOTIFIED';
""",
    ),
    (
        "auditaction incident values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_INCIDENT_CLOSED';
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
    print("FX.50 security incident tracker schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""GSEC FX.119 p3a/p3b (owner decision Q106=A) — the on-call PAGE RECORD
(models.OncallPage) + the audit-action labels that carry ack/escalation.

p2a shipped the paging CHANNEL (a page was only an audit row beside the
monitor's own record). p3a makes the page an OBJECT so it can be ACKED
(the on-call person taps the link on their phone) and ESCALATED (nobody
acked inside oncall.ack_window_seconds = the owner decision's 15 min ->
re-page oncall.owner_topic EXACTLY ONCE, mark 'escalated'). p3b adds the
ROTA (settings only, no DB change).

    oncall_pages
      id                     BIGSERIAL PRIMARY KEY
      dedupe_key             VARCHAR(128) NULL   the monitor's dedupe key
      title                  VARCHAR(200) NOT NULL
      priority               INTEGER NOT NULL DEFAULT 5   (ntfy 1..10)
      tenant_id              INTEGER NULL  FK tenants(id)  NULL = platform-level
      sent_to                VARCHAR(128) NULL   the topic the page went to
      sent_at                TIMESTAMPTZ NULL
      ack_token_hash         VARCHAR(64) NULL    SHA-256 hex ONLY (never a token)
      acked_at / acked_by    TIMESTAMPTZ / INTEGER NULL
      escalated_at / escalated_to  TIMESTAMPTZ / VARCHAR(128) NULL
      status                 VARCHAR(16) NOT NULL DEFAULT 'sent'
                             sent|acked|escalated|failed
      created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
      deleted_at             TIMESTAMPTZ NULL    soft delete ONLY

NEW TABLE ONLY (models.py is bind-mounted live: no column was added to any
existing table). SOFT-DELETE ONLY (ISO 27001 — the page is the compliance
record of "we told someone about this incident at this time"; removal is a
deleted_at stamp, and there is NO ON DELETE CASCADE on the tenants FK: a
deleted tenant must not drop the record of a page it once owned).

Constraints (the DB enforces the compliance rules, not just the service):
    ck_oncall_pages_status              the four states only
    ck_oncall_pages_acked_clocks        an ack says WHEN
    ck_oncall_pages_escalated_clocks    an escalation says WHEN and WHERE
    ck_oncall_pages_token_only_live     an ack token exists only on a still-
                                        'sent' page: a terminal page has
                                        nothing to ack, and the consumed
                                        token is cleared, so a replayed link
                                        can never re-decide a page.

Indexes: status+sent_at (the escalation sweep reads exactly this order),
dedupe_key+status (the dedupe/escalation pairing), deleted_at.

The THREE audit-action LABELS (ONCALL_PAGE_ACKED / ONCALL_PAGE_ESCALATED /
ONCALL_NO_ROTA) ship in the same step: models.AuditAction is Column(Enum(AuditAction))
with no values_callable, so SQLAlchemy writes the member NAME and the live
`auditaction` PG enum needs those exact UPPERCASE labels or every ack row is
silently dropped (the FX.158 p2a lowercase-label bug class; tests/test_audit_action_enum_sync.py
guards it). ALTER TYPE ... ADD VALUE cannot run inside a transaction, which
is why the whole script runs on an autocommit connection.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzc_fx119_p3a_oncall_pages.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzc_fx119_p3a_oncall_pages.py --commit   # apply

NOT applied by the p3a task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "oncall_pages"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.119 p3a)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    dedupe_key VARCHAR(128) NULL,
    title VARCHAR(200) NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5,
    tenant_id INTEGER NULL REFERENCES tenants(id),
    sent_to VARCHAR(128) NULL,
    sent_at TIMESTAMPTZ NULL,
    ack_token_hash VARCHAR(64) NULL,
    acked_at TIMESTAMPTZ NULL,
    acked_by INTEGER NULL,
    escalated_at TIMESTAMPTZ NULL,
    escalated_to VARCHAR(128) NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'sent',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_{_TABLE}_status CHECK (
        status IN ('sent', 'acked', 'escalated', 'failed')
    ),
    CONSTRAINT ck_{_TABLE}_acked_clocks CHECK (
        status != 'acked' OR acked_at IS NOT NULL
    ),
    CONSTRAINT ck_{_TABLE}_escalated_clocks CHECK (
        status != 'escalated'
        OR (escalated_at IS NOT NULL AND escalated_to IS NOT NULL)
    ),
    CONSTRAINT ck_{_TABLE}_token_only_live CHECK (
        ack_token_hash IS NULL OR status = 'sent'
    )
);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_status_sent
    ON {_TABLE} (status, sent_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_dedupe_status
    ON {_TABLE} (dedupe_key, status);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at
    ON {_TABLE} (deleted_at);
""",
    ),
    (
        "auditaction labels for the three FX.119 p3a on-call actions",
        """
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'ONCALL_PAGE_ACKED';
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'ONCALL_PAGE_ESCALATED';
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'ONCALL_NO_ROTA';
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
        for stmt in (s.strip() for s in sql.split(";") if s.strip()):
            try:
                cur.execute(stmt)
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {label}: {e}")
                return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "FX.119 p3a ready: oncall_pages exists and the three "
        "ONCALL_* p3a audit labels are on the live enum."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

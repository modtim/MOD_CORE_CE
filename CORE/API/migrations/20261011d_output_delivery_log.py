#!/usr/bin/env python3
"""sz-4.7.1 (sz-productivity-floor decomp §4.7.1) — the ONE new table:
OUTPUT DELIVERY LOG (append-only audit of every output-node delivery the
platform attempted: slack / discord / email / sms / push / webhook / file).

revision      : 20261011d_output_delivery_log
down_revision : 20261011c_workflow_run_dispatch_priority

20261011c_workflow_run_dispatch_priority is the CURRENT head: no other
migration names it as its down_revision (verified by grep over
migrations/), so this row chains onto it.

Model + migration only — no writer yet. The output nodes themselves
(154/160) run in containers; a later decomp row wires the API-side logger
that writes these rows.

Columns, per the sz-4.7.1 decomp row:
    id               BIGSERIAL PRIMARY KEY
    tenant_id        INTEGER NOT NULL REFERENCES tenants (id)
                     — NO ON DELETE action (matching the other audit
                     tables in models.py: CostRoutingDecision,
                     WorkflowOutputAuditLog, AuditEvent, PermissionAuditLog,
                     PlatformSettingAudit — none cascade): a tenant row
                     cannot be hard-deleted out from under its own
                     delivery trail.
    workflow_run_id  INTEGER NULL REFERENCES workflow_runs (id)
                     — NO ON DELETE CASCADE: the delivery record outlives
                     the run it describes (append-only history).
    api_task_id      INTEGER NULL — NOT a FK: api_task_queue.id is an
                     Integer PK and the queue is pruned/rotated, so a
                     delivery row must not be tied to a queue row's life.
    node_id          VARCHAR(128) NULL
    channel          VARCHAR(32) NOT NULL
                     CHECK (channel IN ('slack','discord','email','sms',
                                        'push','webhook','file'))
    destination_hint VARCHAR(255) NULL
                     — REDACTED destination: host / channel name only
                     ("hooks.slack.com", "#mods-general"). NEVER a full
                     webhook URL, bot token, or phone number.
    status           VARCHAR(16) NOT NULL
                     CHECK (status IN ('delivered','failed','retrying',
                                       'skipped'))
    http_status      INTEGER NULL
    attempt          INTEGER NOT NULL DEFAULT 1
    payload_summary  JSONB NOT NULL DEFAULT '{}'::jsonb
                     — REDACTED payload: sizes/keys + truncated preview
                     <= 256 chars, never secrets.
    error            VARCHAR(1024) NULL
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()

Indexes: ix_output_delivery_log_tenant on (tenant_id),
ix_output_delivery_log_run on (workflow_run_id), and the composite
ix_output_delivery_log_tenant_created on (tenant_id, created_at DESC) for
the compliance/retention view (newest deliveries for one tenant).

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.
No ON DELETE clause anywhere — no CASCADE, no ONDELETE (a glued ONDELETE is
a Postgres syntax error anyway).

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. A matching models.py
ORM class (`OutputDeliveryLog`) is added alongside this migration.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261011d_output_delivery_log.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "output_delivery_log"
_REVISION = "20261011d_output_delivery_log"
_DOWN_REVISION = "20261011c_workflow_run_dispatch_priority"
_ALLOWED_CHANNELS = ("slack", "discord", "email", "sms", "push",
                     "webhook", "file")
_ALLOWED_STATUSES = ("delivered", "failed", "retrying", "skipped")

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-4.7.1, append-only output-delivery audit)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    workflow_run_id INTEGER REFERENCES workflow_runs (id),
    api_task_id INTEGER,
    node_id VARCHAR(128),
    channel VARCHAR(32) NOT NULL
        CHECK (channel IN ('slack', 'discord', 'email', 'sms', 'push',
                           'webhook', 'file')),
    destination_hint VARCHAR(255),
    status VARCHAR(16) NOT NULL
        CHECK (status IN ('delivered', 'failed', 'retrying', 'skipped')),
    http_status INTEGER,
    attempt INTEGER NOT NULL DEFAULT 1,
    payload_summary JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    error VARCHAR(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ),
    (
        f"{_TABLE} index (tenant_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant
    ON {_TABLE} (tenant_id);
""",
    ),
    (
        f"{_TABLE} index (workflow_run_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_run
    ON {_TABLE} (workflow_run_id);
""",
    ),
    (
        f"{_TABLE} composite index (tenant_id, created_at DESC)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_created
    ON {_TABLE} (tenant_id, created_at DESC);
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the CREATE TABLE + index steps. "
                        "Without this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (delivery rows are append-only audit "
              "history, kept per ISO 27001 — no destructive rollback "
              "defined)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "sz-4.7.1 ready: output_delivery_log exists; the API-side output "
        "logger (a later decomp row) writes it. The lead applies --commit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

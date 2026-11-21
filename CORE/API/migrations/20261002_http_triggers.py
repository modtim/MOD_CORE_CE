"""GX.S7 (launch-completion-megaplan Group X) — scheduled HTTP triggers.

Adds two tables so a tenant/app can schedule signed HTTP calls against a
Persistent Stack service or an App target over the internal network:

  http_triggers         one row per trigger. cron (5-field) OR interval
                        seconds; target = `stack-service` (stack_id +
                        stack_service + stack_port) or `app` (app_id) — the
                        concrete URL is resolved from those rows at fire
                        time, never stored freehand. Every call is
                        HMAC-SHA256 signed with the per-trigger secret
                        (encrypted under the tenant key, same pattern as
                        ApiKey.encrypted_keycloak_secret; returned ONCE at
                        creation). `paused` stops firing without deleting.
                        SoftDeleteMixin only — ISO 27001: no hard delete.

  http_trigger_runs     APPEND-ONLY run history: one row per fire (success
                        OR failure), status / http status / error /
                        duration / alert flags. A BEFORE DELETE trigger
                        (http_trigger_runs_no_delete) makes any physical
                        delete raise — history rows are immutable by
                        design (same posture as RunWebhookDeliveryAttempt).

`app_id` / `stack_id` reference `apps` / `persistent_stacks`. `apps` may
not exist yet (GX.A2 migration); `persistent_stacks` does. Until `apps`
lands, `app_id` is enforced as a plain INTEGER in the model and this
migration adds the FK only when the `apps` table exists (guarded below),
mirroring the plain-integer pattern in 20261001_email_send_api.py.

Sweep: ONE APScheduler job every 30s (services/scheduler.py) picks due
rows (`next_fire_at <= now`) with
`SELECT ... FOR UPDATE SKIP LOCKED` so multiple API processes never
double-fire, commits the claim, THEN makes the outbound call (never inside
an open transaction — pgbouncer pinning). Failure alert fan-out (MOD
notification + email + outbound webhook) fires when the consecutive-failure
count reaches the `triggers.failure_alert_threshold` platform setting.

Limits (triggers.* platform settings — timeout_seconds / max_per_tenant /
failure_alert_threshold / min_interval_seconds) live in the
platform-settings registry; this migration seeds no values.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX/TYPE IF NOT EXISTS + guarded ALTER. Safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261002_http_triggers.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("http_triggers", "http_trigger_runs")

CREATE_HTTP_TRIGGERS_SQL = """
CREATE TABLE IF NOT EXISTS http_triggers (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- GX.A2 adds: REFERENCES apps(id). Plain nullable integer until the
    -- `apps` table exists (see _APP_ID_FK_SQL guard below).
    app_id              INTEGER,
    name                VARCHAR(255) NOT NULL,
    -- 'cron' (cron_expression) | 'interval' (interval_seconds)
    schedule_kind       VARCHAR(16) NOT NULL,
    cron_expression     VARCHAR(128),
    interval_seconds    INTEGER,
    method              VARCHAR(8) NOT NULL DEFAULT 'POST',
    -- Path on the target (leading '/'); empty = root.
    path                VARCHAR(512) NOT NULL DEFAULT '',
    -- Target identity: which internal service this calls. The concrete
    -- URL is RESOLVED from these rows at fire time.
    target_kind         VARCHAR(16) NOT NULL,   -- 'stack-service' | 'app'
    stack_id            INTEGER REFERENCES persistent_stacks(id) ON DELETE CASCADE,
    stack_service       VARCHAR(127),
    stack_port          INTEGER,
    -- HMAC-SHA256 per-trigger secret, encrypted under the tenant key
    -- (ApiKey.encrypted_keycloak_secret pattern). Returned once, at
    -- creation; never readable afterwards.
    encrypted_secret    TEXT NOT NULL,
    -- Optional outbound-webhook target for the failure alert fan-out
    -- (third channel, alongside MOD notification + email). Signed with the
    -- same per-trigger secret (X-MOD-Signature / X-MOD-Timestamp).
    callback_url        VARCHAR(1024),
    -- active | paused
    status              VARCHAR(16) NOT NULL DEFAULT 'active',
    paused_at           TIMESTAMPTZ,
    paused_by_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_fired_at       TIMESTAMPTZ,
    next_fire_at        TIMESTAMPTZ NOT NULL,
    last_alerted_at     TIMESTAMPTZ,
    created_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);
"""

CREATE_HTTP_TRIGGER_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS http_trigger_runs (
    id                  SERIAL PRIMARY KEY,
    trigger_id          INTEGER NOT NULL REFERENCES http_triggers(id) ON DELETE CASCADE,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- scheduled | success | failed | skipped
    status              VARCHAR(16) NOT NULL,
    resolved_url        VARCHAR(1024),
    http_status         INTEGER,
    error               TEXT,
    duration_ms         INTEGER,
    -- Set true when this failure tripped the consecutive-failure alert
    -- (the fan-out channels attempted).
    alert_sent          BOOLEAN NOT NULL DEFAULT false,
    fired_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

INDEX_TRIGGERS_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_http_triggers_tenant_id
    ON http_triggers (tenant_id);
"""
INDEX_TRIGGERS_DUE_SQL = """
CREATE INDEX IF NOT EXISTS ix_http_triggers_due
    ON http_triggers (next_fire_at)
    WHERE deleted_at IS NULL AND status = 'active';
"""
INDEX_RUNS_TRIGGER_SQL = """
CREATE INDEX IF NOT EXISTS ix_http_trigger_runs_trigger_id
    ON http_trigger_runs (trigger_id);
"""
INDEX_RUNS_TENANT_FIRED_SQL = """
CREATE INDEX IF NOT EXISTS ix_http_trigger_runs_tenant_fired
    ON http_trigger_runs (tenant_id, fired_at DESC);
"""

# AuditAction is a PG enum (`auditaction`) storing member NAMES.
ENUM_HTTP_TRIGGER_CREATED_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'HTTP_TRIGGER_CREATED'"
)
ENUM_HTTP_TRIGGER_FIRED_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'HTTP_TRIGGER_FIRED'"
)
ENUM_HTTP_TRIGGER_ALERT_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'HTTP_TRIGGER_ALERT'"
)

# ISO 27001: run history is append-only. Any physical DELETE on
# http_trigger_runs raises (application soft-deletes touch http_triggers
# only, and even those never physically remove rows).
NO_DELETE_FUNC_SQL = """
CREATE OR REPLACE FUNCTION http_trigger_runs_no_delete() RETURNS trigger AS
$$
BEGIN
    RAISE EXCEPTION 'http_trigger_runs is append-only: physical deletes are forbidden (ISO 27001)';
END;
$$ LANGUAGE plpgsql;
"""
NO_DELETE_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS http_trigger_runs_no_delete ON http_trigger_runs;
CREATE TRIGGER http_trigger_runs_no_delete
    BEFORE DELETE ON http_trigger_runs
    FOR EACH ROW EXECUTE FUNCTION http_trigger_runs_no_delete();
"""

# `apps` may not exist yet (GX.A2) — add the FK only when it does.
_APP_ID_FK_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'apps')
       AND NOT EXISTS (SELECT 1 FROM information_schema.table_constraints
                       WHERE constraint_name = 'http_triggers_app_id_fkey')
    THEN
        ALTER TABLE http_triggers
            ADD CONSTRAINT http_triggers_app_id_fkey
            FOREIGN KEY (app_id) REFERENCES apps(id) ON DELETE SET NULL;
    END IF;
END
$$;
"""

_STATEMENTS = [
    ("auditaction += HTTP_TRIGGER_CREATED", ENUM_HTTP_TRIGGER_CREATED_SQL),
    ("auditaction += HTTP_TRIGGER_FIRED", ENUM_HTTP_TRIGGER_FIRED_SQL),
    ("auditaction += HTTP_TRIGGER_ALERT", ENUM_HTTP_TRIGGER_ALERT_SQL),
    ("create table http_triggers", CREATE_HTTP_TRIGGERS_SQL),
    ("create table http_trigger_runs", CREATE_HTTP_TRIGGER_RUNS_SQL),
    ("index http_triggers.tenant_id", INDEX_TRIGGERS_TENANT_SQL),
    ("index http_triggers(next_fire_at) due", INDEX_TRIGGERS_DUE_SQL),
    ("index http_trigger_runs.trigger_id", INDEX_RUNS_TRIGGER_SQL),
    ("index http_trigger_runs(tenant_id, fired_at DESC)", INDEX_RUNS_TENANT_FIRED_SQL),
    ("apps FK (guarded — no-op until GX.A2)", _APP_ID_FK_SQL),
    ("append-only guard function", NO_DELETE_FUNC_SQL),
    ("append-only guard trigger", NO_DELETE_TRIGGER_SQL),
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
    print("http_triggers + http_trigger_runs ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

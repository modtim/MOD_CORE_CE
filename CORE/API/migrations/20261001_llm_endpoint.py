"""GX.S5 (launch-completion-megaplan Group X) — tenant LLM endpoint.

Adds ONE table so apps/tenants can call their own model stacks through the
OpenAI-compatible POST /v1/llm/chat/completions (and list models via
GET /v1/llm/models).

  tenant_llm_calls      one row per chat/completions call (usage-metering seam: this edition writes no row).
                        METADATA ONLY: model, engine-reported prompt/
                        completion token counts, latency_ms, outcome
                        status. NEVER the prompt or completion text
                        (ISO 27001). SoftDeleteMixin — never hard-deleted.

The table is the usage-metering record. CE tenants still route +
serve but write no row (the service gates the write on the usage_metering
capability), so the table is empty for CE — that is expected.

`app_id` is a plain nullable integer — NO FK yet. The `apps` table (GX.A2)
does not exist yet; GX.A2/A3 adds `REFERENCES apps(id)` + the index in its
own migration (same pattern as EmailMessage.app_id / email_messages).

No new columns on existing tables, no enum changes. Purely additive —
nothing existing is touched. Idempotent: CREATE TABLE/INDEX IF NOT
EXISTS. Safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261001_llm_endpoint.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("tenant_llm_calls",)

CREATE_TENANT_LLM_CALLS_SQL = """
CREATE TABLE IF NOT EXISTS tenant_llm_calls (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- GX.A2/A3 adds: REFERENCES apps(id). Plain nullable integer until the
    -- `apps` table exists.
    app_id              INTEGER,
    user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
    -- The model name the call was made against (tenant-visible stack model).
    model               VARCHAR(255) NOT NULL,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER,
    status              VARCHAR(16) NOT NULL DEFAULT 'success',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);
"""

INDEX_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_tenant_llm_calls_tenant_id
    ON tenant_llm_calls (tenant_id);
"""
INDEX_TENANT_CREATED_SQL = """
CREATE INDEX IF NOT EXISTS ix_tenant_llm_calls_tenant_created
    ON tenant_llm_calls (tenant_id, created_at DESC);
"""
INDEX_TENANT_MODEL_SQL = """
CREATE INDEX IF NOT EXISTS ix_tenant_llm_calls_tenant_model
    ON tenant_llm_calls (tenant_id, model);
"""
INDEX_APP_SQL = """
CREATE INDEX IF NOT EXISTS ix_tenant_llm_calls_app_id
    ON tenant_llm_calls (app_id);
"""

_STATEMENTS = [
    ("create table tenant_llm_calls", CREATE_TENANT_LLM_CALLS_SQL),
    ("index tenant_llm_calls.tenant_id", INDEX_TENANT_SQL),
    ("index tenant_llm_calls(tenant_id, created_at DESC)",
     INDEX_TENANT_CREATED_SQL),
    ("index tenant_llm_calls(tenant_id, model)", INDEX_TENANT_MODEL_SQL),
    ("index tenant_llm_calls.app_id", INDEX_APP_SQL),
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
    print("tenant_llm_calls ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

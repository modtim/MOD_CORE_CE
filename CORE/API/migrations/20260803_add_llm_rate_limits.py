"""managed-ai-surfaces-and-stack-ops §1 — per-tenant LLM rate limits.

Nothing bounded per-tenant LLM volume. All five tools (wizard, adapter,
assistant, support, repair) share ONE model server whose concurrent_users is
finite per preset — 16 on compact, 48 on balanced — so an unbounded tenant does
not merely spend its own budget, it occupies slots other tenants need. A chat
turn can queue a node build behind it.

Two caps, because the failure modes differ. A burst of parallel requests
exhausts slots immediately; a steady loop exhausts the day. A single
calls-per-day cap permits the burst.

Adds:
  - platform_llm_rate_limits   — singleton (id=1) platform defaults
  - tenant_llm_rate_limits     — per-tenant overrides, NULL = inherit

Resolution is tenant override -> platform row -> env var, the same chain
llm_idle already uses, so a deployment that touches neither table behaves
exactly as it did before this existed.

Additive + idempotent. Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260803_add_llm_rate_limits.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


PLATFORM_SQL = """
CREATE TABLE IF NOT EXISTS platform_llm_rate_limits (
    id                          INTEGER PRIMARY KEY DEFAULT 1,
    enabled                     BOOLEAN NOT NULL DEFAULT FALSE,
    concurrent_max              INTEGER NOT NULL DEFAULT 4,
    window_seconds              INTEGER NOT NULL DEFAULT 3600,
    window_max_calls            INTEGER NOT NULL DEFAULT 240,
    assistant_window_max_calls  INTEGER NOT NULL DEFAULT 120,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by_user_id          INTEGER REFERENCES users(id),
    CONSTRAINT platform_llm_rate_limits_singleton CHECK (id = 1)
);
"""

# Seeded DISABLED. A deployment upgrading into this must not suddenly start
# refusing calls; an operator turns it on once they have looked at the numbers.
# The defaults are placeholders — per-tenant LLM volume has never been measured
# here, so they are a starting point to be replaced from observed p95, not a
# considered answer. That is recorded in tasks.md §9.2.
SEED_SQL = """
INSERT INTO platform_llm_rate_limits (id, enabled)
VALUES (1, FALSE)
ON CONFLICT (id) DO NOTHING;
"""

# Every column nullable: NULL means INHERIT the platform default, which is not
# the same as zero. A NOT NULL DEFAULT 0 would silently pin every tenant to
# "no calls allowed" the moment limiting was switched on.
TENANT_SQL = """
CREATE TABLE IF NOT EXISTS tenant_llm_rate_limits (
    tenant_id                   INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    enabled                     BOOLEAN,
    concurrent_max              INTEGER,
    window_seconds              INTEGER,
    window_max_calls            INTEGER,
    assistant_window_max_calls  INTEGER,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by_user_id          INTEGER REFERENCES users(id)
);
"""

# Rejections are recorded so a limit set too low shows up as a pattern in the
# data rather than as unexplained user complaints weeks later.
REJECTION_SQL = """
CREATE TABLE IF NOT EXISTS llm_rate_limit_rejections (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   INTEGER NOT NULL,
    tool        VARCHAR(64) NOT NULL,
    cap         VARCHAR(32) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_llm_rl_rejections_tenant_time
    ON llm_rate_limit_rejections(tenant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_llm_rl_rejections_time
    ON llm_rate_limit_rejections(occurred_at DESC);
"""

STATEMENTS = [PLATFORM_SQL, SEED_SQL, TENANT_SQL, REJECTION_SQL]


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
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        for s in STATEMENTS:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    for s in STATEMENTS:
        cur.execute(s)
    cur.close()
    print("llm rate limits ready: platform_llm_rate_limits (singleton, DISABLED), "
          "tenant_llm_rate_limits, llm_rate_limit_rejections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""llm-stack-wake-idle — operator-controllable idle reclaim.

Idle reclaim of managed-LLM capacity existed but could only be configured with
environment variables (LLM_IDLE_ENABLED / LLM_IDLE_AFTER_S /
LLM_WARM_REQUIRED_TOOLS), which means a redeploy to change it and no way for a
platform admin to see or set it. It also forced one answer on every stack: an
interactive assistant and a nightly batch model want opposite policies, and a
single global setting picks the conservative one, which is how scale-to-zero
ends up switched off entirely.

Adds:
  - platform_llm_idle_settings          — singleton (id=1) platform defaults
  - persistent_stacks.idle_enabled      — NULL = inherit platform default
  - persistent_stacks.idle_after_seconds— NULL = inherit platform default

Resolution order for any stack is stack override -> platform row -> env var, so
a deployment that never touches the new table behaves exactly as before.

Additive + idempotent. Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260802_add_llm_idle_settings.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


TABLE_SQL = """
CREATE TABLE IF NOT EXISTS platform_llm_idle_settings (
    id                   INTEGER PRIMARY KEY DEFAULT 1,
    enabled              BOOLEAN NOT NULL DEFAULT FALSE,
    idle_after_seconds   INTEGER NOT NULL DEFAULT 3600,
    warm_required_tools  JSONB   NOT NULL DEFAULT '[]'::jsonb,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by_user_id   INTEGER REFERENCES users(id),
    CONSTRAINT platform_llm_idle_settings_singleton CHECK (id = 1)
);
"""

# Deliberately NOT seeded with enabled=TRUE. Scale-to-zero is opted into, never
# inherited — a deployment that upgrades must not silently start stopping
# stacks. Seeding the row itself (disabled) so the settings endpoint always has
# something to read.
SEED_SQL = """
INSERT INTO platform_llm_idle_settings (id, enabled, idle_after_seconds, warm_required_tools)
VALUES (1, FALSE, 3600, '[]'::jsonb)
ON CONFLICT (id) DO NOTHING;
"""

# NULL means "inherit", which is why neither column has a default. A stack that
# has never been configured must follow the platform setting, and a boolean
# defaulting to FALSE would silently pin every existing stack to "never reclaim".
STACK_COLUMNS_SQL = """
ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS idle_enabled BOOLEAN;
ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS idle_after_seconds INTEGER;
"""

STATEMENTS = [TABLE_SQL, SEED_SQL, STACK_COLUMNS_SQL]


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
    print("llm idle settings ready: platform_llm_idle_settings (singleton), "
          "persistent_stacks.idle_enabled, persistent_stacks.idle_after_seconds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

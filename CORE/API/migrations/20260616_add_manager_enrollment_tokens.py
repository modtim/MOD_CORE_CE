"""manager enrollment tokens (add-multi-region-mesh — worker-parity onboarding)

One-time, hashed, expiring tokens that let an admin generate a paste-able bootstrap
command for a new manager VM — the control-plane analog of worker enrollment tokens.
Additive + idempotent.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260616_add_manager_enrollment_tokens.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS manager_enrollment_tokens (
    id                SERIAL PRIMARY KEY,
    token_hash        VARCHAR(255) NOT NULL UNIQUE,
    manager_id        VARCHAR(255) NOT NULL,
    region            VARCHAR(100),
    zone              VARCHAR(100),
    label             VARCHAR(255),
    created_by_user_id INTEGER,
    expires_at        TIMESTAMPTZ,
    redeemed_at       TIMESTAMPTZ,
    redeemed_self_url VARCHAR(512),
    revoked_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_manager_enrollment_tokens_token_hash
    ON manager_enrollment_tokens (token_hash);
"""

STATEMENTS = [CREATE_SQL]


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
    print("manager_enrollment_tokens ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

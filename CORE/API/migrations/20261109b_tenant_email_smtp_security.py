"""tenant_email_configs: explicit SMTP encryption choice + self-signed opt-in.

Platform email used to guess the TLS mode from the port (465 SSL, 25 plain,
everything else STARTTLS), so a local/internal relay on 2525/1025 without TLS
could not be used, and a relay with a private CA could not be trusted. Two
ADDITIVE columns make both explicit:

  * smtp_security          VARCHAR(16) NULL   -- NULL / 'auto' = the old port rule;
                                                'starttls' | 'ssl' | 'none' = explicit
  * smtp_allow_selfsigned  BOOLEAN NOT NULL DEFAULT FALSE
                                              -- TRUE = encrypt but do not verify the
                                                 server certificate (internal relays)

No existing row changes behaviour: NULL / FALSE reproduce today's mailer exactly.

Idempotent (`ADD COLUMN IF NOT EXISTS`; a re-run is a no-op) and safe to re-run.
DRY-RUN by default, mod-migrate passes `--commit`:
    python3 migrations/20261109b_tenant_email_smtp_security.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

STATEMENTS = [
    "ALTER TABLE tenant_email_configs ADD COLUMN IF NOT EXISTS smtp_security VARCHAR(16) NULL",
    "ALTER TABLE tenant_email_configs ADD COLUMN IF NOT EXISTS "
    "smtp_allow_selfsigned BOOLEAN NOT NULL DEFAULT FALSE",
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
    dbapi.autocommit = False
    return dbapi


def apply_columns(cur) -> int:
    """Run the ALTERs on a DBAPI cursor inside the caller's transaction."""
    for stmt in STATEMENTS:
        cur.execute(stmt)
    return len(STATEMENTS)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        for stmt in STATEMENTS:
            print(f"{stmt};")
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: DDL needs the owner role (CE has no modTech)
    set_role_ok = _owner_role.maybe_set_role(cur)
    try:
        n = apply_columns(cur)
        if set_role_ok:
            cur.execute("RESET ROLE")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    print(f"tenant_email_configs: {n} column(s) ensured (smtp_security, smtp_allow_selfsigned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

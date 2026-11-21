"""R-095/R-110 — re-hash legacy shareable-link gate passwords to bcrypt.

`crud/shareable_link_cud.py` used to store the gate password as
`f"hashed_{password}"` — plaintext at rest behind a fake prefix. New rows
are bcrypt; the runtime verify path accepts a legacy `hashed_<pw>` row once
and re-hashes it in place on first use. This migration sweeps the
*remaining* legacy rows so no plaintext-at-rest survives.

Each legacy `hashed_<pw>` row becomes `hash_link_password(<pw>)` — the
prefix is stripped and the plaintext is hashed with the SAME helper the
runtime verify path uses (crud/shareable_link_cud.hash_link_password,
including its 72-byte bcrypt truncation). After the sweep, rows verify
through ordinary bcrypt and the plaintext no longer exists anywhere.
UPDATE only — rows are never deleted (ISO no-hard-delete). Idempotent:
the sweep SELECT only matches the `hashed_` prefix, so bcrypt rows
(starting with `$2`) are never touched on a re-run.

Dry run (default, no --commit) PRINTS the row COUNT ONLY (never any
value) and exits 0 without modifying any row. Apply with --commit.

Run inside the API container (the LEAD applies — do not run --commit
from an agent lane):
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261004i_shareable_link_bcrypt.py            # dry run: prints count
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261004i_shareable_link_bcrypt.py --commit   # apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crud.shareable_link_cud import (  # noqa: E402
    LEGACY_HASHED_PREFIX,
    hash_link_password,
)

_LEGACY_PARAM = LEGACY_HASHED_PREFIX + "%"
_LEGACY_COUNT_SQL = (
    "SELECT COUNT(*) FROM shareable_links "
    "WHERE password_hash IS NOT NULL AND password_hash LIKE %s"
)
_LEGACY_SELECT_SQL = (
    "SELECT id, password_hash FROM shareable_links "
    "WHERE password_hash IS NOT NULL AND password_hash LIKE %s"
)


def _get_conn():
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


def count_legacy(cur) -> int:
    cur.execute(_LEGACY_COUNT_SQL, (_LEGACY_PARAM,))
    return int(cur.fetchone()[0])


def select_legacy_rows(cur):
    """Return (id, password_hash) rows still stored in legacy form."""
    cur.execute(_LEGACY_SELECT_SQL, (_LEGACY_PARAM,))
    return cur.fetchall()


def rehash_legacy(stored_hash: str) -> str:
    """Re-hash one legacy `hashed_<pw>` row to bcrypt.

    Uses the exact helper the runtime in-place re-hash uses, so a row
    swept here verifies identically to one re-hashed on first use.
    Raises ValueError for non-legacy rows — the sweep SELECT only ever
    feeds legacy rows, so this guards against a widened predicate.
    """
    if not stored_hash.startswith(LEGACY_HASHED_PREFIX):
        raise ValueError("not a legacy hashed_ row")
    return hash_link_password(stored_hash[len(LEGACY_HASHED_PREFIX):])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)

    try:
        legacy = count_legacy(cur)

        if not args.commit:
            print(f"legacy 'hashed_' shareable_link rows: {legacy}")
            print("(dry-run; pass --commit to re-hash to bcrypt)")
            return 0

        if legacy == 0:
            print("nothing to do — no legacy 'hashed_' rows")
            return 0

        done = 0
        for _row_id, stored in select_legacy_rows(cur):
            cur.execute(
                "UPDATE shareable_links SET password_hash = %s WHERE id = %s",
                (rehash_legacy(stored), _row_id),
            )
            done += 1
        # COUNTS ONLY — never print any password or hash value.
        print(f"re-hashed {done} legacy shareable_link password rows to bcrypt")
        return 0
    finally:
        # RESET ROLE + close happen exactly once, while the cursor is
        # still open (the previous revision closed it and then tried to
        # RESET on the dead cursor).
        try:
            if set_role_ok:
                cur.execute("RESET ROLE")
        except Exception:  # noqa: BLE001
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

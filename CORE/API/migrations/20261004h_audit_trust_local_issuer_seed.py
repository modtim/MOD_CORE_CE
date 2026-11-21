"""GSEC — backfill the missing audit row for the trust_local_issuer seed.

Context: on 2026-10-03 ~18:07Z the platform setting `licensing.trust_local_issuer`
was seeded True DIRECTLY (a raw INSERT into `platform_settings`, bypassing
`services.platform_settings.set_setting`), so the append-only
`platform_settings_audit` table has NO row for that change. Every write through
the normal path IS audited (set_setting/clear_setting add a PlatformSettingAudit
row in the same transaction — verified in services/platform_settings.py); the
gap is only this manual install seeding.

This migration appends ONE audit row recording it:
    key          = licensing.trust_local_issuer
    scope        = platform, tenant_id NULL
    old_value    = NULL  (no prior live row — the seed created the first one)
    new_value    = true
    actor_user_id= NULL  (= system — the same convention set_setting uses for
                          seed writes, actor_user_id=None)
    changed_at   = 2026-10-03 18:07:00+00:00 (the seeding time, pinned so the
                          trail reads when it happened, not when we backfilled)

Append-only: INSERT only — the 20261003c append-only triggers block UPDATE/
DELETE/TRUNCATE, INSERT is allowed. Idempotent: skips when an equivalent row
(same key/scope/new_value/actor) already exists, so re-runs are no-ops.

The value is a boolean switch, not a secret — no redaction applies (see
utils.secret_redaction for keys whose values look secret; none of those
columns store them).

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. The lead applies with --commit.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261004h_audit_trust_local_issuer_seed.py            # dry run
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261004h_audit_trust_local_issuer_seed.py --commit   # apply
"""
from __future__ import annotations

import argparse
import os
import sys

KEY = "licensing.trust_local_issuer"
SEEDING_TS = "2026-10-03 18:07:00+00:00"

# Equivalent-row guard: same key/scope, new_value true, system (NULL) actor.
# changed_at is deliberately NOT part of the equivalence — a second row for
# the same seed event is a duplicate regardless of its timestamp.
GUARD_SQL = f"""
SELECT EXISTS (
    SELECT id FROM platform_settings_audit
    WHERE key = '{KEY}'
      AND scope = 'platform'
      AND tenant_id IS NULL
      AND new_value = 'true'::jsonb
      AND actor_user_id IS NULL
);
"""

INSERT_SQL = f"""
INSERT INTO platform_settings_audit
    (key, scope, tenant_id, old_value, new_value, actor_user_id, changed_at)
    VALUES ('{KEY}', 'platform', NULL, NULL, 'true'::jsonb,
            NULL, '{SEEDING_TS}');
"""


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
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        print(GUARD_SQL)
        print(INSERT_SQL)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(GUARD_SQL)
    row = cur.fetchone()
    if row and row[0]:
        print(f"equivalent audit row for {KEY} already recorded — no-op.")
        cur.close()
        return 0
    cur.execute(INSERT_SQL)
    cur.close()
    print(f"appended audit row for {KEY} (old NULL -> true, actor system, "
          f"changed_at {SEEDING_TS}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

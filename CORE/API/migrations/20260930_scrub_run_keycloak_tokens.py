"""GC.MWL.7c — scrub live keycloak/refresh/access tokens out of existing
``workflow_runs.parameters`` rows.

Until this change, ``routers/workflows_domains/workflows.py`` (and a couple
of other run-creation call sites) wrote the requesting user's live
``keycloak_token`` into the run's transient ``conf`` dict, which
``crud.workflow_cud.create_workflow_run`` persisted verbatim into
``workflow_runs.parameters``. Nothing in-process ever needed the durable
copy — the Go worker never reads ``keycloak_token`` out of a task payload
(storage credentials are minted on-demand via
``GET /workers/tasks/{id}/credentials``) — so it was a live secret sitting
in the database for the run's entire (potentially indefinite) retention.

New runs no longer write it (``crud.workflow_cud._execution_conf_snapshot``
now also gates ``parameters``, not just ``execution_conf`` — see GC.MWL.7b).
This migration is the one-time scrub for rows written before that fix.

Mechanism: a plain ``UPDATE ... SET parameters = parameters - 'key' - ...``
per batch of ids — a JSONB key removal, not a row DELETE. Never uses
``.astext`` (which throws on this column's JSONB shape); membership is
checked with the ``?|`` "any of these keys exist" operator instead.

Safety:
  - Dry-run by default. Prints ONLY counts (total affected rows, batch
    sizes) — NEVER prints ``parameters`` values, so a token can't leak into
    a terminal/log via this script.
  - ``--commit`` is required to write anything.
  - Batched by id range (default 2000 rows/statement) so one long-running
    transaction never holds a lock on the whole table.
  - Idempotent: the ``?|`` guard means a row with no secret key left is
    simply not touched again; re-running after a partial/interrupted
    ``--commit`` is safe.

Run inside the API container:

    docker exec my_fastapi_app python3 \\
        migrations/20260930_scrub_run_keycloak_tokens.py            # dry run (counts only)
    docker exec my_fastapi_app python3 \\
        migrations/20260930_scrub_run_keycloak_tokens.py --commit    # apply
"""
from __future__ import annotations

import argparse
import os
import sys


_TABLE = "workflow_runs"

# Keep in sync with crud.workflow_cud._EXECUTION_CONF_SECRET_KEYS' credential
# subset (the three keys GC.MWL.7c's task explicitly names). Fixed literal
# keys — never interpolated from user input — so they're safe to inline
# directly into the SQL text below.
_SECRET_KEYS = ("keycloak_token", "refresh_token", "access_token")

_SECRET_KEYS_ARRAY_SQL = "ARRAY['" + "','".join(_SECRET_KEYS) + "']"

_COUNT_SQL = f"""
SELECT COUNT(*) FROM {_TABLE}
 WHERE parameters ?| {_SECRET_KEYS_ARRAY_SQL}
"""

_BOUNDS_SQL = f"""
SELECT MIN(id), MAX(id) FROM {_TABLE}
 WHERE parameters ?| {_SECRET_KEYS_ARRAY_SQL}
"""

_SCRUB_SQL_TEMPLATE = f"""
UPDATE {_TABLE}
   SET parameters = parameters - 'keycloak_token' - 'refresh_token' - 'access_token'
 WHERE id BETWEEN %s AND %s
   AND parameters ?| {_SECRET_KEYS_ARRAY_SQL}
"""


def _get_conn():
    """Reuse the API's configured SQLAlchemy engine (Vault-dynamic creds +
    SSL posture) instead of parsing DATABASE_URL ourselves. Run this
    migration from inside the API container where `database` is
    importable."""
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                     help="Apply the scrub. Without this flag, only counts "
                          "are printed and no rows are touched.")
    ap.add_argument("--batch-size", type=int, default=2000,
                     help="Rows per UPDATE statement (by id range). "
                          "Default 2000.")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(_COUNT_SQL)
    total = cur.fetchone()[0]
    print(f"workflow_runs.parameters rows carrying one of "
          f"{_SECRET_KEYS}: {total}")

    if total == 0:
        print("Nothing to scrub.")
        return 0

    if not args.commit:
        import math
        n_batches = math.ceil(total / args.batch_size) if args.batch_size > 0 else 1
        print(f"DRY  would scrub {total} row(s) in ~{n_batches} batch(es) of "
              f"{args.batch_size} via: parameters - 'keycloak_token' - "
              f"'refresh_token' - 'access_token'")
        print("(dry-run; pass --commit to apply. No values are ever printed.)")
        return 0

    cur.execute(_BOUNDS_SQL)
    lo, hi = cur.fetchone()
    scrubbed = 0
    start = lo
    while start is not None and start <= hi:
        end = start + args.batch_size - 1
        cur.execute(_SCRUB_SQL_TEMPLATE, (start, end))
        n = cur.rowcount
        scrubbed += n
        print(f"WRITE  scrubbed {n} row(s) in id range [{start}, {end}]")
        start = end + 1

    print(f"Done. Scrubbed {scrubbed} row(s) total out of {total} reported "
          f"(idempotent; safe to re-run).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

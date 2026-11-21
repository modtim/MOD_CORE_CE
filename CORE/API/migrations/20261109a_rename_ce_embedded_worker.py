"""CE embedded worker is ALWAYS named `mod-worker-embedded`.

Until now the CE compose started the manager-node (embedded) worker as
`mod-worker-ce`, while every other edition runs it as `mod-worker-embedded`.
Platform scripts and the API key off the exact name `mod-worker-embedded`, so CE
now uses it too (open-core/overlay/docker-compose.yml, and the Go worker pins the
name in embedded mode). This migration renames an existing CE install's identity
row IN PLACE so the upgrade does not leave an orphaned `mod-worker-ce` worker
beside a freshly auto-created `mod-worker-embedded` one (the row id, licence,
placements and hardware profile all stay attached to the same worker).

`worker_licenses.worker_name` is the target of NAME-keyed foreign keys
(`api_task_queue.claimed_by_worker` / `assigned_to_worker`,
`persistent_stacks.target_worker`, `worker_resource_timelines.worker_name`), none
of which is ON UPDATE CASCADE, so a bare UPDATE fails with a
ForeignKeyViolation. In ONE transaction this migration therefore:
  1. discovers every single-column FK that references worker_licenses(worker_name)
     (from pg_constraint, so a future FK is covered too) and DROPs it,
  2. renames the identity row,
  3. carries the old name over in each of those child columns,
  4. re-ADDs each FK with its exact original definition (the schema ends identical).
Any failure rolls the whole thing back and leaves the install untouched.

Not touched on purpose:
  * history/telemetry tables with a plain `worker_name` text column and no FK
    (node_lockdown_verdicts, task_resource_samples, ...): some are append-only and
    guarded, and they should keep the name the worker had at the time;
  * `worker_enrollments`: the API seeds BOTH names at startup, so the canonical
    enrollment already exists on every install.

Idempotent and conservative:
  * no `mod-worker-ce` row                      -> nothing to do
  * `mod-worker-ce` row, no canonical row       -> rename it (and its FK children)
  * BOTH rows exist (a canonical worker already registered) -> leave both and
    print a loud line for a manual merge; this never guesses or deletes.

DRY-RUN by default. mod-migrate passes `--commit`:
    python3 migrations/20261109a_rename_ce_embedded_worker.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

LEGACY_NAME = "mod-worker-ce"
CANONICAL_NAME = "mod-worker-embedded"

# Single-column FKs whose referenced column is worker_licenses.worker_name.
# regclass::text is schema-qualified/quoted as needed; quote_ident covers the rest.
_NAME_FKS_SQL = """
SELECT c.conrelid::regclass::text,
       quote_ident(c.conname),
       quote_ident(a.attname),
       pg_get_constraintdef(c.oid)
FROM pg_constraint c
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
WHERE c.contype = 'f'
  AND c.confrelid = 'worker_licenses'::regclass
  AND array_length(c.conkey, 1) = 1
  AND EXISTS (SELECT 1 FROM pg_attribute r
              WHERE r.attrelid = c.confrelid
                AND r.attnum = c.confkey[1]
                AND r.attname = 'worker_name')
ORDER BY 1, 2
"""


def rename_legacy_embedded_worker(cur, apply: bool) -> str:
    """Core logic on a DBAPI cursor (psycopg2 `%s` params), inside the caller's
    transaction. Returns one of 'noop', 'renamed', 'would-rename', 'conflict'."""
    cur.execute(
        "SELECT id, worker_name FROM worker_licenses WHERE worker_name IN (%s, %s) ORDER BY id",
        (LEGACY_NAME, CANONICAL_NAME),
    )
    names = {row[1]: row[0] for row in cur.fetchall()}
    if LEGACY_NAME not in names:
        print(f"embedded worker: no '{LEGACY_NAME}' row, nothing to rename.")
        return "noop"
    if CANONICAL_NAME in names:
        print(
            f"embedded worker: CONFLICT - both '{LEGACY_NAME}' (id {names[LEGACY_NAME]}) and "
            f"'{CANONICAL_NAME}' (id {names[CANONICAL_NAME]}) exist; left untouched, "
            f"merge by hand."
        )
        return "conflict"

    cur.execute(_NAME_FKS_SQL)
    fks = cur.fetchall()
    if not apply:
        print(
            f"embedded worker: would rename worker_licenses id {names[LEGACY_NAME]} "
            f"'{LEGACY_NAME}' -> '{CANONICAL_NAME}' and carry it through "
            f"{len(fks)} name-keyed FK column(s): "
            + ", ".join(f"{t}.{col}" for t, _n, col, _d in fks)
        )
        return "would-rename"

    for table, conname, _col, _definition in fks:
        cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT {conname}")
    cur.execute(
        "UPDATE worker_licenses SET worker_name = %s "
        "WHERE worker_name = %s "
        "AND NOT EXISTS (SELECT 1 FROM worker_licenses WHERE worker_name = %s)",
        (CANONICAL_NAME, LEGACY_NAME, CANONICAL_NAME),
    )
    renamed = cur.rowcount
    carried = 0
    for table, _conname, col, _definition in fks:
        cur.execute(
            f"UPDATE {table} SET {col} = %s WHERE {col} = %s",
            (CANONICAL_NAME, LEGACY_NAME),
        )
        carried += cur.rowcount
    for table, conname, _col, definition in fks:
        cur.execute(f"ALTER TABLE {table} ADD CONSTRAINT {conname} {definition}")
    print(
        f"embedded worker: renamed worker_licenses id {names[LEGACY_NAME]} "
        f"'{LEGACY_NAME}' -> '{CANONICAL_NAME}' ({renamed} row), carried the name through "
        f"{carried} child row(s) in {len(fks)} FK column(s), constraints restored."
    )
    return "renamed"


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
    # ONE transaction: the FK drop / rename / carry / re-add is all-or-nothing.
    dbapi.autocommit = False
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    conn = _get_conn()
    cur = conn.cursor()
    try:
        rename_legacy_embedded_worker(cur, apply=args.commit)
        if args.commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    if not args.commit:
        print("(dry-run; pass --commit to apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

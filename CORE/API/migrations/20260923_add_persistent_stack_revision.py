"""stack-definition-edit — durable persistent-stack composition+config
revisions.

Everyone-facing feature: let a user with edit permission on a stack edit its
composition (services) AND config (env/ports/volumes/reservations), see a
diff against history, revert, and redeploy. Mirrors the existing
`node_source_revision` table (migrations/20260901_node_source_revision.py) —
same ordered, append-only, forever-retained-by-design shape, one row per save.

Table ``persistent_stack_revision``:

  - ``stack_id``            owning stack (FK, ON DELETE SET NULL so a revision
                             survives stack teardown+reap — ISO 27001 provenance)
  - ``stack_name``          denormalized name kept if the stack row is later gone
  - ``tenant_id``            owning tenant (ON DELETE CASCADE on tenant offboard)
  - ``revision_no``          1 = materialized from whatever compose_spec already
                             held; each saved edit +1
  - ``parent_revision_id``   prior revision this one derives from
  - ``origin``               created | edited | reverted
  - ``author_user_id``       who authored the revision
  - ``compose_spec``         full compose_spec snapshot at this revision
  - ``storage_class``        snapshot of the stack's storage_class at this revision
  - ``message``              optional free-text note attached to the save
  - ``created_at``           row creation time

RETENTION = FOREVER. No delete endpoint exists or is planned for these rows
(ISO 27001 — no hard deletes; see memory note iso-no-hard-delete) — a stack's
edit history is small and bounded by how often a human edits it, unlike a
per-request audit log, so it needs no tier-aware pruning.

No backfill: an existing stack materializes revision 1 lazily on first
GET/PUT of its definition.

Run inside the API container:

    docker exec my_fastapi_app python3 \\
        migrations/20260923_add_persistent_stack_revision.py --commit

Idempotent — repeated runs are safe (existence tested before each CREATE,
IF NOT EXISTS on columns/indexes).
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


_CREATE = """
CREATE TABLE IF NOT EXISTS persistent_stack_revision (
    id                  SERIAL PRIMARY KEY,
    stack_id            INTEGER REFERENCES persistent_stacks(id) ON DELETE SET NULL,
    stack_name          VARCHAR(255),
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    revision_no         INTEGER NOT NULL,
    parent_revision_id  INTEGER REFERENCES persistent_stack_revision(id) ON DELETE SET NULL,
    origin              VARCHAR(20) NOT NULL DEFAULT 'edited',
    author_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    compose_spec        JSONB NOT NULL,
    storage_class       VARCHAR(20),
    message             TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_INDEXES = [
    ("ix_persistent_stack_revision_stack_rev",
     "persistent_stack_revision (stack_id, revision_no)"),
    ("ix_persistent_stack_revision_tenant_id",
     "persistent_stack_revision (tenant_id)"),
    ("ix_persistent_stack_revision_created_at",
     "persistent_stack_revision (created_at)"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)

    if _table_exists(cur, "persistent_stack_revision"):
        print("Table persistent_stack_revision already present.")
    else:
        if not args.commit:
            print("DRY RUN: would CREATE TABLE persistent_stack_revision")
        else:
            cur.execute(_CREATE)
            print("Created persistent_stack_revision.")

    # Vault rotates the API's Postgres role between connections, so a table
    # created under one ephemeral role can't be ALTERed/FK-referenced by
    # another. Re-own to the static POSTGRES_USER, matching every other
    # table's convention (see 20260901_node_source_revision.py). Idempotent.
    if _table_exists(cur, "persistent_stack_revision"):
        if not args.commit:
            print(f"DRY RUN: would ALTER TABLE persistent_stack_revision OWNER TO {owner}")
        else:
            try:
                cur.execute(f'ALTER TABLE persistent_stack_revision OWNER TO {owner}')
                print(f"Set owner of persistent_stack_revision to {owner}.")
            except Exception as e:  # noqa: BLE001 — best-effort, matches startup reassign
                print(f"Could not set owner to {owner}: {e}")

    for name, target in _INDEXES:
        cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (name,))
        if cur.fetchone() is not None:
            print(f"Index {name} already present.")
            continue
        if not args.commit:
            print(f"DRY RUN: would create index {name}")
            continue
        cur.execute(f"CREATE INDEX {name} ON {target}")
        print(f"Created index {name}.")

    if not args.commit:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

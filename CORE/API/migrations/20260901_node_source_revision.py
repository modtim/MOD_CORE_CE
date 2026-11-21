"""node-code-edit-rebuild — durable node source revisions (§1).

The auto-generated / hand-edited source for a node is stored per-node as an
ordered revision chain and is the DURABLE source of truth — `build_specs.spec_json`
is garbage-collected by the retention sweep, so it cannot hold a registered
node's code long-term.

Table ``node_source_revision``:

  - ``node_definition_id``   owning node (FK, ON DELETE SET NULL so a revision
                             survives node retirement — ISO 27001 provenance)
  - ``node_name``            denormalized name kept if the node row is later gone
  - ``tenant_id``            owning tenant (ON DELETE CASCADE on tenant offboard)
  - ``revision_no``          1 = generated/copied original; each edit +1
  - ``parent_revision_id``   prior revision this one derives from
  - ``origin``               generated | copied | edited | reverted
  - ``author_user_id``       who authored the revision
  - ``hero``                 {dockerfile, entrypoint_sh, build_and_push_sh, source_files}
  - ``source_overrides_by_variant``  per-accelerator file overrides
  - ``variants_snapshot``    variant set at authoring time (for full-fanout seed)
  - ``critic_warnings``      executability / GPU-classification findings (warnings)
  - ``deleted_at`` / ``deleted_by_user_id``   explicit, audited delete only — the
                             row is RETAINED (never age-pruned; retention = forever)
  - ``created_at``           row creation time

Also adds to ``node_definitions``:
  - ``forked_from_node_definition_id``  lineage: the node this was forked/copied from
  - ``source_origin``        marker for how the node's source arrived

RETENTION = FOREVER. This table is intentionally NOT added to the
``hard_delete_after_retention`` sweep — revisions are an ISO 27001 provenance
record and are never reaped by age.

No backfill: legacy nodes materialize revision 1 lazily on first view/edit.

Run inside the API container:

    docker exec my_fastapi_app python3 \\
        migrations/20260901_node_source_revision.py --commit

Idempotent — repeated runs are safe (existence tested before each CREATE/ALTER
because the Vault dynamic credential means a later run is a different role).
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


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def _index_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (name,))
    return cur.fetchone() is not None


_CREATE = """
CREATE TABLE node_source_revision (
    id                          SERIAL PRIMARY KEY,
    node_definition_id          INTEGER REFERENCES node_definitions(id) ON DELETE SET NULL,
    node_name                   VARCHAR(100),
    tenant_id                   INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    revision_no                 INTEGER NOT NULL,
    parent_revision_id          INTEGER REFERENCES node_source_revision(id) ON DELETE SET NULL,
    origin                      VARCHAR(20) NOT NULL DEFAULT 'edited',
    author_user_id              INTEGER REFERENCES users(id) ON DELETE SET NULL,
    hero                        JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_overrides_by_variant JSONB NOT NULL DEFAULT '{}'::jsonb,
    variants_snapshot           JSONB,
    critic_warnings             JSONB,
    deleted_at                  TIMESTAMPTZ,
    deleted_by_user_id          INTEGER,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_INDEXES = [
    ("ix_node_source_revision_node_rev",
     "node_source_revision (node_definition_id, revision_no)"),
    ("ix_node_source_revision_tenant_id",
     "node_source_revision (tenant_id)"),
    ("ix_node_source_revision_deleted_at",
     "node_source_revision (deleted_at)"),
]

_NODE_DEF_COLUMNS = [
    ("forked_from_node_definition_id",
     "ALTER TABLE node_definitions ADD COLUMN forked_from_node_definition_id "
     "INTEGER REFERENCES node_definitions(id) ON DELETE SET NULL"),
    ("source_origin",
     "ALTER TABLE node_definitions ADD COLUMN source_origin VARCHAR(20)"),
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

    if _table_exists(cur, "node_source_revision"):
        print("Table node_source_revision already present.")
    else:
        if not args.commit:
            print("DRY RUN: would CREATE TABLE node_source_revision")
        else:
            cur.execute(_CREATE)
            print("Created node_source_revision.")

    # Vault rotates the API's Postgres role between connections, so a table
    # created under one ephemeral role can't be ALTERed/FK-referenced by another.
    # Re-own to the static POSTGRES_USER (default modTech), matching every other
    # table's convention (see 20260605_add_sso_idp_configs.py). Idempotent.
    if _table_exists(cur, "node_source_revision"):
        if not args.commit:
            print(f"DRY RUN: would ALTER TABLE node_source_revision OWNER TO {owner}")
        else:
            try:
                cur.execute(f'ALTER TABLE node_source_revision OWNER TO {owner}')
                print(f"Set owner of node_source_revision to {owner}.")
            except Exception as e:  # noqa: BLE001 — best-effort, matches startup reassign
                print(f"Could not set owner to {owner}: {e}")

    # tenant_id is nullable — a SYSTEM node (tenant_id NULL) has tenant-less
    # revisions. Drop NOT NULL if an earlier run created the column NOT NULL.
    if _table_exists(cur, "node_source_revision"):
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='node_source_revision' AND column_name='tenant_id'"
        )
        row = cur.fetchone()
        if row and row[0] == "NO":
            if not args.commit:
                print("DRY RUN: would ALTER node_source_revision.tenant_id DROP NOT NULL")
            else:
                try:
                    cur.execute("ALTER TABLE node_source_revision ALTER COLUMN tenant_id DROP NOT NULL")
                    print("Dropped NOT NULL on node_source_revision.tenant_id.")
                except Exception as e:  # noqa: BLE001 — best-effort (ownership)
                    print(f"Could not drop NOT NULL on tenant_id: {e}")

    for name, target in _INDEXES:
        if _index_exists(cur, name):
            print(f"Index {name} already present.")
            continue
        if not args.commit:
            print(f"DRY RUN: would create index {name}")
            continue
        cur.execute(f"CREATE INDEX {name} ON {target}")
        print(f"Created index {name}.")

    for column, ddl in _NODE_DEF_COLUMNS:
        if _column_exists(cur, "node_definitions", column):
            print(f"Column node_definitions.{column} already present.")
            continue
        if not args.commit:
            print(f"DRY RUN: would add column node_definitions.{column}")
            continue
        cur.execute(ddl)
        print(f"Added node_definitions.{column}.")

    if not args.commit:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

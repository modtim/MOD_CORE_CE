"""human-assisted-discovery — the learned-crossover store.

When the describe-search verdict is WEAK (no curated crossover, no learned
crossover, and the scored candidates are worth-showing-thin) the wizard asks the
user to name a library / GitHub repo / technical term. The parsed answer is
folded into the search AND recorded in this table, keyed to the description's
arctic-embed embedding, so a future near-identical description resolves without
asking again.

Columns:

  - ``tenant_id``             owning tenant (a learned entry is private to it
                              unless promoted)
  - ``description``           the verbatim user description that triggered the ask
  - ``embedding``             JSONB list[float] — arctic-embed unit vector of the
                              description; cosine is done in Python (NO pgvector)
  - ``jargon`` / ``github`` / ``pypi``   the crossover payload parsed from the
                              human answer (mirrors curated crossover_for())
  - ``approved_cross_tenant`` operator gate: eligible as a cross-tenant match
  - ``created_by``            user who supplied the answer (nullable)
  - ``hit_count``             later queries that matched this row
  - ``created_at``            row creation time

No backfill: no learned entries exist yet, and nothing else reads this table.

Run inside the API container:

    docker exec my_fastapi_app python3 \\
        migrations/20260829_wizard_learned_crossovers.py --commit

Idempotent — repeated runs are safe. Existence is tested before each CREATE
because the Vault dynamic credential means a later run is a different Postgres
role (IF NOT EXISTS still raises on ownership).
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


def _index_exists(cur, name: str) -> bool:
    """Check before creating, rather than leaning on IF NOT EXISTS.

    The database credential is a Vault dynamic role, so a later run is a
    DIFFERENT Postgres user from the one that created the table. Postgres checks
    ownership BEFORE honouring `IF NOT EXISTS`, so the idempotent form still
    raises `InsufficientPrivilege: must be owner of table` on re-run. Testing
    first keeps this genuinely re-runnable by whichever role Vault hands out.
    """
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (name,))
    return cur.fetchone() is not None


_CREATE = """
CREATE TABLE wizard_learned_crossovers (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER NOT NULL,
    description            TEXT NOT NULL,
    embedding              JSONB NOT NULL DEFAULT '[]'::jsonb,
    jargon                 JSONB NOT NULL DEFAULT '[]'::jsonb,
    github                 JSONB NOT NULL DEFAULT '[]'::jsonb,
    pypi                   JSONB NOT NULL DEFAULT '[]'::jsonb,
    approved_cross_tenant  BOOLEAN NOT NULL DEFAULT false,
    created_by             INTEGER,
    hit_count              INTEGER NOT NULL DEFAULT 0,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_INDEXES = [
    ("ix_wizard_learned_crossovers_tenant_id",
     "wizard_learned_crossovers (tenant_id)"),
    ("ix_wizard_learned_crossovers_tenant_approved",
     "wizard_learned_crossovers (tenant_id, approved_cross_tenant)"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if _table_exists(cur, "wizard_learned_crossovers"):
        print("Table wizard_learned_crossovers already present.")
    else:
        if not args.commit:
            print("DRY RUN: would CREATE TABLE wizard_learned_crossovers")
        else:
            cur.execute(_CREATE)
            print("Created wizard_learned_crossovers.")

    for name, target in _INDEXES:
        if _index_exists(cur, name):
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

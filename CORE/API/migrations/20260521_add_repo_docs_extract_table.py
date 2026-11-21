"""Wizard-rebuild §3c — repo_docs_extract cache table.

Caches the chunk + intent-tag pipeline output for a (tenant, repo_url,
ref) tuple at a specific extractor_version. The Git-mode BuildPlan
extractor (later §3 chunk) hits this cache instead of re-fetching +
re-chunking + re-tagging on every wizard submission.

Schema:
  - id (PK)
  - tenant_id (FK tenants, ON DELETE CASCADE, indexed)
  - repo_url (TEXT, indexed)
  - ref (TEXT) — branch / tag / commit sha that was fetched
  - extractor_version (INTEGER) — matches BuildPlan.EXTRACTOR_VERSION
  - chunks_json (JSONB) — list of DocChunk dicts, each carrying
    intents already populated by the §3b tagger
  - files_missing (JSONB) — list of sentinel file paths we tried
    but didn't find (surfaces in View Plan UI so users know we
    looked)
  - fetched_at (TIMESTAMPTZ NOT NULL DEFAULT NOW()) — when the
    cache row was populated
  - expires_at (TIMESTAMPTZ NOT NULL, indexed) — when the row
    becomes invalid; 7 days from fetched_at by default

  UNIQUE (tenant_id, repo_url, ref, extractor_version) — cache key.
  When extractor_version bumps, the old row coexists with the new
  one until the sweep purges it; this avoids races where two
  wizard submissions for the same repo race to populate the
  cache after a version bump.

Lifecycle:
  - Wizard's Git-mode extractor calls `store.get_or_extract(db,
    tenant_id, repo_url, ref)`. Cache hit returns cached row;
    cache miss fetches sentinels, chunks them, tags intents,
    writes the row, returns it.
  - APScheduler hourly job (`_run_repo_docs_extract_sweep`) deletes
    rows where `expires_at < NOW()`.
  - Tenant offboarding cascades (FK ON DELETE CASCADE).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260521_add_repo_docs_extract_table.py --commit
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    steps: list[tuple[str, str]] = []

    if not _table_exists(cur, "repo_docs_extract"):
        steps.append((
            "create repo_docs_extract table",
            """
            CREATE TABLE repo_docs_extract (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                repo_url TEXT NOT NULL,
                ref TEXT NOT NULL,
                extractor_version INTEGER NOT NULL,
                chunks_json JSONB NOT NULL,
                files_missing JSONB NOT NULL DEFAULT '[]'::jsonb,
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT uq_repo_docs_extract_key
                    UNIQUE (tenant_id, repo_url, ref, extractor_version)
            )
            """,
        ))
        steps.append((
            "index tenant_id",
            "CREATE INDEX IF NOT EXISTS ix_repo_docs_extract_tenant "
            "ON repo_docs_extract(tenant_id)",
        ))
        steps.append((
            "index repo_url for prefix lookups",
            "CREATE INDEX IF NOT EXISTS ix_repo_docs_extract_repo_url "
            "ON repo_docs_extract(repo_url)",
        ))
        steps.append((
            "index expires_at for sweep efficiency",
            "CREATE INDEX IF NOT EXISTS ix_repo_docs_extract_expires_at "
            "ON repo_docs_extract(expires_at)",
        ))

    if not steps:
        print("schema already current — nothing to do.")
        return 0

    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        print(f"        {stmt.strip()[:200]}")
        if args.commit:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

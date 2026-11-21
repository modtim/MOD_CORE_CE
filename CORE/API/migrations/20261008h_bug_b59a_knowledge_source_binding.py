#!/usr/bin/env python3
"""owner BUG.B59 (Q-B59=A) — the ONE new table for assistant knowledge sources.

revision      : 20261008h_bug_b59a_knowledge_source_binding
down_revision : 20261008g_ai_slot_binding

`ai_slot_binding` binds ONE embedding slot (`assistant_knowledge_embed`) to ONE
model, so the whole assistant knowledge base is embedded by a single model.
Q-B59=A makes the assistant's knowledge a PICKER over knowledge SOURCES, each
with its own embedding model — MOD Docs, a tenant's own files, tenant uploads —
so a query is embedded with the model bound to the source it searches. This
table is that picker: one row per (scope, tenant, source_kind).

    ai_knowledge_source_binding
      id                    BIGSERIAL PRIMARY KEY
      scope                 text NOT NULL   'platform' | 'tenant'
      tenant_id             int             FK tenants.id, NULL when scope='platform'
      source_kind           text NOT NULL   'mod_docs' | 'tenant_files' | 'uploads'
      enabled               bool  default true   soft delete: false, never a DELETE
      target_kind           text NOT NULL   'stack' | 'external' | 'auto'
      stack_id              int             FK persistent_stacks.id, NULL otherwise
      stack_name            text            denormalized name (FK is SET NULL)
      served_model          text            the model id the target actually serves
      base_url / model      text            external transport only
      key_ref               text            a Vault path, NEVER the secret itself
      created_at / updated_at / retired_at
      created_by_user_id    int             FK users.id

`id` is `BIGSERIAL PRIMARY KEY` — NOT `DEFAULT COALESCE((SELECT max(id) ...))`,
which Postgres rejects (a subquery is not a legal DEFAULT).

`source_kind` values are the ingestion kinds that ACTUALLY exist in code plus the
platform corpus (services/assistant_knowledge_service.py: create_upload_document
-> source_type='upload', create_text_document -> 'paste', create_folder_documents
-> 'folder' over an existing tenant storage folder/Cog; services/assistant_orchestrator.py
`retrieve_docs` reads MOD's documentation corpus):

  'mod_docs'      MOD's own documentation corpus (the retrieve_docs tool)
  'tenant_files'  the tenant's existing storage folders/Cogs (/folder, 'folder')
  'uploads'       documents a tenant uploaded or pasted (/upload 'upload',
                  /text 'paste')

The embedding target columns mirror AiSlotBinding's verbatim (target_kind
stack|external|auto, stack_id FK SET NULL + denormalized stack_name, served_model,
base_url, model, key_ref) so the resolver code that already understands that shape
understands this table too.

FKs carry NO `ON DELETE CASCADE`: deleting a tenant/stack/user must never erase
the record of what that source once ran on (ISO 27001 A.18.1.3 — protection of
records). `stack_id` is `ON DELETE SET NULL` (two words) and keeps a denormalized
`stack_name` so a removed stack still leaves the row readable.

Rows are NEVER hard-deleted: retiring a source sets `enabled=false` and stamps
`retired_at`. The PARTIAL unique index enforces at most ONE LIVE row per
(scope, tenant, source_kind) — `coalesce(tenant_id, 0)` makes NULL tenant_id mean
platform — so a source has exactly one embedding model at a time, and retiring it
frees that kind for a new pick without colliding with the retired row itself.

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE ... IF NOT EXISTS, so a re-run is
a no-op. This is a brand-new table (no old shape to converge). Downgrade is a
NO-OP (ISO 27001 — never drop the bindings).

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the lane.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "ai_knowledge_source_binding"
_SHORT = "akb"            # Postgres truncates identifiers at 31 bytes, so derived
_LIVE_INDEX = f"uq_{_SHORT}_live"  # names use this short form, not the 27-char table name

_SOURCE_KIND_CHECK = ("source_kind IN ('mod_docs', 'tenant_files', 'uploads')")
_SCOPE_CHECK = "scope IN ('platform', 'tenant')"
_TARGET_KIND_CHECK = "target_kind IN ('stack', 'external', 'auto')"
_SCOPE_TENANT_CHECK = (
    "(scope = 'platform' AND tenant_id IS NULL)\n"
    "        OR (scope = 'tenant' AND tenant_id IS NOT NULL)"
)
_STACK_TARGET_CHECK = "target_kind != 'stack' OR stack_id IS NOT NULL"
_EXTERNAL_TARGET_CHECK = "target_kind != 'external' OR base_url IS NOT NULL"
_RETIREMENT_CLOCK_CHECK = "enabled OR retired_at IS NOT NULL"
_RETIRED_IS_DISABLED_CHECK = "retired_at IS NULL OR NOT enabled"


_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    scope TEXT NOT NULL,
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
    source_kind TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    target_kind TEXT NOT NULL,
    stack_id INTEGER REFERENCES persistent_stacks(id) ON DELETE SET NULL,
    stack_name TEXT,
    served_model TEXT,
    base_url TEXT,
    model TEXT,
    key_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at TIMESTAMPTZ,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT ck_{_SHORT}_source_kind CHECK (
        {_SOURCE_KIND_CHECK}
    ),
    CONSTRAINT ck_{_SHORT}_scope CHECK (
        {_SCOPE_CHECK}
    ),
    CONSTRAINT ck_{_SHORT}_target_kind CHECK (
        {_TARGET_KIND_CHECK}
    ),
    CONSTRAINT ck_{_SHORT}_scope_tenant CHECK (
        {_SCOPE_TENANT_CHECK}
    ),
    CONSTRAINT ck_{_SHORT}_stack_target_names_stack CHECK (
        {_STACK_TARGET_CHECK}
    ),
    CONSTRAINT ck_{_SHORT}_ext_target_names_base_url CHECK (
        {_EXTERNAL_TARGET_CHECK}
    ),
    CONSTRAINT ck_{_SHORT}_retirement_clock CHECK (
        {_RETIREMENT_CLOCK_CHECK}
    ),
    CONSTRAINT ck_{_SHORT}_retired_is_disabled CHECK (
        {_RETIRED_IS_DISABLED_CHECK}
    )
);
"""

_LIVE_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_LIVE_INDEX}
    ON {_TABLE} (scope, COALESCE(tenant_id, 0), source_kind)
    WHERE (retired_at IS NULL);
"""

_PLAIN_INDEXES = f"""
CREATE INDEX IF NOT EXISTS ix_{_SHORT}_scope ON {_TABLE} (scope);
CREATE INDEX IF NOT EXISTS ix_{_SHORT}_tenant_id ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_{_SHORT}_source_kind ON {_TABLE} (source_kind);
CREATE INDEX IF NOT EXISTS ix_{_SHORT}_stack_id ON {_TABLE} (stack_id);
CREATE INDEX IF NOT EXISTS ix_{_SHORT}_enabled ON {_TABLE} (enabled);
CREATE INDEX IF NOT EXISTS ix_{_SHORT}_created_at ON {_TABLE} (created_at);
CREATE INDEX IF NOT EXISTS ix_{_SHORT}_retired_at ON {_TABLE} (retired_at);
CREATE INDEX IF NOT EXISTS ix_{_SHORT}_created_by_user_id
    ON {_TABLE} (created_by_user_id);
CREATE INDEX IF NOT EXISTS ix_{_SHORT}_scope_tenant_kind
    ON {_TABLE} (scope, tenant_id, source_kind);
"""

_CREATE_STEPS = [
    (f"{_TABLE} table (owner BUG.B59 Q-B59=A knowledge-source picker)",
     _CREATE_TABLE),
    (f"{_TABLE} partial-unique live index", _LIVE_INDEX_SQL),
    (f"{_TABLE} indexes", _PLAIN_INDEXES),
]


def _sql_statements(sql: str) -> list[str]:
    """Split a block into single statements so each is its own guarded step."""
    return [s.strip() for s in sql.split(";\n") if s.strip()]


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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the CREATE + index steps. Without this flag, "
                        "dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _CREATE_STEPS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (ISO 27001 — never drop the bindings)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)

    for label, sql in _CREATE_STEPS:
        for stmt in _sql_statements(sql):
            try:
                cur.execute(stmt)
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {label}: {e}")
                return 1
        print(f"OK   {label}")

    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "ai_knowledge_source_binding ready: one LIVE row per (scope, tenant, "
        "source_kind) via the partial-unique live index; retired sources are "
        "tombstones, never deleted."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

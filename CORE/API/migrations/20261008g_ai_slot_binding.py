#!/usr/bin/env python3
"""ai-config-stack-binding 1.1 — the ONE new table for AI slot bindings.

revision      : 20261008g_ai_slot_binding
down_revision : 20261008f_b19_listing_code_secrets

`ai_slot_binding` (design D1) moves the AI-role -> target binding OUT of the
Vault tool-config blob and INTO a queryable table: "which slots point at stack
42?" is a SQL read, and tenant-over-platform precedence resolves in SQL, not
by reading six Vault paths per request. Vault keeps the secret (`key_ref` is a
Vault PATH, never the secret) and the non-binding knobs.

    ai_slot_binding
      id                    BIGSERIAL PRIMARY KEY
      slot                  text NOT NULL   wizard|adapter|assistant|support|repair|critic
                                            |wizard_learning_embed|assistant_user_rag_embed
                                            |assistant_knowledge_embed
      capability            text NOT NULL   'chat' | 'embedding'  (default 'chat')
      scope                 text NOT NULL   'platform' | 'tenant'
      tenant_id             int             FK tenants.id, NULL when scope='platform'
      target_kind           text NOT NULL   'stack' | 'external' | 'auto'
      stack_id              int             FK persistent_stacks.id, NULL otherwise
      stack_name            text            denormalized name (FK is SET NULL)
      base_url / model      text            external transport only
      key_ref               text            a Vault path, NEVER the secret itself
      priority              smallint default 0   ordered fallback chain (owner BUG.B56):
                                            0 = primary, 1..15 = fallbacks tried in
                                            order after a failure
      enabled               bool  default true   soft delete: false, never a DELETE
      created_at / updated_at / retired_at
      created_by_user_id    int             FK users.id

`id` is `BIGSERIAL PRIMARY KEY` — NOT `DEFAULT COALESCE((SELECT max(id) ...))`,
which Postgres rejects (a subquery is not a legal DEFAULT).

FKs carry NO `ON DELETE CASCADE`: deleting a tenant/stack/user must never erase
the record of what that slot once ran on (ISO 27001 A.18.1.3 — protection of
records). `stack_id` is `ON DELETE SET NULL` (two words) and keeps a denormalized
`stack_name` so a removed stack still leaves the row readable.

Rows are NEVER hard-deleted: retiring a binding sets `enabled=false` and stamps
`retired_at`. The PARTIAL unique index enforces at most ONE ENABLED row per
(slot, scope, tenant, priority) — `coalesce(tenant_id, 0)` makes NULL tenant_id
mean platform — so a slot holds an ORDERED fallback chain (#0 primary, #1..#15
fallbacks, owner BUG.B56) keyed by position, and retiring frees that position
for a new pick without colliding with the retired row itself.

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE ... IF NOT EXISTS.

CONVERGE (job 1.1c): the LIVE DB already carries `ai_slot_binding` in an OLD
shape (capability/priority absent, `uq_ai_slot_binding_live` keyed without
priority). `CREATE TABLE IF NOT EXISTS` skips the table, so the fresh CREATE's
new columns/constraints/index never land and the fresh index step then fails
("column priority does not exist"). Every step below the CREATE is therefore
re-stated as its own guarded, separately-executed statement so a re-run is a
no-op and an old-shape table converges to the fresh shape:

  ADD COLUMN IF NOT EXISTS capability / priority,
  ALTER COLUMN slot TYPE only when the existing type cannot hold the model's
  String(28) (live is TEXT -> no-op),
  DROP CONSTRAINT IF EXISTS ck_ai_slot_binding_slot + guarded re-ADD of the
  widened slot CHECK, guarded ADD of ck_ai_slot_binding_capability /
  _capability_matches_slot / _priority_range,
  DROP INDEX IF EXISTS uq_ai_slot_binding_live + CREATE UNIQUE INDEX IF NOT
  EXISTS with the priority key, plus the plain priority/capability indexes.

Dropping/recreating a constraint + index on THIS table is only acceptable while
it holds 0 rows, so `_converge_guard` aborts the converge phase with a clear
message when the table has rows AND the old (priority-less) live index is
present — a populated old-shape table must be rebuilt by hand, never silently
re-indexed. Downgrade stays a NO-OP.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_TABLE = "ai_slot_binding"
_LIVE_INDEX = f"uq_{_TABLE}_live"


# One source of truth for the CHECKs: the fresh CREATE and the converge
# re-ADDs must state the identical predicate.
_SLOT_CHECK = (
    "slot IN ('wizard', 'adapter', 'assistant', 'support', 'repair', 'critic',\n"
    "                 'wizard_learning_embed', 'assistant_user_rag_embed',\n"
    "                 'assistant_knowledge_embed')"
)
_CAPABILITY_CHECK = "capability IN ('chat', 'embedding')"
_CAPABILITY_MATCHES_SLOT_CHECK = (
    "(slot IN ('wizard_learning_embed', 'assistant_user_rag_embed',\n"
    "                  'assistant_knowledge_embed') AND capability = 'embedding')\n"
    "        OR\n"
    "        (slot NOT IN ('wizard_learning_embed', 'assistant_user_rag_embed',\n"
    "                      'assistant_knowledge_embed') AND capability = 'chat')"
)
_PRIORITY_RANGE_CHECK = "priority BETWEEN 0 AND 15"
# models.py declares slot String(28); TEXT holds it, varchar(N<28) does not.
_SLOT_MIN_WIDTH = 28


def _guarded_constraint(name: str, check: str) -> str:
    return f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = '{name}'
    ) THEN
        ALTER TABLE {_TABLE}
            ADD CONSTRAINT {name}
            CHECK ({check});
    END IF;
END
$$;
"""


_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    slot TEXT NOT NULL,
    scope TEXT NOT NULL,
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
    target_kind TEXT NOT NULL,
    stack_id INTEGER REFERENCES persistent_stacks(id) ON DELETE SET NULL,
    stack_name TEXT,
    base_url TEXT,
    model TEXT,
    key_ref TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at TIMESTAMPTZ,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    capability VARCHAR(16) NOT NULL DEFAULT 'chat',
    priority SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT ck_{_TABLE}_slot CHECK (
        {_SLOT_CHECK}
    ),
    CONSTRAINT ck_{_TABLE}_capability CHECK (
        {_CAPABILITY_CHECK}
    ),
    CONSTRAINT ck_{_TABLE}_capability_matches_slot CHECK (
        {_CAPABILITY_MATCHES_SLOT_CHECK}
    ),
    CONSTRAINT ck_{_TABLE}_priority_range CHECK (
        {_PRIORITY_RANGE_CHECK}
    ),
    CONSTRAINT ck_{_TABLE}_scope CHECK (scope IN ('platform', 'tenant')),
    CONSTRAINT ck_{_TABLE}_target_kind CHECK (
        target_kind IN ('stack', 'external', 'auto')
    ),
    CONSTRAINT ck_{_TABLE}_scope_tenant CHECK (
        (scope = 'platform' AND tenant_id IS NULL)
        OR (scope = 'tenant' AND tenant_id IS NOT NULL)
    ),
    CONSTRAINT ck_{_TABLE}_stack_target_names_stack CHECK (
        target_kind != 'stack' OR stack_id IS NOT NULL
    ),
    CONSTRAINT ck_{_TABLE}_external_target_names_base_url CHECK (
        target_kind != 'external' OR base_url IS NOT NULL
    ),
    CONSTRAINT ck_{_TABLE}_retirement_clock CHECK (
        enabled OR retired_at IS NOT NULL
    ),
    CONSTRAINT ck_{_TABLE}_retired_is_disabled CHECK (
        retired_at IS NULL OR NOT enabled
    )
);
"""

_LIVE_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_LIVE_INDEX}
    ON {_TABLE} (slot, scope, COALESCE(tenant_id, 0), priority)
    WHERE (enabled IS TRUE);
"""

_PLAIN_INDEXES = f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_slot ON {_TABLE} (slot);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_scope ON {_TABLE} (scope);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_target_kind ON {_TABLE} (target_kind);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_id ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_stack_id ON {_TABLE} (stack_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_enabled ON {_TABLE} (enabled);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_created_at ON {_TABLE} (created_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_retired_at ON {_TABLE} (retired_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_created_by_user_id
    ON {_TABLE} (created_by_user_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_slot_scope ON {_TABLE} (slot, scope);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_slot_scope_tenant_priority
    ON {_TABLE} (slot, scope, tenant_id, priority);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_capability ON {_TABLE} (capability);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_priority ON {_TABLE} (priority);
"""

# Phase 1: the table itself. On a fresh DB the CREATE carries every column,
# CHECK and FK; on a live old-shape table it is a no-op and the CONVERGE
# phase below re-states the missing columns/CHECKs/indexes guarded (the fresh
# CREATE's own index steps are NOT run separately — they reference `priority`
# and would fail on the old shape, which is the 1.1b bug this converges).
_CREATE_STEPS = [
    (f"{_TABLE} table (ai-config-stack-binding 1.1)", _CREATE_TABLE),
]

# --- CONVERGE: each step is its own statement so a re-run is a no-op ---------
_CONVERGE = [
    (
        f"{_TABLE} capability column",
        f"""
ALTER TABLE {_TABLE}
    ADD COLUMN IF NOT EXISTS capability VARCHAR(16) NOT NULL DEFAULT 'chat';
""",
    ),
    (
        f"{_TABLE} priority column",
        f"""
ALTER TABLE {_TABLE}
    ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 0;
""",
    ),
    (
        f"{_TABLE} slot CHECK widened",
        f"""
ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS ck_{_TABLE}_slot;
""",
    ),
    (
        f"{_TABLE} slot CHECK",
        _guarded_constraint(f"ck_{_TABLE}_slot", _SLOT_CHECK),
    ),
    (
        f"{_TABLE} capability CHECK",
        _guarded_constraint(f"ck_{_TABLE}_capability", _CAPABILITY_CHECK),
    ),
    (
        f"{_TABLE} capability-matches-slot CHECK",
        _guarded_constraint(
            f"ck_{_TABLE}_capability_matches_slot",
            _CAPABILITY_MATCHES_SLOT_CHECK,
        ),
    ),
    (
        f"{_TABLE} priority range CHECK",
        _guarded_constraint(f"ck_{_TABLE}_priority_range",
                            _PRIORITY_RANGE_CHECK),
    ),
    (
        f"{_TABLE} partial-unique live index (priority-keyed)",
        f"""
DROP INDEX IF EXISTS {_LIVE_INDEX};
""",
    ),
    (
        f"{_TABLE} partial-unique live index create",
        _LIVE_INDEX_SQL,
    ),
    (
        f"{_TABLE} indexes",
        _PLAIN_INDEXES,
    ),
]


def _sql_statements(sql: str) -> list[str]:
    """Split a step into executable statements, keeping DO $$ bodies whole."""
    out, buf, depth = [], "", 0
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf += line + "\n"
        depth += stripped.count("$$")
        # A statement ends at a trailing ';' ONLY outside a dollar-quoted
        # block: the slot-widen DO body holds `EXECUTE 'ALTER ...';`, whose
        # semicolon must not split the DO in two.
        if depth == 0 and stripped.endswith(";"):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def _converge_guard(fetch) -> bool:
    """True = converge may run. `fetch(sql)` runs a read-only SELECT and
    returns its first row (works for a psycopg2 cursor AND a SQLAlchemy
    Session, so the test can drive the same guard through db_session).

    Aborts (False) when a NON-EMPTY table still carries the old shape:
    dropping/recreating the live unique index and the slot CHECK is only
    safe because that table holds 0 rows. A populated old-shape table must
    be rebuilt by hand, never silently re-indexed."""
    rows = int(fetch(f"SELECT count(*) FROM {_TABLE}")[0])
    row = fetch(
        f"SELECT indexdef FROM pg_indexes "
        f"WHERE indexname = '{_LIVE_INDEX}'"
    )
    # The OLD shape is exactly "the live index exists and is keyed WITHOUT
    # priority" — that is the index the converge step drops and re-creates.
    old_index_shape = row is not None and "priority" not in row[0]

    if not old_index_shape:
        # No live index yet, or it already carries priority: nothing is
        # re-keyed, so the guarded CREATE steps below are pure additions.
        return True
    if rows == 0:
        return True
    print(
        f"ABORT {_TABLE} converge: {rows} rows and the OLD live index shape "
        f"({row[0]}). DROP INDEX + re-CREATE of uq_{_TABLE}_live (and of the "
        "slot CHECK) is only safe while the table is empty; with rows it "
        "silently changes which bindings count as duplicates. Retire/rewrite "
        "the rows, then re-run."
    )
    return False


def _slot_widen_step() -> str:
    """Widen `slot` ONLY when the existing column cannot hold the model's
    String(28) (live is TEXT -> no-op; varchar(10) -> varchar(28))."""
    return f"""
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = '{_TABLE}'
          AND column_name = 'slot'
          AND data_type = 'character varying'
          AND coalesce(character_maximum_length, 0) > 0
          AND character_maximum_length < {_SLOT_MIN_WIDTH}
    ) THEN
        EXECUTE 'ALTER TABLE {_TABLE} ALTER COLUMN slot TYPE VARCHAR({_SLOT_MIN_WIDTH})';
    END IF;
END
$$;
"""


_CONVERGE.insert(
    2,
    (
        f"{_TABLE} slot type (only if narrower than String({_SLOT_MIN_WIDTH}))",
        _slot_widen_step(),
    ),
)


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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the CREATE + the converge steps. Without this "
                        "flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _CREATE_STEPS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        for label, sql in _CONVERGE:
            print(f"DRY  would: {label} (converge)")
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

    # psycopg2's cursor.execute() returns None (the row lives on the cursor),
    # unlike SQLAlchemy's execute() which returns a Result carrying
    # .fetchone(). So the guard gets a real function, not a one-liner lambda.
    def _fetch(sql):
        cur.execute(sql)
        return cur.fetchone()

    if not _converge_guard(_fetch):
        cur.close()
        conn.close()
        return 1

    for label, sql in _CONVERGE:
        for stmt in _sql_statements(sql):
            try:
                cur.execute(stmt)
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {label}: {e}")
                return 1
        print(f"OK   {label} (converge)")

    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "ai_slot_binding ready: one ENABLED row per (slot, scope, tenant, "
        "priority) via the partial-unique live index; retired bindings are "
        "tombstones, never deleted."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

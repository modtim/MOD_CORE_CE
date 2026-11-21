#!/usr/bin/env python3
"""sz-2.1 (workflow-productivity-floor decomp §2.1) — the ONE new table:
WORKFLOW BREAKPOINTS (per-node breakpoints for the live-debugging canvas).

revision      : 20261011h_workflow_breakpoints
down_revision : 20261011g_vector_search_embeddings

20261011g_vector_search_embeddings is the CURRENT head: no other migration
names it as its down_revision (verified by grep over migrations/), so this
row chains onto it.

Model + migration only — NO orchestrator pause logic, NO
/workflows/runs/{run_id}/debug WebSocket, NO breakpoint_hit event emitter
(those are later decomp rows). This table is where whatever the live-
debugging layer later reads/writes lands.

workflow_breakpoints — at most ONE LIVE row per (workflow_version_id,
node_id, user_id): a breakpoint belongs to ONE user's debugging session on
ONE canvas version, so two users can hold breakpoints on the same node of
the same version at the same time and a new version of the workflow starts
clean.

Columns, per the sz-2.1 decomp row:
    id                  BIGSERIAL PRIMARY KEY
    tenant_id           INTEGER NOT NULL REFERENCES tenants (id)
                        — NO ON DELETE action (matching the audit/vault
                          tables in models.py: CostRoutingDecision,
                          WorkflowOutputAuditLog, AuditEvent,
                          PermissionAuditLog, PlatformSettingAudit,
                          OAuthConnection, WorkflowCrdtState — none
                          cascade): a tenant row cannot be hard-deleted
                          out from under the record of what its users
                          once paused on.
    workflow_id         INTEGER NOT NULL REFERENCES workflows (id)
                        — NO ON DELETE action either: the breakpoint row
                          is the record of a debugging session; a workflow
                          must not be dropped out from under it.
    workflow_version_id INTEGER NOT NULL REFERENCES workflow_versions (id)
                        — NO ON DELETE action: breakpoints are keyed by
                          VERSION, so deleting a version must not silently
                          erase the history of what was paused on it.
    node_id             VARCHAR(255) NOT NULL
                        CHECK (LENGTH(node_id) > 0)
                        — the canvas node's id. An empty string is not a
                          node: it would make "the breakpoint on nothing"
                          a legal row and would let a buggy writer pause
                          every node of a version.
    user_id             INTEGER NOT NULL REFERENCES users (id)
                        — WHO set it: the spec pauses a run only for the
                          breakpoint "set by the dispatching user", so the
                          owner is part of the key, not an afterthought.
                        — NO ON DELETE action: a user being deleted must
                          not erase the breakpoints they set.
    enabled             BOOLEAN NOT NULL DEFAULT TRUE
                        — the set/cleared toggle. Clearing can either
                          tombstone (ISO soft-delete) or flip this to
                          false; the orchestrator only ever pauses on
                          live + enabled rows, which is exactly what the
                          lookup index indexes.
    condition_json      JSONB NULL
                        — RESERVED for the later conditional-breakpoint
                          row: NULL = unconditional pause. No CHECK here:
                          the shape of a future predicate language is not
                          this table's contract.
    hit_count           INTEGER NOT NULL DEFAULT 0
                        CHECK (hit_count >= 0)
                        — how many times the orchestrator actually paused
                          here; the CHECK keeps a decrement bug from
                          silently corrupting debug stats.
    last_hit_at         TIMESTAMPTZ NULL
                        — when the pause last fired; NULL = never hit.
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    deleted_at          TIMESTAMPTZ NULL
    deleted_by          INTEGER NULL
                        — ISO 27001 soft delete: clearing a breakpoint
                          stamps these and keeps the row; nothing is
                          hard-deleted.

    The PARTIAL UNIQUE uq_workflow_breakpoints_live on
    (workflow_version_id, node_id, user_id) WHERE deleted_at IS NULL is
    what makes "one live breakpoint per (version, node, user)" a database
    fact, not a service convention: a second live row for the same key is
    a hard IntegrityError, while a soft-deleted row never blocks a fresh
    live row.

Index ix_workflow_breakpoints_tenant_version_enabled on
(tenant_id, workflow_version_id) WHERE deleted_at IS NULL AND enabled — the
orchestrator's lookup path ("which breakpoints pause THIS run of THIS
version, for THIS tenant"), so the live+enabled filter is baked into the
index, not left to the query.

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.
No ON DELETE clause anywhere — no CASCADE, no ONDELETE (a glued ONDELETE is
a Postgres syntax error anyway).

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. A matching models.py
ORM class (`WorkflowBreakpoint`) is added alongside this migration.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261011h_workflow_breakpoints.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflow_breakpoints"
_REVISION = "20261011h_workflow_breakpoints"
_DOWN_REVISION = "20261011g_vector_search_embeddings"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-2.1, one live breakpoint row per "
        "(version, node, user)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    workflow_id INTEGER NOT NULL REFERENCES workflows (id),
    workflow_version_id INTEGER NOT NULL REFERENCES workflow_versions (id),
    node_id VARCHAR(255) NOT NULL
        CHECK (LENGTH(node_id) > 0),
    user_id INTEGER NOT NULL REFERENCES users (id),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    condition_json JSONB,
    hit_count INTEGER NOT NULL DEFAULT 0
        CHECK (hit_count >= 0),
    last_hit_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    deleted_by INTEGER
);
""",
    ),
    (
        f"{_TABLE} partial UNIQUE (workflow_version_id, node_id, user_id) "
        "WHERE live",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live
    ON {_TABLE} (workflow_version_id, node_id, user_id)
    WHERE deleted_at IS NULL;
""",
    ),
    (
        f"{_TABLE} index (tenant_id, workflow_version_id) "
        "WHERE live AND enabled",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_version_enabled
    ON {_TABLE} (tenant_id, workflow_version_id)
    WHERE deleted_at IS NULL AND enabled;
""",
    ),
]


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
                   help="Apply the CREATE TABLE + index steps. "
                        "Without this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (breakpoint rows are records of "
              "debugging sessions kept per ISO 27001 — no destructive "
              "rollback defined)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "sz-2.1 ready: workflow_breakpoints exists; the live-debugging "
        "orchestrator (a later decomp row) reads it. The lead applies "
        "--commit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

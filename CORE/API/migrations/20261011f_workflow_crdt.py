#!/usr/bin/env python3
"""sz-3.1 (workflow-productivity-floor decomp §3.1) — the TWO new tables:
WORKFLOW CRDT STATE + CRDT SNAPSHOTS (server-side persistence for the
Yjs-backed WorkflowBuilder canvas).

revision      : 20261011f_workflow_crdt
down_revision : 20261011e_oauth_connections

20261011e_oauth_connections is the CURRENT head: no other migration names
it as its down_revision (verified by grep over migrations/), so this row
chains onto it.

Model + migration only — NO WebSocket server, NO y-postgres adapter, NO
snapshot writer (that is sz-3.2, a later decomp row). These two tables are
where whatever the CRDT layer later persists lands.

workflow_crdt_state — ONE LIVE row per (tenant_id, workflow_id): the
current Yjs document state for a canvas, updated in place on every
awareness update.

Columns, per the sz-3.1 decomp row:
    id            BIGSERIAL PRIMARY KEY
    tenant_id     INTEGER NOT NULL REFERENCES tenants (id)
                  — NO ON DELETE action (matching the audit/vault tables
                    in models.py: CostRoutingDecision, WorkflowOutputAuditLog,
                    AuditEvent, PermissionAuditLog, PlatformSettingAudit,
                    OAuthConnection — none cascade): a tenant row cannot be
                    hard-deleted out from under the record of what its
                    canvases once held.
    workflow_id   INTEGER NOT NULL REFERENCES workflows (id)
                  — NO ON DELETE action either: the CRDT state is the
                    authoritative record of a canvas's edit history; a
                    workflow must not be dropped out from under it.
    ydoc_state    BYTEA NOT NULL
                  — the Yjs ENCODED state update (y-postgres adapter
                    format). Binary on purpose: it is a lib0 update, not
                    text, and must never be re-encoded on the way in/out.
                  SIZE IS THE SERVICE'S JOB, NOT THE TABLE'S: sz-3.2 caps
                  ydoc_state (and rotates into a snapshot when the cap is
                  hit). No CHECK here — a byte-length CHECK would be
                  re-evaluated on every update and would make the table
                  reject a legal write the moment the cap setting changed.
    state_vector  BYTEA NULL
                  — the encoded state vector (update ids), kept so a
                    reconnect can tell which updates it already has.
    update_count  BIGINT NOT NULL DEFAULT 0
                  — awareness updates folded into this row since the last
                    snapshot; the periodic-snapshot trigger reads it.
    byte_size     INTEGER NOT NULL DEFAULT 0 CHECK (byte_size >= 0)
                  — len(ydoc_state) as last written, so the service can
                    decide to rotate without decoding the update.
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    deleted_at    TIMESTAMPTZ NULL
    deleted_by    INTEGER NULL
                  — ISO 27001 soft delete: a canvas whose state is
                    tombstoned keeps its row; nothing is hard-deleted.

    The PARTIAL UNIQUE uq_workflow_crdt_state_live on
    (tenant_id, workflow_id) WHERE deleted_at IS NULL is what makes
    "ONE live row per canvas" a database fact, not a service convention:
    a second live row for the same canvas is a hard IntegrityError, while
    a soft-deleted row never blocks a fresh live row.

workflow_crdt_snapshots — append-only history: every periodic (60s),
manual, pre-restore and shutdown snapshot of a canvas.

Columns, per the sz-3.1 decomp row:
    id           BIGSERIAL PRIMARY KEY
    tenant_id    INTEGER NOT NULL REFERENCES tenants (id)
                 — NO ON DELETE action, same reasoning as above.
    workflow_id  INTEGER NOT NULL REFERENCES workflows (id)
                 — NO ON DELETE action: disaster recovery is the whole
                    point of this table, and recovery must still be
                    possible after the owning workflow is soft-deleted.
    ydoc_state   BYTEA NOT NULL
                 — the full Yjs state at this point, enough on its own to
                    rebuild the document from scratch (the disaster-recovery
                    path in the spec: "recover from the latest snapshot").
    graph_json   JSONB NULL
                 — the MATERIALIZED canvas (the same shape the
                    workflows.graph_json holds), so disaster recovery can
                    restore a canvas even when no Yjs runtime is available
                    to decode ydoc_state.
    byte_size    INTEGER NOT NULL
                 — len(ydoc_state) as written; the retention/rotation job
                    reads it.
    sha256       CHAR(64) NOT NULL
                 — hex sha256 of the canonical ydoc_state bytes: the
                    dedup guard (an unchanged canvas does not get a new
                    snapshot) and the integrity check on restore.
                 — CHECK (LENGTH(sha256) = 64): a truncated or padded
                   digest is a corrupt row, not a cosmetic one.
    reason       VARCHAR(32) NOT NULL
                 CHECK (reason IN ('periodic', 'manual', 'pre_restore',
                                   'shutdown'))
                 — WHY the snapshot exists, so the restore picker can
                   prefer a manual/pre_restore snapshot over a periodic
                   one and so a shutdown snapshot is never mistaken for a
                   user action.
    created_by   INTEGER NULL REFERENCES users (id)
                 — NULL = written by the platform (the periodic / shutdown
                   writer), not by a person.
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    deleted_at   TIMESTAMPTZ NULL
    deleted_by   INTEGER NULL
                 — ISO 27001 soft delete: pruning history stamps these and
                   keeps the row; history is never hard-deleted.

Index ix_workflow_crdt_snapshots_tenant_workflow_created on
(tenant_id, workflow_id, created_at DESC) — the restore read is
"latest snapshot for this canvas", so the ordering is baked into the
index, not left to the query.

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.
No ON DELETE clause anywhere — no CASCADE, no ONDELETE (a glued ONDELETE
is a Postgres syntax error anyway).

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. A matching models.py
ORM class (`WorkflowCrdtState`, `WorkflowCrdtSnapshots`) is added
alongside this migration.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261011f_workflow_crdt.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_STATE_TABLE = "workflow_crdt_state"
_SNAP_TABLE = "workflow_crdt_snapshots"
_REVISION = "20261011f_workflow_crdt"
_DOWN_REVISION = "20261011e_oauth_connections"
_ALLOWED_REASONS = ("periodic", "manual", "pre_restore", "shutdown")

_STATEMENTS = [
    (
        f"{_STATE_TABLE} table (sz-3.1, one live Yjs state row per canvas)",
        f"""
CREATE TABLE IF NOT EXISTS {_STATE_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    workflow_id INTEGER NOT NULL REFERENCES workflows (id),
    ydoc_state BYTEA NOT NULL,
    state_vector BYTEA,
    update_count BIGINT NOT NULL DEFAULT 0,
    byte_size INTEGER NOT NULL DEFAULT 0
        CHECK (byte_size >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    deleted_by INTEGER
);
""",
    ),
    (
        f"{_STATE_TABLE} partial UNIQUE (tenant_id, workflow_id) "
        "WHERE live",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_STATE_TABLE}_live
    ON {_STATE_TABLE} (tenant_id, workflow_id)
    WHERE deleted_at IS NULL;
""",
    ),
    (
        f"{_SNAP_TABLE} table (sz-3.1, append-only snapshot history)",
        f"""
CREATE TABLE IF NOT EXISTS {_SNAP_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    workflow_id INTEGER NOT NULL REFERENCES workflows (id),
    ydoc_state BYTEA NOT NULL,
    graph_json JSONB,
    byte_size INTEGER NOT NULL,
    sha256 CHAR(64) NOT NULL
        CHECK (LENGTH(sha256) = 64),
    reason VARCHAR(32) NOT NULL
        CHECK (reason IN ('periodic', 'manual', 'pre_restore',
                          'shutdown')),
    created_by INTEGER REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    deleted_by INTEGER
);
""",
    ),
    (
        f"{_SNAP_TABLE} index (tenant_id, workflow_id, created_at DESC)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_SNAP_TABLE}_tenant_workflow_created
    ON {_SNAP_TABLE} (tenant_id, workflow_id, created_at DESC);
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
        print("DRY  downgrade: no-op (CRDT state + snapshot history are "
              "edit records kept per ISO 27001 — no destructive rollback "
              "defined)")
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
        "sz-3.1 ready: workflow_crdt_state + workflow_crdt_snapshots exist; "
        "the CRDT persistence layer (sz-3.2, a later decomp row) writes "
        "them. The lead applies --commit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

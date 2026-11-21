# API/migrations/20261007zzz_fx115_p2_node_lockdown_verdicts.py
"""FX.115 p2 (owner decision Q102=A) — node lockdown verdicts: the NEW table.

models.NodeLockdownVerdict (services/node_lockdown_report.py writes it from the
worker's task-result `lockdown_report` field, routers/node_lockdown_report.py
reads it) is the aggregation key behind the platform-admin breakage report:
ONE live row per (node_kind, flag) holding the LATEST verdict any worker
reported, so the owner can flip a worker.node_lockdown_* knob from evidence —
"what WOULD change under enforce", "already hardened" (missing=false, the
current fleet ships most posture so most flags are no-ops), and "deliberately
exempt + why" — instead of scraping the worker's log line.

    node_lockdown_verdicts
      id                  BIGSERIAL PRIMARY KEY
      node_kind           VARCHAR(250) NOT NULL   transport.TaskPayload.node_kind
      flag                VARCHAR(32)  NOT NULL   nonroot|capdrop|noprivs|seccomp
                                                  |rootfs|scratch_tmpfs|limits_pids
                                                  |limits_memory|limits_cpu
      mode                VARCHAR(16)  NOT NULL   off|report|enforce in effect
      missing             BOOLEAN      NOT NULL   posture lacks the flag
      would_apply         BOOLEAN      NOT NULL   enforcing WOULD change it
      applied             BOOLEAN      NOT NULL   the layer actually wrote it
      exempt              BOOLEAN      NOT NULL   allow-listed (FX.115 p3 row)
      exempt_reason       VARCHAR(64)  NULL       why it was skipped
      cap_value           BIGINT       NULL       requested floor (limits_*)
      detail              JSONB        NULL       profile name / scratch paths
      worker_name         VARCHAR(255) NULL       which box reported it
      task_id             INTEGER      NULL       the api_task_queue row it came
                                                  from (the ledger stays there)
      last_seen_at        TIMESTAMPTZ  NULL       when this verdict was taken
      deleted_at          TIMESTAMPTZ  NULL       soft-delete tombstone

Latest-wins, NOT a ledger: every verdict ever reported also stays on its task
row (api_task_queue.result_payload.lockdown_report), so this table only carries
what the report endpoint needs to group. ISO 27001: NO hard delete — a flag a
node stops reporting keeps its last row with `last_seen_at` aged, and the
uniqueness index is PARTIAL on `deleted_at IS NULL` (the node_egress_approvals
idiom) so a tombstone never squats the (node_kind, flag) key.

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE ... IF NOT EXISTS on every
index. Nothing existing is altered, dropped, or truncated — additive only.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzz_fx115_p2_node_lockdown_verdicts.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzz_fx115_p2_node_lockdown_verdicts.py --commit   # apply

NOT applied by the p2 task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "node_lockdown_verdicts"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.115 p2)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    node_kind VARCHAR(250) NOT NULL,
    flag VARCHAR(32) NOT NULL,
    mode VARCHAR(16) NOT NULL,
    missing BOOLEAN NOT NULL,
    would_apply BOOLEAN NOT NULL,
    applied BOOLEAN NOT NULL,
    exempt BOOLEAN NOT NULL,
    exempt_reason VARCHAR(64) NULL,
    cap_value BIGINT NULL,
    detail JSONB NULL,
    worker_name VARCHAR(255) NULL,
    task_id INTEGER NULL,
    last_seen_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);
""",
    ),
    (
        f"{_TABLE} node_kind index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_node_kind
    ON {_TABLE} (node_kind);
""",
    ),
    (
        f"{_TABLE} flag index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_flag
    ON {_TABLE} (flag);
""",
    ),
    (
        f"{_TABLE} tombstone index",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at
    ON {_TABLE} (deleted_at);
""",
    ),
    (
        f"{_TABLE} one live verdict per (node_kind, flag)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live_kind_flag
    ON {_TABLE} (node_kind, flag)
    WHERE deleted_at IS NULL;
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
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        cur.execute(sql)
        print(f"DID  {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

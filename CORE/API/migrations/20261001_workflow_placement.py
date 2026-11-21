"""launch-completion-megaplan GX.D1 — workflow-level worker/pool placement.

An app built on MOD runs its workflows against an LLM that only SOME workers
can reach (a local gateway on one host, a stack pinned to one box). There was
no way to say "every task of this workflow must land on a worker that can
reach it": `target_worker`/`pinned_worker` exist only on persistent stacks,
and `run.parameters["pinned_worker"]` is an undocumented, system-workflow-only
spelling honoured by a single downstream-dispatch branch.

This adds the workflow-level DEFAULT for that placement:

    workflows.placement = {"worker_id": <worker_licenses.id>} and/or
                           {"worker_tag": <hardware_tag>}

Per-NODE placement already rides the graph (node data `workerId` /
`workerTag`) and a per-RUN override rides `WorkflowRunRequest.placement`;
precedence is run > node > workflow, resolved at dispatch and enforced at
CLAIM time (a pinned task is claimable only by a matching worker — see
routers/worker_tasks.py `_placement_claim_guard_sql` + the claim queries).

WHY A COLUMN AND NOT graph_json: placement constrains execution, not the
graph's shape; it must be settable without bumping a new workflow version
(the graph never changed), and the placement endpoint must read/write it
without touching 200-node graphs. Same rationale as persistent_stacks
having its own pinned_worker column instead of burying it in storage_refs.

NULLABLE MEANS UNPINNED, which is the existing behaviour, so a deployment
that never sets one is unaffected. No FK on purpose: a pin must survive the
worker row going away (that is the "pinned to a host that is no longer
online" state the sweep reports) rather than silently dropping the
constraint.

Additive + idempotent. Written by GX.D1 — NOT applied by that task; the
owner applies it on the next API deploy window:
    docker exec my_fastapi_app python3 \\
        migrations/20261001_workflow_placement.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

SQL = """
ALTER TABLE workflows
    ADD COLUMN IF NOT EXISTS placement JSONB;
"""

STATEMENTS = [SQL]


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
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        for s in STATEMENTS:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    for s in STATEMENTS:
        cur.execute(s)
    cur.close()
    print("workflow placement ready: workflows.placement (NULL = unpinned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

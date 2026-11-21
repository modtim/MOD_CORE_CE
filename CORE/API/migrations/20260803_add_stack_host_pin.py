"""managed-ai-surfaces-and-stack-ops §8.1 — a persistent host pin for a stack.

There was no way to say "this stack lives on this machine and stays there".
`to_worker` is an argument to a single /migrate call — it moves a stack once and
then stops meaning anything, so the very next rebalancer pass is free to move it
back. An operator who wants a stack fixed to a host had no expressible way to
say so, and the only workaround was to leave the rebalancer off for everyone.

That is a real requirement, not a preference. A stack can be tied to a host by
things the placement model cannot see: a licence bound to a machine, a local
dataset that is expensive to re-stage, a NIC on the right VLAN, a card the
operator is deliberately reserving.

WHY A COLUMN AND NOT storage_refs. `_pin_warm` (idle reclaim) lives in
storage_refs precisely because it is transient operational state. A host pin is
different in kind: it constrains placement, it must survive stop/start, and it
has to be queryable — the rebalancer needs to filter on it and the UI needs to
show it. Burying a placement constraint inside a JSON blob named "storage_refs"
would also be a lie about what that field means.

NULLABLE MEANS UNPINNED, which is the existing behaviour, so a deployment that
never sets one is unaffected.

Additive + idempotent. Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260803_add_stack_host_pin.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


# No FK to worker_licenses on purpose. A pin must survive the worker row being
# absent — that is exactly the state §8.4 has to report ("pinned to a host that
# can no longer satisfy it") rather than silently drop the constraint and
# relocate. ON DELETE SET NULL would turn an operator's explicit instruction
# into a silent relocation, which is the failure this exists to prevent.
SQL = """
ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS pinned_worker VARCHAR(255);

ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS pinned_reason TEXT;

ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS pinned_at TIMESTAMPTZ;

ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS pinned_by_user_id INTEGER;

CREATE INDEX IF NOT EXISTS ix_persistent_stacks_pinned_worker
    ON persistent_stacks(pinned_worker) WHERE pinned_worker IS NOT NULL;
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
    print("host pin ready: persistent_stacks.pinned_worker (NULL = unpinned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

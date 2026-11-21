"""wizard-learning-from-feedback §1.1 — wizard_feedback_event table.

Append-only event log of every wizard apply call. Captures the
user's action on a proposal so the Phase-2 reranker (and downstream
classifiers) can train on real accept/reject signal.

Schema:
- id (BIGSERIAL) — surrogate key
- tenant_id (FK CASCADE on tenants.id) — multi-tenant scope
- proposal_id (INTEGER, soft ref to support_chat_messages.id, no
  FK because cross-partition FK on a partitioned table is finicky
  in pg13) — links back to the assistant message whose token_usage
  carries the proposal payload
- apply_mode (TEXT, CHECK in {replace, merge}) — which apply path
- action (TEXT, CHECK accepted | dismissed_without_action |
  rejected_<error_code>) — outcome of the apply call
- was_edited (BOOLEAN) — whether the user edited the proposal
  before applying (FE-driven flag; needed to distinguish the
  "accepted as-is" reranker positive from the weaker "accepted
  with edits")
- time_to_action_ms (INTEGER, nullable) — wall time from proposal
  shown to apply click; null when not knowable (e.g. background
  retry)
- applied_node_ids_diff (JSONB) — {added: [...], removed: [...],
  unchanged: [...]} — what the server actually wrote vs what the
  proposal contained
- intent (TEXT) — resolved wizard intent for the originating turn
- created_at (TIMESTAMPTZ) — partition key
- feedback_event_metadata (JSONB) — open extension slot

Partitioning: declarative range partitioning by `created_at` month.
Postgres requires the partition key to be in every UNIQUE
constraint, so the primary key is `(id, created_at)` not just `id`.

Initial partitions: current month + next two months. The §1.5
APScheduler job (separate task, separate migration) keeps creating
future partitions on the 25th of each month.

Idempotent — a second run is a no-op.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260508_add_wizard_feedback_event_table.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date


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


def _partition_bounds_for_month(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1).isoformat()
    end = date(year + (month // 12), (month % 12) + 1, 1).isoformat()
    return start, end


def _months_from(year: int, month: int, n: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for i in range(n):
        m = month + i
        y = year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        out.append((y, m))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    steps: list[tuple[str, str]] = []

    if not _table_exists(cur, "wizard_feedback_event"):
        steps.append((
            "create wizard_feedback_event parent (partitioned by created_at)",
            """
            CREATE TABLE wizard_feedback_event (
                id BIGSERIAL,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                proposal_id INTEGER NOT NULL,
                apply_mode TEXT NOT NULL CHECK (apply_mode IN ('replace', 'merge')),
                action TEXT NOT NULL CHECK (
                    action = 'accepted'
                    OR action = 'dismissed_without_action'
                    OR action LIKE 'rejected_%'
                ),
                was_edited BOOLEAN NOT NULL DEFAULT FALSE,
                time_to_action_ms INTEGER,
                applied_node_ids_diff JSONB,
                intent TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                feedback_event_metadata JSONB,
                PRIMARY KEY (id, created_at)
            ) PARTITION BY RANGE (created_at)
            """,
        ))
        steps.append((
            "index tenant_id (propagates to partitions)",
            "CREATE INDEX IF NOT EXISTS ix_wfe_tenant ON wizard_feedback_event(tenant_id)",
        ))
        steps.append((
            "index action (action-class queries)",
            "CREATE INDEX IF NOT EXISTS ix_wfe_action ON wizard_feedback_event(action)",
        ))
        steps.append((
            "index proposal_id (join to support_chat_messages)",
            "CREATE INDEX IF NOT EXISTS ix_wfe_proposal ON wizard_feedback_event(proposal_id)",
        ))
        steps.append((
            "index created_at (training-window queries)",
            "CREATE INDEX IF NOT EXISTS ix_wfe_created_at ON wizard_feedback_event(created_at)",
        ))

    today = date.today()
    for year, month in _months_from(today.year, today.month, 3):
        partition_name = f"wizard_feedback_event_y{year}m{month:02d}"
        if _table_exists(cur, partition_name):
            continue
        start, end = _partition_bounds_for_month(year, month)
        steps.append((
            f"create partition {partition_name} [{start}, {end})",
            f"""
            CREATE TABLE {partition_name} PARTITION OF wizard_feedback_event
                FOR VALUES FROM ('{start}') TO ('{end}')
            """,
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

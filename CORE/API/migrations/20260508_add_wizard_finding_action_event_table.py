"""wizard-learning-from-feedback §1.2 — wizard_finding_action_event table.

Append-only event log of every user action on a wizard audit finding
(Fix-via-wizard / Open-node / Ignore / dismissed_without_action).
Trains the Phase-5 audit-finding usefulness classifier.

Schema:
- id (BIGSERIAL) — surrogate key
- tenant_id (FK CASCADE on tenants.id) — multi-tenant scope
- audit_session_id (TEXT) — session id of the audit response that
  produced the finding (mirrors SupportChatSession.id semantics
  without a hard FK so a deleted session doesn't lose the
  training signal — sessions are durable in practice but the
  FK invariant is too strict here)
- finding_id (TEXT) — stable id from the audit response (hash of
  severity + title + sorted affected_node_ids per the canvas-aware-
  wizard §5.3 contract)
- severity (TEXT, CHECK in {error, warning, info}) — finding severity
- action (TEXT, CHECK in {fix_via_wizard, open_node, ignore,
  dismissed_without_action}) — what the user did
- finding_title_hash (TEXT) — short hash of the finding title for
  cross-tenant aggregation without raw-text leakage
- created_at (TIMESTAMPTZ) — partition key
- finding_event_metadata (JSONB) — open extension slot

Partitioning: declarative range partitioning by created_at month.
Same PK shape as wizard_feedback_event: (id, created_at).

Idempotent — a second run is a no-op.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260508_add_wizard_finding_action_event_table.py --commit
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

    if not _table_exists(cur, "wizard_finding_action_event"):
        steps.append((
            "create wizard_finding_action_event parent (partitioned by created_at)",
            """
            CREATE TABLE wizard_finding_action_event (
                id BIGSERIAL,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                audit_session_id TEXT NOT NULL,
                finding_id TEXT NOT NULL,
                severity TEXT NOT NULL CHECK (severity IN ('error', 'warning', 'info')),
                action TEXT NOT NULL CHECK (
                    action IN ('fix_via_wizard', 'open_node', 'ignore', 'dismissed_without_action')
                ),
                finding_title_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finding_event_metadata JSONB,
                PRIMARY KEY (id, created_at)
            ) PARTITION BY RANGE (created_at)
            """,
        ))
        steps.append((
            "index tenant_id (multi-tenant scoped reads)",
            "CREATE INDEX IF NOT EXISTS ix_wfae_tenant ON wizard_finding_action_event(tenant_id)",
        ))
        steps.append((
            "index audit_session_id (per-session lookup)",
            "CREATE INDEX IF NOT EXISTS ix_wfae_session ON wizard_finding_action_event(audit_session_id)",
        ))
        steps.append((
            "index finding_id (per-finding aggregation)",
            "CREATE INDEX IF NOT EXISTS ix_wfae_finding ON wizard_finding_action_event(finding_id)",
        ))
        steps.append((
            "index action (action-class queries for triage classifier training)",
            "CREATE INDEX IF NOT EXISTS ix_wfae_action ON wizard_finding_action_event(action)",
        ))
        steps.append((
            "index created_at (training-window queries)",
            "CREATE INDEX IF NOT EXISTS ix_wfae_created_at ON wizard_finding_action_event(created_at)",
        ))

    today = date.today()
    for year, month in _months_from(today.year, today.month, 3):
        partition_name = f"wizard_finding_action_event_y{year}m{month:02d}"
        if _table_exists(cur, partition_name):
            continue
        start, end = _partition_bounds_for_month(year, month)
        steps.append((
            f"create partition {partition_name} [{start}, {end})",
            f"""
            CREATE TABLE {partition_name} PARTITION OF wizard_finding_action_event
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

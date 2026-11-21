"""add-workflow-triggers §6.3 — DB-row event triggers via Postgres LISTEN/NOTIFY.

A lightweight row trigger on an OPTED-IN table emits `NOTIFY mod_row_event` with a
JSON payload `{table, op, row}`; a background listener picks it up and fires matching
DB_ROW_EVENT triggers through the shared pipeline. Install is per-table + opt-in
(see INSTALL_SQL); the listener is gated by TRIGGER_DB_ROW_LISTENER (default off so it
stays inert until a table is wired and the operator turns it on).

NOTE (mesh): LISTEN/NOTIFY is per-connection and owner-local — not cross-region. For
the multi-region mesh this fires on the manager whose Postgres received the write,
which is the owner — acceptable; documented.
"""
from __future__ import annotations

import json
import os
import threading
import time

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')

CHANNEL = "mod_row_event"
_started = False

# Run ONCE per table an operator wants to watch (psql). $TABLE is the table name.
INSTALL_SQL = """
CREATE OR REPLACE FUNCTION mod_row_event_notify() RETURNS trigger AS $$
DECLARE rec jsonb;
BEGIN
  IF (TG_OP = 'DELETE') THEN rec := to_jsonb(OLD); ELSE rec := to_jsonb(NEW); END IF;
  PERFORM pg_notify('mod_row_event',
    json_build_object('table', TG_TABLE_NAME, 'op', lower(TG_OP), 'row', rec)::text);
  RETURN NULL;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS mod_row_event_trg ON $TABLE;
CREATE TRIGGER mod_row_event_trg AFTER INSERT OR UPDATE OR DELETE ON $TABLE
  FOR EACH ROW EXECUTE FUNCTION mod_row_event_notify();
"""


def fire_db_row_event_triggers(db, *, table: str, op: str, row: dict) -> int:
    """Fire DB_ROW_EVENT triggers matching (table, op) and the configured filter.
    Returns the number dispatched. Never raises."""
    from models import WorkflowTrigger, WorkflowTriggerType
    from services.triggers import pipeline
    fired = 0
    try:
        triggers = (db.query(WorkflowTrigger).filter(
            WorkflowTrigger.trigger_type == WorkflowTriggerType.DB_ROW_EVENT,
            WorkflowTrigger.is_active.is_(True),
            WorkflowTrigger.deleted_at.is_(None)).all())
        for trig in triggers:
            cfg = trig.configuration or {}
            if cfg.get("table") != table:
                continue
            if cfg.get("op", "insert") not in ("any", op):
                continue
            flt = cfg.get("filter") or {}
            if any((row or {}).get(k) != v for k, v in flt.items()):
                continue
            ctx = {
                "event_key": f"row:{table}:{op}:{trig.id}:{(row or {}).get('id')}",
                "row": row, "inputs": {"table": table, "op": op, "row": row},
            }
            try:
                if pipeline.evaluate_and_dispatch(db, trig, ctx).get("dispatched"):
                    fired += 1
            except Exception:  # noqa: BLE001
                logger.exception("db_row_event trigger %s fire failed", trig.id)
                try:
                    db.rollback()
                except Exception:
                    pass
    except Exception:  # noqa: BLE001
        logger.exception("fire_db_row_event_triggers failed (%s/%s)", table, op)
    return fired


def _listen_loop():
    """Dedicated connection LISTENing on `mod_row_event`; reconnects on error."""
    import select
    from database import SessionLocal, get_direct_engine
    while True:
        try:
            # DIRECT connection (bypasses PgBouncer): LISTEN is session-pinned and
            # would lose its subscription under PgBouncer transaction pooling.
            raw = get_direct_engine().raw_connection()
            conn = getattr(raw, "driver_connection", None) or getattr(raw, "connection", None) or raw
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(f"LISTEN {CHANNEL};")
            logger.info("db-row trigger listener attached to channel %s", CHANNEL)
            while True:
                if select.select([conn], [], [], 30) == ([], [], []):
                    continue
                conn.poll()
                while conn.notifies:
                    note = conn.notifies.pop(0)
                    try:
                        payload = json.loads(note.payload)
                        with SessionLocal() as db:
                            fire_db_row_event_triggers(
                                db, table=payload.get("table"),
                                op=payload.get("op", "insert"),
                                row=payload.get("row") or {})
                    except Exception:  # noqa: BLE001
                        logger.exception("db-row listener: failed handling notify")
        except Exception as e:  # noqa: BLE001 — reconnect after a backoff
            logger.warning("db-row listener connection lost (%s) — reconnecting in 5s", e)
            time.sleep(5)


def start_db_row_listener() -> None:
    """Start the LISTEN/NOTIFY listener (daemon). Opt-in via TRIGGER_DB_ROW_LISTENER;
    default off so it's inert until a table has the notify trigger installed."""
    global _started
    if _started or os.getenv("TRIGGER_DB_ROW_LISTENER", "false").lower() != "true":
        return
    _started = True
    threading.Thread(target=_listen_loop, daemon=True, name="DbRowTriggerListener").start()

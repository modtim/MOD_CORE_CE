"""add-workflow-triggers §2.3 — composite AND/OR triggers.

A composite fires from a boolean combination of constituent triggers. When any
constituent fires (hook in the pipeline), `notify_composites` advances every
composite that lists it: OR composites fire immediately; AND composites fire once
all constituents have fired within the window, then re-arm. Composites cannot be
constituents of other composites (no nesting) — the guard prevents cascades/loops.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')


def notify_composites(db, fired_trigger) -> None:
    from models import WorkflowTrigger, WorkflowTriggerType, TriggerEventLog
    if fired_trigger.trigger_type == WorkflowTriggerType.COMPOSITE:
        return  # no nesting — stops any cascade
    try:
        composites = (db.query(WorkflowTrigger).filter(
            WorkflowTrigger.trigger_type == WorkflowTriggerType.COMPOSITE,
            WorkflowTrigger.is_active.is_(True),
            WorkflowTrigger.deleted_at.is_(None),
            WorkflowTrigger.tenant_id == fired_trigger.tenant_id).all())
        if not composites:
            return
        from services.triggers import pipeline
        now = datetime.now(timezone.utc)
        for comp in composites:
            cfg = comp.configuration or {}
            constituents = [int(c) for c in (cfg.get("constituents") or [])]
            if fired_trigger.id not in constituents:
                continue
            if cfg.get("op", "and") == "or":
                pipeline.evaluate_and_dispatch(
                    db, comp, {"event_key": f"comp:{comp.id}:{fired_trigger.id}",
                               "inputs": {"composite_via": fired_trigger.id}})
                continue
            # AND — barrier over distinct constituent fires within the window.
            window_s = int(cfg.get("window_seconds", 3600))
            db.add(TriggerEventLog(trigger_id=comp.id, tenant_id=comp.tenant_id,
                                   event_key=f"c:{fired_trigger.id}"))
            db.commit()
            cutoff = now - timedelta(seconds=window_s)
            rows = (db.query(TriggerEventLog.event_key).filter(
                TriggerEventLog.trigger_id == comp.id,
                TriggerEventLog.fired_at >= cutoff,
                TriggerEventLog.event_key.like("c:%")).all())
            if {f"c:{c}" for c in constituents}.issubset({r[0] for r in rows}):
                res = pipeline.evaluate_and_dispatch(
                    db, comp, {"event_key": f"comp:{comp.id}", "inputs": {}})
                if res.get("dispatched"):
                    db.query(TriggerEventLog).filter(
                        TriggerEventLog.trigger_id == comp.id,
                        TriggerEventLog.event_key.like("c:%")).delete(
                        synchronize_session=False)
                    db.commit()
    except Exception:  # noqa: BLE001 — composite advancement must never break the constituent's fire
        logger.exception("notify_composites failed for trigger %s",
                         getattr(fired_trigger, "id", "?"))

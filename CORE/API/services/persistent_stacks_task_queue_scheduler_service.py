"""Business logic for `routers/persistent_stacks/task_queue_scheduler.py`
(Gate 2b).

Companion to `crud/persistent_stacks_task_queue_scheduler.py` (the only place
this slice's `db.query` lives). Extracted verbatim from the router during the
Gate-2b migration — a behaviour-preserving MOVE: every function below is the
same logic that used to live inline in the router handler of the same name,
with only the query calls now routed through the crud module.

Keeps this router package's existing convention of raising plain
`HTTPException` inline rather than `services._base.ServiceError` (see
`services/persistent_stacks_domain_service.py`'s docstring).

`_shared` is imported from, never edited — function-local imports, matching
this package's existing convention (avoids forcing
`routers.persistent_stacks` to initialize at this module's import time).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

import models
from crud import persistent_stacks_task_queue_scheduler as pts_crud


# =============================================================================
# apply_managed_ai — POST /managed-ai/apply
# =============================================================================

def apply_managed_ai(db: Session, user: "models.User", preset: Optional[str]):
    from utils.paid_import import require_paid

    # GT.D6 DS-4 — managed-AI apply is a Paid feature: on CE the whole call
    # raises HTTP 402 (upgrade path) instead of an ImportError 500. The core
    # lives in the paid service module (DS-1/DS-2 move); while the move is in
    # flight it is still in `_shared`, so fall back to that copy.
    _paid = require_paid("services.persistent_stacks_paid_service",
                         "Managed-AI stack apply")
    core = getattr(_paid, "apply_managed_ai_core", None)
    if core is None:
        from routers.persistent_stacks._shared import apply_managed_ai_core as core
    return core(db, user, preset, source="manual")


# =============================================================================
# accelerator_capacity — GET /accelerator-capacity
# =============================================================================

def accelerator_capacity(db: Session, user: "models.User") -> Dict[str, Any]:
    from routers.persistent_stacks._shared import _fleet_capacity_workers, _EMBEDDED_WORKER_NAME, logger
    from utils.accelerator_fit import capacity_report

    out = []
    workers = _fleet_capacity_workers(db)
    for w in workers:
        try:
            report = capacity_report(db, w)
        except Exception as e:  # noqa: BLE001
            logger.debug("capacity report failed for %s: %s", w.worker_name, e)
            report = {}
        holders = pts_crud.list_stack_holders_for_worker(db, w.worker_name)
        is_embedded = w.worker_name == _EMBEDDED_WORKER_NAME
        out.append({
            "worker": w.worker_name,
            "network_zone": w.network_zone,
            "accelerators": report,
            "held_by_stacks": [{"id": i, "name": n} for i, n in holders],
            # The embedded worker carries the MANAGER's own card. Marked so the
            # budget surface can name it as such, and its status surfaced because
            # it is counted here regardless of status (system-managed-ai §1) —
            # the UI should not imply a card is on a fully-licensed worker when
            # its worker is offline or unlicensed.
            "is_manager": is_embedded,
            "worker_status": getattr(w.status, "value", str(w.status)),
        })
    return {"workers": out}


# =============================================================================
# set_reconciler_config — PUT /reconciler-config
# =============================================================================

def set_reconciler_config(db: Session, body: "_ReconcilerConfig", user: "models.User") -> Dict[str, Any]:  # noqa: F821
    from fastapi import HTTPException
    from routers.persistent_stacks._shared import logger

    if not getattr(user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Platform admin required")
    row = pts_crud.get_reconciler_config_singleton(db)
    if row is None:
        row = models.StackReconcilerConfig(singleton_id=1)
        db.add(row)
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.live_mode is not None:
        row.live_mode = body.live_mode
        logger.warning("stack reconciler live mode %s by user %s — automatic "
                        "recovery will now %s", "ENABLED" if body.live_mode else "disabled",
                        user.id, "ACT on failed stacks" if body.live_mode else "only report (dry run)")
    row.updated_by_user_id = user.id
    db.commit()
    db.refresh(row)
    logger.info("stack reconciler config set (enabled=%s live_mode=%s) by user %s",
                row.enabled, row.live_mode, user.id)
    return {"enabled": row.enabled, "live_mode": row.live_mode, "source": "db"}

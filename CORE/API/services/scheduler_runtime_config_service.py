"""services/scheduler_runtime_config_service.py — business logic for
routers/scheduler_runtime_config.py (Gate 2b).

Owns: server-side preset detection, the singleton-row upsert, audit-event
emission, and the post-commit cache refresh. All `db.query` calls this
domain needs go through `crud/scheduler_runtime_config.py`. The DB→env→
default resolution + preset-detection MATH stays in the pre-existing,
shared `services/scheduler_runtime_cache.py` (`cache`) — this module only
owns the WRITE-side flow that used to live inline in the router.

Auth (platform-admin gate) stays in the router's `_require_platform_admin`
dependency — this module assumes the caller has already been authorized and
only needs the resolved actor (`models.User`) for audit attribution.
"""
from __future__ import annotations

from product_names import themed_logger

from sqlalchemy.orm import Session

import crud.scheduler_runtime_config as crud
import models
from services import scheduler_runtime_cache as cache

logger = themed_logger(__name__, 'scheduler')

SCHEDULER_CONFIG_CHANGED_EVENT = "SCHEDULER_CONFIG_CHANGED"


def get_effective_config(db: Session) -> cache.SchedulerRuntimeConfigSnapshot:
    """Return the currently-effective scheduler runtime config.

    Resolution order: DB row → env var → in-code defaults. Source field
    surfaces which path won. Empty table is NOT lazily seeded by GET.
    """
    return cache.refresh(db)


def update_config(
    db: Session,
    *,
    admin: models.User,
    capacity_floor_pct: int,
    packer_full_above_util_pct: int,
    variance_penalty_s: int,
    background_max_fleet_pct: int,
) -> cache.SchedulerRuntimeConfigSnapshot:
    """Upsert the singleton config row, emit audit, refresh cache.

    Server-side preset detection — slider values dictate the preset name
    regardless of what the client claimed.
    """
    server_preset = cache.detect_preset(
        capacity_floor_pct=capacity_floor_pct,
        packer_full_above_util_pct=packer_full_above_util_pct,
        variance_penalty_s=variance_penalty_s,
        background_max_fleet_pct=background_max_fleet_pct,
    )

    # Capture pre-write snapshot for the audit event's `old_values`.
    pre = cache.refresh(db)
    was_initial_save = pre.source != "db"

    row = crud.get_config(db)
    if row is None:
        row = crud.create_config(db)
    row.preset_name = server_preset
    row.capacity_floor_pct = capacity_floor_pct
    row.packer_full_above_util_pct = packer_full_above_util_pct
    row.variance_penalty_s = variance_penalty_s
    row.background_max_fleet_pct = background_max_fleet_pct
    row.updated_by_user_id = admin.id

    # Emit audit event before commit so the FK back-link works in one
    # transaction. If audit emission fails, log + proceed — the config
    # write is the load-bearing change.
    audit_event_id = None
    try:
        details = {
            "actor_user_id": admin.id,
            "old_preset_name": pre.preset_name,
            "new_preset_name": server_preset,
            "old_values": {
                "capacity_floor_pct": pre.capacity_floor_pct,
                "packer_full_above_util_pct": pre.packer_full_above_util_pct,
                "variance_penalty_s": pre.variance_penalty_s,
                "background_max_fleet_pct": pre.background_max_fleet_pct,
            },
            "new_values": {
                "capacity_floor_pct": capacity_floor_pct,
                "packer_full_above_util_pct": packer_full_above_util_pct,
                "variance_penalty_s": variance_penalty_s,
                "background_max_fleet_pct": background_max_fleet_pct,
            },
        }
        if was_initial_save:
            details["was_initial_save"] = True
        audit = models.AuditEvent(
            tenant_id=admin.tenant_id,
            user_id=admin.id,
            event_name=SCHEDULER_CONFIG_CHANGED_EVENT,
            details=details,
        )
        db.add(audit)
        db.flush()
        audit_event_id = audit.id
        row.audit_event_id = audit_event_id
    except Exception as audit_err:  # noqa: BLE001
        logger.debug("AuditEvent emit failed for SCHEDULER_CONFIG_CHANGED: %s", audit_err)

    db.commit()

    # Refresh cache AFTER the DB commit so a failed commit doesn't
    # diverge in-memory state from the persisted row.
    snap = cache.refresh(db)
    logger.info(
        "scheduler_runtime_config updated: preset=%s capacity_floor=%d "
        "packer_full_at=%d variance_penalty_s=%d (actor=%d, audit=%s)",
        snap.preset_name,
        snap.capacity_floor_pct,
        snap.packer_full_above_util_pct,
        snap.variance_penalty_s,
        admin.id,
        audit_event_id,
    )
    return snap

"""CRUD for WorkerSystemState — kernel/OS/package telemetry per worker.

Called from the heartbeat handler when a worker reports its `system_state`
block. The baseline_* snapshot is taken at calibration completion (Bucket B
work, in pattern_updater.py); this module is just the heartbeat-driven
upsert of *current* values.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from models import WorkerSystemState

logger = logging.getLogger(__name__)


def upsert_worker_system_state(
    db: Session,
    worker_license_id: int,
    payload: Dict[str, Any],
) -> WorkerSystemState:
    """Insert or update the WorkerSystemState row for a worker.

    payload comes straight from the heartbeat:
        {
            "kernel_version": "6.8.0-110-generic",
            "os_distro":      "ubuntu",
            "os_version":     "22.04",
            "package_count":  3812,
            "air_gapped":     false
        }

    None / missing keys are treated as "no update for that field" so a
    worker reporting only some fields doesn't blow away previously-known
    values. baseline_* and last_baselined_at are NEVER touched by this
    upsert — those only change at calibration completion.
    """
    if not payload:
        return None

    row = db.query(WorkerSystemState).filter(
        WorkerSystemState.worker_license_id == worker_license_id
    ).first()

    if row is None:
        row = WorkerSystemState(
            worker_license_id=worker_license_id,
            kernel_version=payload.get("kernel_version"),
            os_distro=payload.get("os_distro"),
            os_version=payload.get("os_version"),
            package_count=int(payload.get("package_count") or 0),
            air_gapped=bool(payload.get("air_gapped", False)),
        )
        db.add(row)
    else:
        if payload.get("kernel_version") is not None:
            row.kernel_version = payload["kernel_version"]
        if payload.get("os_distro") is not None:
            row.os_distro = payload["os_distro"]
        if payload.get("os_version") is not None:
            row.os_version = payload["os_version"]
        if payload.get("package_count") is not None:
            try:
                row.package_count = int(payload["package_count"])
            except (TypeError, ValueError):
                pass
        if payload.get("air_gapped") is not None:
            row.air_gapped = bool(payload["air_gapped"])

    row.updated_at = datetime.now(timezone.utc)
    db.flush()
    return row


def snapshot_baseline(
    db: Session,
    worker_license_id: int,
) -> Optional[WorkerSystemState]:
    """Copy current kernel/os/package values into baseline_* fields.

    Called at calibration completion — the values reported in the
    *current* heartbeat become the new baseline for drift detection
    on subsequent heartbeats. No-op when no row exists yet.
    """
    row = db.query(WorkerSystemState).filter(
        WorkerSystemState.worker_license_id == worker_license_id
    ).first()
    if row is None:
        return None

    row.baseline_kernel_version = row.kernel_version
    row.baseline_os_version = row.os_version
    row.baseline_package_count = row.package_count
    row.last_baselined_at = datetime.now(timezone.utc)
    db.flush()
    logger.info(
        "snapshot_baseline: worker_license=%s kernel=%s os=%s pkgs=%d",
        worker_license_id, row.kernel_version, row.os_version, row.package_count or 0,
    )
    return row

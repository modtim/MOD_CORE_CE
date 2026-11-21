"""
Service layer for the predictive overlap scheduler admin endpoints
(routers/scheduler_admin.py).

Owns the business logic that used to live inline in the router: the
reschedule-rate / prediction-accuracy computations, the flat-vs-nested
`node_kind` payload lookup (the scheduler's flat-payload migration missed
this file — see
tests/test_pattern_updater_payload_shape.py::TestSchedulerAdminReadsFlatPayload),
and shaping the response dicts the router returns unchanged.

No HTTP concerns here. These endpoints have no `response_model` (unchanged
by this migration — adding one would change the contract snapshot); both
"not found" cases return an `{"error": ...}` dict rather than raising, matching
the router's pre-existing behaviour, since these are read-only observability
endpoints rather than resource CRUD.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

import crud.scheduler_admin as scheduler_admin_crud


def _resolve_node_kind(task_payload: Optional[Dict[str, Any]]) -> Any:
    """A task's node_kind may be flat (`task_payload["node_kind"]`) or nested
    under `node_config` — support both (see module docstring)."""
    payload = task_payload or {}
    return payload.get("node_kind") or (payload.get("node_config") or {}).get("node_kind")


def get_scheduler_stats(db: Session) -> Dict[str, Any]:
    """Overview stats for the predictive overlap scheduler."""
    now = datetime.now(timezone.utc)
    last_hour = now - timedelta(hours=1)
    last_day = now - timedelta(days=1)

    # Pattern database stats
    pattern_count, avg_confidence, high_confidence = scheduler_admin_crud.get_pattern_counts(db)

    # Task queue stats
    pending, assigned, running = scheduler_admin_crud.get_task_queue_counts(db)

    # Placements in the last hour
    placed_last_hour = scheduler_admin_crud.count_placed_since(db, last_hour)

    # Reschedule rate (scheduling_version > 0 means rescheduled)
    rescheduled = scheduler_admin_crud.count_rescheduled_since(db, last_day)
    total_day = scheduler_admin_crud.count_created_since(db, last_day)
    reschedule_rate = rescheduled / max(total_day, 1)

    # Prediction accuracy from scheduling_metadata
    completed_with_meta = scheduler_admin_crud.list_completed_with_scheduling_metadata(
        db, last_day, limit=100
    )

    accuracy_samples = 0
    accuracy_sum = 0.0
    for task in completed_with_meta:
        meta = task.scheduling_metadata or {}
        validations = meta.get("validations", [])
        for v in validations:
            if "actual" in v:
                accuracy_samples += 1
                # Simple accuracy: did the task complete without rescheduling?
                if (task.scheduling_version or 0) == 0:
                    accuracy_sum += 1.0

    prediction_accuracy = accuracy_sum / max(accuracy_samples, 1)

    # Worker health
    total_workers, workers_with_snapshots, thermal_throttled = (
        scheduler_admin_crud.get_worker_health_counts(db)
    )

    enabled = os.getenv("PREDICTIVE_SCHEDULING_ENABLED", "false").lower() == "true"

    return {
        "enabled": enabled,
        "pattern_database": {
            "total_patterns": pattern_count,
            "avg_confidence": round(avg_confidence, 3),
            "high_confidence_count": high_confidence,
        },
        "task_queue": {
            "pending": pending,
            "assigned": assigned,
            "running": running,
        },
        "scheduling": {
            "placements_last_hour": placed_last_hour,
            "reschedule_rate_24h": round(reschedule_rate, 3),
            "prediction_accuracy_24h": round(prediction_accuracy, 3),
            "accuracy_samples": accuracy_samples,
        },
        "workers": {
            "total_licensed": total_workers,
            "with_resource_snapshots": workers_with_snapshots,
            "thermal_throttled": thermal_throttled,
        },
    }


def get_worker_timeline(db: Session, worker_name: str) -> Dict[str, Any]:
    """Current projected resource timeline for a specific worker. Useful for
    debugging scheduling decisions."""
    worker = scheduler_admin_crud.get_worker_by_name(db, worker_name)
    if not worker:
        return {"error": "Worker not found"}

    snapshot = scheduler_admin_crud.get_worker_snapshot(db, worker_name)

    # Active tasks on this worker
    active_tasks = scheduler_admin_crud.list_active_tasks_for_worker(db, worker_name)

    task_summaries = []
    for t in active_tasks:
        task_summaries.append({
            "task_id": t.id,
            "status": t.status.value,
            "node_kind": _resolve_node_kind(t.task_payload),
            "start_after": t.start_after.isoformat() if t.start_after else None,
            "phase_plan_confidence": (t.phase_plan or {}).get("scheduling_confidence"),
            "started_at": t.started_at.isoformat() if t.started_at else None,
        })

    hw = worker.hardware_specs or {}

    return {
        "worker_name": worker_name,
        "status": worker.status.value,
        "hardware": {
            "ram_gb": hw.get("ram_gb"),
            "cpu_cores": hw.get("cpu_cores"),
            "gpu_type": hw.get("gpu_type"),
            "vram_gb": hw.get("vram_gb"),
        },
        "current_snapshot": {
            "timestamp": snapshot.timestamp.isoformat() if snapshot else None,
            "cpu_percent": snapshot.cpu_percent if snapshot else None,
            "ram_used_mb": snapshot.ram_used_mb if snapshot else None,
            "ram_available_mb": snapshot.ram_available_mb if snapshot else None,
            "vram_used_mb": snapshot.vram_used_mb if snapshot else None,
            "gpu_temperature_c": snapshot.gpu_temperature_c if snapshot else None,
            "gpu_power_state": snapshot.gpu_power_state if snapshot else None,
            "thermal_throttled": snapshot.thermal_throttled if snapshot else None,
            "nic_rx_mbps": snapshot.nic_rx_mbps if snapshot else None,
            "nic_tx_mbps": snapshot.nic_tx_mbps if snapshot else None,
            "active_containers": snapshot.active_containers if snapshot else None,
            "task_snapshots": snapshot.task_snapshots if snapshot else None,
        } if snapshot else None,
        "active_tasks": task_summaries,
    }


def list_patterns(db: Session) -> Dict[str, Any]:
    """List all known resource patterns with confidence scores."""
    patterns = scheduler_admin_crud.list_top_patterns(db, limit=100)

    return {
        "patterns": [
            {
                "node_kind": p.node_kind,
                "queue_name": p.queue_name,
                "pattern_type": p.pattern_type,
                "confidence": round(p.confidence, 3),
                "sample_count": p.sample_count,
                "avg_duration_s": round(p.avg_duration_seconds, 1) if p.avg_duration_seconds else None,
                "avg_peak_ram_mb": round(p.avg_peak_ram_mb, 0) if p.avg_peak_ram_mb else None,
                "avg_peak_vram_mb": round(p.avg_peak_vram_mb, 0) if p.avg_peak_vram_mb else None,
                "last_updated": p.last_updated_at.isoformat() if p.last_updated_at else None,
            }
            for p in patterns
        ]
    }


def get_pattern_detail(db: Session, node_kind: str) -> Dict[str, Any]:
    """Detailed pattern for a specific node_kind with phase profiles."""
    patterns = scheduler_admin_crud.list_patterns_for_node_kind(db, node_kind)

    if not patterns:
        return {"error": "No patterns found for this node_kind"}

    return {
        "node_kind": node_kind,
        "patterns": [
            {
                "queue_name": p.queue_name,
                "pattern_type": p.pattern_type,
                "pattern_data": p.pattern_data,
                "phase_profiles": p.phase_profiles,
                "confidence": round(p.confidence, 3),
                "sample_count": p.sample_count,
                "avg_duration_s": p.avg_duration_seconds,
                "avg_peak_ram_mb": p.avg_peak_ram_mb,
                "avg_peak_vram_mb": p.avg_peak_vram_mb,
                "avg_input_size_mb": p.avg_input_size_mb,
                "avg_output_size_mb": p.avg_output_size_mb,
                "last_updated": p.last_updated_at.isoformat() if p.last_updated_at else None,
            }
            for p in patterns
        ],
    }

"""Service layer for the `ml` domain (Gate 2b migration).

Business logic backing `routers/ml.py`'s two ML queue-routing training
endpoints (service-account-only; called by the ML_Queue_Routing_Training_Dag
trainer, not end users). See `crud/ml.py` for the tenant/soft-delete/JSONB
notes -- both queries are intentionally cross-tenant.

No `ServiceError` subclass is used here: neither endpoint fetches a single
resource by id (there is no 404-vs-403 case in this domain), and both
pre-migration handlers caught *any* exception and mapped it to a generic 500.
The router keeps that same broad try/except around the service call so
behavior (and the contract snapshot) doesn't shift.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import crud.ml as ml_crud


def check_training_data_availability(db: Session) -> Dict[str, Any]:
    """Shape the training-data-availability stats for the API response.

    Business logic (the None-coalescing / isoformat shaping) moved verbatim
    from the pre-migration router handler; the query itself now lives in
    `crud.ml.get_training_data_stats`.
    """
    result = ml_crud.get_training_data_stats(db)

    total_runs = result.total_runs or 0
    unique_workers = result.unique_workers or 0
    unique_dags = result.unique_dags or 0
    oldest_run = result.oldest_run.isoformat() if result.oldest_run else None
    newest_run = result.newest_run.isoformat() if result.newest_run else None

    return {
        "total_runs": total_runs,
        "unique_workers": unique_workers,
        "unique_dags": unique_dags,
        "oldest_run": oldest_run,
        "newest_run": newest_run,
    }


def get_training_data(db: Session, *, limit: Optional[int]) -> Dict[str, List[Dict[str, Any]]]:
    """Flatten each Cog's `processing_metadata.processing_history` into
    per-node training-data rows.

    Business logic (the success/duration/hardware/queue_name+file_format
    filtering and row shaping -- including the Phase 3b required-field
    filter) moved verbatim from the pre-migration router handler; the row
    source now comes from `crud.ml.list_cogs_with_processing_history`.
    """
    rows: List[Dict[str, Any]] = []

    for cog in ml_crud.list_cogs_with_processing_history(db, limit=limit):
        metadata = cog.processing_metadata or {}
        processing_history = metadata.get('processing_history', [])

        for run in processing_history:
            workflow_run_id = run.get('workflow_run_id')
            nodes_executed = run.get('nodes_executed', [])

            for node in nodes_executed:
                # Only include successful runs with complete data
                if node.get('status') != 'success':
                    continue

                duration = node.get('duration_seconds')
                if not duration or duration <= 0:
                    continue

                hardware_specs = node.get('hardware_specs')
                if not hardware_specs:
                    continue

                parameters = node.get('parameters', {})
                queue_name = parameters.get('queue_name')
                file_format = parameters.get('file_format')

                # Phase 3b: filter out rows missing queue_name or file_format
                if not queue_name or not file_format:
                    continue

                resource_usage = node.get('resource_usage', {})

                # Extract hardware specs
                gpu_info = hardware_specs.get('gpu', {})
                gpu_model = gpu_info.get('model') if gpu_info else None

                # Build training data row
                row = {
                    "workflow_run_id": str(workflow_run_id) if workflow_run_id else None,
                    "node_id": node.get('node_id'),
                    "node_kind": node.get('node_kind'),
                    "input_size_mb": float(resource_usage.get('input_size_mb', 0)) if resource_usage.get('input_size_mb') else 0.0,
                    "file_count": int(resource_usage.get('file_count', 1)) if resource_usage.get('file_count') else 1,
                    "duration_seconds": float(duration),
                    "gpu_model": gpu_model,
                    "cpu_cores": int(hardware_specs.get('cpu_cores', 0)) if hardware_specs.get('cpu_cores') else 0,
                    "network_speed_mbps": 1000,  # Default to 1Gbps
                    "disk_type": hardware_specs.get('disk_type', 'unknown'),
                    "queue_name": queue_name,  # Phase 3b: CRITICAL
                    "file_format": file_format,  # Phase 3b: CRITICAL
                }

                rows.append(row)

    return {"rows": rows}

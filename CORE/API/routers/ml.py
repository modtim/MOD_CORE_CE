# API/routers/ml.py

"""
ML Routing Endpoints

Provides training data for ML-based queue routing system (Phase 3b).
These endpoints are called by the ML_Queue_Routing_Training_Dag to check
for sufficient training data and retrieve it for model training.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import logging

from dependencies import get_db, require_service_account_token
import services.ml_service as ml_service

logger = logging.getLogger(__name__)

# Service router for ML training endpoints (service-to-service auth only)
router = APIRouter(
    prefix="/ml",
    tags=["ML - Machine Learning"],
    responses={404: {"description": "Not found"}},
)


@router.post("/queue-routing/check-training-data", status_code=status.HTTP_200_OK)
def check_training_data_availability(
    db: Session = Depends(get_db),
    service_claims: dict = Depends(require_service_account_token)
):
    """
    Check if sufficient training data exists for ML queue routing model.

    Requirements for training:
    - >= 500 successful workflow runs
    - >= 5 unique workers
    - >= 3 unique node categorys
    - Must have queue_name and file_format in parameters

    This endpoint requires service account authentication.

    Returns:
        {
            "total_runs": int,
            "unique_workers": int,
            "unique_dags": int,
            "oldest_run": str (ISO 8601),
            "newest_run": str (ISO 8601)
        }
    """
    try:
        result = ml_service.check_training_data_availability(db)

        logger.info(
            f"ML training data check: {result['total_runs']} runs, "
            f"{result['unique_workers']} workers, {result['unique_dags']} DAGs"
        )

        return result

    except Exception as e:
        logger.error(f"Error checking training data availability: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error checking training data"
        )


@router.post("/queue-routing/get-training-data", status_code=status.HTTP_200_OK)
def get_training_data(
    limit: Optional[int] = 10000,
    db: Session = Depends(get_db),
    service_claims: dict = Depends(require_service_account_token)
):
    """
    Retrieve training data for ML queue routing model.

    Returns all successful workflow runs with complete metadata including:
    - queue_name (which queue executed the task)
    - file_format (file format/codec processed)
    - Hardware specifications (GPU, CPU, disk, network)
    - Performance metrics (duration, file size, file count)

    This endpoint requires service account authentication.

    Args:
        limit: Maximum number of records to return (default: 10000)

    Returns:
        {
            "rows": [
                {
                    "workflow_run_id": str,
                    "node_id": str,
                    "node_kind": str,
                    "input_size_mb": float,
                    "file_count": int,
                    "duration_seconds": float,
                    "gpu_model": str,
                    "cpu_cores": int,
                    "network_speed_mbps": int,
                    "disk_type": str,
                    "queue_name": str,
                    "file_format": str
                },
                ...
            ]
        }
    """
    try:
        result = ml_service.get_training_data(db, limit=limit)

        logger.info(f"ML training data retrieved: {len(result['rows'])} rows")

        return result

    except Exception as e:
        logger.error(f"Error retrieving training data: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving training data"
        )

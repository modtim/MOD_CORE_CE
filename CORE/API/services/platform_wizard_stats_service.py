"""Service layer for the platform wizard stats router.

This module provides data access functions for the wizard stats dashboard,
returning raw query results that the router shapes into response models.
All functions return the same values that the router previously computed inline.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models


def get_recent_failures(
    db: Session, *, cutoff: datetime, limit: int
) -> List[models.WorkflowRun]:
    """Failed build workflow runs within [cutoff, now], newest first, capped at *limit*."""
    return (
        db.query(models.WorkflowRun)
        .join(models.Workflow, models.Workflow.id == models.WorkflowRun.workflow_id)
        .filter(
            models.WorkflowRun.status == models.TaskStatus.FAILED,
            models.Workflow.name.like("build-%"),
            models.WorkflowRun.started_at >= cutoff,
        )
        .order_by(models.WorkflowRun.started_at.desc())
        .limit(limit)
        .all()
    )


def get_workflow_by_id(db: Session, *, workflow_id: int) -> Optional[models.Workflow]:
    """The workflow with *workflow_id*, or None."""
    return db.query(models.Workflow).filter_by(id=workflow_id).first()


def get_build_spec_record_by_workflow_run_id(
    db: Session, *, workflow_run_id: int
) -> Optional[models.BuildSpecRecord]:
    """The BuildSpecRecord for *workflow_run_id*, or None."""
    return db.query(models.BuildSpecRecord).filter_by(workflow_run_id=workflow_run_id).first()

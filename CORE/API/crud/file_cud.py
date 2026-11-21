"""File CRUD operations - File management within tenants."""
from typing import Optional, List

from sqlalchemy.orm import Session
import models, schemas


def create_file_record(
    db: Session,
    file_data: schemas.FileCreate,
    tenant_id: int,
    user_id: int
) -> models.File:
    """Create a new file record in the database."""
    db_file = models.File(
        **file_data.model_dump(),
        tenant_id=tenant_id,
        user_id=user_id
    )
    db.add(db_file)

    # This function creates a File row directly with no ContentBlob dedup
    # check, so it's always "unbacked" per storage_measure._DB_SUMS_SQL.
    # NOTE: unreferenced by any router/service as of 2026-09-27 — fixed here
    # for hygiene so a future caller doesn't reintroduce the saw-tooth bug.
    from utils.paid_import import optional_paid
    _em = optional_paid("services.egress_metering")
    if _em is not None:
        _em.apply_storage_change(db, tenant_id, unbacked_bytes_delta=file_data.size_bytes,
                                 commit=False)

    db.commit()
    db.refresh(db_file)
    return db_file


def get_file_record(
    db: Session,
    file_id: str,
    tenant_id: int
) -> Optional[models.File]:
    """Retrieve a single active (non-deleted) file record by its ID and tenant_id."""
    return db.query(models.File).filter(
        models.File.id == file_id,
        models.File.tenant_id == tenant_id,
        models.File.deleted_at.is_(None)
    ).first()


def create_workflow_run_file(
    db: Session,
    workflow_run_id: int,
    file_id: str,
    title: str
) -> models.WorkflowRunFile:
    """Create a new record linking a file to a workflow run with a title."""
    db_run_file = models.WorkflowRunFile(
        workflow_run_id=workflow_run_id,
        file_id=file_id,
        title=title,
        status=models.TaskStatus.QUEUED
    )
    db.add(db_run_file)
    db.commit()
    db.refresh(db_run_file)
    return db_run_file


def get_workflow_run_files(db: Session, workflow_run_id: int) -> List[models.WorkflowRunFile]:
    """Retrieve all files associated with a specific workflow run."""
    return db.query(models.WorkflowRunFile).filter(models.WorkflowRunFile.workflow_run_id == workflow_run_id).all()


def get_workflow_run_file_by_id(
    db: Session,
    workflow_run_id: int,
    file_id: str,
    tenant_id: int
) -> Optional[models.WorkflowRunFile]:
    """Retrieve a single WorkflowRunFile record by its IDs, ensuring it belongs to the correct tenant."""
    return (
        db.query(models.WorkflowRunFile)
        .join(models.WorkflowRun, models.WorkflowRunFile.workflow_run_id == models.WorkflowRun.id)
        .join(models.Workflow, models.WorkflowRun.workflow_id == models.Workflow.id)
        .filter(
            models.WorkflowRunFile.workflow_run_id == workflow_run_id,
            models.WorkflowRunFile.file_id == file_id,
            models.Workflow.tenant_id == tenant_id
        )
        .first()
    )


def update_workflow_run_file(
    db: Session,
    db_run_file: models.WorkflowRunFile,
    file_update: schemas.WorkflowRunFileUpdate
) -> models.WorkflowRunFile:
    """Update the metadata for a file within a workflow run."""
    update_data = file_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_run_file, key, value)
    db.add(db_run_file)
    db.commit()
    db.refresh(db_run_file)
    return db_run_file


def delete_workflow_run_file(db: Session, db_run_file: models.WorkflowRunFile):
    """Soft-delete a file record from a workflow run.

    iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    iso-no-hard-delete). `WorkflowRunFile` now carries a `deleted_at` column
    (SoftDeleteMixin) and is in database.py's soft-delete listener, so the
    tombstoned row leaves every live read.
    """
    from datetime import datetime, timezone
    db_run_file.deleted_at = datetime.now(timezone.utc)
    db.commit()

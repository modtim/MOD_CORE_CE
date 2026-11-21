# API/routers/workflow_run_files.py

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import schemas
import crud
import models
import logging
from dependencies import get_db, require_active_and_onboarded_user, get_impersonated_user_from_service_account
# GY.C16 follow-up: auditor data-content gate (these GETs expose a run's file
# records — the same run-content surface /previews + /node-versions guard).
from dependencies import require_auditor_data_content

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Workflow Run Files", "The Road"],
    # The default dependency is removed so we can specify per-route
    responses={404: {"description": "Not found"}},
)


def _authorize_run_for_write(db: Session, user: models.User, run_id: int):
    """Resolve a workflow run and enforce the run-scoped write gate.

    Write-side IDOR fix: the run-file POST/PATCH/DELETE endpoints were
    tenant-only, so any tenant user could create/modify/delete files on a
    co-tenant's PRIVATE run by run id (and were cross-tenant unfiltered on the
    padmin path). Gate them on the same per-workflow view scope the read
    endpoints use (owner / view-grant / platform admin) — mirrors
    resource_permissions.can_view_workflow_run usage in the sibling fixes.

    Returns the run when the caller may write it, else None (caller raises 404
    to avoid leaking run existence). For a non-admin, the tenant-scoped run
    lookup also enforces the tenant match for free; a platform admin bypasses
    the tenant filter and is allowed cross-tenant.
    """
    from resource_permissions import can_view_workflow_run
    is_admin = getattr(user, "is_platform_admin", False)
    db_run = crud.get_workflow_run_by_id(
        db, run_id=run_id, tenant_id=None if is_admin else user.tenant_id
    )
    if db_run is None or not can_view_workflow_run(db, user, db_run):
        return None
    return db_run


@router.post("/{run_id}/files", response_model=schemas.WorkflowRunFile, status_code=status.HTTP_201_CREATED)
def create_workflow_run_file(
        run_id: int,
        file_id: str,
        title: str,
        db: Session = Depends(get_db),
        # This endpoint is called by worker containers, so it uses the impersonation dependency
        current_user: models.User = Depends(get_impersonated_user_from_service_account),
):
    """
    Adds a file to an existing workflow run. Intended to be called by a service account.
    """
    # Write-side IDOR gate. The caller here is the service account impersonating
    # the run's user (worker containers write run outputs), so the run owner
    # passes can_view_workflow_run and the legitimate worker write path keeps
    # working; a would-be writer with no access to the run gets 404.
    db_run = _authorize_run_for_write(db, current_user, run_id)
    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found or permission denied")

    db_file = crud.get_file_record(db, file_id=file_id, tenant_id=current_user.tenant_id)
    if not db_file:
        raise HTTPException(status_code=404, detail="File not found or permission denied")

    try:
        return crud.create_workflow_run_file(db, workflow_run_id=run_id, file_id=file_id, title=title)
    except Exception as e:
        logger.error("Failed to create workflow run file: %s", e)
        raise HTTPException(status_code=500, detail="Failed to create workflow run file")


@router.get("/{run_id}/files", response_model=List[schemas.WorkflowRunFile],
            dependencies=[Depends(require_auditor_data_content)])
def list_workflow_run_files(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Lists all files associated with a specific workflow run.
    """
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=current_user.tenant_id)
    if not db_run:
        raise HTTPException(status_code=404, detail="Workflow run not found or permission denied")
    # Per-workflow gate: a run's file list belongs to its workflow's viewers
    # (owner / view-grant / platform admin), not to every tenant user.
    from resource_permissions import can_view_workflow_run
    if not can_view_workflow_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found or permission denied")

    return crud.get_workflow_run_files(db, workflow_run_id=run_id)


@router.get("/{run_id}/files/{file_id}", response_model=schemas.WorkflowRunFile,
            dependencies=[Depends(require_auditor_data_content)])
def get_workflow_run_file(
        run_id: int,
        file_id: str,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Retrieves a single file record from a specific workflow run.
    """
    db_run_file = crud.get_workflow_run_file_by_id(
        db, workflow_run_id=run_id, file_id=file_id, tenant_id=current_user.tenant_id
    )
    if not db_run_file:
        raise HTTPException(status_code=404, detail="File not found in this workflow run or permission denied")
    # Per-workflow gate: mirror the run-content endpoints so a co-tenant user
    # without a view grant can't read a private run's file records by run id.
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=current_user.tenant_id)
    from resource_permissions import can_view_workflow_run
    if not can_view_workflow_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="File not found in this workflow run or permission denied")
    return db_run_file


@router.patch("/{run_id}/files/{file_id}", response_model=schemas.WorkflowRunFile)
def update_workflow_run_file(
        run_id: int,
        file_id: str,
        file_update: schemas.WorkflowRunFileUpdate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Updates the metadata for a file within a workflow run.
    """
    # Write-side IDOR gate: only a user who can access the run may modify its
    # files (owner / view-grant / platform admin), not every tenant user.
    db_run = _authorize_run_for_write(db, current_user, run_id)
    if not db_run:
        raise HTTPException(status_code=404, detail="File not found in this workflow run or permission denied")
    db_run_file = crud.get_workflow_run_file_by_id(
        db, workflow_run_id=run_id, file_id=file_id, tenant_id=db_run.tenant_id
    )
    if not db_run_file:
        raise HTTPException(status_code=404, detail="File not found in this workflow run or permission denied")

    return crud.update_workflow_run_file(db, db_run_file=db_run_file, file_update=file_update)


@router.delete("/{run_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow_run_file(
        run_id: int,
        file_id: str,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Removes a file from a workflow run.
    """
    # Write-side IDOR gate: only a user who can access the run may delete its
    # files (owner / view-grant / platform admin), not every tenant user.
    db_run = _authorize_run_for_write(db, current_user, run_id)
    if not db_run:
        raise HTTPException(status_code=404, detail="File not found in this workflow run or permission denied")
    db_run_file = crud.get_workflow_run_file_by_id(
        db, workflow_run_id=run_id, file_id=file_id, tenant_id=db_run.tenant_id
    )
    if not db_run_file:
        raise HTTPException(status_code=404, detail="File not found in this workflow run or permission denied")

    crud.delete_workflow_run_file(db, db_run_file=db_run_file)
    return
"""Tenant Knowledge Base — tenant-admin management surface
(tenant-assistant-knowledge-base §3).

Tenant admins populate a private, tenant-scoped knowledge base from three
sources — uploaded documents, pasted text, and existing storage folders — and
manage its lifecycle (list / delete / reindex). Ingestion runs off the request
path via FastAPI BackgroundTasks (design D4); each management endpoint validates
+ persists a `pending` TenantKnowledgeDocument then schedules the background
ingest.

Isolation: `tenant_id` ALWAYS comes from the authenticated principal, never the
request body. Every endpoint is gated by the same `_is_tenant_admin` idiom as
`PUT /assistant/config` (design D9). Retrieval (any active tenant user) lives in
the orchestrator, not here.

The background task MUST NOT reuse the request-scoped DB session (it is closed
once the response is sent), so `_run_ingest` opens a fresh `SessionLocal()` and
closes it in a finally.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File as FastAPIFile,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

import models
from dependencies import (
    get_db,
    get_tenant_minio_client,
    require_active_and_onboarded_user,
)
from routers.assistant import _is_tenant_admin
from services import assistant_knowledge_service as _kb_service
from services._base import ServiceError as _ServiceError
from utils import tenant_kb_ingester
from utils.minio_client import MinioClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant/knowledge", tags=["Assistant Knowledge Base"])

# Gate 2b db.query migration — query layer + policy for this router now live
# in crud/assistant_knowledge.py and services/assistant_knowledge_service.py
# (see those modules' docstrings). This maps a service-layer ServiceError to
# the exact HTTPException shape the pre-migration inline `raise
# HTTPException(...)` call sites used.
_STATUS_BY_CODE = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "bad_request": status.HTTP_400_BAD_REQUEST,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _raise_http(exc: _ServiceError) -> None:
    raise HTTPException(
        status_code=_STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.message,
    )


def require_tenant_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    """Gate every management endpoint on the tenant-admin idiom shared with
    `PUT /assistant/config` (platform admin OR the tenant's root admin)."""
    if not _is_tenant_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant admin only",
        )
    return current_user


class _TextIngest(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    text: str = Field(..., min_length=1)


class _FolderIngest(BaseModel):
    cog_id: int


# Response models. Every field is Optional (default None) and extra keys are
# allowed, so these document the existing payloads without being able to
# reject or strip one. `to_dict` returns {id, title, source_type, status,
# chunk_count, bytes, error_message, created_at} on every single-doc path;
# /folder wraps those in a `documents` list; /delete returns {deleted, id}.
# The list endpoint returns the same wrapper, so both are captured here.


class KnowledgeDocumentResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Optional[int] = None
    title: Optional[str] = None
    source_type: Optional[str] = None
    status: Optional[str] = None
    chunk_count: Optional[int] = None
    bytes: Optional[int] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None


class KnowledgeDocumentListResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    documents: Optional[list] = None


class DeleteKnowledgeDocumentResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    deleted: Optional[bool] = None
    id: Optional[int] = None


def _run_ingest(doc_id: int) -> None:
    """Background entry point. Opens a FRESH session — the request-scoped one is
    gone by the time this runs — and delegates to the ingester, which owns the
    document's status transitions (processing → ready | failed)."""
    from database import SessionLocal

    db = SessionLocal()
    try:
        tenant_kb_ingester.ingest_document(db, doc_id)
    except Exception:
        logger.exception("KB ingest failed for document %s", doc_id)
    finally:
        db.close()


@router.post("/upload", status_code=status.HTTP_201_CREATED, response_model=KnowledgeDocumentResponse)
async def upload_knowledge_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = FastAPIFile(...),
    current_user: models.User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
) -> dict:
    """Upload a document (PDF/DOCX/MD/TXT/CSV) into the tenant KB. Validates the
    type + size, persists the bytes, creates a `pending` record, and schedules
    the background ingest."""
    try:
        _kb_service.assert_supported_upload_type(file.filename or "")
    except _ServiceError as exc:
        _raise_http(exc)

    content = await file.read()

    doc = await _kb_service.create_upload_document(
        db, minio_client,
        current_user=current_user,
        filename=file.filename or "document",
        content=content,
        content_type=file.content_type or "application/octet-stream",
    )

    background_tasks.add_task(_run_ingest, doc.id)
    return _kb_service.to_dict(doc)


@router.post("/text", status_code=status.HTTP_201_CREATED, response_model=KnowledgeDocumentResponse)
async def add_knowledge_text(
    background_tasks: BackgroundTasks,
    body: _TextIngest = Body(...),
    current_user: models.User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
) -> dict:
    """Add pasted text/notes to the tenant KB. The text is persisted as a
    tenant-scoped blob so the background ingest can re-read it, then a `pending`
    record is created and ingest scheduled."""
    doc = await _kb_service.create_text_document(
        db, minio_client,
        current_user=current_user,
        title=body.title,
        text=body.text,
    )

    background_tasks.add_task(_run_ingest, doc.id)
    return _kb_service.to_dict(doc)


@router.post("/folder", status_code=status.HTTP_201_CREATED, response_model=KnowledgeDocumentListResponse)
def add_knowledge_folder(
    background_tasks: BackgroundTasks,
    body: _FolderIngest = Body(...),
    current_user: models.User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Index the supported files under an existing tenant storage folder (Cog).
    The folder must belong to the caller's tenant (404 otherwise). Each supported
    file becomes its own `pending` record with ingest scheduled; unsupported
    files are skipped by the enumerator."""
    try:
        created = _kb_service.create_folder_documents(
            db, current_user=current_user, cog_id=body.cog_id,
        )
    except _ServiceError as exc:
        _raise_http(exc)

    for doc in created:
        background_tasks.add_task(_run_ingest, doc.id)

    return {"documents": [_kb_service.to_dict(d) for d in created]}


@router.get("", response_model=KnowledgeDocumentListResponse)
def list_knowledge_documents(
    current_user: models.User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict:
    """List the tenant's knowledge documents with status, chunk count, source
    type, and any error message."""
    rows = _kb_service.list_documents(db, current_user.tenant_id)
    return {"documents": [_kb_service.to_dict(r) for r in rows]}


@router.delete("/{doc_id}", response_model=DeleteKnowledgeDocumentResponse)
def delete_knowledge_document(
    doc_id: int,
    current_user: models.User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Delete a knowledge document: remove its chunks from the tenant vector
    collection (by `kb_document_id` filter) then delete the record. Fail-closed —
    if the vector delete can't be confirmed, the record is left intact so its
    chunks are never orphaned as 'deleted' while still retrievable."""
    try:
        _kb_service.delete_document(db, current_user.tenant_id, doc_id)
    except _ServiceError as exc:
        _raise_http(exc)
    return {"deleted": True, "id": doc_id}


@router.post("/{doc_id}/reindex", response_model=KnowledgeDocumentResponse)
def reindex_knowledge_document(
    doc_id: int,
    background_tasks: BackgroundTasks,
    current_user: models.User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Reindex a document: drop its existing chunks (delete-by-document) then
    reset it to `pending` and reschedule the background ingest."""
    try:
        doc = _kb_service.reindex_document(db, current_user.tenant_id, doc_id)
    except _ServiceError as exc:
        _raise_http(exc)

    background_tasks.add_task(_run_ingest, doc.id)
    return _kb_service.to_dict(doc)

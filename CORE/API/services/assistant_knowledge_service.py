"""Service layer for the tenant Assistant Knowledge Base (Gate 2b migration
of ``routers/assistant_knowledge.py`` — see ``services/_base.py`` for the
layering contract this follows).

Owns the policy this domain's handlers previously inlined next to their
``db.query`` calls: the platform-admin-at-tenant-1 storage-quota bypass, the
content-addressed blob upload (MinIO I/O, external to this module's
``db.query`` calls — those now live in ``crud.assistant_knowledge``), the
folder-ownership check, and the fail-closed vector-store-purge-before-mutate
rule shared by delete and reindex.

``services._base`` ships ``NotFoundError``/``ConflictError`` but no
403 — not needed here (see ``crud/assistant_knowledge.py`` module docstring:
this domain has no doc-level 403). Two small domain errors are declared below
for the cases ``_base`` doesn't cover.

IMPORTANT: scheduling the background ingest (``background_tasks.add_task``)
stays in the ROUTER, not here. ``tests/test_cov_assistant_kb_endpoints.py``
patches ``routers.assistant_knowledge._run_ingest`` — that only works if the
call site doing the scheduling is a bare-name lookup inside the router
module at request time, so this module returns the created/updated
document(s) and lets the router do the scheduling.
"""
from __future__ import annotations

import hashlib
import logging
from io import BytesIO
from typing import Any, List

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

import crud
import models
import schemas
from crud import assistant_knowledge as kb_crud
from services._base import NotFoundError, ServiceError
from utils import tenant_kb_ingester, user_vector_store
from utils.minio_client import MinioClient

logger = logging.getLogger(__name__)


class BadRequestError(ServiceError):
    """Domain validation failure (router -> 400)."""

    def __init__(self, message: str = "Bad request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


class UnavailableError(ServiceError):
    """A required dependency (the vector store) is unreachable; the caller
    fails closed rather than partially applying the change (router -> 503)."""

    def __init__(self, message: str = "Unavailable", *, details: Any = None):
        super().__init__(message, code="unavailable", details=details)


def to_dict(doc: models.TenantKnowledgeDocument) -> dict:
    """Shape a ``TenantKnowledgeDocument`` row into the management-surface
    response dict (verbatim pre-migration shape)."""
    return {
        "id": doc.id,
        "title": doc.title,
        "source_type": doc.source_type,
        "status": doc.status,
        "chunk_count": int(doc.chunk_count or 0),
        "bytes": doc.bytes,
        "error_message": doc.error_message,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def assert_supported_upload_type(filename: str) -> None:
    """Reject unsupported file types before any bytes are read or persisted."""
    if not tenant_kb_ingester.is_supported(filename):
        raise BadRequestError(
            "Unsupported file type. Supported: PDF, DOCX, MD, TXT, CSV."
        )


def enforce_storage_quota(
    db: Session, current_user: models.User, additional_bytes: int,
) -> None:
    """Platform admins operating tenant 1 (the platform's own tenant) bypass
    the tenant storage cap; every other caller is enforced through the
    existing cross-router quota chokepoint. That utility already raises its
    own ``HTTPException`` (413) on breach — not reinvented as a
    ``ServiceError`` here, matching the pre-migration call site verbatim."""
    from utils.usage_enforcement import enforce_storage_quota as _enforce

    _enforce(db, current_user.tenant_id, additional_bytes, user=current_user)


async def persist_blob(
    db: Session,
    minio_client: MinioClient,
    *,
    tenant_id: int,
    user_id: int,
    filename: str,
    content: bytes,
    content_type: str,
) -> models.File:
    """Persist raw bytes as a tenant-scoped File + ContentBlob (content-
    addressed, mirrors routers/files.py) so the background ingest can re-read
    them by ``file_id``. No Cog is created — KB blobs are not user-facing
    folder files.

    FLAGGED, NOT FIXED (out of migration scope): this interleaves a blocking
    MinIO upload between two DB commits on the request-scoped session — the
    same shape as the pre-migration code (`External fetch inside DB
    transaction` is a known repo-wide anti-pattern; here the upload happens
    between two commits rather than inside one open transaction, so it is
    not the acute pgbouncer-pinning case, but it is still worth a follow-up
    look if this domain ever moves onto a longer-lived session).
    """
    content_hash = hashlib.sha256(content).hexdigest()
    file_extension = ""
    if filename and "." in filename:
        file_extension = filename.rsplit(".", 1)[-1].lower()
    object_name = content_hash

    file_data = schemas.FileCreate(
        name=filename,
        size_bytes=len(content),
        mime_type=content_type,
        storage_path="",
        cog_id=None,
    )
    db_file = crud.create_file_record(
        db, file_data=file_data, tenant_id=tenant_id, user_id=user_id,
    )

    try:
        storage_path = await minio_client.upload_file(
            object_name=object_name,
            file_stream=BytesIO(content),
            file_size=len(content),
            content_type=content_type,
        )
    except Exception as e:
        # ISO 27001 no-hard-delete (MEMORY iso-no-hard-delete): File carries
        # SoftDeleteMixin, so stamp `deleted_at` and RETAIN the row on the
        # storage-fail rollback instead of hard-removing it.
        from datetime import datetime as _dt, timezone as _tz
        db_file.deleted_at = _dt.now(_tz.utc)
        db.commit()
        logger.error("Failed to persist knowledge blob to storage: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist knowledge document to storage",
        )

    db_file.storage_path = storage_path

    existing_blob = kb_crud.get_content_blob(db, content_hash, tenant_id)
    if existing_blob:
        existing_blob.storage_path = storage_path
        existing_blob.mime_type = content_type
        existing_blob.file_extension = file_extension
    else:
        db.add(models.ContentBlob(
            id=content_hash,
            tenant_id=tenant_id,
            size_bytes=len(content),
            mime_type=content_type,
            file_extension=file_extension,
            storage_path=storage_path,
            cog_id=None,
        ))

    db.commit()
    db.refresh(db_file)
    return db_file


async def create_upload_document(
    db: Session,
    minio_client: MinioClient,
    *,
    current_user: models.User,
    filename: str,
    content: bytes,
    content_type: str,
) -> models.TenantKnowledgeDocument:
    """Full ``/upload`` pipeline: quota check, blob persist, ``pending``
    record. Caller (``routers.assistant_knowledge.upload_knowledge_document``)
    has already validated the file type via ``assert_supported_upload_type``."""
    enforce_storage_quota(db, current_user, len(content))

    db_file = await persist_blob(
        db, minio_client,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        filename=filename,
        content=content,
        content_type=content_type,
    )

    doc = models.TenantKnowledgeDocument(
        tenant_id=current_user.tenant_id,
        source_type="upload",
        title=filename,
        file_id=db_file.id,
        status="pending",
        chunk_count=0,
        bytes=len(content),
        created_by_user_id=current_user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


async def create_text_document(
    db: Session,
    minio_client: MinioClient,
    *,
    current_user: models.User,
    title: str,
    text: str,
) -> models.TenantKnowledgeDocument:
    """Full ``/text`` pipeline: persist the pasted text as a blob, ``pending``
    record."""
    content = text.encode("utf-8")
    enforce_storage_quota(db, current_user, len(content))

    db_file = await persist_blob(
        db, minio_client,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        filename=f"{title}.txt",
        content=content,
        content_type="text/plain",
    )

    doc = models.TenantKnowledgeDocument(
        tenant_id=current_user.tenant_id,
        source_type="paste",
        title=title,
        file_id=db_file.id,
        status="pending",
        chunk_count=0,
        bytes=len(content),
        created_by_user_id=current_user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def create_folder_documents(
    db: Session, *, current_user: models.User, cog_id: int,
) -> List[models.TenantKnowledgeDocument]:
    """Full ``/folder`` pipeline: verify tenant ownership of the Cog, enumerate
    its supported files, and create one ``pending`` record per file."""
    cog = kb_crud.get_owned_cog(db, cog_id, current_user.tenant_id)
    if cog is None:
        raise NotFoundError("Folder not found")

    files = tenant_kb_ingester.enumerate_supported_files(
        db, current_user.tenant_id, cog_id,
    )

    created: List[models.TenantKnowledgeDocument] = []
    for f in files:
        file_id = getattr(f, "id", None) if not isinstance(f, dict) else (f.get("file_id") or f.get("id"))
        name = getattr(f, "name", None) if not isinstance(f, dict) else f.get("name")
        size = (
            getattr(f, "size_bytes", None)
            if not isinstance(f, dict)
            else f.get("size_bytes")
        )
        doc = models.TenantKnowledgeDocument(
            tenant_id=current_user.tenant_id,
            source_type="folder",
            title=name or "document",
            file_id=file_id,
            cog_id=cog_id,
            status="pending",
            chunk_count=0,
            bytes=size,
            created_by_user_id=current_user.id,
        )
        db.add(doc)
        created.append(doc)

    db.commit()
    for doc in created:
        db.refresh(doc)
    return created


def list_documents(
    db: Session, tenant_id: int,
) -> List[models.TenantKnowledgeDocument]:
    """List the tenant's knowledge documents (status/chunk_count/etc for the
    admin surface)."""
    return kb_crud.list_tenant_documents(db, tenant_id)


def get_owned_document(
    db: Session, tenant_id: int, doc_id: int,
) -> models.TenantKnowledgeDocument:
    """Fetch a knowledge document the caller's tenant owns, or raise 404."""
    doc = kb_crud.get_owned_document(db, doc_id, tenant_id)
    if doc is None:
        raise NotFoundError("Knowledge document not found")
    return doc


def delete_document(
    db: Session, tenant_id: int, doc_id: int,
) -> models.TenantKnowledgeDocument:
    """Delete a knowledge document: purge its vector-store chunks first, fail
    closed (raise, leave the row intact) if that can't be confirmed."""
    doc = get_owned_document(db, tenant_id, doc_id)

    try:
        user_vector_store.delete_by_document(tenant_id, doc.id)
    except user_vector_store.VectorStoreUnavailable as e:
        logger.error("KB delete: vector store unavailable for doc %s: %s", doc.id, e)
        raise UnavailableError("Vector store unavailable; document not deleted")

    # ISO 27001 no-hard-delete (MEMORY iso-no-hard-delete):
    # TenantKnowledgeDocument carries SoftDeleteMixin and is in database.py's
    # soft-delete listener, so stamp `deleted_at` and RETAIN the row (its
    # vector-store chunks are already purged above). Live reads drop it.
    from datetime import datetime as _dt, timezone as _tz
    doc.deleted_at = _dt.now(_tz.utc)
    db.commit()
    return doc


def reindex_document(
    db: Session, tenant_id: int, doc_id: int,
) -> models.TenantKnowledgeDocument:
    """Reindex a document: drop its existing chunks, fail closed if that can't
    be confirmed, otherwise reset it to ``pending`` for the caller to
    reschedule the ingest."""
    doc = get_owned_document(db, tenant_id, doc_id)

    try:
        user_vector_store.delete_by_document(tenant_id, doc.id)
    except user_vector_store.VectorStoreUnavailable as e:
        logger.error("KB reindex: vector store unavailable for doc %s: %s", doc.id, e)
        raise UnavailableError("Vector store unavailable; reindex not started")

    doc.status = "pending"
    doc.chunk_count = 0
    doc.error_message = None
    db.commit()
    db.refresh(doc)
    return doc

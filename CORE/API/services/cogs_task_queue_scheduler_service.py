"""Service layer for `routers/cogs/task_queue_scheduler.py` (openspec:
stack-training-and-prewarm, Gate 2b).

Business logic for the module's one endpoint (`GET /cogs/{cog_id}/pdf-view`).
All data access goes through `crud/cogs_task_queue_scheduler.py`. HTTP
concerns (inline bearer/query-token auth resolution, `HTTPException`,
`StreamingResponse` construction, docstrings) stay in the router — this
module raises `services._base.NotFoundError` where the pre-migration inline
code raised its 404 "File not found", and the router maps it back to the
identical `HTTPException`.
"""
from __future__ import annotations

import os
from typing import Tuple

from sqlalchemy.orm import Session

from crud import cogs_task_queue_scheduler as tqs_crud
from services._base import NotFoundError


def resolve_pdf_object_path(db: Session, *, cog_id: int, tenant_id: int) -> Tuple[str, str]:
    """Resolve the MinIO object path + display filename for a cog's PDF
    preview (File record or ContentBlob fallback). Raises `NotFoundError`
    exactly where the pre-migration inline `pdf_view` handler raised
    `HTTPException(404, "File not found")`."""
    import crud

    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=tenant_id)
    if not cog:
        raise NotFoundError("File not found")

    # Get storage path (File record or ContentBlob fallback)
    storage_path = None
    file_record = tqs_crud.get_active_file_for_cog(db, cog_id=cog_id, tenant_id=tenant_id)

    if file_record:
        storage_path = file_record.storage_path
    elif cog.latest_version_id:
        version = tqs_crud.get_version_by_id(db, cog.latest_version_id)
        if version and version.content_id:
            blob = tqs_crud.get_content_blob(
                db, content_id=version.content_id, tenant_id=tenant_id
            )
            if blob:
                storage_path = blob.storage_path

    if not storage_path:
        raise NotFoundError("File not found")

    # P10e-a dual-read: new rows carry `s3://`, rows stored before the
    # rename still hold `minio://`; the configured STORAGE_SCHEME stays
    # honoured, default s3.
    storage_scheme = os.getenv('STORAGE_SCHEME', 's3')
    sp = storage_path
    if sp.startswith((f'{storage_scheme}://', 's3://', 'minio://')):
        path_after_scheme = sp.split('://', 1)[1]
        object_path = '/'.join(path_after_scheme.split('/')[1:]) if '/' in path_after_scheme else path_after_scheme
    else:
        object_path = sp

    return object_path, cog.name

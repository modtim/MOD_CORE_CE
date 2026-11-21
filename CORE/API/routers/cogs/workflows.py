from ._shared import *  # noqa: F401,F403

from fastapi import APIRouter  # noqa: F811  (local, prefix-free; the aggregator carries prefix/tags/deps)
from services import cogs_workflows_service
from services._base import NotFoundError, ServiceError
# GY.C16 follow-up: auditor data-content gate on tenant data-content routes.
from dependencies import require_auditor_data_content

router = APIRouter()
service_router = APIRouter()

@service_router.post("/cogs/files")
async def get_multiple_cogs_files(
    cog_ids: List[int],
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal)
):
    """
    Service endpoint: Get all files from multiple cogs (files and/or folders).

    For files: returns the file info directly
    For folders: recursively gets all files

    Returns combined list of all files with their MinIO paths and relative paths.
    Used by zip_folder_download DAG to create combined archives.
    """
    import time
    start_time = time.time()
    logging.info(f"[ZIP-MULTI] === REQUEST START === cog_ids={cog_ids}")

    if not cog_ids:
        raise HTTPException(status_code=400, detail="At least one cog_id is required")

    return cogs_workflows_service.get_multiple_cogs_files(
        db, cog_ids=cog_ids, principal=principal, start_time=start_time
    )

@router.get("/{cog_id}/preview-url",
            dependencies=[Depends(require_auditor_data_content)])
async def get_preview_url(
    cog_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
):
    """
    Get a presigned URL for inline preview (no Content-Disposition: attachment).
    Used by the workflow builder to show thumbnails of selected files.
    """
    from fastapi.responses import JSONResponse

    try:
        url = cogs_workflows_service.get_preview_url(
            db, cog_id=cog_id, current_user=current_user, minio_client=minio_client
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
    except ServiceError as e:
        raise HTTPException(status_code=500, detail=e.message)
    return JSONResponse(content={"url": url})

@router.get("/{cog_id}/text-content",
            dependencies=[Depends(require_auditor_data_content)])
async def get_text_content(
    cog_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
):
    """
    Get the text content of a file. Reads from MinIO and returns as plain text.
    Used by the workflow builder and dashboard to preview text/code files.
    Max 500KB to prevent loading huge files.
    """
    from fastapi.responses import PlainTextResponse

    # CUIP.25 — the SAME mobile-CUI guard CUIP.11e put on GET /cogs/{id}/download
    # (one `enforce_mobile_download` call, one CUIP.12 audit row). This route hands
    # a MARKED asset's own TEXT bytes straight to the caller (the workflow builder /
    # dashboard code preview), so a phone holding a mobile-client token could
    # otherwise skip the shelf verdict entirely. Placed after the route's auth +
    # auditor data-content dependency above and BEFORE the service call, so no
    # object is ever read from MinIO for a refused phone. Keyed on the COG id —
    # the same id the shelf marks and the files/download routes pass. No-op for
    # every non-mobile azp (web/desktop/API-key behaviour unchanged) and for
    # unmarked cogs; `azp_from_request` reads the bearer the auth dependency above
    # already signature-validated.
    # 1788 CE carve: mobile_cui_policy is paid; CE has no mobile-client shelf
    # verdict, so the route keeps its pre-GY.V1d web behaviour (no-op).
    from utils.paid_import import optional_paid
    _mobile_cui = optional_paid("services.mobile_cui_policy")
    azp_from_request = (_mobile_cui.azp_from_request
                        if _mobile_cui is not None
                        else (lambda *a, **kw: None))
    enforce_mobile_download = (_mobile_cui.enforce_mobile_download
                               if _mobile_cui is not None
                               else (lambda *a, **kw: None))

    enforce_mobile_download(
        db,
        token_azp=azp_from_request(http_request),
        device_id_header=http_request.headers.get("X-MOD-Device-Id"),
        device_token_header=http_request.headers.get("X-MOD-Device-Token"),
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        resource_type="asset",
        resource_id=cog_id,
    )

    try:
        content = cogs_workflows_service.get_text_content(
            db, cog_id=cog_id, current_user=current_user, minio_client=minio_client
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
    except ServiceError as e:
        raise HTTPException(status_code=500, detail=e.message)
    return PlainTextResponse(content)

__all__ = [
    'get_multiple_cogs_files',
    'get_preview_url',
    'get_text_content',
]

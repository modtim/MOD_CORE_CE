from ._shared import *  # noqa: F401,F403

from fastapi import APIRouter  # noqa: F811  (local, prefix-free; the aggregator carries prefix/tags/deps)
from services import cogs_task_queue_scheduler_service
from services._base import NotFoundError
public_router = APIRouter()

@public_router.get("/{cog_id}/pdf-view")
async def pdf_view(
    cog_id: int,
    token: Optional[str] = None,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None),
):
    """
    Stream a PDF through the API for inline viewing in iframes.
    Lives on `public_router` (no router-level auth dep) and resolves
    auth inline because iframes can't attach an Authorization header
    — the Dashboard passes the access token in the URL via `?token=`.

    NOTE on dependencies: we do NOT take `minio_client` via
    `Depends(get_tenant_minio_client)` because that resolves through
    `get_current_db_user → get_current_user_claims → oauth2_scheme`,
    which auto-errors with 401 "Not authenticated" when there's no
    Authorization header — exactly the case we're trying to support.
    We instantiate MinioClient inline after validating the query token.
    """
    from fastapi.responses import StreamingResponse
    from keycloak_auth import get_current_user_claims
    from dependencies import get_current_user as resolve_user

    # Auth: header or query param (iframes can't send headers)
    bearer = None
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization[7:]
    elif token:
        bearer = token

    if not bearer:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        claims = get_current_user_claims(bearer)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    current_user = resolve_user(db=db, claims=claims)

    # GSEC R-047: like the other cog read routes, this direct-read path must
    # pass the per-cog view-grant check — tenant-scoped resolution alone lets
    # any intra-tenant user stream any cog's PDF.
    from permissions import can_user_view_cog
    if not can_user_view_cog(db, current_user.id, cog_id):
        raise HTTPException(status_code=404, detail="File not found")

    minio_client = MinioClient(tenant_id=current_user.tenant_id)

    try:
        object_path, cog_name = cogs_task_queue_scheduler_service.resolve_pdf_object_path(
            db, cog_id=cog_id, tenant_id=current_user.tenant_id,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)

    try:
        response = minio_client.client.get_object(minio_client.bucket_name, object_path)
        return StreamingResponse(
            response.stream(8192),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{cog_name}"'},
        )
    except Exception as e:
        logger.error("Error streaming PDF: %s", e)
        raise HTTPException(status_code=500, detail="Error streaming PDF")

__all__ = [
    'pdf_view',
]

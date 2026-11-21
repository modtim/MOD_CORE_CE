from ._shared import *  # noqa: F401,F403

from fastapi import APIRouter  # noqa: F811  (local, prefix-free; the aggregator carries prefix/tags/deps)
from crud import cogs_assets_cogs_files as _acf_crud
from services import cogs_assets_cogs_files_service as _acf_svc
from services import files_service as _fx90_svc  # FX.90 p2b quarantine
try:
    from services import malware_scan_async as _fx90_async  # FX.90 p3a async media scan
except ImportError:
    # CE: no async malware-scan backend ships — guard, not reclassify.
    _fx90_async = None
from services._base import ConflictError, NotFoundError, ServiceError
from utils.usage_enforcement import enforce_storage_quota
from utils.http_errors import internal_http_error
# GY.C16 follow-up: auditor data-content gate on tenant data-content routes.
from dependencies import require_auditor_data_content
router = APIRouter()
service_router = APIRouter()


def _meter_egress_stream(response, chunk_size: int, *, tenant_id: int, context: str):
    """Shared generator for the two server-read-then-send download paths
    (`stream_file`, `download_zip_proxy`): yields the object's bytes while
    counting exactly what is sent, then meters it once bytes are known —
    from `finally`, so this fires on a clean finish, an early `.close()`
    (client disconnect / TestClient generator close), or an exception
    raised mid-iteration alike, always with the REAL byte count, never the
    object's declared/full size.

    Owner decision 09-29: egress that flows through MOD's servers must be
    billed for house AND BYO tenants alike — both call sites read the
    object server-side and re-send it, so `via_mod_proxy=True` always
    (see services/egress_metering.record_egress's docstring for the
    house-vs-BYO/via_mod_proxy split).

    Metering uses its OWN short-lived `database.SessionLocal()` (the same
    pattern `confirm_upload`'s background-thread closure uses above), not
    the caller's request-scoped `db` — that session is kept open by
    FastAPI's `Depends(get_db)` teardown for the entire lifetime of the
    streaming response, and per the "external fetch inside DB transaction"
    hazard, no DB transaction may stay pinned open across a (potentially
    slow) client stream. A metering failure is caught and logged; it must
    never break or truncate the download.
    """
    bytes_sent = 0
    try:
        for chunk in response.stream(chunk_size):
            bytes_sent += len(chunk)
            yield chunk
    finally:
        try:
            response.close()
            response.release_conn()
        except Exception:
            pass
        if bytes_sent > 0:
            try:
                from utils.paid_import import optional_paid
                # egress_metering is a PAID module: on CE there is no egress to
                # meter, so the download just completes without a usage record.
                _egress = optional_paid("services.egress_metering")
                if _egress is not None:
                    from database import SessionLocal
                    meter_db = SessionLocal()
                    try:
                        _egress.record_egress(meter_db, tenant_id, bytes_sent,
                                              via_mod_proxy=True)
                    finally:
                        meter_db.close()
            except Exception:
                logger.exception("egress metering failed (%s)", context)


def _map_acf_service_error(e: ServiceError) -> HTTPException:
    """Gate 2b: translate a services/cogs_assets_cogs_files_service.py typed
    error into the exact HTTPException the pre-migration inline handler body
    raised for that condition (same status code, same message/shape)."""
    if isinstance(e, _acf_svc.NameCollisionError):
        return _name_collision_409(
            code="name_collision", existing=e.existing,
            target_parent_id=e.target_parent_id, incoming_name=e.incoming_name,
        )
    if isinstance(e, _acf_svc.ForbiddenError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message)
    if isinstance(e, NotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=e.message)
    if isinstance(e, _acf_svc.InvalidRequestError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.message)
    if isinstance(e, ConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.details or e.message)
    # FX.90 p2b (Q76=A) — malware quarantine: 423 Locked (the content
    # exists but is locked pending an admin release), never a 403 that
    # would read as a permission failure.
    if isinstance(e, _fx90_svc.QuarantinedArtefactError):
        return HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=e.message,
            headers={"X-Error-Code": "MALWARE_QUARANTINED"},
        )
    if isinstance(e, _acf_svc.ExternalStorageError):
        return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=e.message)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=e.message)

@router.post("/", response_model=schemas.Cog, status_code=status.HTTP_201_CREATED)
def create_cog(
        cog: schemas.CogCreate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
        minio_client: MinioClient = Depends(get_tenant_minio_client)
) -> models.Cog:
    """
    Create a new Cog (folder or placeholder file).
    - **name**: The name of the cog.
    - **cog_type**: The type of cog (e.g., 'master_folder', 'asset_folder', 'placeholder_file').
    - **parent_id**: The ID of the parent cog, if any.

    - **FILE type Cogs cannot be created directly via this endpoint.** Use the `/cogs/upload-file` endpoint for file uploads.
    - **MASTER_FOLDER**: A top-level organizational folder.
    - **ASSET_FOLDER**: A sub-folder for assets.
    - **PLACEHOLDER_FILE**: A file entry that acts as a placeholder, to be filled by an upload or workflow.
    """
    # Prevent creating system-only cog types via this endpoint
    # FILE: Must use /cogs/upload-file endpoint
    # WORKFLOW_*: Created only by system during workflow execution (audit-protected)
    system_only_types = [
        models.CogType.FILE,
        models.CogType.WORKFLOW_FOLDER,
        models.CogType.WORKFLOW_RUN,
        models.CogType.WORKFLOW_STEP,
    ]
    if cog.cog_type in system_only_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{cog.cog_type.value} type Cogs cannot be created directly. "
                   f"FILE types use /cogs/upload-file. WORKFLOW types are system-generated only."
        )

    # Validate parent_id for any cog type
    if cog.parent_id:
        # Tenant-scoped lookup so a non-owner with `can_create_subfolders`
        # granted via /permissions/cogs/{cog_id} can actually pre-flight
        # the parent. The owner-scoped lookup would 404 here and short-
        # circuit the can_user_create_subfolder check below.
        parent_cog = crud.get_cog_by_id_and_tenant(
            db, cog_id=cog.parent_id, tenant_id=current_user.tenant_id
        )
        if not parent_cog:  # Check if parent exists
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Parent Cog not found"
            )
        # Check if user has permission to create subfolders
        if not can_user_create_subfolder(db, current_user.id, cog.parent_id):
            _maybe_audit_xtenant_block(db, current_user, cog.parent_id)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to create subfolders in this folder"
            )
        # Ensure parent is a folder type for any new cog
        if parent_cog.cog_type not in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent Cog must be a folder type (MASTER_FOLDER or ASSET_FOLDER)"
            )
    else:
        # If no parent_id is provided, only MASTER_FOLDER is allowed at the root
        if cog.cog_type != models.CogType.MASTER_FOLDER:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only MASTER_FOLDER Cogs can be created without a parent_id."
            )

    # Create the database record
    # Note: Folders are virtual (database-only). No MinIO folder creation needed.
    # All files are stored flat in MinIO using SHA256 content hashes.
    db_cog = crud.create_cog(db=db, cog=cog, user_id=current_user.id, tenant_id=current_user.tenant_id)

    return db_cog

@router.get("/root", response_model=schemas.Cog)
def get_root_folder(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Return (creating on first use) the tenant's root MASTER_FOLDER — the
    Dashboard's "Home". Files and asset folders live INSIDE it, so the Data page
    has one real, navigable root that can hold files, instead of a no-parent
    level that only accepts master folders (which silently shoved uploaded files
    into an auto-created folder the user never saw)."""
    return _get_or_create_tenant_root_folder(db, current_user)

@router.get("/", response_model=List[schemas.Cog])
def list_cogs(
        parent_id: Optional[int] = None,  # Filter by parent folder
        all_items: bool = False,  # If true, ignore parent_id filter and return all cogs
        search: Optional[str] = None,  # Filter by name
        selectable_inputs_only: bool = False,  # Input pickers: hide contentless FILE cogs (orphaned/failed uploads). Folders + content-backed files still shown.
        include_permission_info: bool = False,
        deleted: bool = False,  # If true, return ONLY soft-deleted (deleted_at IS NOT NULL) cogs — powers the Dashboard "Deleted" filter / restore flow.
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "name",
        order: str = "asc",
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    List all cogs, with options for pagination, filtering, and sorting.
    If all_items=true, returns all cogs regardless of parent_id.
    If include_permission_info=true, each cog gets direct_permission_count.
    If deleted=true, returns ONLY soft-deleted cogs (the "Deleted"/trash view),
    scoped to the tenant; a tenant manager (parent user) or platform admin sees
    every soft-deleted cog in the tenant, a regular user sees only their own.
    """
    from services.assets_cogs_files_service import list_cogs as _list_cogs_svc

    return _list_cogs_svc(
        db,
        current_user=current_user,
        parent_id=parent_id,
        all_items=all_items,
        search=search,
        selectable_inputs_only=selectable_inputs_only,
        include_permission_info=include_permission_info,
        deleted=deleted,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        order=order,
    )

@router.get("/search/recursive", response_model=List[schemas.Cog])
def search_cogs_recursive(
        q: str,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Search all cogs (files and folders) recursively across all folders for the current user's tenant.
    Supports wildcards: * (matches any characters) and ? (matches single character)
    Example: "*.txt" finds all text files, "report_?" finds report_1, report_a, etc.
    """
    from services.assets_cogs_files_service import search_cogs_recursive as _search_svc

    return _search_svc(db, current_user=current_user, q=q)

@router.get("/{cog_id:int}", response_model=schemas.Cog)
def get_cog(
        cog_id: int,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Retrieve details for a single file or folder.
    Requires view permission.
    """
    # Check if user has permission to view this cog
    if not can_user_view_cog(db, current_user.id, cog_id):
        _maybe_audit_xtenant_block(db, current_user, cog_id, http_request)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to view this folder/file"
        )

    # Look up by tenant rather than owner: a non-owner with an explicit
    # /permissions/cogs/{cog_id} grant must still be able to GET. The
    # earlier `owner_id=...` filter rejected exactly that case and
    # returned 404 for granted users.
    db_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if db_cog is None:
        raise HTTPException(status_code=404, detail="Cog not found")

    # Same authoritative format-metadata enrichment as the list endpoint —
    # a single Cog GET should also expose file_extension/mime_type so the
    # client's preview/visualizer doesn't fall back to parsing the
    # rename-mutable display name.
    from services.assets_cogs_files_service import attach_format_metadata

    attach_format_metadata(db, db_cog)
    return db_cog

@router.patch("/{cog_id}", response_model=schemas.Cog)
def update_cog(
        cog_id: int,
        cog_update: schemas.CogUpdate,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Update a cog's metadata (e.g., rename or move).
    Requires 'can_move_files' permission.
    """
    # Check permission
    if not can_user_move_files_in_cog(db, current_user.id, cog_id):
        _maybe_audit_xtenant_block(db, current_user, cog_id, http_request)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to modify this folder/file"
        )

    # Tenant-scoped lookup so a non-owner with `can_move_files` granted
    # via /permissions/cogs/{cog_id} can actually run the PATCH (the
    # owner-scoped lookup would 404 for them).
    db_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if db_cog is None:
        raise HTTPException(status_code=404, detail="Cog not found")

    try:
        return _acf_svc.update_cog(
            db, cog_id=cog_id, db_cog=db_cog, cog_update=cog_update, current_user=current_user,
        )
    except ServiceError as e:
        raise _map_acf_service_error(e)

@router.delete("/{cog_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cog(
        cog_id: int,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Soft delete a file or folder.
    Requires 'can_delete' permission.
    Users cannot delete system-generated folders.

    FIX-PAID-GAPS (owner Q5=B): this route used to carry
    `minio_client: MinioClient = Depends(get_tenant_minio_client)`, which built
    the object-storage client BEFORE the handler ran — so a caller without
    `can_delete` on a room with no browser-facing presigned endpoint got a 500
    (utils/storage._resolve_endpoints fails closed) instead of the 403 the
    permission check below is supposed to answer. The client was never used
    here: this path is soft-delete-only and NEVER touches object storage
    (see the CMMC/ISO 27001 note in `services/cogs_assets_cogs_files_service.delete_cog`
    — physical cleanup is a separate, admin-controlled, retention-aware process).
    Dropping the dependency makes the permission check the first thing that
    runs; authorized behaviour is unchanged.
    """
    # Check permission
    if not can_user_delete_cog(db, current_user.id, cog_id):
        _maybe_audit_xtenant_block(db, current_user, cog_id, http_request)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to delete this folder/file"
        )

    # Tenant-scoped lookup so a non-owner with `can_delete` granted via
    # /permissions/cogs/{cog_id} can actually run the DELETE (the
    # owner-scoped lookup would 404 for them).
    db_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if db_cog is None:
        raise HTTPException(status_code=404, detail="Cog not found")

    try:
        _acf_svc.delete_cog(db, cog_id=cog_id, db_cog=db_cog, current_user=current_user)
    except ServiceError as e:
        raise _map_acf_service_error(e)
    return

@router.post("/bulk-delete", status_code=status.HTTP_200_OK)
def bulk_delete_cogs(
    request: BulkDeleteRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Soft-delete multiple files/folders at once. Skips items the user can't delete."""
    deleted = 0
    skipped = 0
    for cog_id in request.cog_ids:
        try:
            if not can_user_delete_cog(db, current_user.id, cog_id):
                _maybe_audit_xtenant_block(db, current_user, cog_id, http_request)
                skipped += 1
                continue
            # Use the existing tenant-scoped lookup. The previous
            # `crud.get_cog(...)` call referenced a function that doesn't
            # exist on `crud` (only `get_cog_by_id` and
            # `get_cog_by_id_and_tenant` are exported), so this branch
            # raised AttributeError → silently skipped every item. The
            # endpoint has been broken for every caller; surfaced by
            # data-files-launch-bulletproof §M8 bulk-operations-deep.
            db_cog = crud.get_cog_by_id_and_tenant(
                db=db, cog_id=cog_id, tenant_id=current_user.tenant_id
            )
            if not db_cog:
                _maybe_audit_xtenant_block(db, current_user, cog_id, http_request)
                skipped += 1
                continue
            # The Cog column is `is_system_generated`, not
            # `is_system_folder`. Same shape of pre-existing bug.
            if db_cog.is_system_generated:
                skipped += 1
                continue
            crud.delete_cog(db=db, db_cog=db_cog)
            deleted += 1
        except Exception:
            logger.exception(
                "bulk_delete_cogs: per-item failure on cog_id=%s", cog_id
            )
            skipped += 1
    return {"deleted": deleted, "skipped": skipped, "total": len(request.cog_ids)}

@router.post("/{cog_id}/restore", response_model=schemas.Cog)
def restore_cog(
        cog_id: int,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Restore a soft-deleted cog (bug #4 recovery path).

    Clears ``deleted_at`` on the cog and un-soft-deletes its associated File
    rows (re-crediting tenant storage) so a recovered file has its content back.

    Authz (owner / manager): the cog's owner, a tenant manager (parent user),
    or a platform admin. Grant-based access does not apply — a cog's
    ResourcePermission grants are revoked on delete, so a shared user can't
    silently resurrect content.

    - 404 if the cog does not exist or belongs to another tenant.
    - 409 if the cog is not currently soft-deleted.
    """
    try:
        return _acf_svc.restore_cog(
            db, cog_id=cog_id, current_user=current_user, http_request=http_request,
        )
    except ServiceError as e:
        raise _map_acf_service_error(e)

@router.post("/{cog_id}/copy", response_model=schemas.Cog, status_code=status.HTTP_201_CREATED)
def copy_cog(
        cog_id: int,
        copy_request: schemas.CogCopy,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
        minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Create a copy of a cog (file or folder) to a new location.
    If copying a folder, recursively copies all children.
    Requires 'can_upload' permission in destination folder.
    """
    # Get source cog — tenant-scoped, so a same-tenant teammate holding a
    # read grant on a teammate-owned source can copy it (the owner-scoped
    # lookup 404d them). The READ gate right below (can_user_view_cog, the
    # router's own view/download helper) is authoritative; cross-tenant
    # sources stay invisible (404).
    source_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if not source_cog:
        raise HTTPException(status_code=404, detail="Source cog not found")

    # Check permission to read source
    if not can_user_view_cog(db, current_user.id, cog_id):
        _maybe_audit_xtenant_block(db, current_user, cog_id, http_request)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to copy this item"
        )

    # Check permission to write to destination
    dest_parent_id = copy_request.destination_parent_id
    if dest_parent_id is not None:
        if not can_user_upload_to_cog(db, current_user.id, dest_parent_id):
            _maybe_audit_xtenant_block(db, current_user, dest_parent_id, http_request)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to upload to the destination folder"
            )
        # Validate destination parent exists and is a folder — tenant-scoped:
        # the upload gate right above (can_user_upload_to_cog) is authoritative,
        # so a teammate holding an upload grant on a teammate-owned folder must
        # resolve it here (the owner-scoped lookup 404d the copy).
        dest_parent = crud.get_cog_by_id_and_tenant(
            db, cog_id=dest_parent_id, tenant_id=current_user.tenant_id
        )
        if not dest_parent:
            raise HTTPException(status_code=404, detail="Destination parent not found")
        if dest_parent.cog_type not in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Destination parent must be a folder"
            )

    # Name resolution rules (#18, #19):
    #   1. If the caller passed an explicit new_name, honor it.
    #   2. Otherwise default to source_cog.name.
    #   3. If that name collides in the destination AND the source
    #      lives in the same destination folder (copy-into-self), pick
    #      the next available "(copy)", "(copy 2)", … suffix —
    #      copying to the same place is unambiguous.
    #   4. Otherwise (collision but copying to a *different* folder)
    #      return the structured 409 unless replace=True. The
    #      dashboard's collision modal handles the prompt.
    if copy_request.new_name:
        new_name = copy_request.new_name
    else:
        new_name = source_cog.name

    collision = _find_name_collision(
        db,
        name=new_name,
        parent_id=dest_parent_id,
        tenant_id=current_user.tenant_id,
    )
    same_folder = source_cog.parent_id == dest_parent_id
    if collision is not None:
        if copy_request.replace:
            crud.delete_cog(db=db, db_cog=collision)
        elif same_folder and not copy_request.new_name:
            new_name = _next_copy_suffix_name(
                db,
                base_name=source_cog.name,
                parent_id=dest_parent_id,
                tenant_id=current_user.tenant_id,
            )
        else:
            raise _name_collision_409(
                code="name_collision",
                existing=collision,
                target_parent_id=dest_parent_id,
                incoming_name=new_name,
            )

    # Copy the cog based on type
    if source_cog.cog_type in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
        # Copy folder
        new_cog = _copy_folder_recursive(
            db=db,
            source_cog=source_cog,
            dest_parent_id=dest_parent_id,
            new_name=new_name,
            owner_id=current_user.id,
            tenant_id=current_user.tenant_id,
            minio_client=minio_client
        )
    elif source_cog.cog_type == models.CogType.FILE:
        # Copy file
        new_cog = _copy_file(
            db=db,
            source_cog=source_cog,
            dest_parent_id=dest_parent_id,
            new_name=new_name,
            owner_id=current_user.id,
            tenant_id=current_user.tenant_id,
            minio_client=minio_client
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot copy cog of type {source_cog.cog_type}"
        )

    db.commit()
    db.refresh(new_cog)
    return new_cog

def _namespaced_temp_key(user_id, upload_id) -> str:
    """Temp object key, namespaced by the initiating user (GSEC R-032)."""
    return f"uploads/temp/{user_id}/{upload_id}"


def _assert_upload_owned_by(current_user, minio_client, upload_id, *,
                            s3_upload_id: str = "", multipart: bool) -> str:
    """GSEC R-032 — bind a finalize/confirm request to the user who initiated it.

    There is no DB record tying an upload_id to its initiator, so the temp
    object key is namespaced by the initiating user id
    (``uploads/temp/<user_id>/<upload_id>``). On complete/confirm we
    reconstruct that key from the CALLER's id and verify the S3 object (a plain
    PUT) or the in-progress multipart upload actually exists there. A cross-user
    claim reconstructs a key under the attacker's own namespace that was never
    written → 404, so neither existence nor ownership is revealed.

    LEGACY IN-FLIGHT UPLOADS: objects already in flight under the pre-namespaced
    flat key (``uploads/temp/<upload_id>``) are NOT accepted. No multipart-session
    metadata is persisted that would bind a flat key to its initiator, so the flat
    key cannot be attributed to a caller — accepting it would let any intra-tenant
    user finalize it (the exact R-032 hole). Fail-closed: the initiator simply
    re-initiates (uploads are short-lived, so this only affects requests straddling
    the deploy).
    """
    if not upload_id or "/" in upload_id or "\\" in upload_id or upload_id in (".", ".."):
        raise HTTPException(status_code=404, detail="Upload not found")
    bucket = minio_client.bucket_name
    client = minio_client.client
    try:
        if multipart and s3_upload_id:
            res = client._list_multipart_uploads(
                bucket, prefix=f"uploads/temp/{current_user.id}/", max_uploads=1000,
            )
            owned = any(
                getattr(u, "object_name", None) == _namespaced_temp_key(current_user.id, upload_id)
                and getattr(u, "upload_id", None) == s3_upload_id
                for u in (getattr(res, "uploads", None) or [])
            )
        else:
            client.stat_object(bucket, _namespaced_temp_key(current_user.id, upload_id))
            owned = True
    except HTTPException:
        raise
    except Exception:
        owned = False
    if not owned:
        raise HTTPException(status_code=404, detail="Upload not found")
    return _namespaced_temp_key(current_user.id, upload_id)


@router.post("/uploads/initiate")
async def initiate_multipart_upload(
    filename: str,
    parent_id: int,
    file_size: int,
    content_type: str = "application/octet-stream",
    part_size: int = 64 * 1024 * 1024,  # 64MB default chunk size
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Initiate a multipart upload. Returns upload_id and presigned URLs for each part.

    Flow:
    1. Browser calls this with filename + file_size
    2. Gets back upload_id + presigned PUT URLs for each chunk
    3. Browser uploads chunks in parallel directly to MinIO
    4. Browser calls POST /uploads/{upload_id}/complete with part ETags
    5. API assembles, hashes, deduplicates, creates DB records

    Supports resume: if upload fails, call GET /uploads/{upload_id}/parts
    to see which parts already uploaded, then re-request URLs for missing parts.
    """
    from datetime import timedelta
    import math
    import urllib.parse

    # Validate parent folder — tenant-scoped so a same-tenant teammate holding
    # an upload grant on a teammate-owned folder can initiate the multipart
    # upload there (the owner-scoped lookup 404d them). The upload gate right
    # below is authoritative; cross-tenant folders stay invisible (404).
    parent_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=parent_id, tenant_id=current_user.tenant_id
    )
    if not parent_cog:
        raise HTTPException(status_code=404, detail="Parent folder not found")
    if not can_user_upload_to_cog(db, current_user.id, parent_id):
        _maybe_audit_xtenant_block(db, current_user, parent_id)
        raise HTTPException(status_code=403, detail="No upload permission for this folder")
    if parent_cog.cog_type not in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
        raise HTTPException(status_code=400, detail="Parent must be a folder")
    # Q.2: declared size; the bytes actually received are re-checked at hashing.
    enforce_storage_quota(db, current_user.tenant_id, max(0, file_size), user=current_user)

    # Clamp part size: 5MB min (S3 requirement), 5GB max
    part_size = max(5 * 1024 * 1024, min(part_size, 5 * 1024 * 1024 * 1024))
    total_parts = max(1, math.ceil(file_size / part_size))

    # S3 limit: max 10,000 parts — increase part_size if needed
    if total_parts > 10000:
        part_size = math.ceil(file_size / 10000)
        total_parts = math.ceil(file_size / part_size)

    upload_id = str(uuid.uuid4())
    # GSEC R-032: namespace by the initiating user so complete/confirm can
    # verify the caller owns the upload (reconstruct the key from their id).
    temp_object = _namespaced_temp_key(current_user.id, upload_id)

    # Initiate S3 multipart upload
    try:
        # The browser PUTs straight to S3, bypassing every client op that
        # lazily creates the bucket — make sure it exists first.
        minio_client.ensure_bucket_exists()
        from minio.helpers import ObjectWriteResult
        # Use the internal client for the multipart initiation
        result = minio_client.client._create_multipart_upload(
            minio_client.bucket_name, temp_object, {}
        )
        s3_upload_id = result
    except Exception as e:
        logging.error(f"Multipart initiate failed: {e}")
        raise HTTPException(status_code=500, detail="Could not initiate upload")

    # Generate presigned PUT URL for each part
    part_urls = []
    for part_num in range(1, total_parts + 1):
        try:
            # Build presigned URL for this part
            url = minio_client.presigned_client.get_presigned_url(
                "PUT",
                minio_client.bucket_name,
                temp_object,
                expires=timedelta(hours=24),
                extra_query_params={"partNumber": str(part_num), "uploadId": s3_upload_id}
            )
            part_urls.append({"part_number": part_num, "url": url})
        except Exception:
            # Fallback to internal client with URL rewrite
            import re
            url = minio_client.client.get_presigned_url(
                "PUT",
                minio_client.bucket_name,
                temp_object,
                expires=timedelta(hours=24),
                extra_query_params={"partNumber": str(part_num), "uploadId": s3_upload_id}
            )
            # minio_client.external_endpoint is already resolved per-tenant:
            # BYO external_endpoint for BYO tenants, house default otherwise.
            external_endpoint = minio_client.external_endpoint
            protocol = os.getenv("PROTOCOL", "https")
            url = re.sub(r'https?://[^/]+/', f'{protocol}://{external_endpoint}/', url, count=1)
            part_urls.append({"part_number": part_num, "url": url})

    # Store upload metadata for resume support
    upload_meta = models.UploadJob if hasattr(models, 'UploadJob') else None
    # For now, store in a simple way — the upload_id and s3_upload_id are the key
    # The browser persists this in localStorage for resume

    return {
        "upload_id": upload_id,
        "s3_upload_id": s3_upload_id,
        "bucket": minio_client.bucket_name,
        "object_name": temp_object,
        "part_size": part_size,
        "total_parts": total_parts,
        "file_size": file_size,
        "part_urls": part_urls,
    }

@router.post("/uploads/{upload_id}/complete", response_model=schemas.Cog, status_code=status.HTTP_201_CREATED)
async def complete_multipart_upload(
    upload_id: str,
    filename: str,
    parent_id: int,
    file_size: int,
    s3_upload_id: str,
    parts: List[Dict[str, Any]],  # [{"part_number": 1, "etag": "..."}, ...]
    content_type: str = "application/octet-stream",
    existing_cog_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Complete a multipart upload. Assembles parts, hashes, deduplicates, creates DB records.
    """
    import hashlib
    import tempfile

    # GSEC R-032: bind this finalize to the initiating user (404 otherwise) and
    # use the caller-namespaced temp key for the S3 ops below.
    temp_object = _assert_upload_owned_by(
        current_user, minio_client, upload_id,
        s3_upload_id=s3_upload_id, multipart=True,
    )

    # Step 1: Tell MinIO to assemble the parts
    try:
        from minio.datatypes import Part
        minio_parts = [Part(p["part_number"], p["etag"]) for p in sorted(parts, key=lambda x: x["part_number"])]
        minio_client.client._complete_multipart_upload(
            minio_client.bucket_name, temp_object, s3_upload_id, minio_parts
        )
    except Exception as e:
        logging.error(f"Multipart complete failed: {e}")
        raise HTTPException(status_code=500, detail="Could not assemble upload")

    # Step 2: Hash the assembled file (streaming 8MB chunks — no memory pressure)
    # B111 CE fallback: no async malware-scan backend ships on CE (`_fx90_async`
    # is None), so there is no streaming scanner for this large-media path.
    # B111b (reviewer FAIL on B111): the first cut buffered the whole file in a
    # `bytearray` before `malware_scan.max_bytes` was even consulted — an OOM
    # risk for multi-GB CE uploads. Spool to a temp file on disk instead
    # (ONLY on CE — SaaS never allocates this), then scan it with the
    # existing streaming-from-disk scanner (`malware_scan.scan_file()`, the
    # same one `gate_artefact_malware_scan()` uses for downloaded artefacts)
    # so peak process memory stays one 8MB chunk regardless of file size.
    _ce_scan_tmp = None
    if _fx90_async is None:
        _ce_scan_tmp = tempfile.NamedTemporaryFile(prefix="b111-ce-scan-", delete=False)
    try:
        response = minio_client.client.get_object(minio_client.bucket_name, temp_object)
        sha256 = hashlib.sha256()
        actual_size = 0
        for chunk in response.stream(8 * 1024 * 1024):
            sha256.update(chunk)
            actual_size += len(chunk)
            if _ce_scan_tmp is not None:
                _ce_scan_tmp.write(chunk)
        response.close()
        response.release_conn()
        content_hash = sha256.hexdigest()
    except Exception as e:
        logger.error("Failed to hash uploaded file: %s", e)
        if _ce_scan_tmp is not None:
            try:
                _ce_scan_tmp.close()
            finally:
                try:
                    os.remove(_ce_scan_tmp.name)
                except OSError:
                    pass
        raise HTTPException(status_code=500, detail="Failed to hash uploaded file")
    else:
        if _ce_scan_tmp is not None:
            _ce_scan_tmp.close()

    # Step 2.5 (B111, CE only): scan BEFORE storing — before the temp object
    # is renamed into its permanent content-addressed path and before any
    # ContentBlob/Cog/Version/File row is created, same as files.py's inline
    # gate (files.py's docstring: "scan BEFORE storing... because the
    # security-audit write commits" — the gate's own audit row is committed
    # inside the call, so scanning after any db.add() above would leak those
    # rows even though THIS function's own db.commit() below never runs).
    # Same fail-safe semantics as files.py: infected -> 422 (temp object
    # removed, nothing stored); scan error -> fail_closed (default) -> 503,
    # never treated as clean (this includes an over-cap artefact: scan_file()
    # measures the SPOOLED file's real on-disk size via os.path.getsize —
    # never the client-declared `file_size` param — and reports it as the
    # same kind of scan "error", which flows through fail_closed/fail_open
    # exactly like a clamd outage); no-op while malware_scan.enabled is
    # False. SaaS (the paid async-scan-queue module present) is untouched —
    # `_fx90_async` is not None there, so this whole branch is skipped and
    # the large-media upload takes the existing scan-pending + async-queue
    # path lower down in this function.
    if _fx90_async is None:
        try:
            _fx90_svc.gate_artefact_malware_scan(
                db, path=_ce_scan_tmp.name, source="ce_multipart_fallback",
                artefact=filename, user=current_user,
                tenant_id=current_user.tenant_id,
            )
        except _fx90_svc.MalwareInfectedError as e:
            try:
                minio_client.client.remove_object(minio_client.bucket_name, temp_object)
            except Exception:
                pass
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.message
            )
        except _fx90_svc.MalwareScanUnavailableError as e:
            try:
                minio_client.client.remove_object(minio_client.bucket_name, temp_object)
            except Exception:
                pass
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=e.message
            )
        finally:
            try:
                os.remove(_ce_scan_tmp.name)
            except OSError:
                pass

    # Step 3: Dedup check + rename
    file_extension = ""
    if filename and '.' in filename:
        file_extension = os.path.splitext(filename)[1].lstrip('.').lower()

    storage_scheme = os.getenv('STORAGE_SCHEME', 's3')
    bucket = minio_client.bucket_name
    storage_path = f"{storage_scheme}://{bucket}/{content_hash}"

    existing_blob = _acf_crud.get_content_blob_by_id_and_tenant(
        db, blob_id=content_hash, tenant_id=current_user.tenant_id
    )
    is_new_blob = existing_blob is None

    if existing_blob:
        # Dedup: delete temp
        try:
            minio_client.client.remove_object(minio_client.bucket_name, temp_object)
        except Exception:
            pass
        logging.info(f"Upload deduped: {filename} -> existing {content_hash[:16]}...")
    else:
        # Q.2: enforce on the bytes actually assembled, not the declared size.
        try:
            enforce_storage_quota(db, current_user.tenant_id, actual_size, user=current_user)
        except HTTPException:
            try:
                minio_client.client.remove_object(minio_client.bucket_name, temp_object)
            except Exception:
                pass
            raise
        # Rename temp to content hash
        try:
            from minio.commonconfig import CopySource
            minio_client.client.copy_object(
                minio_client.bucket_name, content_hash,
                CopySource(minio_client.bucket_name, temp_object),
            )
            minio_client.client.remove_object(minio_client.bucket_name, temp_object)
        except Exception as e:
            logger.error("Storage rename failed: %s", e)
            raise HTTPException(status_code=500, detail="Storage rename failed")

        existing_blob = models.ContentBlob(
            id=content_hash,
            tenant_id=current_user.tenant_id,
            size_bytes=actual_size,
            mime_type=content_type,
            file_extension=file_extension,
            storage_path=storage_path,
        )
        db.add(existing_blob)
        db.flush()

    # Step 4: Create Cog + Version + File records
    target_cog = None
    if existing_cog_id:
        # Tenant-scoped + the router's upload gate: a same-tenant teammate
        # completing an upload into a teammate-owned placeholder 404d through
        # the owner-scoped lookup (and silently created a duplicate cog). The
        # owner still passes via the derived-owner rule; cross-tenant cogs stay
        # invisible (404).
        target_cog = crud.get_cog_by_id_and_tenant(
            db, cog_id=existing_cog_id, tenant_id=current_user.tenant_id
        )
        if not target_cog:
            raise HTTPException(
                status_code=404, detail="Existing cog not found for this tenant"
            )
        if not can_user_upload_to_cog(db, current_user.id, existing_cog_id):
            _maybe_audit_xtenant_block(db, current_user, existing_cog_id)
            raise HTTPException(
                status_code=403, detail="No upload permission for this cog"
            )
        if target_cog.cog_type == models.CogType.PLACEHOLDER_FILE:
            target_cog.cog_type = models.CogType.FILE
            db.flush()

    if not target_cog:
        cog_create_schema = schemas.CogCreate(
            name=filename,
            cog_type=models.CogType.FILE,
            parent_id=parent_id
        )
        target_cog = crud.create_cog(db=db, cog=cog_create_schema, user_id=current_user.id, tenant_id=current_user.tenant_id)

    existing_blob.cog_id = target_cog.id
    db.add(existing_blob)
    db.flush()

    current_version_count = _acf_crud.count_versions_for_cog(db, target_cog.id)

    new_version = models.Version(
        cog_id=target_cog.id,
        tenant_id=current_user.tenant_id,
        content_id=existing_blob.id,
        version_number=current_version_count + 1,
        name=f"v{current_version_count + 1}",
        author_user_id=current_user.id
    )
    db.add(new_version)
    db.flush()
    target_cog.latest_version_id = new_version.id

    db_file = models.File(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        cog_id=target_cog.id,
        name=filename,
        size_bytes=actual_size,
        mime_type=content_type,
        storage_path=storage_path
    )
    db.add(db_file)
    # FX.90 p3a (Q76=A): the multipart flow IS the large-media path, so every
    # assembled upload at/above `malware_scan.async_above_bytes` is stored
    # scan-pending and queued to the async streaming scanner (no inline scan
    # ever ran here — the bytes never fit the request handler). Same
    # transaction as the Cog/Version/File rows above. The hook is a no-op for
    # sub-threshold uploads and while malware_scan.enabled is False, so
    # disabled installs stay byte-for-byte unchanged.
    # B111: CE ships no async malware-scan backend, so large multipart
    # uploads on CE are instead scanned inline, earlier in this function
    # (Step 2.5, right after hashing — before any storage/DB write) via the
    # same gate_upload_malware_scan() files.py's small-upload path uses. By
    # the time we reach this point an infected/erroring CE upload has
    # already 422/503'd and returned — this branch is SaaS-only and
    # byte-identical to before B111.
    if _fx90_async is not None:
        _fx90_async.queue_large_upload(
            db, cog_id=target_cog.id, tenant_id=current_user.tenant_id,
            user_id=current_user.id, size_bytes=actual_size)
    db.commit()
    db.refresh(target_cog)

    # remove-minio-versitygw Gate 4 (4.1) — API-side storage accounting for the
    # multipart finalize path (its own File-creation site). Only add bytes when
    # a NEW content_blobs row was created above (`is_new_blob`) — dedup (the
    # `if existing_blob:` branch) adds nothing. Non-fatal.
    try:
        from utils.paid_import import optional_paid
        # egress_metering is a PAID module: on CE there is no storage
        # accounting to record, so the accounting step is simply skipped.
        _egress = optional_paid("services.egress_metering")
        if _egress is not None:
            _egress.apply_storage_change(db, current_user.tenant_id,
                                         new_blob_bytes=actual_size if is_new_blob else 0)
    except Exception:
        logger.exception("storage accounting failed for multipart upload=%s", upload_id)

    logging.info(f"Multipart upload complete: {filename} -> {content_hash[:16]}... ({actual_size/(1024*1024):.1f} MB)")

    # remove-minio-versitygw Gate 3 (D4) — multipart finalize is its own
    # object-creation path (does not go through confirm-upload); emit the
    # ObjectCreated file_event so large uploads fire "on file upload" triggers.
    try:
        from services.triggers.watchers import emit_object_event
        emit_object_event(db, tenant_id=current_user.tenant_id,
                          key=filename, event_type="created")
    except Exception:
        logger.exception("file_event emission failed for multipart upload=%s", upload_id)

    return target_cog

@router.post("/request-upload")
async def request_upload(
    filename: str,
    parent_id: Optional[int] = None,
    upload_id: str = "",
    file_size: int = 0,
    content_type: str = "application/octet-stream",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Request a presigned PUT URL for direct S3 upload.

    Flow:
    1. Browser generates a temporary upload_id (UUID)
    2. Calls this endpoint — gets a presigned PUT URL
    3. Uploads directly to MinIO (file bytes never touch the API)
    4. Calls POST /confirm-upload — API hashes the file in MinIO, creates DB records
    5. API renames the object from upload_id to content_hash (dedup)

    Works for files of any size (KB to TB) since the browser streams directly to S3.
    """
    from datetime import timedelta

    # If no parent_id, find — or auto-create — the tenant's root folder.
    if not parent_id:
        parent_id = _get_or_create_tenant_root_folder(db, current_user).id

    if not upload_id:
        upload_id = str(uuid.uuid4())

    # Validate parent folder — tenant-scoped so a same-tenant teammate holding
    # an upload grant on a teammate-owned folder can request the presigned PUT
    # there (the owner-scoped lookup 404d them). The upload gate right below is
    # authoritative; cross-tenant folders stay invisible (404).
    parent_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=parent_id, tenant_id=current_user.tenant_id
    )
    if not parent_cog:
        raise HTTPException(status_code=404, detail="Parent folder not found")
    if not can_user_upload_to_cog(db, current_user.id, parent_id):
        _maybe_audit_xtenant_block(db, current_user, parent_id)
        raise HTTPException(status_code=403, detail="No upload permission for this folder")
    if parent_cog.cog_type not in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
        raise HTTPException(status_code=400, detail="Parent must be a folder")
    # Q.2: declared size; the bytes actually received are re-checked at hashing.
    enforce_storage_quota(db, current_user.tenant_id, max(0, file_size), user=current_user)

    # Upload to a temp path using the upload_id — will be renamed after hashing.
    # GSEC R-032: namespace by the initiating user so confirm-upload can verify
    # the caller owns the upload.
    object_name = _namespaced_temp_key(current_user.id, upload_id)

    try:
        # The browser PUTs straight to S3, bypassing every client op that
        # lazily creates the bucket — make sure it exists first.
        minio_client.ensure_bucket_exists()
        upload_url = minio_client.presigned_client.presigned_put_object(
            bucket_name=minio_client.bucket_name,
            object_name=object_name,
            expires=timedelta(seconds=7200),  # 2 hours for large files
        )
    except Exception:
        # Fallback: internal client with URL rewrite
        try:
            upload_url = minio_client.client.presigned_put_object(
                bucket_name=minio_client.bucket_name,
                object_name=object_name,
                expires=timedelta(seconds=7200),
            )
            import re
            # minio_client.external_endpoint is already resolved per-tenant:
            # BYO external_endpoint for BYO tenants, house default otherwise.
            external_endpoint = minio_client.external_endpoint
            protocol = os.getenv("PROTOCOL", "https")
            upload_url = re.sub(r'https?://[^/]+/', f'{protocol}://{external_endpoint}/', upload_url, count=1)
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"Could not generate upload URL: {e2}")

    return {
        "upload_url": upload_url,
        "upload_id": upload_id,
        "bucket": minio_client.bucket_name,
        "object_name": object_name,
    }

@router.post("/uploads/check")
async def check_upload_by_hash(
    content_hash: str,
    size: int = 0,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Pre-flight upload dedupe (#20). The dashboard hashes the file in
    a Web Worker, calls this endpoint with the content_hash and size,
    and on `{exists: true}` skips the byte transfer entirely — the
    follow-up call goes to /cogs/uploads/from-hash instead of
    request-upload + presigned PUT + confirm-upload.

    Tenancy: only ContentBlobs owned by the current tenant return
    `exists: true`. Other tenants' content with the same hash never
    leaks across.
    """
    if not content_hash or len(content_hash) < 16:
        raise HTTPException(status_code=400, detail="content_hash required")
    return _acf_svc.check_upload_by_hash(
        db, content_hash=content_hash, tenant_id=current_user.tenant_id,
    )

@router.post("/uploads/from-hash", response_model=schemas.Cog, status_code=status.HTTP_201_CREATED)
async def create_cog_from_existing_hash(
    filename: str,
    content_hash: str,
    parent_id: Optional[int] = None,
    content_type: str = "application/octet-stream",
    replace: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Create a new Cog/Version/File record linked to existing content
    (#20). Called only after /cogs/uploads/check returned exists=true.
    Skips the upload + hash steps entirely — bytes never re-transfer.

    Tenancy: the ContentBlob lookup is filtered by current tenant, so
    this endpoint cannot grant access to another tenant's content
    even if the caller knows the hash.
    """
    if not filename:
        raise HTTPException(status_code=400, detail="filename required")
    if not content_hash:
        raise HTTPException(status_code=400, detail="content_hash required")

    try:
        return _acf_svc.create_cog_from_existing_hash(
            db,
            filename=filename,
            content_hash=content_hash,
            parent_id=parent_id,
            content_type=content_type,
            replace=replace,
            current_user=current_user,
        )
    except ServiceError as e:
        raise _map_acf_service_error(e)

@router.post("/confirm-upload")
async def confirm_upload(
    filename: str,
    parent_id: Optional[int] = None,
    replace: bool = False,
    upload_id: str = "",
    file_size: int = 0,
    content_type: str = "application/octet-stream",
    s3_upload_id: str = "",
    parts_json: str = "[]",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
):
    """Confirm an upload — hashes + registers the file in-process.

    No worker dispatch, no STS handoff, no image pull. The API
    service's MinIO client (Vault-loaded service credentials) does
    the streaming hash and content-hash rename directly. The 2026-
    06-01 task=6575 AccessDenied failure class (and round-14's
    empty-triplet repro before it) is eliminated for this code path
    because we never ship STS creds to anyone.

    Returns the same shape the dashboard already polls:
    ``{task_id, status, message}``. The task row is inserted so the
    dashboard's existing ``/workers/tasks/<task_id>/status`` poll
    works unchanged. The work runs in a background thread that
    updates the task row when done.

    For small files (<8 MB) we run synchronously — the hash is
    faster than the thread spawn + poll round-trip. For larger
    files we hand off to a thread so the POST response stays snappy.
    """
    # Audit: content upload (TPN CS-7, CMMC AU-2)
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="CONTENT_UPLOAD",
            details={"filename": filename, "file_size": file_size, "parent_id": parent_id},
        ))
        db.commit()
    except Exception:
        pass

    # If no parent_id, find — or auto-create — the tenant's root folder.
    if not parent_id:
        parent_id = _get_or_create_tenant_root_folder(db, current_user).id

    # GSEC R-032: bind this confirm to the initiating user. Reconstruct the
    # namespaced temp key from the CALLER's id and verify the object exists there
    # (404 otherwise). Runs before the quota gate / name-collision replace so a
    # cross-user confirm can't claim someone else's upload. An empty upload_id
    # has no object to claim, so the legacy flat key is used (the hash step
    # fails on the missing object, as before).
    if upload_id:
        temp_object = _assert_upload_owned_by(
            current_user, minio_client, upload_id,
            s3_upload_id=s3_upload_id, multipart=bool(s3_upload_id),
        )
    else:
        temp_object = f"uploads/temp/{upload_id}"

    # Bug-2: name-collision prompt. A same-named, non-deleted sibling in the
    # target folder must ask the user (rename / replace / cancel) via the
    # structured 409 the dashboard collision modal already handles for
    # move/copy/rename — not silently create a duplicate. `replace=true` (the
    # modal's Replace button) soft-deletes the existing item first so the upload
    # takes its place. Bug-B: snapshot its grants first so the replacement keeps
    # the same shares (else everyone with access silently loses it).
    # Q.2: gate before `replace` soft-deletes the existing file.
    enforce_storage_quota(db, current_user.tenant_id, max(0, file_size), user=current_user)

    _replace_perms = []
    _collide = _find_name_collision(
        db, name=filename, parent_id=parent_id, tenant_id=current_user.tenant_id
    )
    if _collide:
        if replace:
            _replace_perms = _snapshot_cog_permissions(db, _collide.id)
            crud.delete_cog(db=db, db_cog=_collide)
        else:
            raise _name_collision_409(
                code="name_collision", existing=_collide,
                target_parent_id=parent_id, incoming_name=filename,
            )

    # Insert an ApiTaskQueue row purely for status-polling continuity.
    # The dashboard polls /workers/tasks/<id>/status; that endpoint
    # reads from this row. Status starts RUNNING (no PENDING — the
    # work begins immediately in this API process). We leave
    # `claimed_by_worker` NULL because it's an FK into
    # `worker_licenses.worker_name` — there's no enrolled worker
    # for the "in-process" pseudo-worker, and inventing one would
    # require seeding a row in the licenses table. NULL is fine;
    # the polling endpoint reads `status` and `result_payload`, not
    # the claim identity.
    task = models.ApiTaskQueue(
        workflow_run_id=None,
        node_id=f"file_hash_{upload_id[:8]}",
        tenant_id=current_user.tenant_id,
        queue_name="api",  # marker: ran in-process, not on any worker queue
        priority=10,
        task_type="processing",
        task_payload={
            "node_kind": "file_hasher_inproc",
            "upload_id": upload_id,
            "filename": filename,
            "file_size": file_size,
        },
        status=models.ApiTaskStatus.RUNNING,
        claimed_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        max_attempts=1,
        output_namespace="attempt_0",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id

    # Closure runs in a background thread. Each thread gets its own
    # DB session (SQLAlchemy sessions are not thread-safe). Errors
    # are captured into the task row's error_message so the
    # dashboard's polling surfaces them.
    from services.file_processing import hash_and_register_upload

    def _run_inproc():
        from database import SessionLocal
        local_db = SessionLocal()
        try:
            result = hash_and_register_upload(
                db=local_db,
                upload_id=upload_id,
                filename=filename,
                parent_id=parent_id,
                file_size=file_size,
                content_type=content_type,
                tenant_id=current_user.tenant_id,
                user_id=current_user.id,
                s3_upload_id=s3_upload_id,
                parts_json=parts_json,
                temp_object=temp_object,
            )
            # Bug-B: re-apply the replaced file's grants onto the new cog.
            if _replace_perms and result and result.get("cog_id"):
                _apply_permission_snapshot(local_db, result["cog_id"], _replace_perms)
            row = local_db.query(models.ApiTaskQueue).filter(
                models.ApiTaskQueue.id == task_id,
            ).first()
            if row:
                row.status = models.ApiTaskStatus.COMPLETED
                row.completed_at = datetime.now(timezone.utc)
                row.result_payload = result
                local_db.commit()
            # remove-minio-versitygw Gate 3 (D4) — emit the ObjectCreated
            # file_event API-side now that the object is confirmed in storage,
            # so a bound "on file upload" workflow schedules. This replaces the
            # MinIO bucket-notification webhook and works for versitygw/BYO-S3
            # too. Non-fatal: a trigger failure must not fail the upload.
            try:
                from services.triggers.watchers import emit_object_event
                emit_object_event(local_db, tenant_id=current_user.tenant_id,
                                  key=filename, event_type="created")
            except Exception:
                logger.exception("file_event emission failed for upload=%s", upload_id)
        except Exception as e:
            logger.exception(
                "in-process file hash failed for upload=%s: %s",
                upload_id, e,
            )
            try:
                row = local_db.query(models.ApiTaskQueue).filter(
                    models.ApiTaskQueue.id == task_id,
                ).first()
                if row:
                    row.status = models.ApiTaskStatus.FAILED
                    row.completed_at = datetime.now(timezone.utc)
                    row.error_message = f"file hash failed: {e}"
                    local_db.commit()
            except Exception:
                logger.exception("failed to mark task=%d FAILED", task_id)
        finally:
            local_db.close()

    # Below 8 MB the streaming hash takes <100 ms — cheaper to run
    # synchronously than to spawn a thread and have the client poll.
    # Above that, hand off so the POST returns immediately.
    if file_size < 8 * 1024 * 1024:
        _run_inproc()
    else:
        import threading
        threading.Thread(target=_run_inproc, daemon=True,
                         name=f"file-hash-{task_id}").start()

    logging.info(
        "File hash task %d started in-process for upload %s "
        "(size=%d bytes, mode=%s)",
        task_id, upload_id, file_size,
        "sync" if file_size < 8 * 1024 * 1024 else "async-thread",
    )
    return {"task_id": task_id, "status": "processing",
            "message": "File is being hashed and registered"}

@router.post("/upload-file", response_model=schemas.Cog, status_code=status.HTTP_201_CREATED)
async def upload_file(
        file: UploadFile = File(...),
        name: Optional[str] = Form(None),  # Use Form for optional string fields in multipart
        parent_id: Optional[int] = Form(None),  # Use Form for optional int fields in multipart
        existing_cog_id: Optional[int] = Form(None),  # Optional ID for an existing placeholder cog
        replace: bool = Form(False),  # Bug-2: overwrite a same-named sibling instead of 409
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
        minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Uploads a file directly to MinIO storage and creates/updates corresponding
    Cog, ContentBlob, and Version records. This endpoint is the unified
    entry point for all file uploads.

    - If an `existing_cog_id` (of type `PLACEHOLDER_FILE`) is provided, that placeholder
      Cog will be filled with the uploaded file content and its type changed to `FILE`.
    - If `existing_cog_id` is NOT provided, a new `FILE` type Cog will be created.
    - This endpoint also handles creating new versions for existing `FILE` type Cogs.
    """
    # FX.110 (owner decision Q97=A) — outside collaborators: a per-grant
    # `view_only` restriction on this external user's grant(s) on the
    # target folder blocks the upload (403 EXTERNAL_VIEW_ONLY). Internal
    # users pass; root-level uploads are already refused below by the
    # normal upload-capability check (external users hold no root grant).
    if parent_id is not None:
        try:
            from services import outside_collaborators as _fx110
            _fx110_parent = crud.get_cog_by_id_and_tenant(
                db, cog_id=parent_id, tenant_id=current_user.tenant_id)
            if _fx110_parent is not None:
                _fx110.require_not_view_only_write(
                    db, current_user, _fx110_parent, action="upload")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — never 500 on a gate read;
            pass  # the normal upload-capability check still applies

    enforce_storage_quota(db, current_user.tenant_id, file.size or 0, user=current_user)
    db_cog = None
    if existing_cog_id:
        # Tenant-scoped fetch + the router's own upload-write gate
        # (permissions.can_user_upload_to_cog, the same helper the folder check
        # below and the FX.110 gate use). The old owner-scoped lookup 404d a
        # same-tenant teammate holding an upload grant on the placeholder
        # (directly or inherited from its folder); the owner still passes via
        # the derived-owner rule, and a cross-tenant cog is never visible here.
        db_cog = crud.get_cog_by_id_and_tenant(
            db, cog_id=existing_cog_id, tenant_id=current_user.tenant_id
        )

        if not db_cog:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Existing Cog not found."
            )
        if not can_user_upload_to_cog(db, current_user.id, existing_cog_id):
            _maybe_audit_xtenant_block(db, current_user, existing_cog_id)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to upload files to this cog"
            )
        # If it's a placeholder, we'll fill it. If it's an existing FILE, we'll create a new version.
        if db_cog.cog_type not in [models.CogType.PLACEHOLDER_FILE, models.CogType.FILE]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot upload file to Cog type {db_cog.cog_type}. Must be PLACEHOLDER_FILE or FILE."
            )

        # If a placeholder is being filled, its name and parent_id are already set.
        # Allow overriding name if provided, but parent_id should ideally match or be implicit.
        if not name:
            name = db_cog.name
        # If parent_id is provided for an existing cog, validate it's the same or raise error
        if parent_id is not None and db_cog.parent_id != parent_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change parent_id when updating an existing Cog via file upload. Use PATCH /cogs/{cog_id} for moving."
            )
        parent_id = db_cog.parent_id  # Inherit parent from existing cog

    if not name:
        name = file.filename if file.filename else str(uuid.uuid4())

    # Bug-1: a file needs a parent folder, but uploading at the root must NOT
    # error — default to the tenant root folder ("Home"), matching confirm-upload
    # and the presigned flow, so a file dropped at the top level lands in the
    # visible root folder instead of failing.
    if parent_id is None:
        parent_id = _get_or_create_tenant_root_folder(db, current_user).id

    # Tenant-scoped: the write gate right below (can_user_upload_to_cog) is
    # authoritative. The owner-scoped lookup 404d a same-tenant teammate
    # holding an upload grant on a teammate-owned folder (and, via
    # `parent_id = db_cog.parent_id` above, on any placeholder fill).
    parent_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=parent_id, tenant_id=current_user.tenant_id
    )
    if not parent_cog:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Parent Cog not found"
        )
    # Check if user has permission to upload to this folder
    if not can_user_upload_to_cog(db, current_user.id, parent_id):
        _maybe_audit_xtenant_block(db, current_user, parent_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to upload files to this folder"
        )
    if parent_cog.cog_type not in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Parent Cog must be a folder type (MASTER_FOLDER or ASSET_FOLDER)"
        )

    # Bug-2: prompt on a same-named sibling for NEW uploads (the existing_cog_id
    # path is an intentional placeholder-fill / new-version, so it's exempt).
    # Bug-B: snapshot the replaced cog's grants so the new one keeps them.
    _replace_perms = []
    if not existing_cog_id:
        _collide = _find_name_collision(
            db, name=name, parent_id=parent_id, tenant_id=current_user.tenant_id
        )
        if _collide:
            if replace:
                _replace_perms = _snapshot_cog_permissions(db, _collide.id)
                crud.delete_cog(db=db, db_cog=_collide)
            else:
                raise _name_collision_409(
                    code="name_collision", existing=_collide,
                    target_parent_id=parent_id, incoming_name=name,
                )

    # Content Hash Generation (for deduplication)
    sha256_hash = hashlib.sha256()
    # Read file in chunks to avoid loading large files into memory
    for chunk in iter(lambda: file.file.read(4096), b""):
        sha256_hash.update(chunk)
    content_blob_id = sha256_hash.hexdigest()
    await file.seek(0) # Reset file stream to the beginning for upload


    # Note: Folders are virtual (database-only). No MinIO folder creation needed.
    # Files are stored flat in MinIO using SHA256 content hashes.

    # Check if content blob with this ID already exists FOR THIS TENANT
    # Each tenant gets their own copy of files for proper isolation
    content_blob = _acf_crud.get_content_blob_by_id_and_tenant(
        db, blob_id=content_blob_id, tenant_id=current_user.tenant_id
    )
    is_new_blob = content_blob is None

    if content_blob is None:
        # FLAT STORAGE: Files stored as pure SHA256 hash (no extension) in MinIO
        # Example: s3://tenant-1-files/abc123def456...
        # Extension stored in database for processing tools and downloads

        # Extract file extension from original filename (lowercase, no dot)
        file_extension = ""
        if file.filename and '.' in file.filename:
            file_extension = os.path.splitext(file.filename)[1].lstrip('.').lower()

        # Pure hash storage - no extension in MinIO path
        minio_object_name = content_blob_id

        try:
            storage_path = await minio_client.upload_file(
                object_name=minio_object_name,
                file_stream=file.file,
                file_size=file.size,
                content_type=file.content_type
            )
        except Exception as e:
            logger.error("Failed to upload file to storage: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload file to storage"
            )

        # Create ContentBlob record with extension as metadata
        content_blob = ContentBlob(
            id=content_blob_id,
            tenant_id=current_user.tenant_id,
            size_bytes=file.size,
            mime_type=file.content_type,
            file_extension=file_extension,  # Store extension in DB, not in path
            storage_path=storage_path,
        )


    target_cog = db_cog  # Use the retrieved cog if existing_cog_id was provided

    if not target_cog:  # If no existing cog was provided, create a new one
        # Create a new Cog (logical file entity)
        cog_create_schema = schemas.CogCreate(
            name=name,
            cog_type=CogType.FILE,  # This endpoint creates FILE type if no placeholder
            parent_id=parent_id
        )
        # --- THIS IS THE FIX ---
        target_cog = crud.create_cog(db=db, cog=cog_create_schema, user_id=current_user.id, tenant_id=current_user.tenant_id)
    else:
        # If existing_cog_id was provided, update the cog_type if it was a placeholder
        if target_cog.cog_type == models.CogType.PLACEHOLDER_FILE:
            target_cog.cog_type = models.CogType.FILE
            db.add(target_cog)  # Mark for update
            db.flush()  # Flush to ensure target_cog updates are in session

    # Now update the content_blob_create with the actual cog_id
    content_blob.cog_id = target_cog.id
    db.add(content_blob)  # Add the ContentBlob to the session
    db.flush()

    # Create File record for workflow compatibility
    # This ensures workflows can find File records linked to this Cog
    db_file = models.File(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        cog_id=target_cog.id,
        name=name,
        size_bytes=file.size,
        mime_type=file.content_type,
        storage_path=content_blob.storage_path
    )
    db.add(db_file)

    # Update tenant's storage usage — only when a NEW content_blobs row was
    # created above (`is_new_blob`); a dedup'd upload onto an existing hash
    # adds nothing (the blob already counts). Previously this reset the
    # counter to 0 then unconditionally added `file.size` on every upload,
    # including dedups — removed.
    from utils.paid_import import optional_paid
    # egress_metering is a PAID module: on CE there is no storage accounting to
    # record, so the accounting step is simply skipped (the upload itself is CE).
    _egress = optional_paid("services.egress_metering")
    if _egress is not None:
        _egress.apply_storage_change(db, current_user.tenant_id,
                                     new_blob_bytes=file.size if is_new_blob else 0)

    try:
        db.flush()
    except Exception as e:
        # Check if it's a duplicate storage_path error
        if "duplicate key" in str(e).lower() and "storage_path" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This file already exists at this location. The file '{name}' with the same content is already uploaded to this folder."
            )
        else:
            raise

    # Determine version number:
    # If existing_cog_id was provided, increment version. Otherwise, start at 1.
    next_version_number = 1
    if target_cog.latest_version:  # Check target_cog, not db_cog, for latest version
        next_version_number = target_cog.latest_version.version_number + 1

    # Create Version record
    version_create = models.Version(
        cog_id=target_cog.id,
        tenant_id=current_user.tenant_id,
        content_id=content_blob.id,  # Link to the ContentBlob
        version_number=next_version_number,
        name=name,  # Use the name of the file
        author_user_id=current_user.id,
        created_at=datetime.now()
    )
    db.add(version_create)
    db.flush()

    # Update Cog with latest_version_id
    target_cog.latest_version_id = version_create.id
    db.add(target_cog)
    db.commit()
    db.refresh(target_cog)  # Refresh to get latest_version relationship populated

    # Bug-B: a replace re-applies the replaced file's grants onto the new cog.
    if _replace_perms:
        _apply_permission_snapshot(db, target_cog.id, _replace_perms)

    # remove-minio-versitygw Gate 3 (D4) — the direct upload path is a distinct
    # object-creation point from the presigned confirm-upload flow; emit the
    # ObjectCreated file_event here too so "on file upload" triggers fire
    # regardless of which upload path the client used. Non-fatal.
    try:
        from services.triggers.watchers import emit_object_event
        emit_object_event(db, tenant_id=current_user.tenant_id,
                          key=name, event_type="created")
    except Exception:
        logger.exception("file_event emission failed for upload-file cog=%s", target_cog.id)

    return target_cog

@router.get("/{cog_id}/versions", response_model=List[schemas.Version])
def list_versions(
        cog_id: int,
        limit: int = 50,
        offset: int = 0,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    List all historical versions of a file, with pagination.
    """
    # Tenant-scoped so a same-tenant teammate holding a read grant on a
    # teammate-owned file can list its versions (the owner-scoped lookup 404d
    # them). The view gate right below is authoritative; cross-tenant cogs stay
    # invisible (404).
    db_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if db_cog is None:
        raise HTTPException(status_code=404, detail="Cog not found")
    if not can_user_view_cog(db, current_user.id, cog_id):
        _maybe_audit_xtenant_block(db, current_user, cog_id)
        raise HTTPException(status_code=403, detail="No permission to view this cog")

    # Ensure it's a file type to list versions
    if db_cog.cog_type not in [models.CogType.FILE, models.CogType.PLACEHOLDER_FILE]:
        raise HTTPException(status_code=400, detail="Cannot list versions for a folder type Cog.")

    return crud.get_cog_versions(db=db, cog_id=cog_id, limit=limit, offset=offset)

@router.post("/{cog_id}/tags/{tag_id}", response_model=schemas.Cog, status_code=status.HTTP_201_CREATED)
def add_tag_to_cog(
        cog_id: int,
        tag_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Associate an existing tag with a specific file or folder.
    """
    # Tenant-scoped so a same-tenant teammate holding a metadata-edit grant on
    # a teammate-owned cog can tag it (the owner-scoped lookup 404d them). The
    # gate right below is authoritative; cross-tenant cogs stay invisible (404).
    db_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if db_cog is None:
        raise HTTPException(status_code=404, detail="Cog not found")
    # Tagging is a metadata edit: the router's other metadata edit (PATCH /{cog},
    # update_cog) is gated by can_user_move_files_in_cog, and the capability
    # registry (_LEGACY_COG_CAP) has no tag/edit capability, so tags follow the
    # same gate the rest of the router uses for metadata edits.
    if not can_user_move_files_in_cog(db, current_user.id, cog_id):
        _maybe_audit_xtenant_block(db, current_user, cog_id)
        raise HTTPException(status_code=403, detail="No permission to tag this cog")

    from services.assets_cogs_files_service import get_tag_for_tenant_or_none

    db_tag = get_tag_for_tenant_or_none(db, tag_id=tag_id, tenant_id=current_user.tenant_id)
    if db_tag is None:  # Ensure tag exists and belongs to same tenant
        raise HTTPException(status_code=404, detail="Tag not found")

    return crud.add_tag_to_cog(db=db, db_cog=db_cog, db_tag=db_tag)

@router.get("/{cog_id}/tags", response_model=List[schemas.Tag])
def get_tags_for_cog(
        cog_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    List all tags currently associated with a specific file or folder.
    """
    # Tenant-scoped so a same-tenant teammate holding a read grant on a
    # teammate-owned cog can list its tags (the owner-scoped lookup 404d them).
    # The view gate right below is authoritative; cross-tenant cogs stay
    # invisible (404).
    db_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if db_cog is None:
        raise HTTPException(status_code=404, detail="Cog not found")
    if not can_user_view_cog(db, current_user.id, cog_id):
        _maybe_audit_xtenant_block(db, current_user, cog_id)
        raise HTTPException(status_code=403, detail="No permission to view this cog")

    return crud.get_tags_for_cog(db=db, cog_id=cog_id)

@router.delete("/{cog_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_tag_from_cog(
        cog_id: int,
        tag_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Remove the association between a tag and a file or folder.
    """
    # Tenant-scoped + the same metadata-edit gate as POST /{cog}/tags (see the
    # add-tag site): a teammate holding the grant on a teammate-owned cog can
    # untag it; cross-tenant cogs stay invisible (404).
    db_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if db_cog is None:
        raise HTTPException(status_code=404, detail="Cog not found")
    if not can_user_move_files_in_cog(db, current_user.id, cog_id):
        _maybe_audit_xtenant_block(db, current_user, cog_id)
        raise HTTPException(status_code=403, detail="No permission to untag this cog")

    from services.assets_cogs_files_service import get_tag_for_tenant_or_none

    db_tag = get_tag_for_tenant_or_none(db, tag_id=tag_id, tenant_id=current_user.tenant_id)
    if db_tag is None:  # Ensure tag exists and belongs to same tenant
        raise HTTPException(status_code=404, detail="Tag not found")

    crud.remove_tag_from_cog(db=db, db_cog=db_cog, db_tag=db_tag)
    return

@router.post("/{cog_id}/shareable-links", response_model=schemas.ShareableLink, status_code=status.HTTP_201_CREATED)
def create_shareable_link_for_cog(
        cog_id: int,
        link_create: schemas.ShareableLinkCreate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
        request: Request = None,
):
    """
    Creates a new public shareable link for a specific Cog.

    FX.102 (owner Q88=A): the tenant share-link policy is enforced HERE
    (services/share_link_policy.enforce_create) — public links refused
    when disabled (403), gate password required when mandated (422),
    confidential/pre-release/CUI-labelled content refused (403) — and
    the expiry is clamped to share.max_expiry_days.
    """
    from utils.paid_import import optional_paid
    slp = optional_paid("services.share_link_policy") or _CENoSharePolicy

    # Tenant-scoped so a same-tenant teammate holding a share grant on a
    # teammate-owned cog can mint a link for it (the owner-scoped lookup 404d
    # them). The share gate right below is authoritative; cross-tenant cogs stay
    # invisible (404).
    db_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if db_cog is None:
        raise HTTPException(status_code=404, detail="Cog not found")
    from permissions import can_user_share_cog

    if not can_user_share_cog(db, current_user.id, cog_id):
        _maybe_audit_xtenant_block(db, current_user, cog_id)
        raise HTTPException(status_code=403, detail="No permission to share this cog")

    # GSEC FX.79 p2a — a live data-marking whose label sets
    # blocks_external_share stops the public link from being minted.
    from utils.paid_import import optional_paid
    _dm_p2a = optional_paid("services.data_markings")
    if _dm_p2a is not None:  # FX.162.p5 — CE carve: no markings -> shareable
        _dm_p2a.assert_shareable(
            db, current_user.tenant_id, "asset", cog_id,
            action="create_share_link", user_id=current_user.id)

    try:
        link_create.expires_at = slp.enforce_create(
            db, current_user.tenant_id, db_cog, link_create)
    except slp.SharePolicyError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    db_link = crud.create_shareable_link(
        db=db, cog_id=cog_id, user_id=current_user.id,
        tenant_id=current_user.tenant_id, link_create=link_create)

    # Audit: share link created (TPN CS-7 / CMMC AU-2) — existing
    # helper + AuditAction.CONTENT_SHARE_LINK_CREATED; best-effort.
    try:
        import audit as _audit
        _audit.log_content_share_link_created(
            db=db, user_id=current_user.id, tenant_id=current_user.tenant_id,
            cog_id=cog_id, share_link_id=str(db_link.id),
            expiry_datetime=db_link.expires_at,
            password_protected=bool(db_link.password_hash),
            request=request,
        )
    except Exception:
        pass

    return db_link

@router.get("/{cog_id}/shareable-links", response_model=List[schemas.ShareableLink])
def list_shareable_links_for_cog(
        cog_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Lists all active shareable links for a specific Cog.
    """
    # Tenant-scoped + the same share gate as POST /{cog}/shareable-links: a
    # teammate holding the share grant on a teammate-owned cog can list its
    # links; cross-tenant cogs stay invisible (404).
    db_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if db_cog is None:
        raise HTTPException(status_code=404, detail="Cog not found or you don't have permission")
    from permissions import can_user_share_cog

    if not can_user_share_cog(db, current_user.id, cog_id):
        _maybe_audit_xtenant_block(db, current_user, cog_id)
        raise HTTPException(status_code=403, detail="No permission to share this cog")

    return crud.get_shareable_links_for_cog(db=db, cog_id=cog_id, tenant_id=current_user.tenant_id)

@router.delete("/shareable-links", status_code=status.HTTP_200_OK)
def revoke_all_shareable_links(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    FX.102 — tenant admin: revoke EVERY active share link in this tenant
    at once (soft: deleted_at stamped via the model, no hard delete —
    the rows stay for the audit trail). Non-admins get 403.
    """
    # 1788 CE carve: data_classification is paid; CE keeps the SAME admin
    # predicate (platform-admin bypass + tenant parent user), so revoking
    # every link stays admin-only without the paid module.
    from utils.paid_import import optional_paid
    _dc_admin = optional_paid("services.data_classification")
    is_admin_user = (
        _dc_admin.is_admin_user
        if _dc_admin is not None else
        lambda u: bool(u) and (getattr(u, "is_platform_admin", False)
                               or getattr(u, "parent_user_id", None) is None)
    )
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403,
                            detail="Revoking all share links requires a tenant admin")

    db_links = crud.revoke_all_active_links(db, tenant_id=current_user.tenant_id)

    # Audit: one revocation row per link (existing helper +
    # AuditAction.CONTENT_SHARE_LINK_REVOKED); best-effort.
    for db_link in db_links:
        try:
            from models import AuditAction as _AA
            audit.log_permission_audit(
                db=db, action=_AA.CONTENT_SHARE_LINK_REVOKED,
                actor_user_id=current_user.id,
                tenant_id=current_user.tenant_id,
                resource_type="shareable_link", resource_id=db_link.id,
                metadata={"revoked_all": True, "cog_id": db_link.cog_id},
            )
        except Exception:
            pass

    return {"revoked": len(db_links)}


@router.delete("/shareable-links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shareable_link(
        link_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Deletes a specific shareable link by its ID.
    The user must own the cog the link points to.
    """
    # This single call now finds the link AND verifies the current user owns the associated cog.
    db_link = crud.get_shareable_link_for_owner(db, link_id=link_id, owner_id=current_user.id, tenant_id=current_user.tenant_id)

    # If the link doesn't exist or doesn't belong to a cog owned by the user, we can raise a 404.
    if db_link is None:
        raise HTTPException(status_code=404, detail="Shareable link not found or permission denied")

    crud.soft_delete_shareable_link(db=db, db_link=db_link)

    # FX.102 audit: revocation (AuditAction.CONTENT_SHARE_LINK_REVOKED);
    # best-effort — an audit failure never masks the 204.
    try:
        from models import AuditAction as _AA
        audit.log_permission_audit(
            db=db, action=_AA.CONTENT_SHARE_LINK_REVOKED,
            actor_user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            resource_type="shareable_link", resource_id=db_link.id,
            metadata={"cog_id": db_link.cog_id},
        )
    except Exception:
        pass

    return


# ---------------------------------------------------------------------------
# FX.102 (owner Q88=A) — public share-link surface (VISIT / DOWNLOAD).
#
# Lives on `public_router` (no router-level user dep, mounted at the same
# /v1/assets prefix as the guarded router): the token IS the credential,
# and the tenant is carried by the row — never taken from the request.
# Every access runs the tenant share policy (services/share_link_policy.
# check_access): public-links switch (incl. the gov/DoD profile force),
# expiry, gate password, viewer-domain gate and the per-link download cap.
# Visits/download/revoke all land audit rows (AuditAction
# CONTENT_SHARE_LINK_ACCESSED / CONTENT_SHARE_LINK_REVOKED via the
# existing audit.py helpers).
# ---------------------------------------------------------------------------
class _CENoSharePolicy:
    """1788 CE carve: services/share_link_policy is paid and absent in CE.

    The share link itself (an unguessable token, plus its own gate password)
    is the credential; with no tenant share policy there is nothing to clamp
    or refuse, so every check is a no-op. The exception classes exist only so
    the handlers' `except slp.SharePolicyError` clauses stay valid; they carry
    the same status codes the paid policy would have produced.
    """
    class SharePolicyError(Exception):
        pass

    class LinkRevoked(SharePolicyError):
        status_code, detail = 404, "Shareable link not found"

    class PasswordRequiredForAccess(SharePolicyError):
        status_code, detail = 422, "A password is required to open this link."

    class PasswordRejected(SharePolicyError):
        status_code, detail = 403, "Invalid password for this link."

    enforce_create = staticmethod(lambda db, tenant_id, cog, link_create: None)
    check_access = staticmethod(lambda *a, **kw: None)


public_router = APIRouter()


def _resolve_share_link_viewer_email(db: Session, authorization: Optional[str]) -> Optional[str]:
    """Best-effort Bearer resolution → the viewer's e-mail (None =
    anonymous or unresolvable). A broken token degrades to anonymous;
    the policy then decides (open links let them in, domain-gated ones
    refuse). Never raises."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        from keycloak_auth import get_current_user_claims
        from dependencies import get_current_user as resolve_user
        claims = get_current_user_claims(authorization[7:])
        user = resolve_user(db=db, claims=claims)
        return getattr(user, "email", None) if user else None
    except Exception:
        return None


def _resolve_public_share_access(
        db: Session,
        token: str,
        viewer_email: Optional[str],
        password: Optional[str],
        is_download: bool,
):
    """FX.102 — resolve a live link by token and run the FULL tenant
    policy check (incl. gate password). Raises the policy's
    SharePolicyError subclasses (the caller maps them to HTTP); returns
    the link on success."""
    from utils.paid_import import optional_paid
    slp = optional_paid("services.share_link_policy") or _CENoSharePolicy
    from crud import shareable_link_cud as sl_cud

    link = crud.get_active_link_by_token(db, token)
    if link is None:
        # One answer for unknown OR revoked tokens — a visitor must not
        # be able to distinguish the two by probing.
        raise slp.LinkRevoked("Shareable link not found")
    if link.password_hash:
        if not password:
            raise slp.PasswordRequiredForAccess(
                "A password is required to open this link.")
        if not sl_cud.verify_shareable_link_password(db, link, password):
            raise slp.PasswordRejected("Invalid password for this link.")
    slp.check_access(db, link, viewer_email=viewer_email, is_download=is_download)
    return link


@public_router.get("/shareable-links/{token}")
def open_shareable_link(
        token: str,
        db: Session = Depends(get_db),
        authorization: Optional[str] = Header(None),
        password: Optional[str] = None,
):
    """FX.102 — VISIT a share link (metadata, no bytes). Runs the tenant
    share policy; on success bumps visit_count + last_visited_at and
    writes the CONTENT_SHARE_LINK_ACCESSED audit row."""
    from utils.paid_import import optional_paid
    slp = optional_paid("services.share_link_policy") or _CENoSharePolicy

    viewer_email = _resolve_share_link_viewer_email(db, authorization)
    try:
        link = _resolve_public_share_access(
            db, token, viewer_email, password, is_download=False)
    except slp.SharePolicyError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    link.visit_count = (link.visit_count or 0) + 1
    link.last_visited_at = datetime.utcnow()
    db.add(link)
    db.commit()
    db.refresh(link)

    try:
        audit.log_content_share_link_accessed(
            db=db, tenant_id=link.tenant_id, cog_id=link.cog_id,
            share_link_id=str(link.id),
            accessor_user_id=None,  # anonymous surface; domain-gated
            metadata={"kind": "visit",
                      "viewer_authenticated": bool(viewer_email)},
        )
    except Exception:
        pass

    cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=link.cog_id, tenant_id=link.tenant_id)
    return {
        "cog_id": link.cog_id,
        "name": cog.name if cog else None,
        "permission_level": (link.permission_level.value
                             if link.permission_level else None),
        "expires_at": link.expires_at,
        "visit_count": link.visit_count,
    }


@public_router.get("/shareable-links/{token}/download")
async def download_shareable_link(
        token: str,
        db: Session = Depends(get_db),
        authorization: Optional[str] = Header(None),
        password: Optional[str] = None,
):
    """FX.102 — DOWNLOAD the file behind a share link. Runs the tenant
    share policy (download cap included) BEFORE any storage read, bumps
    download_count + visit_count, audits, then streams the object with
    the same egress metering as the guarded download paths."""
    from fastapi.responses import StreamingResponse
    from utils.paid_import import optional_paid
    slp = optional_paid("services.share_link_policy") or _CENoSharePolicy

    viewer_email = _resolve_share_link_viewer_email(db, authorization)
    try:
        link = _resolve_public_share_access(
            db, token, viewer_email, password, is_download=True)
    except slp.SharePolicyError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=link.cog_id, tenant_id=link.tenant_id)
    if cog is None or cog.cog_type != models.CogType.FILE:
        raise HTTPException(status_code=404, detail="File not found")

    link.visit_count = (link.visit_count or 0) + 1
    link.download_count = (link.download_count or 0) + 1
    link.last_visited_at = datetime.utcnow()
    db.add(link)
    db.commit()
    db.refresh(link)

    try:
        audit.log_content_share_link_accessed(
            db=db, tenant_id=link.tenant_id, cog_id=link.cog_id,
            share_link_id=str(link.id),
            accessor_user_id=None,
            metadata={"kind": "download",
                      "viewer_authenticated": bool(viewer_email),
                      "download_number": link.download_count},
        )
    except Exception:
        pass

    # Storage path (File record or ContentBlob fallback — same lookup the
    # guarded stream_file route uses).
    storage_path = None
    mime_type = "application/octet-stream"
    file_record = _acf_crud.get_active_file_for_cog_and_tenant(
        db, cog_id=link.cog_id, tenant_id=link.tenant_id)
    if file_record:
        storage_path = file_record.storage_path
        mime_type = file_record.mime_type or mime_type
    elif cog.latest_version_id:
        version = _acf_crud.get_version_by_id(db, cog.latest_version_id)
        if version and version.content_id:
            blob = _acf_crud.get_content_blob_by_id_and_tenant(
                db, blob_id=version.content_id, tenant_id=link.tenant_id)
            if blob:
                storage_path = blob.storage_path
                mime_type = blob.mime_type or mime_type
    if not storage_path:
        raise HTTPException(status_code=404, detail="File not found")

    storage_scheme = os.getenv('STORAGE_SCHEME', 's3')
    sp = storage_path
    if sp.startswith((f'{storage_scheme}://', 's3://', 'minio://')):
        path_after_scheme = sp.split('://', 1)[1]
        object_path = '/'.join(path_after_scheme.split('/')[1:]) if '/' in path_after_scheme else path_after_scheme
    else:
        object_path = sp

    try:
        minio_client = MinioClient(tenant_id=link.tenant_id)
        response = minio_client.client.get_object(
            minio_client.bucket_name, object_path)
    except Exception as e:
        logger.error("share-link download stream failed: %s", e)
        raise HTTPException(status_code=500, detail="Error streaming file")

    filename = cog.name or "download"
    if "." not in os.path.basename(filename):
        filename = f"{filename}.bin"
    return StreamingResponse(
        _meter_egress_stream(
            response, 8192,
            tenant_id=link.tenant_id, context="share_link_download"),
        media_type=mime_type,
        headers={"Content-Disposition":
                 f'attachment; filename="{filename}"'},
    )

@service_router.post("/batch-update-processing-history", status_code=status.HTTP_200_OK)
def batch_update_processing_history(
    request: schemas.BatchProcessingHistoryUpdate,
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Appends processing history entry to multiple assets.
    Called by worker containers during/after node execution.

    This is the PRIMARY endpoint for metadata accumulation.

    Accepts either the existing Bearer/API-key service auth or a run_task node token
    (X-API-Key). For the run_task path the request body's tenant_id (and workflow_run_id)
    are pinned to the token — the tenant is never trusted from the body alone.
    """
    # For service calls, tenant_id must be provided
    if not request.tenant_id:
        raise HTTPException(
            status_code=400,
            detail="tenant_id is required for service-to-service calls"
        )

    # run_task node caller: pin the body's tenant + run to the token.
    if isinstance(principal, dict):
        pin_run_task_tenant(
            principal, tenant_id=request.tenant_id, run_id=request.workflow_run_id
        )

    tenant_id = request.tenant_id
    user_id = request.user_id or 0  # Default to system user if not provided

    updated_count = 0

    for cog_id in request.cog_ids:
        cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=tenant_id)
        if not cog:
            logging.warning(f"Cog {cog_id} not found, skipping metadata update")
            continue

        # Initialize metadata structure if empty
        if not cog.processing_metadata:
            cog.processing_metadata = {
                'processing_history': [],
                'derived_assets': [],
                'quality_metrics': {}
            }

        # Find or create workflow run entry
        workflow_run_id = request.workflow_run_id
        workflow_runs = cog.processing_metadata.get('processing_history', [])

        current_run = None
        for run in workflow_runs:
            if run.get('workflow_run_id') == workflow_run_id:
                current_run = run
                break

        if not current_run:
            # Create new workflow run entry
            current_run = {
                'workflow_run_id': workflow_run_id,
                'workflow_name': request.metadata_entry.node_kind,  # Will be updated by Traffic Cop
                'workflow_version': None,
                'executed_at': request.metadata_entry.started_at.isoformat(),
                'executed_by': user_id,
                'total_cost': 0,
                'status': 'running',
                'nodes_executed': []
            }
            workflow_runs.append(current_run)

        # Append node execution to current run
        # Use json() then parse to ensure datetimes are serialized to strings
        import json
        node_entry = json.loads(request.metadata_entry.json())
        current_run['nodes_executed'].append(node_entry)
        current_run['total_cost'] += request.metadata_entry.cost

        # Update quality metrics if provided
        if request.metadata_entry.quality_metrics:
            cog.processing_metadata['quality_metrics'].update(
                request.metadata_entry.quality_metrics
            )

        # Update top-level tracking fields
        cog.total_processing_cost += request.metadata_entry.cost
        cog.last_processed_at = datetime.now()
        if request.metadata_entry.status == 'success':
            cog.processing_count += 1

        # Update workflow run status
        if request.metadata_entry.status == 'failure':
            current_run['status'] = 'failure'
        elif all(n.get('status') == 'success' for n in current_run['nodes_executed']):
            current_run['status'] = 'success'

        cog.processing_metadata['processing_history'] = workflow_runs

        # Mark for update
        flag_modified(cog, 'processing_metadata')
        updated_count += 1

    db.commit()

    return {
        "detail": f"Updated processing history for {updated_count} assets",
        "updated_cog_ids": request.cog_ids
    }

@service_router.post("/workflows/{workflow_run_id}/create-output-folder", status_code=status.HTTP_201_CREATED)
def create_workflow_output_folder(
    workflow_run_id: int,
    workflow_name: str,
    input_cog_id: Optional[int] = None,
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Creates an output folder for workflow results.
    Inherits permissions from the input cog's parent folder.
    If no input_cog_id (e.g., text-only workflows), creates at root level.

    Used by export DAGs to store processed files.
    """
    try:
        return _acf_svc.create_workflow_output_folder(
            db,
            workflow_run_id=workflow_run_id,
            workflow_name=workflow_name,
            input_cog_id=input_cog_id,
            principal=principal,
        )
    except ServiceError as e:
        raise _map_acf_service_error(e)

@service_router.post("/workflows/upload-processed-file", status_code=status.HTTP_201_CREATED)
async def upload_processed_file(
    output_folder_id: int = Form(...),
    file_path: str = Form(...),  # Path to file in MinIO (bucket/hash format)
    original_filename: str = Form(...),  # Original input filename (e.g., "Logo.jpg")
    category: str = Form(...),  # node category (e.g., "albedo", "normal", "input")
    node_order: int = Form(...),  # Node execution order (1, 2, 3, ...)
    folder_pad_width: int = Form(1),  # Zero-padding width for folder name
    file_sequence: int = Form(1),  # File sequence within this node
    file_count: int = Form(1),  # Total files in this node (for padding decision)
    file_pad_width: int = Form(0),  # Zero-padding width for file sequence (0 = no sequence)
    # Extension of the OUTPUT file (the bytes about to be uploaded).
    # Optional for back-compat with older ReturnExport builds — when
    # absent the handler falls back to the original input filename's
    # extension. Pre-fix that fallback wrote PDF bytes to a `.png` file
    # for converter nodes whose input and output formats differ.
    output_extension: Optional[str] = Form(None),
    tenant_id: int = Form(...),
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Uploads a processed file to the output folder with automatic naming.

    Folder naming: {order}_{category} with zero-padding (e.g., "1_input", "02_albedo")
    File naming: {original}_{category}[_{sequence}].{ext}

    file_path format: bucket/object_name (e.g., "tenant-1-files/abc123hash")

    Naming examples:
      - Single file: Logo_albedo.png (no sequence number)
      - Multiple files: Logo_albedo_01.png, Logo_albedo_02.png (zero-padded sequence)
    """
    # Cross-tenant guard: a run_task node token may only write to its own tenant.
    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=tenant_id)
    try:
        # Build folder name: {order}_{category} with zero-padding
        folder_name = f"{str(node_order).zfill(folder_pad_width)}_{category}"

        logging.info(f"Upload request: file_path={file_path}, output_folder_id={output_folder_id}, folder={folder_name}")

        # Create MinIO client for this tenant (service endpoint - no user context)
        minio_client = MinioClient(tenant_id=tenant_id)

        # Parse file_path which is in bucket/object format
        if '/' in file_path:
            source_bucket, object_name = file_path.split('/', 1)
        else:
            # Fallback: assume it's just an object name in tenant bucket
            source_bucket = minio_client.bucket_name
            object_name = file_path

        logging.info(f"Parsed path: bucket={source_bucket}, object={object_name}")

        # Get output folder (exclude soft-deleted)
        output_folder = _acf_crud.get_active_cog_by_id_and_tenant(
            db, cog_id=output_folder_id, tenant_id=tenant_id
        )

        if not output_folder:
            raise HTTPException(status_code=404, detail="Output folder not found")

        # Create or get node subfolder with new naming convention
        target_folder_id = output_folder_id

        # Check if node subfolder already exists (exclude soft-deleted)
        node_subfolder = _acf_crud.get_active_subfolder_by_name_parent_tenant(
            db, name=folder_name, parent_id=output_folder_id, tenant_id=tenant_id,
        )

        if not node_subfolder:
            # Create the subfolder with new naming
            node_subfolder = models.Cog(
                name=folder_name,
                cog_type=models.CogType.ASSET_FOLDER,
                parent_id=output_folder_id,
                owner_id=output_folder.owner_id,
                tenant_id=tenant_id,
                processing_metadata={
                    "category": category,
                    "node_order": node_order
                }
            )
            db.add(node_subfolder)
            db.flush()
            # Gate 24: node-subfolder owner gets an OWNER grant.
            from resource_permissions import grant_cog_owner_if_folder
            grant_cog_owner_if_folder(db, node_subfolder)
            logging.info(f"Created node subfolder: {folder_name} (cog_id: {node_subfolder.id})")

        target_folder_id = node_subfolder.id
        logging.info(f"Using node subfolder for uploads: {folder_name} (cog_id: {target_folder_id})")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"UPLOAD ERROR (initial): {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Upload failed")

    try:
        # Parse original filename
        name_parts = original_filename.rsplit('.', 1)
        base_name = name_parts[0]
        original_extension = name_parts[1] if len(name_parts) > 1 else ""

        # Prefer the OUTPUT file's actual extension when the caller
        # supplied one. For converter nodes (PNG-in → PDF-out, etc.)
        # the input filename's extension doesn't match the bytes about
        # to be uploaded — using `original_extension` would write PDF
        # bytes to a `.png` file and confuse anyone downloading the
        # bundle. Strip a leading dot if present.
        chosen_extension = (output_extension or original_extension or "").lstrip(".")

        # Build new filename: {original}_{category}[_{sequence}].{ext}
        # Only add sequence number if there are multiple files in this node
        if file_count > 1:
            # Zero-pad the sequence number
            padded_sequence = str(file_sequence).zfill(file_pad_width) if file_pad_width > 0 else str(file_sequence)
            new_filename = f"{base_name}_{category}_{padded_sequence}.{chosen_extension}" if chosen_extension else f"{base_name}_{category}_{padded_sequence}"
        else:
            # Single file - no sequence number
            new_filename = f"{base_name}_{category}.{chosen_extension}" if chosen_extension else f"{base_name}_{category}"

        # Check if file_path is a directory or file
        # If it ends with '/', it's a directory - list files in it
        if object_name.endswith('/'):
            # List all files in this directory
            try:
                logging.info(f"Listing directory in MinIO: {source_bucket}/{object_name}")
                objects = minio_client.client.list_objects(bucket_name=source_bucket, prefix=object_name, recursive=False)
                files_in_dir = [obj.object_name for obj in objects if not obj.object_name.endswith('/')]
                logging.info(f"Found {len(files_in_dir)} files in directory: {files_in_dir}")

                if not files_in_dir:
                    logging.error(f"No files found in directory: {object_name}")
                    raise HTTPException(status_code=404, detail=f"No files found in directory: {object_name}")

                # Use the first file for now
                actual_object_name = files_in_dir[0]
                logging.info(f"Using file: {actual_object_name}")
            except HTTPException:
                raise
            except Exception as e:
                logging.error(f"Failed to list directory {object_name}: {str(e)}")
                raise HTTPException(status_code=404, detail="Failed to list directory")
        else:
            actual_object_name = object_name

        # Download file from source path in MinIO
        logging.info(f"Downloading file from MinIO: {source_bucket}/{actual_object_name}")
        response = minio_client.client.get_object(bucket_name=source_bucket, object_name=actual_object_name)
        file_data = response.read()
        response.close()
        response.release_conn()
        logging.info(f"Successfully downloaded {len(file_data)} bytes")

        # Calculate content hash
        content_blob_id = hashlib.sha256(file_data).hexdigest()

        # Check if content blob already exists for this tenant
        existing_blob = _acf_crud.get_content_blob_by_id_and_tenant(
            db, blob_id=content_blob_id, tenant_id=tenant_id
        )

        if not existing_blob:
            # FLAT STORAGE: Files stored as pure SHA256 hash (no extension) in MinIO
            # Extension stored in database for processing tools and downloads
            dest_object_name = content_blob_id  # Pure hash, no extension

            # Normalize extension (lowercase, no dot)
            file_ext = chosen_extension.lower()

            # Upload file to tenant bucket (TPN CS-1)
            from io import BytesIO
            minio_client.client.put_object(
                bucket_name=minio_client.bucket_name,
                object_name=dest_object_name,
                data=BytesIO(file_data),
                length=len(file_data)
            )

            # Create ContentBlob record with extension as metadata
            content_blob = models.ContentBlob(
                id=content_blob_id,
                storage_path=f"s3://{minio_client.bucket_name}/{dest_object_name}",
                size_bytes=len(file_data),
                mime_type=f"application/octet-stream",  # Could detect from extension
                file_extension=file_ext,
                tenant_id=tenant_id
            )
            db.add(content_blob)
        else:
            content_blob = existing_blob

        # Create Cog for the file (in node subfolder)
        new_cog = models.Cog(
            name=new_filename,
            cog_type=models.CogType.FILE,
            parent_id=target_folder_id,
            owner_id=output_folder.owner_id,
            tenant_id=tenant_id,
            processing_metadata={
                "source_file": original_filename,
                "category": category,
                "node_order": node_order,
                "file_sequence": file_sequence if file_count > 1 else None
            }
        )
        db.add(new_cog)
        db.flush()

        # Create Version linked to ContentBlob
        version = models.Version(
            cog_id=new_cog.id,
            tenant_id=tenant_id,
            content_id=content_blob.id,
            version_number=1,
            author_user_id=output_folder.owner_id
        )
        db.add(version)
        db.flush()  # Flush to get version.id

        # Set latest_version_id on the Cog (required for zip download to find files)
        new_cog.latest_version_id = version.id
        db.commit()
        db.refresh(new_cog)

        logging.info(f"Uploaded processed file: {new_filename} (cog_id: {new_cog.id}, version_id: {version.id}, size: {len(file_data)} bytes)")

        return {"cog_id": new_cog.id, "filename": new_filename, "size_bytes": len(file_data), "content_hash": content_blob_id}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"UPLOAD ERROR (main): {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Upload processing failed")

@service_router.post("/workflows/upload-text-file", status_code=status.HTTP_201_CREATED)
async def upload_text_file(
    filename: str = Form(...),  # e.g., "file_hashes.txt", "workflow_info.json"
    content: str = Form(...),  # Text content of the file
    tenant_id: int = Form(...),
    output_folder_id: int = Form(0),  # 0/omitted => standalone cog at tenant root
    workflow_run_id: int = Form(0),  # used to attribute a folder-less cog to the run's owner
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Uploads a text file directly to the tenant store.

    Two modes:
      - output_folder_id given (export/output writing): the file is created under
        that output folder and inherits its owner. Used for file_hashes.txt,
        workflow_info.json, README.txt, etc.
      - output_folder_id omitted (input-node text staging): a standalone text cog
        is created at the tenant root, attributed to the run's initiating user when
        a workflow_run_id is supplied. This backs the Text Input node's inline
        "Text Content" field — the typed text becomes a real .txt cog so the
        downstream node stages it exactly like an uploaded file.
    """
    # Cross-tenant guard: a run_task node token may only write to its own tenant.
    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=tenant_id)
    try:
        logging.info(f"Upload text file: {filename} to folder {output_folder_id or '<root>'}")

        # Create MinIO client for this tenant
        minio_client = MinioClient(tenant_id=tenant_id)

        # Resolve the parent folder + owner for the new cog.
        if output_folder_id:
            # Get output folder (exclude soft-deleted)
            output_folder = _acf_crud.get_active_cog_by_id_and_tenant(
                db, cog_id=output_folder_id, tenant_id=tenant_id
            )

            if not output_folder:
                raise HTTPException(status_code=404, detail="Output folder not found")

            parent_id = output_folder_id
            owner_id = output_folder.owner_id
        else:
            # Folder-less (input-node) staging: standalone cog at tenant root.
            parent_id = None
            owner_id = None
            if workflow_run_id:
                run = _acf_crud.get_workflow_run_by_id_and_tenant(
                    db, workflow_run_id=workflow_run_id, tenant_id=tenant_id,
                )
                if run is not None:
                    owner_id = run.triggered_by_user_id

        # Convert content to bytes
        file_data = content.encode('utf-8')

        # Calculate content hash
        content_blob_id = hashlib.sha256(file_data).hexdigest()

        # Get extension from filename
        extension = filename.rsplit('.', 1)[-1] if '.' in filename else 'txt'

        # Check if content blob already exists
        existing_blob = _acf_crud.get_content_blob_by_id_and_tenant(
            db, blob_id=content_blob_id, tenant_id=tenant_id
        )

        if not existing_blob:
            # Upload to MinIO
            from io import BytesIO
            minio_client.client.put_object(
                bucket_name=minio_client.bucket_name,
                object_name=content_blob_id,
                data=BytesIO(file_data),
                length=len(file_data)
            )

            # Determine mime type
            mime_types = {
                'txt': 'text/plain',
                'json': 'application/json',
                'log': 'text/plain',
                'md': 'text/markdown'
            }
            mime_type = mime_types.get(extension.lower(), 'text/plain')

            # Create ContentBlob record
            content_blob = models.ContentBlob(
                id=content_blob_id,
                storage_path=f"s3://{minio_client.bucket_name}/{content_blob_id}",
                size_bytes=len(file_data),
                mime_type=mime_type,
                file_extension=extension.lower(),
                tenant_id=tenant_id
            )
            db.add(content_blob)
        else:
            content_blob = existing_blob

        # Create Cog for the file
        new_cog = models.Cog(
            name=filename,
            cog_type=models.CogType.FILE,
            parent_id=parent_id,
            owner_id=owner_id,
            tenant_id=tenant_id,
            processing_metadata={"type": "metadata_file"}
        )
        db.add(new_cog)
        db.flush()

        # Create Version linked to ContentBlob
        version = models.Version(
            cog_id=new_cog.id,
            tenant_id=tenant_id,
            content_id=content_blob.id,
            version_number=1,
            author_user_id=owner_id
        )
        db.add(version)
        db.flush()  # Flush to get version.id

        # Set latest_version_id on the Cog (required for zip download to find files)
        new_cog.latest_version_id = version.id
        db.commit()
        db.refresh(new_cog)

        logging.info(f"Uploaded text file: {filename} (cog_id: {new_cog.id}, version_id: {version.id})")

        return {"cog_id": new_cog.id, "filename": filename, "size_bytes": len(file_data)}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"TEXT FILE UPLOAD ERROR: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Text file upload failed")

@service_router.post("/register-uploaded-file", status_code=status.HTTP_201_CREATED)
async def register_uploaded_file(
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Register a file that was uploaded directly to MinIO and hashed by the file-hasher worker.
    Creates ContentBlob, Cog, Version, and File records.
    """
    content_hash = request.get("content_hash")
    filename = request.get("filename", "unknown")
    parent_id = request.get("parent_id")
    file_size = request.get("file_size", 0)
    content_type = request.get("content_type", "application/octet-stream")
    file_extension = request.get("file_extension", "")
    tenant_id = request.get("tenant_id")
    user_id = request.get("user_id")
    is_dedup = request.get("is_dedup", False)

    if not content_hash or not tenant_id or not parent_id:
        raise HTTPException(status_code=400, detail="content_hash, tenant_id, and parent_id are required")

    # Cross-tenant guard: a run_task node token may only register files in its own tenant.
    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=int(tenant_id))

    target_cog = _acf_svc.register_uploaded_file_record(
        db,
        content_hash=content_hash,
        filename=filename,
        parent_id=parent_id,
        file_size=file_size,
        content_type=content_type,
        file_extension=file_extension,
        tenant_id=tenant_id,
        user_id=user_id,
        principal=principal,
    )

    logging.info(f"Registered uploaded file: {filename} -> {content_hash[:16]}... (cog_id={target_cog.id}, dedup={is_dedup})")

    # Auto-dispatch thumbnail generation for supported formats
    # 3D models, CAD files, and USD scenes all use 3d-thumbnail-gen (supports all via trimesh/cadquery/pxr)
    THUMBNAIL_EXTENSIONS = {'glb', 'gltf', 'obj', 'fbx', 'ply', 'stl', 'step', 'stp', 'usd', 'usda', 'usdc', 'usdz'}
    if file_extension.lower().lstrip('.') in THUMBNAIL_EXTENSIONS:
        try:
            _dispatch_thumbnail_task(db, target_cog.id, content_hash, file_extension, tenant_id, user_id)
        except Exception as e:
            logging.warning(f"Failed to dispatch thumbnail task for cog {target_cog.id}: {e}")

    # Auto-dispatch USD→GLB conversion for inline 3D preview (in addition to PNG thumbnail above)
    USD_EXTENSIONS = {'usd', 'usda', 'usdc', 'usdz'}
    if file_extension.lower().lstrip('.') in USD_EXTENSIONS:
        try:
            _dispatch_glb_conversion_task(db, target_cog.id, content_hash, file_extension, tenant_id, user_id)
        except Exception as e:
            logging.warning(f"Failed to dispatch GLB conversion for cog {target_cog.id}: {e}")

    # Auto-dispatch HDR/EXR tone-mapping to generate viewable thumbnail
    HDR_EXTENSIONS = {'exr', 'hdr'}
    if file_extension.lower().lstrip('.') in HDR_EXTENSIONS:
        try:
            _dispatch_hdr_thumbnail_task(db, target_cog.id, content_hash, file_extension, tenant_id, user_id)
        except Exception as e:
            logging.warning(f"Failed to dispatch HDR thumbnail task for cog {target_cog.id}: {e}")

    # Auto-dispatch SLDPRT → STEP conversion (#8 launch-day-followup-2 §5.8).
    # FreeCAD is the only open-source path that reads SolidWorks Part files;
    # the existing CAD preview already handles STEP via occt-import-js, so
    # converting once on upload lets us reuse the entire 3D asset viewer.
    if file_extension.lower().lstrip('.') == 'sldprt':
        try:
            _dispatch_sldprt_conversion_task(db, target_cog.id, content_hash, tenant_id, user_id)
        except Exception as e:
            logging.warning(f"Failed to dispatch SLDPRT conversion for cog {target_cog.id}: {e}")

    # Auto-dispatch point-cloud preview generation (#7 §5).
    # Converts pcd/las/laz/e57/xyz to PLY + thumbnail so non-PLY formats
    # render in the existing 3D-asset viewer with a normal grid thumbnail.
    POINTCLOUD_EXTENSIONS = {'pcd', 'las', 'laz', 'xyz', 'e57'}
    if file_extension.lower().lstrip('.') in POINTCLOUD_EXTENSIONS:
        try:
            _dispatch_pointcloud_preview_task(
                db, target_cog.id, content_hash, file_extension,
                tenant_id, user_id,
            )
        except Exception as e:
            logging.warning(f"Failed to dispatch point cloud preview for cog {target_cog.id}: {e}")

    return {"cog_id": target_cog.id, "content_hash": content_hash, "filename": filename}

@service_router.post("/set-thumbnail", status_code=status.HTTP_200_OK)
async def set_thumbnail(
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Service callback: set the thumbnail path for a cog.
    Called by the 3d-thumbnail-gen container after rendering.
    """
    cog_id = request.get("cog_id")
    thumbnail_path = request.get("thumbnail_path")
    tenant_id = request.get("tenant_id")

    if not cog_id or not thumbnail_path:
        raise HTTPException(status_code=400, detail="cog_id and thumbnail_path required")

    try:
        _acf_svc.set_thumbnail(
            db,
            cog_id=cog_id,
            thumbnail_path=thumbnail_path,
            tenant_id=tenant_id,
            preview_type=request.get("preview_type", "png"),
            principal=principal,
        )
    except ServiceError as e:
        raise _map_acf_service_error(e)

    return {"status": "ok"}

@service_router.post("/sldprt-conversion-callback/{cog_id}")
async def sldprt_conversion_callback(
    cog_id: int,
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """Service callback from the sldprt-converter container after it
    finishes (success or failure). Updates `processing_metadata` so the
    browser preview can flip from 'converting…' to 'ready' or 'failed'.
    The converted STEP itself lives at `<content_hash>.step` in the
    same tenant bucket — `GET /cogs/{id}/converted-step` serves a
    presigned URL for it.
    """
    success = bool(request.get("success"))
    error_message = (request.get("error_message") or "")[:2000]
    content_hash = request.get("content_hash") or ""

    try:
        cog = _acf_svc.sldprt_conversion_callback_update(
            db,
            cog_id=cog_id,
            success=success,
            error_message=error_message,
            content_hash=content_hash,
            principal=principal,
        )
    except ServiceError as e:
        raise _map_acf_service_error(e)

    meta = cog.processing_metadata or {}

    # Chain a thumbnail-gen task on the converted STEP so the file grid
    # gets a preview tile without a second user action. (#8 round 12.)
    # Skip if the cog already has a thumbnail (re-upload, manual upload).
    if success and not meta.get("thumbnail_path"):
        try:
            _dispatch_thumbnail_task(
                db, cog.id, f"{content_hash}.step", "step",
                cog.tenant_id, cog.owner_id or 1,
            )
        except Exception as e:
            logging.warning(f"SLDPRT thumbnail chain dispatch failed for cog {cog.id}: {e}")
    return {"status": "ok"}

@router.get("/{cog_id}/converted-step",
            dependencies=[Depends(require_auditor_data_content)])
async def get_converted_step(
    cog_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
):
    """Serve the converted STEP file for an SLDPRT cog. Returns:
      - 200 + presigned URL when conversion succeeded.
      - 202 + status='pending' while the converter task is in flight.
      - 404 when the cog is not an SLDPRT or there is no conversion record.
      - 422 + status='failed' + error_message when the converter failed.
    The browser CAD preview reads this and chooses the right state-UX
    (spinner / 3D viewer / error banner). #8 / §5.8.

    Lazy backfill (#8 round 10): if this cog IS a `.sldprt` but has no
    conversion record (e.g. uploaded via a path that bypassed the
    on-upload dispatch hook), fire the conversion task here and return
    202 pending. The user opens preview → conversion runs → next poll
    flips to ready. No manual button required.
    """
    from fastapi.responses import JSONResponse
    from datetime import timedelta

    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="File not found")

    # GSEC R-047: this route serves a presigned URL to the cog's converted STEP bytes, so it needs the same
    # per-cog view-grant check as get_cog/stream (tenant-scoped lookup
    # alone is an intra-tenant read bypass).
    if not can_user_view_cog(db, current_user.id, cog_id):
        raise HTTPException(status_code=404, detail="File not found")

    meta = cog.processing_metadata or {}
    status = (meta.get("sldprt_conversion_status") or "").lower()

    # Heal stuck-pending state. If the cog says "pending" but the
    # latest dispatch task FAILED (e.g. worker couldn't pull the
    # converter image, registry blip), the user would otherwise stare
    # at the spinner forever. Detect it, redispatch (capped at 3
    # retries), and flip to fresh "pending". (#8 round 12.)
    if status == "pending":
        retries = int(meta.get("sldprt_conversion_retries") or 0)
        from models import ApiTaskStatus
        latest_task = _acf_crud.get_latest_task_by_node_id(db, f"sldprt-convert-{cog.id}")
        task_failed = latest_task is not None and latest_task.status == ApiTaskStatus.FAILED
        if task_failed and retries < 3:
            content_hash = None
            try:
                if cog.latest_version_id:
                    version = _acf_crud.get_version_by_id(db, cog.latest_version_id)
                    if version and version.content_id:
                        content_hash = version.content_id
            except Exception:
                content_hash = None
            if content_hash:
                try:
                    _dispatch_sldprt_conversion_task(
                        db, cog.id, content_hash, cog.tenant_id, current_user.id,
                    )
                    cog.processing_metadata = {
                        **(cog.processing_metadata or {}),
                        "sldprt_conversion_retries": retries + 1,
                    }
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(cog, "processing_metadata")
                    db.commit()
                    return JSONResponse(
                        status_code=202,
                        content={"status": "pending", "message": "Retrying SLDPRT conversion."},
                    )
                except Exception as e:
                    logging.warning(f"Heal-redispatch failed for cog {cog.id}: {e}")
        return JSONResponse(
            status_code=202,
            content={"status": "pending", "message": "SLDPRT is still being converted to STEP."},
        )
    if status == "failed":
        return JSONResponse(
            status_code=422,
            content={
                "status": "failed",
                "error_message": meta.get("sldprt_conversion_error") or "conversion failed",
            },
        )
    step_object = meta.get("sldprt_step_object")
    if not step_object:
        # Lazy backfill — only for actual SLDPRT files. Resolve the
        # source content_hash via the cog's latest_version → ContentBlob
        # chain (the content_blob's id IS the SHA256 hash, which is the
        # MinIO object key).
        is_sldprt = (cog.name or "").lower().endswith(".sldprt")
        if is_sldprt:
            content_hash = None
            try:
                if cog.latest_version_id:
                    version = _acf_crud.get_version_by_id(db, cog.latest_version_id)
                    if version and version.content_id:
                        content_hash = version.content_id
            except Exception:
                content_hash = None
            if content_hash:
                try:
                    _dispatch_sldprt_conversion_task(
                        db, cog.id, content_hash, cog.tenant_id, current_user.id,
                    )
                    return JSONResponse(
                        status_code=202,
                        content={
                            "status": "pending",
                            "message": "SLDPRT conversion just queued. Check back in ~30s.",
                        },
                    )
                except Exception as e:
                    logging.warning(f"Lazy SLDPRT dispatch failed for cog {cog.id}: {e}")
        raise HTTPException(status_code=404, detail="No SLDPRT conversion record for this cog")

    bucket = minio_client.bucket_name
    try:
        url = minio_client.presigned_client.presigned_get_object(
            bucket, step_object, expires=timedelta(hours=1),
        )
    except Exception as e:
        logging.warning(f"Failed to presign converted STEP for cog {cog_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not presign STEP URL")

    # GY.V1a: presign delivery audit (best-effort, never blocks the hand-off).
    # 1788 CE carve: delivery_audit is paid; CE records no delivery row
    # (audit-only side effect — the hand-off itself is unchanged).
    from utils.paid_import import optional_paid
    _delivery_audit = optional_paid("services.delivery_audit")
    record_delivery = (_delivery_audit.record_delivery
                       if _delivery_audit is not None
                       else (lambda *a, **kw: None))

    record_delivery(
        db, delivery_path="presign", actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id, resource_id=cog_id,
        object_key=step_object, expiry_seconds=3600, request=http_request,
        metadata={"asset": "converted_step"},
    )
    return {"status": "ready", "url": url}

@service_router.post("/pointcloud-conversion-callback/{cog_id}")
async def pointcloud_conversion_callback(
    cog_id: int,
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """Service callback from the pointcloud-preview container. Updates
    `processing_metadata` so /converted-pointcloud and /thumbnail
    endpoints can find the resulting PLY + PNG. (#7 / §5)"""
    success = bool(request.get("success"))
    error_message = (request.get("error_message") or "")[:2000]
    ply_object = request.get("ply_object") or ""
    thumb_object = request.get("thumbnail_object") or ""

    try:
        _acf_svc.pointcloud_conversion_callback(
            db,
            cog_id=cog_id,
            success=success,
            error_message=error_message,
            ply_object=ply_object,
            thumb_object=thumb_object,
            principal=principal,
        )
    except ServiceError as e:
        raise _map_acf_service_error(e)
    return {"status": "ok"}

@router.get("/{cog_id}/converted-pointcloud",
            dependencies=[Depends(require_auditor_data_content)])
async def get_converted_pointcloud(
    cog_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
):
    """Serve the converted PLY for a non-native point-cloud cog (.las /
    .laz / .e57). Same response shape as `/converted-step`:
      - 200 + presigned URL when conversion succeeded.
      - 202 + status='pending' while the converter task is in flight.
      - 422 + status='failed' + error_message if conversion failed.
      - 404 if the cog isn't a point cloud or has no record.

    Lazy backfill (same as SLDPRT path): if the cog IS a non-native
    point cloud format and has no conversion record, fire the dispatch
    here and return 202. The user opens preview → conversion runs.
    """
    from fastapi.responses import JSONResponse
    from datetime import timedelta

    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="File not found")

    # GSEC R-047: this route serves a presigned URL to the cog's converted point-cloud bytes, so it needs the same
    # per-cog view-grant check as get_cog/stream (tenant-scoped lookup
    # alone is an intra-tenant read bypass).
    if not can_user_view_cog(db, current_user.id, cog_id):
        raise HTTPException(status_code=404, detail="File not found")

    meta = cog.processing_metadata or {}
    status = (meta.get("pointcloud_conversion_status") or "").lower()

    # Same stuck-pending heal as /converted-step (#7 round 12).
    if status == "pending":
        retries = int(meta.get("pointcloud_conversion_retries") or 0)
        from models import ApiTaskStatus
        latest_task = _acf_crud.get_latest_task_by_node_id(db, f"pointcloud-preview-{cog.id}")
        task_failed = latest_task is not None and latest_task.status == ApiTaskStatus.FAILED
        if task_failed and retries < 3:
            cog_name = (cog.name or "").lower()
            ext = cog_name.rsplit(".", 1)[-1] if "." in cog_name else ""
            content_hash = None
            try:
                if cog.latest_version_id:
                    version = _acf_crud.get_version_by_id(db, cog.latest_version_id)
                    if version and version.content_id:
                        content_hash = version.content_id
            except Exception:
                content_hash = None
            if content_hash and ext:
                try:
                    _dispatch_pointcloud_preview_task(
                        db, cog.id, content_hash, ext, cog.tenant_id, current_user.id,
                    )
                    cog.processing_metadata = {
                        **(cog.processing_metadata or {}),
                        "pointcloud_conversion_retries": retries + 1,
                    }
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(cog, "processing_metadata")
                    db.commit()
                    return JSONResponse(
                        status_code=202,
                        content={"status": "pending", "message": "Retrying point cloud conversion."},
                    )
                except Exception as e:
                    logging.warning(f"Heal-redispatch failed for cog {cog.id}: {e}")
        return JSONResponse(
            status_code=202,
            content={"status": "pending", "message": "Point cloud is still being converted to PLY."},
        )
    if status == "failed":
        return JSONResponse(
            status_code=422,
            content={
                "status": "failed",
                "error_message": meta.get("pointcloud_conversion_error") or "conversion failed",
            },
        )
    ply_object = meta.get("pointcloud_ply_object")
    if not ply_object:
        # Lazy backfill — only for non-native point cloud formats.
        cog_name = (cog.name or "").lower()
        ext = cog_name.rsplit(".", 1)[-1] if "." in cog_name else ""
        if ext in _POINTCLOUD_NEEDS_CONVERT_EXTS:
            content_hash = None
            try:
                if cog.latest_version_id:
                    version = _acf_crud.get_version_by_id(db, cog.latest_version_id)
                    if version and version.content_id:
                        content_hash = version.content_id
            except Exception:
                content_hash = None
            if content_hash:
                try:
                    _dispatch_pointcloud_preview_task(
                        db, cog.id, content_hash, ext, cog.tenant_id, current_user.id,
                    )
                    return JSONResponse(
                        status_code=202,
                        content={
                            "status": "pending",
                            "message": "Point cloud conversion just queued. Check back in ~30s.",
                        },
                    )
                except Exception as e:
                    logging.warning(f"Lazy point cloud dispatch failed for cog {cog.id}: {e}")
        raise HTTPException(status_code=404, detail="No point cloud conversion record for this cog")

    bucket = minio_client.bucket_name
    try:
        url = minio_client.presigned_client.presigned_get_object(
            bucket, ply_object, expires=timedelta(hours=1),
        )
    except Exception as e:
        logging.warning(f"Failed to presign converted PLY for cog {cog_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not presign PLY URL")

    # GY.V1a: presign delivery audit (best-effort, never blocks the hand-off).
    # 1788 CE carve: delivery_audit is paid; CE records no delivery row
    # (audit-only side effect — the hand-off itself is unchanged).
    from utils.paid_import import optional_paid
    _delivery_audit = optional_paid("services.delivery_audit")
    record_delivery = (_delivery_audit.record_delivery
                       if _delivery_audit is not None
                       else (lambda *a, **kw: None))

    record_delivery(
        db, delivery_path="presign", actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id, resource_id=cog_id,
        object_key=ply_object, expiry_seconds=3600, request=http_request,
        metadata={"asset": "converted_pointcloud"},
    )
    return {"status": "ready", "url": url}

@router.get("/{cog_id}/thumbnail",
            dependencies=[Depends(require_auditor_data_content)])
async def get_thumbnail(
    cog_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
):
    """
    Get a presigned URL for a cog's thumbnail image.

    When no thumbnail exists yet, the endpoint also LAZY-DISPATCHES the
    appropriate preview-generation task (3D-thumbnail-gen for meshes,
    hdr-converter for EXR/HDR, pointcloud-preview for point clouds).
    The first request returns 202 with status='pending'; subsequent
    requests after the task completes return the thumbnail URL.
    Capped at 3 retries on the cog's processing_metadata so a worker-
    side outage doesn't dispatch in a tight loop.

    When nothing can be generated (no converter applies / retries
    exhausted) the endpoint returns 200 {"status": "unavailable",
    "url": null} rather than 404, so file-browser grids that probe every
    item don't log a 404 per preview-less asset. An unknown or
    non-viewable cog id is still 404.
    """
    from fastapi.responses import JSONResponse
    from datetime import timedelta

    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="File not found")

    # GSEC R-047: this route serves a presigned URL to the cog's thumbnail/preview bytes, so it needs the same
    # per-cog view-grant check as get_cog/stream (tenant-scoped lookup
    # alone is an intra-tenant read bypass).
    if not can_user_view_cog(db, current_user.id, cog_id):
        raise HTTPException(status_code=404, detail="File not found")

    meta = cog.processing_metadata or {}
    thumb_path = meta.get("thumbnail_path")
    glb_path = meta.get("glb_preview_path")

    if not thumb_path and not glb_path:
        # Raster images (png/jpg/gif/bmp/webp): generate a small WebP thumbnail
        # in-process on first request and cache it. This is the lazy backfill
        # path — EXISTING images get a thumbnail with no manual migration, and
        # the (fast, ~KB) thumbnail is served on every subsequent request. The
        # full image is only ever downloaded once, here, to build the thumbnail.
        raster_key = _ensure_raster_thumbnail(db, cog, current_user.tenant_id, minio_client)
        if raster_key:
            thumb_path = raster_key
            meta = cog.processing_metadata or {}
        else:
            # Non-raster derived previews (3D meshes, HDR/EXR, point clouds):
            # lazy-dispatch the right converter task and return 202. The FE's
            # thumbnail loader treats non-200 as "no thumbnail" and renders a
            # generic icon; the thumbnail appears on a later render once the
            # worker task completes.
            if _lazy_dispatch_thumbnail(db, cog, current_user):
                return JSONResponse(
                    status_code=202,
                    content={"status": "pending", "message": "Thumbnail generation queued."},
                )
            return JSONResponse(
                status_code=200,
                content={"status": "unavailable", "url": None},
            )

    try:
        response = {}

        # PNG thumbnail
        if thumb_path:
            url = minio_client.presigned_client.presigned_get_object(
                bucket_name=minio_client.bucket_name,
                object_name=thumb_path,
                expires=timedelta(seconds=900),
            )
            response["url"] = url
            response["source"] = meta.get("thumbnail_source", "auto")

        # GLB 3D preview (for USD files — rendered inline via Three.js)
        if glb_path:
            glb_url = minio_client.presigned_client.presigned_get_object(
                bucket_name=minio_client.bucket_name,
                object_name=glb_path,
                expires=timedelta(seconds=900),
            )
            response["glb_url"] = glb_url

        if not response:
            raise HTTPException(status_code=404, detail="No preview available")

        # GY.V1a: one audit row per preview URL handed to the user.
        # 1788 CE carve: delivery_audit is paid; CE records no delivery row
        # (audit-only side effect — the hand-off itself is unchanged).
        from utils.paid_import import optional_paid
        _delivery_audit = optional_paid("services.delivery_audit")
        record_delivery = (_delivery_audit.record_delivery
                           if _delivery_audit is not None
                           else (lambda *a, **kw: None))

        if thumb_path:
            record_delivery(
                db, delivery_path="presign", actor_user_id=current_user.id,
                tenant_id=current_user.tenant_id, resource_id=cog_id,
                object_key=thumb_path, expiry_seconds=900, request=http_request,
                metadata={"asset": "thumbnail"},
            )
        if glb_path:
            record_delivery(
                db, delivery_path="presign", actor_user_id=current_user.id,
                tenant_id=current_user.tenant_id, resource_id=cog_id,
                object_key=glb_path, expiry_seconds=900, request=http_request,
                metadata={"asset": "glb_preview"},
            )
        return JSONResponse(content=response)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Preview not found in storage: %s", e)
        raise HTTPException(status_code=404, detail="Preview not found in storage")

@router.post("/{cog_id}/thumbnail")
async def upload_thumbnail(
    cog_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
):
    """
    Upload a custom thumbnail for a cog (e.g., canvas screenshot from 3D viewer).
    Overwrites any auto-generated thumbnail.
    """
    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="File not found")

    # Get the content hash for the deterministic thumbnail path
    from services.assets_cogs_files_service import ContentBlobNotFoundError, get_content_blob_or_raise

    try:
        content_blob = get_content_blob_or_raise(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    except ContentBlobNotFoundError:
        raise HTTPException(status_code=404, detail="No content blob found")

    # Read uploaded PNG
    data = await file.read()
    thumb_path = f"thumbnails/{content_blob.id}.png"

    # Upload to MinIO
    import io
    minio_client.client.put_object(
        minio_client.bucket_name,
        thumb_path,
        io.BytesIO(data),
        length=len(data),
        content_type="image/png",
    )

    # Update cog metadata
    meta = cog.processing_metadata or {}
    meta["thumbnail_path"] = thumb_path
    meta["thumbnail_source"] = "user"
    meta["thumbnail_generated_at"] = datetime.utcnow().isoformat()
    cog.processing_metadata = meta
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(cog, "processing_metadata")
    db.commit()

    return {"status": "ok", "thumbnail_path": thumb_path}

@service_router.post("/workflows/create-preview", status_code=status.HTTP_201_CREATED)
async def create_preview(
    output_folder_id: int = Form(...),
    source_hash: str = Form(...),  # SHA256 hash of source file in MinIO
    preview_filename: str = Form("preview.jpg"),  # Output filename
    tenant_id: int = Form(...),
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Creates a preview file by copying an existing file from MinIO.
    Used to create preview.jpg from the first processed file.
    """
    # Cross-tenant guard: a run_task node token may only write to its own tenant.
    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=tenant_id)
    logging.info(f"Create preview: {preview_filename} from hash {source_hash}")
    try:
        return _acf_svc.create_preview(
            db,
            output_folder_id=output_folder_id,
            source_hash=source_hash,
            preview_filename=preview_filename,
            tenant_id=tenant_id,
            principal=principal,
        )
    except ServiceError as e:
        raise _map_acf_service_error(e)

@router.get("/{cog_id}/processing-history", response_model=schemas.ProcessingHistoryResponse)
def get_processing_history(
    cog_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Returns complete processing history for an asset.
    Shows all workflows that have processed this file.
    """
    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="Asset not found")

    # GSEC R-047: this route serves the cog's processing history + cost/quality metrics, so it needs the same
    # per-cog view-grant check as get_cog/stream (tenant-scoped lookup
    # alone is an intra-tenant read bypass).
    if not can_user_view_cog(db, current_user.id, cog_id):
        raise HTTPException(status_code=404, detail="Asset not found")

    metadata = cog.processing_metadata or {}

    # Calculate total processing time
    total_time = 0
    for run in metadata.get('processing_history', []):
        for node in run.get('nodes_executed', []):
            total_time += node.get('duration_seconds', 0)

    return {
        "asset_id": cog.id,
        "original_filename": cog.name,
        "created_at": cog.created_at,
        "processing_history": metadata.get('processing_history', []),
        "derived_assets": metadata.get('derived_assets', []),
        "total_processing_cost": cog.total_processing_cost,
        "total_processing_time_seconds": total_time,
        "processing_count": cog.processing_count,
        "last_processed_at": cog.last_processed_at,
        "quality_metrics": metadata.get('quality_metrics', {})
    }

@router.get("/search-by-processing", response_model=List[schemas.Cog])
def search_by_processing(
    workflow_name: Optional[str] = None,
    node_kind: Optional[str] = None,
    status: Optional[str] = None,
    min_cost: Optional[int] = None,
    max_cost: Optional[int] = None,
    processed_after: Optional[datetime] = None,
    has_errors: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Search for assets by their processing history.
    Uses PostgreSQL JSONB operators for efficient queries.
    """
    from services.assets_cogs_files_service import search_by_processing as _search_by_processing_svc

    return _search_by_processing_svc(
        db,
        current_user=current_user,
        workflow_name=workflow_name,
        node_kind=node_kind,
        status=status,
        min_cost=min_cost,
        max_cost=max_cost,
        processed_after=processed_after,
        has_errors=has_errors,
        limit=limit,
        offset=offset,
    )

@router.get("/processing-analytics")
def get_processing_analytics(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    workflow_name: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get aggregate analytics on processing history.
    Returns cost breakdowns, most-used workflows, failure rates, etc.
    """
    from services.assets_cogs_files_service import processing_analytics as _processing_analytics_svc

    return _processing_analytics_svc(
        db,
        current_user=current_user,
        start_date=start_date,
        end_date=end_date,
        workflow_name=workflow_name,
    )

@service_router.get("/folder/{folder_id}/files")
async def get_folder_files_recursive(
    folder_id: int,
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal)
):
    """
    Service endpoint: Recursively get all files in a folder for zip download.

    Returns folder name and list of all files with their MinIO paths and relative paths.
    Used by zip_folder_download DAG to create folder archives.
    """
    from services.assets_cogs_files_service import (
        FolderNotFoundError,
        NotAFolderError,
        get_folder_files_recursive as _get_folder_files_svc,
    )

    try:
        return _get_folder_files_svc(db, folder_id=folder_id, principal=principal)
    except FolderNotFoundError:
        raise HTTPException(status_code=404, detail="Folder not found")
    except NotAFolderError:
        raise HTTPException(status_code=400, detail="Specified cog is not a folder")

@router.get("/{cog_id}/download",
            dependencies=[Depends(require_auditor_data_content)])
async def download_file(
    cog_id: int,
    http_request: Request,
    use_presigned: bool = False,  # If true, return presigned URL instead of streaming
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Downloads a file with permission checks.

    For small files: streams directly through API
    For large files (use_presigned=true): returns presigned URL for direct MinIO download

    Returns file with correct Content-Disposition header so browser shows
    the user-friendly filename instead of the internal hash.

    GY.V1b (GSEC R-112): when the watermark feature is on and the asset's data
    label is at or above `compliance.watermark_min_label`, no presigned URL is
    handed out — the service returns the watermarked bytes and this route sends
    them as a stream (visible + invisible layers applied, audit row path
    'watermark').
    """
    from fastapi.responses import JSONResponse, Response

    # FX.110 (owner decision Q97=A) — outside collaborators: a per-grant
    # `no_download` restriction on this external user's grant(s) refuses
    # the download (403 EXTERNAL_NO_DOWNLOAD). Internal users pass.
    try:
        from services import outside_collaborators as _fx110
        _fx110_cog = crud.get_cog_by_id_and_tenant(
            db, cog_id=cog_id, tenant_id=current_user.tenant_id)
        if _fx110_cog is not None:
            _fx110.require_can_download(db, current_user, _fx110_cog)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — never 500 the download on a gate
        pass  # read failure degrades to the normal capability checks

    # CUIP.11e — the SAME mobile-CUI guard CUIP.11d put on GET /files/{id}/content
    # (one `enforce_mobile_download` call, one CUIP.12 audit row). A phone holding
    # a mobile-client token could otherwise fetch a MARKED asset's bytes straight
    # through this route and skip the shelf verdict entirely. Placed after the
    # tenant-scoped lookup/permission checks above and BEFORE the service call, so
    # no presigned URL, redirect or stream is ever handed out to a refused phone.
    # resource_id is the COG id — the same id the shelf marks and the files route
    # passes. No-op for every non-mobile azp (web/desktop/API-key behaviour
    # unchanged) and for unmarked cogs; `azp_from_request` reads the bearer the
    # auth dependency above already signature-validated.
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
        result = _acf_svc.download_file(
            db, cog_id=cog_id, use_presigned=use_presigned,
            current_user=current_user, minio_client=minio_client,
            http_request=http_request,
        )
    except ServiceError as e:
        raise _map_acf_service_error(e)
    # GY.V1b: forensic watermark ID on delivery (absent when the
    # delivery.watermark_policy says none applies to this cog/user).
    headers = {}
    if result.get("watermark_id"):
        headers["X-MOD-Watermark"] = result["watermark_id"]
    # GY.V2 R-133: delivery marking headers on EVERY labelled download.
    #   X-MOD-Classification = the file's label (export banner ITAR /
    #     EAR (ECCN ..) / CUI//.. wins over the bare level when present).
    #   X-MOD-Handling = short CUI/EAR/ITAR handling caveat (only those).
    # For a presigned S3 URL the object cannot carry response headers, so
    # the API's JSON response carries the marking (the browser never sees
    # extra headers on the direct S3 download) — noted per R-133.
    _mark = result.get("marking") or result.get("label")
    if _mark:
        headers["X-MOD-Classification"] = _mark
    if result.get("handling"):
        headers["X-MOD-Handling"] = result["handling"]
    # GSEC FX.79 p3b — an admin data-mark on this asset (CUI / ITAR /
    # export-controlled, services/data_markings) stamps the delivery:
    # X-MOD-Marking on the response, for BOTH the streamed bytes and the
    # presigned-URL JSON (a presigned S3 object cannot carry response
    # headers, so the JSON body is where the marking also rides — same
    # R-133 note as the GY.V2 classification headers). Bytes untouched.
    try:
        from services import marking_stamps as _mk
        _mk_stamp = _mk.export_stamp(db, current_user.tenant_id, "asset", cog_id)
        headers.update(_mk.stamp_headers(_mk_stamp))
        if _mk_stamp and isinstance(result, dict):
            # The presigned-URL shape: the marking rides in the body too.
            result = {**result, **_mk.manifest_fields(_mk_stamp)}
    except Exception:  # noqa: BLE001 — a stamp must never break a download
        pass
    if result.get("watermarked"):
        # Watermarked bytes are streamed, not presigned: the client cannot get
        # the original object, so the payload id is only in the header + audit.
        headers["X-MOD-Watermark-State"] = result.get("watermark_state", "applied")
        return Response(
            content=result["content"],
            media_type=result.get("content_type") or "application/octet-stream",
            headers={
                **headers,
                "Content-Disposition": f'attachment; filename="{result["filename"]}"',
            },
        )
    return JSONResponse(content=result,
                        headers=headers or None)

# GY.C16 follow-up: NOT guarded — this route auth inline via ?token= for
# iframe embeds (no Authorization header), so a route-level Bearer funnel
# would 401 normal iframe users. Auditor data-content enforcement for the
# iframe surface lives on the /download + /preview-url routes above.
@router.get("/{cog_id}/stream/{filename:path}")
@router.get("/{cog_id}/stream")
async def stream_file(
    cog_id: int,
    http_request: Request,
    filename: Optional[str] = None,
    token: Optional[str] = None,
    db: Session = Depends(get_db),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
    authorization: Optional[str] = Header(None),
):
    """
    Stream a file through the API for inline viewing.
    Accepts auth via Authorization header OR ?token= query param (for iframes/embeds).
    Sets Content-Type based on file extension. No X-Frame-Options or CORS issues.
    """
    from fastapi.responses import StreamingResponse
    from keycloak_auth import get_current_user_claims
    from dependencies import get_current_user as resolve_user

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
    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="File not found")

    # GSEC R-047: this route serves the object's bytes directly, so it needs the
    # same per-cog view-grant check as get_cog/download (the tenant-scoped lookup
    # alone is an intra-tenant read bypass — any user in the tenant could stream
    # any cog).
    if not can_user_view_cog(db, current_user.id, cog_id):
        raise HTTPException(status_code=404, detail="File not found")

    # FX.90 p2b (Q76=A) — malware quarantine: a flagged artefact is never
    # streamed either (same 423 as the /download route).
    from services import files_service as _fx90
    _fx90_cog = crud.get_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if _fx90_cog is not None and _fx90.get_quarantine(_fx90_cog) is not None:
        raise HTTPException(
            status_code=423,
            detail="Download blocked: this artefact is malware-quarantined; "
                   "a platform admin must release it before it can be streamed",
            headers={"X-Error-Code": "MALWARE_QUARANTINED"},
        )

    # CUIP.26 — the mobile-CUI guard for the iframe/stream surface (the GY.C16
    # note above says this route was NOT guarded because it authenticates inline
    # from ?token=, so mobile_cui_policy.azp_from_request — which reads the
    # Authorization header — never sees the bearer a phone used through ?token=.
    # Read the azp of the token THIS route actually used (`bearer` above: the
    # header bearer if present, else the ?token= value) with
    # jose.jwt.get_unverified_claims — safe ONLY because get_current_user_claims
    # already signature-validated that same token before we get here; we only
    # need to know WHICH client it belongs to, never to re-verify it. An
    # undecodable token means no azp: treated as non-mobile, the guard no-ops
    # and the route is unchanged. The call is the exact CUIP.11e shape, keyed
    # on the COG id, placed after the token/permission checks and BEFORE any
    # marking audit row, presign or streamed byte is handed out.
    from jose import jwt as _cui_jwt
    from utils.paid_import import optional_paid
    _cui_paid = optional_paid("services.mobile_cui_policy")
    _cui_enforce = (_cui_paid.enforce_mobile_download
                    if _cui_paid is not None
                    else (lambda *a, **kw: None))

    _cui_azp = None
    try:
        _cui_azp = _cui_jwt.get_unverified_claims(bearer).get("azp")
    except Exception:
        _cui_azp = None  # unreadable claims: not a mobile caller (route unchanged)

    _cui_enforce(
        db,
        token_azp=_cui_azp,
        device_id_header=http_request.headers.get("X-MOD-Device-Id"),
        device_token_header=http_request.headers.get("X-MOD-Device-Token"),
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        resource_type="asset",
        resource_id=cog_id,
    )

    # GSEC FX.79 p4a — streaming a data-marked file is audit-logged
    # (no-op when the cog carries no live mark; never raises).
    from utils.paid_import import optional_paid
    _dm_p4a = optional_paid("services.data_markings")
    # GSEC FX.79 p4b — an ITAR-class mark restricts the stream to confirmed
    # US persons; the denial is audited inside the gate (p4a record path)
    # and the read row is never written.
    if _dm_p4a is not None:  # FX.162.p5 — CE carve: no markings -> no US gate, no audit stamp
        try:
            _dm_p4a.assert_us_person_access(
                db, current_user.tenant_id, "asset", cog_id,
                user_id=current_user.id, action="stream")
        except _dm_p4a.DataMarkingBlockedError as _dm_us_exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "detail": _dm_us_exc.message,
                    "code": _dm_us_exc.code,
                    "labels": (_dm_us_exc.details or {}).get("labels"),
                    "action": "asset_stream",
                },
                headers={"X-Error-Code": _dm_us_exc.code},
            )
        _dm_p4a.record_marked_access(
            db, tenant_id=current_user.tenant_id, user_id=current_user.id,
            resource_type="asset", resource_id=cog_id, action="stream")

    # Get storage path (File record or ContentBlob fallback)
    storage_path = None
    mime_type = "application/octet-stream"

    file_record = _acf_crud.get_active_file_for_cog_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )

    if file_record:
        storage_path = file_record.storage_path
        mime_type = file_record.mime_type or mime_type
    elif cog.latest_version_id:
        version = _acf_crud.get_version_by_id(db, cog.latest_version_id)
        if version and version.content_id:
            blob = _acf_crud.get_content_blob_by_id_and_tenant(
                db, blob_id=version.content_id, tenant_id=current_user.tenant_id
            )
            if blob:
                storage_path = blob.storage_path
                mime_type = blob.mime_type or mime_type

    if not storage_path:
        raise HTTPException(status_code=404, detail="File not found")

    storage_scheme = os.getenv('STORAGE_SCHEME', 's3')
    sp = storage_path
    if sp.startswith((f'{storage_scheme}://', 's3://', 'minio://')):
        path_after_scheme = sp.split('://', 1)[1]
        object_path = '/'.join(path_after_scheme.split('/')[1:]) if '/' in path_after_scheme else path_after_scheme
    else:
        object_path = sp

    try:
        response = minio_client.client.get_object(minio_client.bucket_name, object_path)
    except Exception as e:
        logger.error("Error streaming file: %s", e)
        raise HTTPException(status_code=500, detail="Error streaming file")

    # Owner decision 09-29: this endpoint reads the object server-side and
    # re-sends it — genuine MOD egress regardless of house/BYO — so it must
    # be metered (via_mod_proxy=True, see _meter_egress_stream() above).
    # GY.V1a: bytes handed to the user through the API — audit it (best-effort).
    # 1788 CE carve: delivery_audit is paid; CE records no delivery row
    # (audit-only side effect — the hand-off itself is unchanged).
    from utils.paid_import import optional_paid
    _delivery_audit = optional_paid("services.delivery_audit")
    record_delivery = (_delivery_audit.record_delivery
                       if _delivery_audit is not None
                       else (lambda *a, **kw: None))

    record_delivery(
        db, delivery_path="stream", actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id, resource_id=cog_id,
        object_key=object_path, request=http_request,
        metadata={"asset": "stream"},
    )
    # GY.V2 R-133: export-control marking banner on the delivery headers
    # (X-MOD-Classification = the cog's label, export banner wins when
    # present; X-MOD-Handling = short CUI/EAR/ITAR caveat). Absent for
    # unlabelled cogs. Opt-in filename marking when the setting is on.
    stream_headers = {
        "Content-Disposition": f'inline; filename="{cog.name}"',
        "Access-Control-Allow-Origin": "*",
    }
    try:
        from services import data_classification as _dc_mark
        _lbl = getattr(cog, "data_classification", None)
        mark = _dc_mark.delivery_classification_header(_lbl, cog)
        if mark:
            stream_headers["X-MOD-Classification"] = mark
            _hc = _dc_mark.handling_caveat(_lbl)
            if _hc:
                stream_headers["X-MOD-Handling"] = _hc
            stream_headers["Content-Disposition"] = (
                f'inline; filename="{_dc_mark.marked_download_filename(cog.name, _lbl, db=db)}"')
    except Exception:
        pass
    # GSEC FX.79 p3b — the admin data-mark stamp on the inline stream too
    # (X-MOD-Marking). Bytes untouched; the marking rides on the headers.
    try:
        from services import marking_stamps as _mk_stream
        _mk_stamp = _mk_stream.export_stamp(db, current_user.tenant_id,
                                            "asset", cog_id)
        stream_headers.update(_mk_stream.stamp_headers(_mk_stamp))
    except Exception:  # noqa: BLE001 — a stamp must never break a stream
        pass
    return StreamingResponse(
        _meter_egress_stream(
            response, 8192, tenant_id=current_user.tenant_id,
            context=f"stream cog_id={cog_id}",
        ),
        media_type=mime_type,
        headers=stream_headers,
    )

@router.get("/{cog_id}/download-folder",
            dependencies=[Depends(require_auditor_data_content)])
async def download_folder(
    cog_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Downloads a folder and all its contents as a zip file.

    Dispatches a zip task to ApiTaskQueue for the Go worker to claim,
    then returns a presigned URL. Zip files are stored temporarily in
    MinIO and auto-deleted after 24 hours.
    """
    # overage-spend-cap §1 — block file egress once the tenant is over their cap.
    from utils.usage_enforcement import check_overage_cap
    check_overage_cap(db=db, tenant_id=current_user.tenant_id, raise_on_cap=True)
    import requests
    import time
    from datetime import timezone, timedelta

    # Get the cog
    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="Folder not found")

    if cog.cog_type not in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
        raise HTTPException(status_code=400, detail="Can only download folder type cogs")

    # Check read permission
    if not can_user_view_cog(db, current_user.id, cog_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to download this folder"
        )

    # CUIP.11f — the bulk gate: a phone could otherwise pull a MARKED asset's
    # bytes out of a folder zip and skip the shelf verdict. The zip packages
    # the WHOLE tree, so the guard runs for EVERY FILE cog the archive will
    # include, BEFORE the zip build / presign starts. The ids walk is a
    # generator consumed ONLY when the guard decides the caller is the mobile
    # client, so web/desktop/API-key azps run byte-for-byte the old path with
    # no extra query. The WHOLE request is refused with the FIRST refusal's
    # 403 — silently dropping the marked member would ship an archive whose
    # contents differ from what the caller asked for.
    # 1788 CE carve: mobile_cui_policy is paid; CE has no mobile-client shelf
    # verdict, so the route keeps its pre-GY.V1d web behaviour (no-op).
    from utils.paid_import import optional_paid
    _mobile_cui = optional_paid("services.mobile_cui_policy")
    azp_from_request = (_mobile_cui.azp_from_request
                        if _mobile_cui is not None
                        else (lambda *a, **kw: None))
    enforce_mobile_download_many = (_mobile_cui.enforce_mobile_download_many
                                    if _mobile_cui is not None
                                    else (lambda *a, **kw: None))
    from crud import assets_cogs_files as _acf_files_crud

    def _descendant_file_cog_ids():  # lazy: consumed only for mobile callers
        stack, seen = [cog_id], set()
        while stack:
            for child in _acf_files_crud.get_active_children(db, stack.pop()):
                if (child.tenant_id != current_user.tenant_id
                        or child.id in seen):
                    continue
                seen.add(child.id)
                if child.cog_type in (models.CogType.MASTER_FOLDER,
                                      models.CogType.ASSET_FOLDER):
                    stack.append(child.id)
                elif child.cog_type == models.CogType.FILE:
                    yield child.id

    enforce_mobile_download_many(
        db,
        cog_ids=_descendant_file_cog_ids(),
        token_azp=azp_from_request(http_request),
        device_id_header=http_request.headers.get("X-MOD-Device-Id"),
        device_token_header=http_request.headers.get("X-MOD-Device-Token"),
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        resource_type="asset",
    )

    try:
        from datetime import timedelta
        from services.file_processing import build_zip_archive
        # FX.162.p5d — CE carve: services/watermark_delivery is paid-only
        # (manifest-denied). Absent -> no watermark policy on the zip build
        # (byte-identical archive) and the fail-closed handler below stays
        # valid via a never-raised stand-in class.
        from utils.paid_import import optional_paid
        _wm_del = optional_paid("services.watermark_delivery")
        if _wm_del is None:
            class WatermarkBlockError(Exception):  # never raised on CE
                pass

            def audit_watermark_refusal(*a, **k):  # noqa: CE stand-in
                return None

            def delivery_watermark_policy(db):  # noqa: CE stand-in
                return None
        else:
            WatermarkBlockError = _wm_del.WatermarkBlockError
            audit_watermark_refusal = _wm_del.audit_watermark_refusal
            delivery_watermark_policy = _wm_del.delivery_watermark_policy

        # In-process zip build. The legacy path enqueued a worker task,
        # waited for the worker to claim + pull image + start the
        # zip-download container, then polled for completion. That
        # added 5-10s of latency on every download AND inherited the
        # same STS-handoff failure class as the file_hasher path.
        # Running here gives us:
        #   - Vault-loaded MinIO service creds, no STS shipping.
        #   - No image pull, no container start.
        #   - Same logic — the legacy container's two API call-backs
        #     (POST /cogs/service/cogs/files, GET /cogs/service/folder/
        #     {id}/files) are now direct DB queries in the service
        #     module.
        result = build_zip_archive(
            db=db,
            tenant_id=current_user.tenant_id,
            folder_id=cog_id,
            # GY.V1c: watermark labelled image members in place. Off / no
            # user => byte-identical to today (wm_policy None, user None).
            current_user=current_user,
            watermark_policy=delivery_watermark_policy(db),
        )
        zip_object = result["zip_object"]
        zip_filename = f"{cog.name}.zip"

        logging.info(
            "Zip built in-process for folder %s: %s (%.1f MB, %.1fs)",
            cog_id, zip_object,
            result["zip_size_bytes"] / (1024 * 1024),
            result["duration_seconds"],
        )

        # Presigned URL for the browser.
        presigned_url = minio_client.presigned_client.presigned_get_object(
            bucket_name=minio_client.bucket_name,
            object_name=zip_object,
            expires=timedelta(seconds=900),
        )

        # GT.D10 — meter + enforce the egress cap (402 at 100%). The check
        # raises HTTPException which we MUST let propagate, so it is kept
        # separate from the best-effort accounting error handler below.
        # GT.D4b — services.egress_meter is Paid (absent in the CE carve); egress
        # metering is a billing side effect, so skip it silently on CE.
        try:
            from services.egress_meter import check_and_record
        except ImportError:
            check_and_record = None
        if check_and_record is not None:
            check_and_record(
                db, current_user.tenant_id, int(result.get("zip_size_bytes") or 0),
                kind="folder_zip",
            )

        # GY.V1a: presign delivery audit for the folder zip.
        # 1788 CE carve: delivery_audit is paid; CE records no delivery row
        # (audit-only side effect — the hand-off itself is unchanged).
        from utils.paid_import import optional_paid
        _delivery_audit = optional_paid("services.delivery_audit")
        record_delivery = (_delivery_audit.record_delivery
                           if _delivery_audit is not None
                           else (lambda *a, **kw: None))

        record_delivery(
            db, delivery_path="presign", actor_user_id=current_user.id,
            tenant_id=current_user.tenant_id, resource_id=cog_id,
            object_key=zip_object, expiry_seconds=900, request=http_request,
            metadata={"asset": "folder_zip"},
        )
        # GY.V2 R-133: archive marking headers on the zip hand-off. The zip
        # itself carries a top-level CLASSIFICATION.txt when any member is
        # labelled above public/internal (built by build_zip_archive).
        from fastapi.responses import JSONResponse
        _zheaders = {}
        _zlab = result.get("zip_max_label")
        if _zlab:
            from utils.paid_import import optional_paid
            _dc_z = optional_paid("services.data_classification")
            _zheaders["X-MOD-Classification"] = _zlab
            if _dc_z is not None:  # 1788 CE carve: no taxonomy -> no caveat header
                _zc = _dc_z.handling_caveat(_zlab)
                if _zc:
                    _zheaders["X-MOD-Handling"] = _zc
        return JSONResponse(
            content={
                "download_url": presigned_url,
                "filename": zip_filename,
                "expires_in_seconds": 900,
            },
            headers=_zheaders or None,
        )

    except HTTPException:
        raise
    except WatermarkBlockError as e:
        # GY.V1c fail-closed: a labelled image member could not be watermarked
        # and the policy is 'block'. Refuse the whole zip (audited), never
        # ship a partially-unmarked archive.
        audit_watermark_refusal(
            db, delivery_path="presign", actor_user_id=current_user.id,
            tenant_id=current_user.tenant_id, resource_id=cog_id,
            object_key=None, reason=e.reason,
            label=getattr(cog, "data_classification", None),
            request=http_request)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This folder contains a labelled file that cannot be "
                   "delivered without a watermark under your tenant's "
                   "watermark policy.") from e
    except Exception as e:
        logging.error(f"Error creating zip for folder {cog_id}: {e}", exc_info=True)
        raise internal_http_error(500, "Could not create zip file", e) from e

@router.post("/download-multiple",
             dependencies=[Depends(require_auditor_data_content)])
async def download_multiple_items(
    cog_ids: List[int],
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Downloads multiple cogs (files and/or folders) as a single zip file.

    Dispatches a zip task to ApiTaskQueue for the Go worker to claim,
    then returns a presigned URL. Files are placed in root of zip,
    folders preserve their structure.
    """
    # overage-spend-cap §1 — block file egress once the tenant is over their cap.
    from utils.usage_enforcement import check_overage_cap
    check_overage_cap(db=db, tenant_id=current_user.tenant_id, raise_on_cap=True)
    import requests
    import time
    from datetime import timezone, timedelta

    if not cog_ids:
        raise HTTPException(status_code=400, detail="At least one cog_id is required")

    # Validate all cogs exist and user has access
    cogs = []
    for cog_id in cog_ids:
        cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
        if not cog:
            raise HTTPException(status_code=404, detail=f"Cog {cog_id} not found")
        if not can_user_view_cog(db, current_user.id, cog_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have permission to download cog {cog_id}"
            )
        cogs.append(cog)

    # CUIP.11f — the bulk gate on the multi-cog zip: the guard runs for EVERY
    # cog this route is about to package (the ids the loop above already
    # validated — no new query loop), BEFORE the zip build / presign starts.
    # Non-mobile azps no-op inside the helper without touching `cog_ids`, so
    # web behaviour is byte-for-byte unchanged. Any marked cog in the set that
    # fails the phone's verdict refuses the WHOLE request with that first
    # refusal's 403 — the zip is never built, never partially packaged.
    # 1788 CE carve: mobile_cui_policy is paid; CE has no mobile-client shelf
    # verdict, so the route keeps its pre-GY.V1d web behaviour (no-op).
    from utils.paid_import import optional_paid
    _mobile_cui = optional_paid("services.mobile_cui_policy")
    azp_from_request = (_mobile_cui.azp_from_request
                        if _mobile_cui is not None
                        else (lambda *a, **kw: None))
    enforce_mobile_download_many = (_mobile_cui.enforce_mobile_download_many
                                    if _mobile_cui is not None
                                    else (lambda *a, **kw: None))

    enforce_mobile_download_many(
        db,
        cog_ids=cog_ids,
        token_azp=azp_from_request(http_request),
        device_id_header=http_request.headers.get("X-MOD-Device-Id"),
        device_token_header=http_request.headers.get("X-MOD-Device-Token"),
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        resource_type="asset",
    )

    try:
        # In-process zip build — same rationale as the folder
        # download path above. Direct MinIO IO + DB queries, no
        # worker dispatch, no STS handoff. Legacy worker container
        # (CORE/DockerContainers/zip-download/) is no longer in use
        # for this endpoint.
        from services.file_processing import build_zip_archive
        # FX.162.p5d — CE carve: services/watermark_delivery is paid-only
        # (manifest-denied). Absent -> no watermark policy on the zip build
        # (byte-identical archive) and the fail-closed handler below stays
        # valid via a never-raised stand-in class.
        from utils.paid_import import optional_paid
        _wm_del = optional_paid("services.watermark_delivery")
        if _wm_del is None:
            class WatermarkBlockError(Exception):  # never raised on CE
                pass

            def audit_watermark_refusal(*a, **k):  # noqa: CE stand-in
                return None

            def delivery_watermark_policy(db):  # noqa: CE stand-in
                return None
        else:
            WatermarkBlockError = _wm_del.WatermarkBlockError
            audit_watermark_refusal = _wm_del.audit_watermark_refusal
            delivery_watermark_policy = _wm_del.delivery_watermark_policy
        result = build_zip_archive(
            db=db,
            tenant_id=current_user.tenant_id,
            cog_ids=cog_ids,
            # GY.V1c: watermark labelled image members in place. Off / no
            # user => byte-identical to today.
            current_user=current_user,
            watermark_policy=delivery_watermark_policy(db),
        )
        zip_object = result["zip_object"]
        zip_filename = f"{result['folder_name']}.zip"
        logging.info(
            "Zip built in-process for cog_ids=%s: %s (%d files, %.1f MB, %.1fs)",
            cog_ids, zip_object, result["files_count"],
            result["zip_size_bytes"] / (1024 * 1024),
            result["duration_seconds"],
        )

        presigned_url = minio_client.presigned_client.presigned_get_object(
            bucket_name=minio_client.bucket_name,
            object_name=zip_object,
            expires=timedelta(seconds=900),
        )

        # GT.D10 — meter + enforce the egress cap (402 at 100%). Kept separate
        # from the accounting error handler so the 402 propagates.
        # CE carve: egress_meter is paid; CE skips metering (no cap) and the
        # zip is created normally.
        from utils.paid_import import optional_paid
        _egress_meter = optional_paid("services.egress_meter")
        if _egress_meter is not None:
            _egress_meter.check_and_record(
                db, current_user.tenant_id,
                int(result.get("zip_size_bytes") or 0),
                kind="multi_zip",
            )

        # GY.V1a: presign delivery audit for the multi-cog zip.
        # 1788 CE carve: delivery_audit is paid; CE records no delivery row
        # (audit-only side effect — the hand-off itself is unchanged).
        _delivery_audit = optional_paid("services.delivery_audit")
        record_delivery = (_delivery_audit.record_delivery
                           if _delivery_audit is not None
                           else (lambda *a, **kw: None))

        record_delivery(
            db, delivery_path="presign", actor_user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            resource_id=(cog_ids[0] if cog_ids else 0),
            object_key=zip_object, expiry_seconds=900, request=http_request,
            metadata={"asset": "multi_zip", "cog_ids": list(cog_ids)},
        )
        # GY.V2 R-133: archive marking headers (CLASSIFICATION.txt is inside
        # the zip when any member is labelled above public/internal).
        from fastapi.responses import JSONResponse
        _zheaders = {}
        _zlab = result.get("zip_max_label")
        if _zlab:
            from utils.paid_import import optional_paid
            _dc_z = optional_paid("services.data_classification")
            _zheaders["X-MOD-Classification"] = _zlab
            if _dc_z is not None:  # 1788 CE carve: no taxonomy -> no caveat header
                _zc = _dc_z.handling_caveat(_zlab)
                if _zc:
                    _zheaders["X-MOD-Handling"] = _zc
        return JSONResponse(
            content={
                "download_url": presigned_url,
                "filename": zip_filename,
                "file_count": result["files_count"],
                "expires_in_seconds": 900,
            },
            headers=_zheaders or None,
        )

    except HTTPException:
        raise
    except WatermarkBlockError as e:
        # GY.V1c fail-closed — see the folder-zip handler above.
        audit_watermark_refusal(
            db, delivery_path="presign", actor_user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            resource_id=(cog_ids[0] if cog_ids else 0),
            object_key=None, reason=e.reason, label=None, request=http_request)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The selection contains a labelled file that cannot be "
                   "delivered without a watermark under your tenant's "
                   "watermark policy.") from e
    except Exception as e:
        logging.error(f"Error creating zip for multiple items: {e}", exc_info=True)
        raise internal_http_error(500, "Could not create zip file", e) from e

@router.get("/download-zip/{path:path}",
            dependencies=[Depends(require_auditor_data_content)])
async def download_zip_proxy(
    path: str,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Proxy download for zip files from MinIO.
    Streams the file through the API since presigned URLs don't work
    with the external MinIO endpoint.
    """
    from starlette.responses import StreamingResponse

    # GSEC R-156 — this route used to `get_object(bucket, path)` for
    # path)` for ANY key in the tenant bucket, so a caller could stream any
    # object (not just zips MOD built for them). Every zip the API hands out
    # lives under `downloads/temp/` (services/file_processing.py:build_zip_archive),
    # so scope `path` to that prefix — mirrors how the other download routes
    # only stream keys they themselves created. Refuse (403) anything outside:
    # other prefixes, empty, or a path that normalises back out of the prefix
    # (e.g. `downloads/temp/../../secrets/x`). The C16 auditor guard
    # (require_auditor_data_content) on the decorator stays intact above.
    _ZIP_TEMP_PREFIX = "downloads/temp/"
    _p = (path or "").lstrip("/")
    if (
        not _p.startswith(_ZIP_TEMP_PREFIX)
        or ".." in _p.split("/")
        or _p == _ZIP_TEMP_PREFIX.rstrip("/")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Zip download path is outside the allowed downloads/temp/ prefix",
        )

    # CUIP.11f — the zip-proxy route streams a zip the API ALREADY BUILT and
    # stored under downloads/temp/ (GSEC R-156's prefix check above). The path
    # is an object key: nothing here — not the route, not the object — maps it
    # back to the cogs that went into the archive, so no per-cog shelf verdict
    # can be computed. A phone therefore cannot be allowed through at all:
    # refuse mobile callers outright (403 mobile_bulk_download_blocked) BEFORE
    # the object is read, audited or streamed. Web/desktop/API-key callers
    # (any other azp, or no bearer) are untouched and stream as before.
    # 1788 CE carve: refuse_mobile_bulk_download is paid; CE has no mobile
    # bulk-download refusal (web/desktop/API-key callers unchanged).
    from utils.paid_import import optional_paid
    _mobile_cui = optional_paid("services.mobile_cui_policy")
    if _mobile_cui is not None:
        _mobile_cui.refuse_mobile_bulk_download(
            http_request, path=_p, db=db,
            tenant_id=current_user.tenant_id, user_id=current_user.id)

    try:
        response = minio_client.client.get_object(minio_client.bucket_name, _p)
        filename = _p.split('/')[-1]
    except Exception as e:
        logging.error(f"Zip download proxy failed for {path}: {e}")
        raise HTTPException(status_code=404, detail="Zip file not found or expired")

    # Owner decision 09-29: the through-MOD fallback when presigning fails —
    # the API reads the zip from storage and re-sends it, genuine MOD egress
    # for house AND BYO tenants — so meter it (via_mod_proxy=True, see
    # _meter_egress_stream() above).
    # GY.V1a: bytes handed to the user through the API — audit it (best-effort).
    # 1788 CE carve: delivery_audit is paid; CE records no delivery row
    # (audit-only side effect — the hand-off itself is unchanged).
    from utils.paid_import import optional_paid
    _delivery_audit = optional_paid("services.delivery_audit")
    record_delivery = (_delivery_audit.record_delivery
                       if _delivery_audit is not None
                       else (lambda *a, **kw: None))

    record_delivery(
        db, delivery_path="stream", actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id, resource_id=0,
        object_key=path, request=http_request,
        metadata={"asset": "zip_proxy"},
    )
    return StreamingResponse(
        _meter_egress_stream(
            response, 32 * 1024, tenant_id=current_user.tenant_id,
            context=f"download-zip path={path}",
        ),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
    )

@router.post("/download-jobs", response_model=schemas.DownloadJobResponse, status_code=status.HTTP_201_CREATED)
async def create_download_job(
    request: schemas.DownloadJobCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Creates a new download job and triggers the zip creation DAG.
    Returns immediately with a job ID that can be polled for progress.
    """
    import requests as http_requests
    from datetime import timezone, timedelta

    cog_ids = request.cog_ids
    if not cog_ids:
        raise HTTPException(status_code=400, detail="At least one cog_id is required")

    # Validate all cogs exist and user has access, count total files
    total_files = 0
    folder_id = None
    for cog_id in cog_ids:
        cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
        if not cog:
            raise HTTPException(status_code=404, detail=f"Cog {cog_id} not found")
        if not can_user_view_cog(db, current_user.id, cog_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have permission to download cog {cog_id}"
            )
        # Count files (rough estimate for progress)
        if cog.cog_type == models.CogType.FILE:
            total_files += 1
        else:
            # For folders, count children (simplified - could be recursive)
            children_count = _acf_crud.count_active_file_children(db, cog_id)
            total_files += children_count or 1

        if len(cog_ids) == 1:
            folder_id = cog_id

    # Create download job record
    job_id = str(uuid.uuid4())
    download_job = models.DownloadJob(
        id=job_id,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        status=models.DownloadJobStatus.PENDING,
        total_files=total_files,
        processed_files=0
    )
    db.add(download_job)
    db.commit()
    db.refresh(download_job)

    try:
        # In-process async zip build. Returns immediately with the
        # DownloadJob's PREPARING status; a background thread does
        # the work and PATCHes the DownloadJob row as it goes (same
        # progress shape the legacy zip-download container used via
        # PATCH /download-jobs/<id>). Browser polls
        # /download-jobs/<id> for completion the same as before.
        download_job.status = models.DownloadJobStatus.PREPARING
        db.commit()

        from services.file_processing import build_zip_archive
        # FX.162.p5d — CE carve: services/watermark_delivery is paid-only
        # (manifest-denied). Absent -> no watermark policy is passed to the
        # thread (byte-identical archive) and the fail-closed handler below
        # stays valid via a never-raised stand-in class.
        from utils.paid_import import optional_paid
        _wm_del = optional_paid("services.watermark_delivery")
        if _wm_del is None:
            class WatermarkBlockError(Exception):  # never raised on CE
                pass

            def audit_watermark_refusal(*a, **k):  # noqa: CE stand-in
                return None

            def delivery_watermark_policy(db):  # noqa: CE stand-in
                return None
        else:
            WatermarkBlockError = _wm_del.WatermarkBlockError
            audit_watermark_refusal = _wm_del.audit_watermark_refusal
            delivery_watermark_policy = _wm_del.delivery_watermark_policy

        # Capture state the thread needs (no db session — the thread
        # mints its own). The watermark policy is read here (request
        # session) and passed into the thread so the labelled-image gate is
        # the same one the owner would get on a synchronous download.
        _tenant_id = current_user.tenant_id
        _user_id = current_user.id
        _folder_id = folder_id
        _cog_ids = list(cog_ids) if cog_ids else []
        _wm_policy = delivery_watermark_policy(db)

        def _run_inproc_zip():
            from database import SessionLocal
            local_db = SessionLocal()
            try:
                def _progress(stage: str, processed: int, current_file):
                    """Mirror the legacy container's PATCH /download-jobs."""
                    try:
                        row = local_db.query(models.DownloadJob).filter(
                            models.DownloadJob.id == job_id,
                        ).first()
                        if not row:
                            return
                        # Map zip-builder stages to DownloadJobStatus values.
                        stage_map = {
                            "downloading": models.DownloadJobStatus.PROCESSING,
                            "creating_zip": models.DownloadJobStatus.PROCESSING,
                            "completed": models.DownloadJobStatus.PROCESSING,
                        }
                        row.status = stage_map.get(stage, row.status)
                        row.processed_files = processed
                        if current_file:
                            row.current_file = str(current_file)[:255]
                        local_db.commit()
                    except Exception:
                        logger.exception(
                            "download-job progress update failed for job=%s",
                            job_id,
                        )

                # GY.V1c: pass a lightweight user (id + tenant only — the
                # request-scoped ORM user may be detached by now) and the
                # policy read at request time.
                from types import SimpleNamespace as _NS
                _wm_user = _NS(id=_user_id, tenant_id=_tenant_id)
                result = build_zip_archive(
                    db=local_db,
                    tenant_id=_tenant_id,
                    folder_id=_folder_id,
                    cog_ids=_cog_ids if _cog_ids else None,
                    download_job_id=job_id,
                    progress_cb=_progress,
                    current_user=_wm_user,
                    watermark_policy=_wm_policy,
                )
                # Mark done. download_url is the bucket-relative
                # object key; the /download-jobs GET endpoint mints
                # the presigned URL when the client polls.
                row = local_db.query(models.DownloadJob).filter(
                    models.DownloadJob.id == job_id,
                ).first()
                if row:
                    row.status = models.DownloadJobStatus.COMPLETED
                    row.processed_files = result["files_count"]
                    row.download_url = result["zip_object"]
                    row.filename = f"{result['folder_name']}.zip"
                    row.completed_at = datetime.now(timezone.utc)
                    local_db.commit()
                    logger.info(
                        "Download job %s completed in-process: %s "
                        "(%d files, %.1f MB, %.1fs)",
                        job_id, result["zip_object"],
                        result["files_count"],
                        result["zip_size_bytes"] / (1024 * 1024),
                        result["duration_seconds"],
                    )
            except WatermarkBlockError as e:
                # GY.V1c fail-closed: a labelled image member could not be
                # watermarked under 'block'. The job fails (no zip ships) and
                # the refusal is audited as a presign hand-off attempt.
                logger.warning(
                    "download job %s REFUSED by watermark policy: %s", job_id, e.reason)
                try:
                    row = local_db.query(models.DownloadJob).filter(
                        models.DownloadJob.id == job_id,
                    ).first()
                    if row:
                        row.status = models.DownloadJobStatus.FAILED
                        row.error_message = (
                            "refused: labelled file cannot be delivered "
                            "without a watermark (tenant watermark policy)")
                        local_db.commit()
                except Exception:
                    logger.exception("failed to mark job=%s FAILED", job_id)
                try:
                    audit_watermark_refusal(
                        local_db, delivery_path="presign",
                        actor_user_id=_user_id, tenant_id=_tenant_id,
                        resource_id=(_cog_ids[0] if _cog_ids else 0),
                        object_key=None, reason=e.reason, label=None)
                except Exception:
                    logger.exception("failed to audit refusal for job=%s", job_id)
            except Exception as e:
                logger.exception(
                    "in-process zip build failed for job=%s: %s",
                    job_id, e,
                )
                try:
                    row = local_db.query(models.DownloadJob).filter(
                        models.DownloadJob.id == job_id,
                    ).first()
                    if row:
                        row.status = models.DownloadJobStatus.FAILED
                        row.error_message = f"zip build failed: {e}"
                        local_db.commit()
                except Exception:
                    logger.exception("failed to mark job=%s FAILED", job_id)
            finally:
                local_db.close()

        import threading
        threading.Thread(target=_run_inproc_zip, daemon=True,
                         name=f"download-job-{job_id[:8]}").start()

        logging.info(
            "Zip download started in-process for job %s (folder=%s, cogs=%s)",
            job_id, folder_id, cog_ids,
        )

        return schemas.DownloadJobResponse(
            id=download_job.id,
            status=download_job.status,
            total_files=download_job.total_files,
            processed_files=download_job.processed_files,
            current_file=download_job.current_file,
            progress_percent=0,
            download_url=download_job.download_url,
            filename=download_job.filename,
            error_message=download_job.error_message,
            created_at=download_job.created_at,
            expires_at=download_job.expires_at
        )

    except Exception as e:
        logging.error(f"Download job creation failed: {type(e).__name__}: {e}", exc_info=True)
        download_job.status = models.DownloadJobStatus.FAILED
        download_job.error_message = f"Failed to start download: {str(e)}"
        db.commit()
        raise HTTPException(status_code=500, detail="Error triggering zip creation")

# Service endpoint for DAG to update job progress
@service_router.patch("/download-jobs/{job_id}")
async def update_download_job_progress(
    job_id: str,
    update: schemas.DownloadJobUpdateRequest,
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal)
):
    """
    Service endpoint: Updates download job progress.
    Called by the zip-download worker container to report progress.
    """
    try:
        _acf_svc.update_download_job_progress(db, job_id=job_id, update=update, principal=principal)
    except ServiceError as e:
        raise _map_acf_service_error(e)

    return {"status": "updated"}

@router.patch("/{cog_id}/classification", response_model=schemas.Cog)
def update_classification(
    cog_id: int,
    update: schemas.CogClassificationUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Update the data classification of a cog (CMMC AC-16).

    Classification levels: public, internal, confidential, restricted.
    """
    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="Cog not found")

    old_classification = cog.data_classification
    cog.data_classification = update.data_classification
    # GY.V2: optional export-control sub-tag (EAR ECCN / CUI category) on
    # the cog's existing custom_metadata JSON — no new column.
    subtag = getattr(update, "export_control", None)
    if subtag:
        from utils.paid_import import optional_paid
        _dc_sub = optional_paid("services.data_classification")
        if _dc_sub is not None:  # 1788 CE carve: no taxonomy -> no export sub-tag
            _dc_sub.set_export_subtag(cog, **subtag)
    db.commit()
    db.refresh(cog)

    # Audit: classification change
    try:
        from models import AuditEvent
        event_name = "CONTENT_RECLASSIFIED" if old_classification != 'internal' else "CONTENT_CLASSIFIED"
        details = {"cog_id": cog_id, "old": old_classification, "new": update.data_classification}
        if subtag:
            details["export_control"] = subtag
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name=event_name,
            details=details,
        ))
        db.commit()
    except Exception:
        pass

    return cog

@router.post("/{cog_id}/legal-hold", response_model=schemas.LegalHoldStatus)
def set_legal_hold(
    cog_id: int,
    hold_request: schemas.LegalHoldSet,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Set a legal hold on a cog and all associated workflow outputs.

    Legal hold prevents deletion by retention cleanup or user action.
    Requires reason for audit trail (CMMC/ISO 27001 compliance).
    """
    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="Cog not found")

    from datetime import datetime, timezone

    from services.assets_cogs_files_service import set_legal_hold as _set_legal_hold_svc

    held_count = _set_legal_hold_svc(db, cog=cog, current_user=current_user, reason=hold_request.reason)

    return schemas.LegalHoldStatus(
        cog_id=cog_id,
        legal_hold=True,
        reason=hold_request.reason,
        set_by_user_id=current_user.id,
        set_at=datetime.now(timezone.utc),
        outputs_held=held_count,
    )

@router.delete("/{cog_id}/legal-hold", response_model=schemas.LegalHoldStatus)
def remove_legal_hold(
    cog_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Remove a legal hold from a cog (platform admin only).

    This re-enables retention cleanup and user deletion for the associated outputs.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform admins can remove legal holds",
        )

    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="Cog not found")

    from services.assets_cogs_files_service import remove_legal_hold as _remove_legal_hold_svc

    _remove_legal_hold_svc(db, cog=cog, current_user=current_user)

    return schemas.LegalHoldStatus(
        cog_id=cog_id,
        legal_hold=False,
        outputs_held=0,
    )

@router.get("/{cog_id}/legal-hold", response_model=schemas.LegalHoldStatus)
def get_legal_hold_status(
    cog_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Check the legal hold status of a cog."""
    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise HTTPException(status_code=404, detail="Cog not found")

    from services.assets_cogs_files_service import get_legal_hold_status as _get_legal_hold_status_svc

    held_count, reason, set_by, set_at = _get_legal_hold_status_svc(db, cog=cog, current_user=current_user)

    return schemas.LegalHoldStatus(
        cog_id=cog_id,
        legal_hold=cog.is_locked or held_count > 0,
        reason=reason,
        set_by_user_id=set_by,
        set_at=set_at,
        outputs_held=held_count,
    )

__all__ = [
    'create_cog',
    'get_root_folder',
    'list_cogs',
    'search_cogs_recursive',
    'get_cog',
    'update_cog',
    'delete_cog',
    'bulk_delete_cogs',
    'restore_cog',
    'copy_cog',
    'initiate_multipart_upload',
    'complete_multipart_upload',
    'request_upload',
    'check_upload_by_hash',
    'create_cog_from_existing_hash',
    'confirm_upload',
    'upload_file',
    'list_versions',
    'add_tag_to_cog',
    'get_tags_for_cog',
    'remove_tag_from_cog',
    'create_shareable_link_for_cog',
    'list_shareable_links_for_cog',
    'delete_shareable_link',
    'revoke_all_shareable_links',
    'open_shareable_link',
    'download_shareable_link',
    # NOTE: `public_router` is deliberately NOT star-exported. The cogs
    # aggregator (routers/cogs/__init__.py) does `from .assets_cogs_files
    # import *`; exporting the router there would rebind the PACKAGE-level
    # `routers.cogs.public_router` (which also carries /{cog_id}/pdf-view,
    # included into it by the aggregator) to this submodule's router, and
    # routers/assets.py re-exports the package-level object for the
    # /v1/assets mount — silently dropping the pdf-view route.
    'batch_update_processing_history',
    'create_workflow_output_folder',
    'upload_processed_file',
    'upload_text_file',
    'register_uploaded_file',
    'set_thumbnail',
    'sldprt_conversion_callback',
    'get_converted_step',
    'pointcloud_conversion_callback',
    'get_converted_pointcloud',
    'get_thumbnail',
    'upload_thumbnail',
    'create_preview',
    'get_processing_history',
    'search_by_processing',
    'get_processing_analytics',
    'get_folder_files_recursive',
    'download_file',
    'stream_file',
    'download_folder',
    'download_multiple_items',
    'download_zip_proxy',
    'create_download_job',
    'update_download_job_progress',
    'update_classification',
    'set_legal_hold',
    'remove_legal_hold',
    'get_legal_hold_status',
]

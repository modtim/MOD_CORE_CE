"""Service layer for the assets/cogs/files domain (openspec:
stack-training-and-prewarm, Gate 2 / D9).

Business logic / orchestration for the handlers migrated out of
`routers/cogs/assets_cogs_files.py`. All data access for these endpoints
goes through `crud/assets_cogs_files.py` (plus the pre-existing
`crud/cog_cud.py` et al. for lookups the router already delegated
correctly); HTTP concerns (permission 403s, `HTTPException`, docstrings,
`response_model`) stay in the router — this module raises plain
`services._base.ServiceError` subclasses where a router-visible error is
needed, and returns rows/dicts otherwise.

Only a subset of the file's ~52 endpoints were migrated this pass. See the
migration report for the deferred list (upload orchestration, streaming/
download, background-thread service-to-service callbacks) and reasons.

Heavy/app modules (`permissions`, `cog_grants`, `models`) are imported
lazily inside functions, matching the function-local-import convention used
throughout this codebase (see api_conventions.py's design note) and
avoiding any import-time circularity with the router package.
"""
from __future__ import annotations

import fnmatch
import logging
import time
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from crud import assets_cogs_files as acf_crud

logger = logging.getLogger(__name__)


# =============================================================================
# list_cogs
# =============================================================================


def list_cogs(
    db: Session,
    *,
    current_user,
    parent_id: Optional[int],
    all_items: bool,
    search: Optional[str],
    selectable_inputs_only: bool,
    include_permission_info: bool,
    deleted: bool,
    limit: int,
    offset: int,
    sort_by: str,
    order: str,
):
    """Canonical listing for `GET /cogs/`. Mirrors the three branches of the
    pre-migration handler exactly (deleted view / all_items / default
    permission-aware children view), then attaches size + format metadata."""
    import models

    if deleted:
        from routers.cogs._shared import _attach_original_paths

        is_manager = bool(
            getattr(current_user, "is_platform_admin", False)
            or getattr(current_user, "parent_user_id", None) is None
        )
        cogs = acf_crud.list_deleted_cogs(
            db,
            tenant_id=current_user.tenant_id,
            owner_id=None if is_manager else current_user.id,
            search=search,
            sort_by=sort_by,
            order=order,
            limit=limit,
            offset=offset,
        )
        _attach_original_paths(db, cogs, current_user.tenant_id)
    elif all_items:
        cogs = acf_crud.list_owned_cogs(
            db,
            owner_id=current_user.id,
            search=search,
            selectable_inputs_only=selectable_inputs_only,
            sort_by=sort_by,
            order=order,
            limit=limit,
            offset=offset,
        )
    else:
        from permissions import can_user_view_cog

        parent_viewable = parent_id is not None and can_user_view_cog(
            db, current_user.id, parent_id
        )
        grant_cog_ids = None
        if not parent_viewable:
            from cog_grants import list_cog_grants

            grant_cog_ids = [
                g.target_id for g in list_cog_grants(
                    db, tenant_id=current_user.tenant_id,
                    user_id=current_user.id, include_expired=False)
            ]
        cogs = acf_crud.list_visible_children(
            db,
            parent_id=parent_id,
            tenant_id=current_user.tenant_id,
            search=search,
            selectable_inputs_only=selectable_inputs_only,
            owner_id=current_user.id,
            grant_cog_ids=grant_cog_ids,
            sort_by=sort_by,
            order=order,
            limit=limit,
            offset=offset,
        )

    if include_permission_info and cogs:
        from cog_grants import list_cog_grants

        cog_ids = [c.id for c in cogs]
        counts: Dict[int, int] = {}
        for g in list_cog_grants(db, cog_ids=cog_ids):
            counts[g.target_id] = counts.get(g.target_id, 0) + 1
        for cog in cogs:
            cog.direct_permission_count = counts.get(cog.id, 0)

    _attach_size_and_format_metadata(db, cogs)
    return cogs


def _attach_size_and_format_metadata(db: Session, cogs: List) -> None:
    """Populate `size_bytes` / `file_extension` / `mime_type` on each cog,
    one grouped query per side so the list endpoint stays a single round
    trip on top of the primary query (unchanged from the pre-migration
    inline version)."""
    import models

    if not cogs:
        return
    cog_ids = [c.id for c in cogs]
    file_sizes = acf_crud.file_sizes_by_cog(db, cog_ids)
    folder_totals = acf_crud.folder_totals_by_parent(db, cog_ids)
    for cog in cogs:
        if cog.cog_type == models.CogType.FILE:
            cog.size_bytes = int(file_sizes.get(cog.id, 0)) or None
        else:
            cog.size_bytes = int(folder_totals.get(cog.id, 0)) or None

    file_cog_ids = [c.id for c in cogs if c.cog_type == models.CogType.FILE]
    if not file_cog_ids:
        return
    blob_meta = acf_crud.blob_format_meta_by_cog(db, file_cog_ids)
    file_meta = acf_crud.file_format_meta_by_cog(db, file_cog_ids)
    for cog in cogs:
        if cog.cog_type != models.CogType.FILE:
            continue
        ext, mime = blob_meta.get(cog.id, (None, None))
        if not ext or not mime:
            fallback = file_meta.get(cog.id)
            if fallback:
                f_mime, f_name = fallback
                if not mime and f_mime:
                    mime = f_mime
                if not ext and f_name and "." in f_name:
                    ext = f_name.rsplit(".", 1)[-1].lower()
        cog.file_extension = (ext or None)
        cog.mime_type = (mime or None)


# =============================================================================
# search_cogs_recursive
# =============================================================================


def search_cogs_recursive(db: Session, *, current_user, q: str):
    """Recursive name/tag search across the tenant, permission-filtered
    in-memory (unchanged algorithm — only the initial candidate-set query
    moved to crud)."""
    from permissions import can_user_view_cog

    if not q or q.strip() == '':
        return []

    all_cogs = acf_crud.list_tenant_cogs_for_search(db, tenant_id=current_user.tenant_id)

    query_lower = q.lower()
    has_wildcard = ('*' in q) or ('?' in q)

    def _matches(text: str) -> bool:
        t = (text or "").lower()
        return fnmatch.fnmatch(t, query_lower) if has_wildcard else (query_lower in t)

    matching_cogs = []
    for cog in all_cogs:
        if can_user_view_cog(db, current_user.id, cog.id):
            matched = _matches(cog.name) or any(_matches(t.name) for t in cog.tags)
            if matched:
                matching_cogs.append(cog)

    logging.info(f"Recursive search for '{q}' found {len(matching_cogs)} results")
    return matching_cogs


# =============================================================================
# get_cog — authoritative format-metadata enrichment
# =============================================================================


def attach_format_metadata(db: Session, db_cog) -> None:
    """Enrich a single FILE-type cog with authoritative
    file_extension/mime_type (ContentBlob, falling back to File) — the same
    enrichment `list_cogs` applies per-row, for the single-cog GET."""
    import models

    if db_cog.cog_type != models.CogType.FILE:
        return
    blob = acf_crud.blob_format_for_cog(db, db_cog.id)
    if blob:
        db_cog.file_extension = blob[0] or None
        db_cog.mime_type = blob[1] or None
    if not getattr(db_cog, "file_extension", None) or not getattr(db_cog, "mime_type", None):
        file_row = acf_crud.file_format_fallback_for_cog(db, db_cog.id)
        if file_row:
            if not getattr(db_cog, "mime_type", None) and file_row[0]:
                db_cog.mime_type = file_row[0]
            if not getattr(db_cog, "file_extension", None) and file_row[1] and "." in file_row[1]:
                db_cog.file_extension = file_row[1].rsplit(".", 1)[-1].lower()


# =============================================================================
# tags
# =============================================================================


def get_tag_for_tenant_or_none(db: Session, *, tag_id: int, tenant_id: int):
    """Tenant-scoped tag lookup for the add/remove-tag endpoints."""
    return acf_crud.get_tag_by_id_and_tenant(db, tag_id=tag_id, tenant_id=tenant_id)


# =============================================================================
# search-by-processing / processing-analytics
# =============================================================================


def search_by_processing(
    db: Session,
    *,
    current_user,
    workflow_name: Optional[str],
    node_kind: Optional[str],
    status: Optional[str],
    min_cost: Optional[int],
    max_cost: Optional[int],
    processed_after,
    has_errors: Optional[bool],
    limit: int,
    offset: int,
):
    return acf_crud.search_cogs_by_processing(
        db,
        tenant_id=current_user.tenant_id,
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


def processing_analytics(db: Session, *, current_user, start_date, end_date, workflow_name: Optional[str]):
    """Aggregate analytics over `processing_metadata` (unchanged in-memory
    aggregation — only the candidate-set query moved to crud)."""
    cogs = acf_crud.list_cogs_for_processing_analytics(
        db, tenant_id=current_user.tenant_id, start_date=start_date, end_date=end_date,
    )

    total_assets = len(cogs)
    total_cost = sum(cog.total_processing_cost for cog in cogs)
    total_runs = sum(cog.processing_count for cog in cogs)

    workflow_stats: Dict[str, Dict] = {}
    stats_by_node_kind: Dict[str, Dict] = {}
    failure_count = 0
    status_counts = {"success": 0, "failure": 0, "running": 0}

    for cog in cogs:
        metadata = cog.processing_metadata or {}
        for run in metadata.get('processing_history', []):
            wf_name = run.get('workflow_name', 'Unknown')
            if not workflow_name or wf_name == workflow_name:
                if wf_name not in workflow_stats:
                    workflow_stats[wf_name] = {
                        "count": 0,
                        "total_cost": 0,
                        "avg_duration": 0,
                        "total_duration": 0
                    }

                workflow_stats[wf_name]["count"] += 1
                workflow_stats[wf_name]["total_cost"] += run.get('total_cost', 0)

                duration = run.get('duration_seconds', 0)
                if duration:
                    workflow_stats[wf_name]["total_duration"] += duration

                run_status = run.get('status', 'unknown')
                if run_status in status_counts:
                    status_counts[run_status] += 1

                if run_status == 'failure':
                    failure_count += 1

                for node in run.get('nodes_executed', []):
                    node_kind = node.get('node_kind', 'Unknown')
                    if node_kind not in stats_by_node_kind:
                        stats_by_node_kind[node_kind] = {
                            "count": 0,
                            "total_cost": 0,
                            "success_count": 0,
                            "failure_count": 0
                        }

                    stats_by_node_kind[node_kind]["count"] += 1
                    stats_by_node_kind[node_kind]["total_cost"] += node.get('cost', 0)

                    if node.get('status') == 'success':
                        stats_by_node_kind[node_kind]["success_count"] += 1
                    elif node.get('status') == 'failure':
                        stats_by_node_kind[node_kind]["failure_count"] += 1

    for wf_name, stats in workflow_stats.items():
        if stats["count"] > 0 and stats["total_duration"] > 0:
            stats["avg_duration"] = stats["total_duration"] / stats["count"]

    workflow_stats_sorted = dict(sorted(
        workflow_stats.items(),
        key=lambda x: x[1]["count"],
        reverse=True
    ))

    sorted_node_stats = dict(sorted(
        stats_by_node_kind.items(),
        key=lambda x: x[1]["count"],
        reverse=True
    )[:20])

    return {
        "summary": {
            "total_assets_processed": total_assets,
            "total_workflow_runs": total_runs,
            "total_cost_credits": total_cost,
            "failure_count": failure_count,
            "success_rate": (total_runs - failure_count) / total_runs if total_runs > 0 else 0,
            "status_breakdown": status_counts
        },
        "workflow_usage": workflow_stats_sorted,
        "top_nodes": sorted_node_stats,
        "date_range": {
            "start": start_date.isoformat() if start_date else None,
            "end": end_date.isoformat() if end_date else None
        }
    }


# =============================================================================
# folder/files recursive (zip-download service endpoint)
# =============================================================================


class FolderNotFoundError(Exception):
    """Folder cog id does not resolve to an active folder cog."""


class NotAFolderError(Exception):
    """Cog id resolves to a non-folder cog type."""


def get_folder_files_recursive(db: Session, *, folder_id: int, principal):
    """Recursively collect every file in a folder tree for zip download.
    Behaviour, log lines and response shape unchanged from the
    pre-migration inline version — only the `db.query(...)` calls moved to
    crud."""
    import models
    from utils.run_task_auth import pin_run_task_tenant

    start_time = time.time()
    logging.info(f"[ZIP] === REQUEST START === folder_id={folder_id} at {start_time}")

    folder = acf_crud.get_active_folder(db, folder_id)
    logging.info(f"[ZIP] Folder query completed: {folder.name if folder else 'NOT FOUND'}")

    if not folder:
        raise FolderNotFoundError("Folder not found")

    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=folder.tenant_id)

    if folder.cog_type not in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
        raise NotAFolderError("Specified cog is not a folder")

    def get_files_recursive(cog_id: int, current_path: str = ""):
        logging.info(f"[ZIP] get_files_recursive called for cog_id={cog_id}, path='{current_path}'")
        files_list = []

        children = acf_crud.get_active_children(db, cog_id)
        logging.info(f"[ZIP] Found {len(children)} children for cog_id={cog_id}")

        for child in children:
            if child.cog_type == models.CogType.FILE:
                logging.info(f"[ZIP] Processing FILE: {child.name} (id={child.id})")
                if child.latest_version_id:
                    current_version = acf_crud.get_version_by_id(db, child.latest_version_id)
                    logging.info(f"[ZIP] Version query done for {child.name}")
                else:
                    current_version = None
                    logging.warning(f"[ZIP] No latest_version_id for {child.name}")

                if current_version:
                    content_blob = acf_crud.get_content_blob_for_version(
                        db, tenant_id=child.tenant_id, content_id=current_version.content_id,
                    )
                    logging.info(f"[ZIP] ContentBlob query done for {child.name}")

                    if content_blob:
                        # GSEC R-088 — the fallback bucket must be the tenant's
                        # effective bucket (BYO-aware), not the house one.
                        from utils.minio_client import get_tenant_bucket_name
                        storage_path_raw = content_blob.storage_path or (
                            f"s3://{get_tenant_bucket_name(child.tenant_id, db)}/{content_blob.id}"
                        )
                        logging.info(f"[ZIP] Using storage_path for {child.name}: {storage_path_raw}")

                        storage_path_str = storage_path_raw
                        if storage_path_str.startswith(("s3://", "minio://")):
                            storage_path_str = storage_path_str.split("://", 1)[1]
                            if "/" in storage_path_str:
                                storage_path_str = storage_path_str.split("/", 1)[1]

                        logging.info(f"[ZIP] Extracted path: {storage_path_str}")

                        relative_path = f"{current_path}{child.name}" if current_path else child.name

                        files_list.append({
                            "cog_id": child.id,
                            "name": child.name,
                            "minio_path": storage_path_str,
                            "relative_path": relative_path,
                            "file_size": content_blob.size_bytes,
                            "mime_type": content_blob.mime_type,
                            "file_extension": content_blob.file_extension or ""
                        })
                        logging.info(f"[ZIP] Added file: {child.name} -> {storage_path_str}")

            elif child.cog_type in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
                subfolder_path = f"{current_path}{child.name}/" if current_path else f"{child.name}/"
                files_list.extend(get_files_recursive(child.id, subfolder_path))

        return files_list

    logging.info(f"[ZIP] Starting recursive file collection for folder_id={folder_id}")
    all_files = get_files_recursive(folder_id)
    logging.info(f"[ZIP] Recursive collection complete, found {len(all_files)} files")

    response_data = {
        "folder_id": folder_id,
        "folder_name": folder.name,
        "files": all_files,
        "total_files": len(all_files),
        "total_size": sum(f["file_size"] for f in all_files)
    }

    end_time = time.time()
    duration = end_time - start_time
    logging.info(f"[ZIP] === REQUEST END === Took {duration:.3f}s, returning {len(all_files)} files, total size: {response_data['total_size']} bytes")
    return response_data


# =============================================================================
# legal hold
# =============================================================================


def _content_hashes_for_cog(db: Session, *, cog_id: int, tenant_id: int) -> List[str]:
    file_records = acf_crud.get_active_file_records_for_cog(db, cog_id=cog_id, tenant_id=tenant_id)
    return [f.storage_path.rsplit("/", 1)[-1] for f in file_records if f.storage_path]


def set_legal_hold(db: Session, *, cog, current_user, reason: str):
    """Set a legal hold on a cog and its associated workflow outputs.
    Returns `(held_count,)`-shaped state; commits are unchanged from the
    pre-migration version (two commits: outputs+lock, then the audit
    event)."""
    from datetime import datetime, timezone

    from models import WorkflowNodeOutput, WorkflowOutputAuditLog  # noqa: F401 (kept for parity/import-time side effects)

    content_hashes = _content_hashes_for_cog(db, cog_id=cog.id, tenant_id=current_user.tenant_id)

    held_count = 0
    if content_hashes:
        outputs = acf_crud.get_workflow_outputs_by_hashes(
            db, content_hashes=content_hashes, tenant_id=current_user.tenant_id,
        )
        for output in outputs:
            output.legal_hold = True
            output.legal_hold_reason = reason
            output.legal_hold_set_by_user_id = current_user.id
            output.legal_hold_set_at = datetime.now(timezone.utc)
            held_count += 1

            db.add(WorkflowOutputAuditLog(
                output_id=output.id,
                content_hash=output.content_hash,
                tenant_id=current_user.tenant_id,
                action="legal_hold_set",
                performed_by_user_id=current_user.id,
                reason=reason,
            ))

    cog.is_locked = True
    db.commit()

    try:
        from models import AuditEvent

        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="LEGAL_HOLD_SET",
            details={"cog_id": cog.id, "reason": reason, "outputs_held": held_count},
        ))
        db.commit()
    except Exception:
        pass

    return held_count


def remove_legal_hold(db: Session, *, cog, current_user):
    """Remove a legal hold from a cog and its associated workflow outputs."""
    from models import WorkflowNodeOutput, WorkflowOutputAuditLog  # noqa: F401

    content_hashes = _content_hashes_for_cog(db, cog_id=cog.id, tenant_id=current_user.tenant_id)

    removed_count = 0
    if content_hashes:
        outputs = acf_crud.get_workflow_outputs_by_hashes(
            db, content_hashes=content_hashes, tenant_id=current_user.tenant_id, legal_hold=True,
        )
        for output in outputs:
            output.legal_hold = False
            output.legal_hold_reason = None
            output.legal_hold_set_by_user_id = None
            output.legal_hold_set_at = None
            removed_count += 1

            db.add(WorkflowOutputAuditLog(
                output_id=output.id,
                content_hash=output.content_hash,
                tenant_id=current_user.tenant_id,
                action="legal_hold_removed",
                performed_by_user_id=current_user.id,
                reason="Legal hold removed by admin",
            ))

    cog.is_locked = False
    db.commit()

    try:
        from models import AuditEvent

        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="LEGAL_HOLD_REMOVED",
            details={"cog_id": cog.id, "outputs_released": removed_count},
        ))
        db.commit()
    except Exception:
        pass

    return removed_count


def get_legal_hold_status(db: Session, *, cog, current_user):
    """Returns `(held_count, reason, set_by, set_at)` for the GET status
    endpoint. Preserves the original's two-query shape (`.first()` then a
    separate `.count()`) rather than collapsing them, to keep behaviour
    (and query count) identical."""
    content_hashes = _content_hashes_for_cog(db, cog_id=cog.id, tenant_id=current_user.tenant_id)

    held_count = 0
    reason = None
    set_by = None
    set_at = None

    if content_hashes:
        held_output = acf_crud.get_first_held_workflow_output(
            db, content_hashes=content_hashes, tenant_id=current_user.tenant_id,
        )
        if held_output:
            held_count = acf_crud.count_held_workflow_outputs(
                db, content_hashes=content_hashes, tenant_id=current_user.tenant_id,
            )
            reason = held_output.legal_hold_reason
            set_by = held_output.legal_hold_set_by_user_id
            set_at = held_output.legal_hold_set_at

    return held_count, reason, set_by, set_at


# =============================================================================
# thumbnail upload
# =============================================================================


class ContentBlobNotFoundError(Exception):
    """No ContentBlob exists for the cog (nothing to derive the thumbnail
    path's deterministic key from)."""


def get_content_blob_or_raise(db: Session, *, cog_id: int, tenant_id: int):
    blob = acf_crud.get_content_blob_for_cog(db, cog_id=cog_id, tenant_id=tenant_id)
    if not blob:
        raise ContentBlobNotFoundError("No content blob found")
    return blob

"""Business/policy layer for the node-run preview & fan-out endpoints (Gate 2b
migration of routers/node_runs_domains/node_runs.py — openspec api-refactor).

    routers/node_runs_domains/node_runs.py   HTTP only, thin delegation.
        -> services/node_runs_service.py      this module: view-gate policy,
                                              lineage/dedup rules, response
                                              shaping, MinIO presign I/O.
            -> crud/node_runs.py               the ONLY db.query for this
                                              domain's NodeRun/ApiTaskQueue/
                                              Cog/ContentBlob reads.
            -> crud.get_node_run_by_id /
               crud.update_node_run_status     pre-existing crud/node_run_cud.py
                                              functions, left untouched.

Follows services/_base.py conventions (ServiceError/NotFoundError; the router
maps them to HTTPException). No FastAPI/HTTPException imports here.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

import crud
import crud.node_runs as node_runs_crud
import models
from services._base import NotFoundError
from utils.pii_redaction_surfaces import safe_redact as _v4b_redact_text
from routers.node_runs_domains._shared import (
    _attachment_url,
    _fanout_key,
    _fanout_preview,
    _fanout_status,
)


# =============================================================================
# Shared view-gate policy
# =============================================================================


def _get_viewable_run(db, *, run_id: int, tenant_id: int, user: "models.User") -> "models.WorkflowRun":
    """Fetch a WorkflowRun the caller may view, else raise NotFoundError.

    Matches the pre-migration behavior exactly: `crud.get_workflow_run_by_id`
    already tenant-scopes, and `can_view_workflow_run` is an additional
    per-workflow view-gate on top — a denial surfaces as 404 (not 403) by
    original design, to avoid revealing a co-tenant's private run even exists.
    That is an intentional existing choice for this domain, not the generic
    404-vs-403 trap (services/_base.py), so it is preserved as-is here.
    """
    from resource_permissions import can_view_workflow_run

    db_workflow_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_id)
    if not db_workflow_run:
        raise NotFoundError("Workflow run not found")
    if not can_view_workflow_run(db, user, db_workflow_run):
        raise NotFoundError("Workflow run not found")
    return db_workflow_run


# =============================================================================
# update_node_run_status
# =============================================================================


def update_node_run_status(
    db,
    *,
    node_run_id: int,
    tenant_id: int,
    status_update,
) -> models.NodeRun:
    """Apply a worker-reported status update to a node run."""
    db_node_run = crud.get_node_run_by_id(db, node_run_id=node_run_id, tenant_id=tenant_id)
    if not db_node_run:
        raise NotFoundError("Node run not found")

    return crud.update_node_run_status(
        db,
        db_node_run=db_node_run,
        status=status_update.status,
        inputs=status_update.inputs,
        outputs=status_update.outputs,
        error_message=status_update.error_message,
    )


# =============================================================================
# get_node_run_previews
# =============================================================================

_PREVIEW_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'tif', 'tiff', 'tga', 'exr', 'hdr', 'bmp'}
_PREVIEW_MODEL3D_EXTS = {'obj', 'fbx', 'glb', 'gltf', 'ply', 'stl', 'dae', 'usd', 'usdz'}
_PREVIEW_VIDEO_EXTS = {'mp4', 'mkv', 'avi', 'mov', 'webm'}
_PREVIEW_AUDIO_EXTS = {'mp3', 'wav', 'flac', 'ogg', 'aac', 'm4a'}
_PREVIEW_TEXT_EXTS = {
    'json', 'csv', 'txt', 'yaml', 'yml', 'xml', 'toml', 'md', 'py', 'sh', 'js', 'ts',
    'go', 'log', 'cfg', 'ini', 'sql',
}
_PREVIEW_PDF_EXTS = {'pdf'}
_PREVIEW_SPLAT_EXTS = {'splat', 'ksplat'}
_PREVIEW_POINTCLOUD_EXTS = {'pcd', 'las', 'laz', 'xyz'}
_PREVIEW_FONT_EXTS = {'ttf', 'otf', 'woff', 'woff2'}
_PREVIEW_ARCHIVE_EXTS = {'zip', 'tar', 'gz', 'tgz', 'rar', '7z'}


def _preview_categorize(ext: str) -> str:
    ext = (ext or '').lower().lstrip('.')
    if ext in _PREVIEW_IMAGE_EXTS:
        return 'image'
    if ext in _PREVIEW_MODEL3D_EXTS:
        return 'model3d'
    if ext in _PREVIEW_VIDEO_EXTS:
        return 'video'
    if ext in _PREVIEW_AUDIO_EXTS:
        return 'audio'
    if ext in _PREVIEW_TEXT_EXTS:
        return 'text'
    if ext in _PREVIEW_PDF_EXTS:
        return 'pdf'
    if ext in _PREVIEW_SPLAT_EXTS:
        return 'splat'
    if ext in _PREVIEW_POINTCLOUD_EXTS:
        return 'pointcloud'
    if ext in _PREVIEW_FONT_EXTS:
        return 'font'
    if ext in _PREVIEW_ARCHIVE_EXTS:
        return 'archive'
    return 'other'


# Objects in MinIO are stored by content_hash with no extension and default to
# Content-Type: application/octet-stream. The PDF preview iframe (and the
# <video>/<audio> elements) need a real Content-Type to render inline —
# otherwise the browser falls back to downloading the blob.
# response-content-disposition=inline tells Chrome/Firefox to render PDFs in
# the iframe instead of triggering a save dialog. Images and audio/video
# usually still work without this because the browser content-sniffs, but
# PDFs do NOT sniff — they need the header. Pass the inferred MIME on every
# category so all previews behave consistently.
_PREVIEW_EXT_TO_MIME = {
    # images
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
    'tif': 'image/tiff', 'tiff': 'image/tiff', 'bmp': 'image/bmp',
    'webp': 'image/webp', 'gif': 'image/gif',
    # docs
    'pdf': 'application/pdf', 'txt': 'text/plain',
    'json': 'application/json', 'csv': 'text/csv',
    'xml': 'application/xml', 'md': 'text/markdown',
    'yaml': 'text/yaml', 'yml': 'text/yaml',
    # video / audio
    'mp4': 'video/mp4', 'mkv': 'video/x-matroska',
    'mov': 'video/quicktime', 'webm': 'video/webm', 'avi': 'video/x-msvideo',
    'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'flac': 'audio/flac',
    'ogg': 'audio/ogg', 'aac': 'audio/aac', 'm4a': 'audio/mp4',
    # fonts
    'ttf': 'font/ttf', 'otf': 'font/otf',
    'woff': 'font/woff', 'woff2': 'font/woff2',
}


def _preview_mime_for(ext: str) -> str:
    return _PREVIEW_EXT_TO_MIME.get((ext or '').lower().lstrip('.'), 'application/octet-stream')


def _should_redact_graph(db, db_workflow_run, user) -> bool:
    """GG.8b — sealed buyers of the run's installed copy get node NAMES
    redacted on the fan-out surfaces (status/outputs remain). Exemptions
    (publisher tenant / platform admin) and 'open' listings stay intact."""
    try:
        from services import sealed_workflow as _sealed
        return (
            db_workflow_run is not None
            and _sealed.should_redact_graph(db, db_workflow_run.workflow, user)
        )
    except Exception:
        return False


def list_run_previews(
    db,
    *,
    run_id: int,
    tenant_id: int,
    user: "models.User",
    minio_client,
) -> Dict[str, List[Dict[str, Any]]]:
    """Presigned preview URLs for every completed node output in a run,
    keyed by graph_node_id."""
    _get_viewable_run(db, run_id=run_id, tenant_id=tenant_id, user=user)

    node_runs = node_runs_crud.get_node_runs_with_workflow_node(db, workflow_run_id=run_id)

    previews: Dict[str, List[Dict[str, Any]]] = {}

    for nr in node_runs:
        graph_node_id = nr.workflow_node.graph_node_id if nr.workflow_node else None
        if not graph_node_id or nr.status not in (models.TaskStatus.SUCCESS, models.TaskStatus.COMPLETED):
            continue
        if not nr.outputs:
            continue

        node_previews = []
        for output in nr.outputs:
            content_hash = output.get('content_hash')
            file_ext = output.get('file_extension', '')
            if not content_hash:
                continue

            category = _preview_categorize(file_ext)
            mime = _preview_mime_for(file_ext)
            logical_name = output.get('logical_name', '') or f"{content_hash[:8]}.{(file_ext or '').lstrip('.')}"

            try:
                presigned_url = minio_client.presigned_client.presigned_get_object(
                    bucket_name=minio_client.bucket_name,
                    object_name=content_hash,
                    expires=timedelta(seconds=900),
                    response_headers={
                        'response-content-type': mime,
                        'response-content-disposition': f'inline; filename="{logical_name}"',
                    },
                )
            except Exception:
                continue

            # GY.V1a follow-up (GSEC R-111): these presigned GETs are handed to
            # the user (GET /runs/{id}/previews), so each hand-off gets its own
            # delivery row. IDs only — the run id + the content hash as the
            # object key; the URL itself is never stored.
            # 1788 CE carve: delivery_audit is paid; CE records no delivery row
            # (audit-only side effect — the presigned hand-off itself is unchanged).
            from utils.paid_import import optional_paid
            _delivery_audit = optional_paid("services.delivery_audit")
            record_delivery = (
                _delivery_audit.record_delivery
                if _delivery_audit is not None
                else (lambda *a, **kw: None)
            )

            record_delivery(
                db,
                delivery_path="presign",
                actor_user_id=user.id,
                tenant_id=tenant_id,
                resource_id=run_id,
                resource_type="workflow_run",
                object_key=content_hash,
                expiry_seconds=900,
                metadata={"channel": "node_run_output",
                          "graph_node_id": graph_node_id},
                # The WB re-polls this endpoint every tick; one row per presign
                # window per hand-off (per process — multi-worker API may record
                # once each, bounded by the worker count).
                dedupe_window_s=900,
            )
            node_previews.append({
                'url': presigned_url,
                'file_extension': file_ext,
                'category': category,
                'content_hash': content_hash,
                'logical_name': output.get('logical_name', ''),
            })

        if node_previews:
            previews[graph_node_id] = node_previews

    return previews


# =============================================================================
# get_run_fanout_summary
# =============================================================================


def get_run_fanout_summary(db, *, run_id: int, tenant_id: int, user: "models.User") -> Dict[str, Any]:
    """Per-node fan-out width for a run (distinct source-cog count per node)."""
    _get_viewable_run(db, run_id=run_id, tenant_id=tenant_id, user=user)

    rows = node_runs_crud.get_task_node_payloads_for_run(db, workflow_run_id=run_id)

    per_node: Dict[str, set] = {}
    for nid, payload in rows:
        if not nid:
            continue
        key = _fanout_key(payload)
        per_node.setdefault(nid, set()).add(key)
    return {"node_task_counts": {nid: len(keys) for nid, keys in per_node.items()}}


# =============================================================================
# get_node_fanout
# =============================================================================


def get_node_fanout(
    db,
    *,
    run_id: int,
    graph_node_id: str,
    tenant_id: int,
    user: "models.User",
    minio_client,
) -> Dict[str, Any]:
    """Exact per-file input<->output pairing for one fanned node in a run."""
    db_workflow_run = _get_viewable_run(db, run_id=run_id, tenant_id=tenant_id, user=user)

    tasks = node_runs_crud.get_tasks_for_node(
        db, workflow_run_id=run_id, graph_node_id=graph_node_id, order="asc"
    )

    # Node display name (best-effort) from the workflow node.
    node_name = graph_node_id
    if _should_redact_graph(db, db_workflow_run, user):
        node_name = None  # GG.8b sealed: hide the graph label from the buyer
    else:
        try:
            wn = node_runs_crud.get_workflow_node_by_graph_id(
                db, workflow_id=db_workflow_run.workflow_id, graph_node_id=graph_node_id
            )
            if wn and isinstance(wn.data, dict):
                node_name = wn.data.get("label") or wn.data.get("name") or graph_node_id
        except Exception:
            pass

    # Resolve each source cog's stored file once (many tasks may share none).
    def _cog_input_preview(cog_id):
        if cog_id is None:
            return None, None
        cog = node_runs_crud.get_cog_for_tenant(db, cog_id=cog_id, tenant_id=tenant_id)
        if not cog:
            return None, None
        name = cog.name
        ver = cog.latest_version
        if not ver or not ver.content_id:
            return None, name
        blob = node_runs_crud.get_content_blob_for_tenant(
            db, content_id=ver.content_id, tenant_id=tenant_id
        )
        ext = blob.file_extension if blob else ''
        return _fanout_preview(minio_client, ver.content_id, ext, cog.name), name

    # Collapse re-run attempts: keep only the latest task (highest id) per
    # source cog, so a re-run of a file updates its row rather than adding a
    # duplicate that would look like extra fan-out width.
    latest_by_key: Dict[Any, Any] = {}
    for t in tasks:  # tasks already ordered by id ascending
        latest_by_key[_fanout_key(t.task_payload or {})] = t
    deduped = list(latest_by_key.values())

    items = []
    for t in deduped:
        payload = t.task_payload or {}
        pi = payload.get("ProjectInfo") or {}
        cogs = pi.get("cog_ids")
        if cogs is None:
            cogs = payload.get("cog_ids") or []
        cog_id = cogs[0] if isinstance(cogs, (list, tuple)) and cogs else None

        input_preview, cog_name = _cog_input_preview(cog_id)

        rp = t.result_payload or {}
        raw_outputs = rp.get("outputs") if isinstance(rp, dict) else None
        output_previews = []
        if isinstance(raw_outputs, list):
            for o in raw_outputs:
                if not isinstance(o, dict):
                    continue
                ext = o.get("file_extension")
                if not ext:
                    fn = o.get("file_name") or ""
                    ext = fn.rsplit(".", 1)[-1] if "." in fn else ""
                p = _fanout_preview(
                    minio_client,
                    o.get("content_hash"),
                    ext,
                    o.get("logical_name") or o.get("file_name"),
                )
                if p:
                    output_previews.append(p)

        # GY.V1a follow-up (GSEC R-111): the fan-out modal hands the user
        # presigned GETs for the source cog's file and every produced output
        # (via routers/node_runs_domains/_shared._fanout_preview, which has no
        # db/actor in scope, so the audit lives here at the user-facing call
        # site). IDs only — cog/run id + the content hash as the object key.
        # 1788 CE carve: delivery_audit is paid; CE records no delivery row.
        from utils.paid_import import optional_paid
        _delivery_audit = optional_paid("services.delivery_audit")
        record_delivery = (
            _delivery_audit.record_delivery
            if _delivery_audit is not None
            else (lambda *a, **kw: None)
        )

        if input_preview and input_preview.get('url'):
            record_delivery(
                db, delivery_path="presign", actor_user_id=user.id,
                tenant_id=tenant_id,
                resource_id=cog_id if cog_id is not None else run_id,
                resource_type="cog" if cog_id is not None else "workflow_run",
                object_key=input_preview.get('content_hash'),
                expiry_seconds=900,
                metadata={"channel": "node_run_output", "asset": "fanout_input"},
                # Poll-hit site: WB re-fetches the fan-out modal; dedupe per
                # presign window (per process — multi-worker API may record
                # once each, bounded by the worker count).
                dedupe_window_s=900,
            )
        for p in output_previews:
            if p.get('url'):
                record_delivery(
                    db, delivery_path="presign", actor_user_id=user.id,
                    tenant_id=tenant_id, resource_id=run_id,
                    resource_type="workflow_run",
                    object_key=p.get('content_hash'), expiry_seconds=900,
                    metadata={"channel": "node_run_output",
                              "asset": "fanout_output"},
                    dedupe_window_s=900,
                )
        items.append({
            "cog_id": cog_id,
            "name": cog_name or (f"cog {cog_id}" if cog_id is not None else "item"),
            "status": _fanout_status(t.status),
            "task_id": t.id,
            "input": input_preview,
            "output": output_previews[0] if output_previews else None,
            "outputs": output_previews,
            # GY.V4b — display-only redaction of the error text shown in the
            # fan-out modal; the stored task row is never rewritten.
            "error_message": _v4b_redact_text(t.error_message),
        })

    # Stable order by cog id (None last), keeping enqueue order as tiebreak.
    items.sort(key=lambda it: (it["cog_id"] is None, it["cog_id"] if it["cog_id"] is not None else 0))

    return {
        "run_id": run_id,
        "node_id": graph_node_id,
        "name": node_name,
        "fanned": len(deduped) > 1,
        "item_count": len(items),
        "items": items,
    }


# =============================================================================
# get_node_versions
# =============================================================================


def get_node_versions(
    db,
    *,
    run_id: int,
    graph_node_id: str,
    tenant_id: int,
    user: "models.User",
    minio_client,
) -> Dict[str, Any]:
    """Every attempt of a node in a run, kept and downloadable, newest first."""
    _get_viewable_run(db, run_id=run_id, tenant_id=tenant_id, user=user)

    tasks = node_runs_crud.get_tasks_for_node(
        db, workflow_run_id=run_id, graph_node_id=graph_node_id, order="desc"
    )

    versions = []
    for idx, t in enumerate(tasks):
        rp = t.result_payload if isinstance(t.result_payload, dict) else {}
        raw_outputs = rp.get("outputs") if isinstance(rp, dict) else None
        outs = []
        if isinstance(raw_outputs, list):
            for o in raw_outputs:
                if not isinstance(o, dict):
                    continue
                ext = o.get("file_extension")
                if not ext:
                    fn = o.get("file_name") or ""
                    ext = fn.rsplit(".", 1)[-1] if "." in fn else ""
                name = o.get("logical_name") or o.get("file_name")
                p = _fanout_preview(minio_client, o.get("content_hash"), ext, name)
                if p:
                    # attachment URL -> real download (inline `url` stays for preview)
                    p["download_url"] = _attachment_url(minio_client, o.get("content_hash"), ext, name)
                    # GY.V1a follow-up (GSEC R-111): the node-versions list is
                    # user-facing (GET /runs/{id}/node-versions/{gid}) and hands
                    # both an inline preview URL and an attachment download URL
                    # for the same blob — one delivery row per blob handed.
                    if p.get('url') or p.get('download_url'):
                        # 1788 CE carve: delivery_audit is paid; CE records no row.
                        from utils.paid_import import optional_paid
                        _delivery_audit = optional_paid("services.delivery_audit")

                        (_delivery_audit.record_delivery if _delivery_audit is not None
                         else (lambda *a, **kw: None))(
                            db,
                            delivery_path="presign",
                            actor_user_id=user.id,
                            tenant_id=tenant_id,
                            resource_id=run_id,
                            resource_type="workflow_run",
                            object_key=o.get("content_hash"),
                            expiry_seconds=900,
                            metadata={"channel": "node_version",
                                      "graph_node_id": graph_node_id,
                                      "task_id": t.id},
                            # Poll-hit site: one row per presign window.
                            dedupe_window_s=900,
                        )
                    outs.append(p)
        versions.append({
            "task_id": t.id,
            # A human label: the newest is "latest", older ones numbered back.
            "attempt": t.output_namespace or f"attempt_{len(tasks) - 1 - idx}",
            "label": "latest" if idx == 0 else f"attempt #{len(tasks) - idx}",
            "status": _fanout_status(t.status),
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "error_message": t.error_message,
            "outputs": outs,
        })

    return {
        "run_id": run_id,
        "node_id": graph_node_id,
        "attempts": len(versions),
        "versions": versions,
    }

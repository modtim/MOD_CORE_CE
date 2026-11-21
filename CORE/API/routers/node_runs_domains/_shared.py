# Auto-generated domain sub-module (Gate 2a split of routers/node_runs.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
from typing import List
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

import crud, models, schemas
from dependencies import get_db, require_active_and_onboarded_user, get_impersonated_user_from_service_account, require_service_account_token, require_bearer_or_api_key, get_tenant_minio_client
from utils.run_task_auth import node_or_service_principal, pin_run_task_tenant
from utils.minio_client import MinioClient

def _enforce_user_run_view(db, principal, workflow_run):
    """The /service/* endpoints accept EITHER a service/node run_task token OR an
    ordinary user session (node_or_service_principal falls back to
    require_bearer_or_api_key). For a service/node token (dict principal) the
    caller is the run's own execution context — keep the existing behaviour. For
    an interactive USER principal, enforce the SAME per-workflow view gate the
    browser endpoints use, and tenant-scope: otherwise these "service" endpoints
    were an ungated side door that returned a co-tenant's (and, with no tenant
    filter, a cross-tenant) private workflow-run node graph, resolved output
    storage paths and resource usage by run id."""
    if isinstance(principal, models.User):
        from resource_permissions import can_view_workflow_run
        if (workflow_run.tenant_id != principal.tenant_id
                or not can_view_workflow_run(db, principal, workflow_run)):
            raise HTTPException(status_code=404, detail="Workflow run not found")

_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'tif', 'tiff', 'tga', 'exr', 'hdr', 'bmp', 'webp', 'gif'}

_MODEL3D_EXTS = {'obj', 'fbx', 'glb', 'gltf', 'ply', 'stl', 'dae', 'usd', 'usdz'}

_VIDEO_EXTS = {'mp4', 'mkv', 'avi', 'mov', 'webm'}

_AUDIO_EXTS = {'mp3', 'wav', 'flac', 'ogg', 'aac', 'm4a'}

_TEXT_EXTS = {'json', 'csv', 'txt', 'yaml', 'yml', 'xml', 'toml', 'md', 'py', 'sh', 'js', 'ts', 'go', 'log', 'cfg', 'ini', 'sql'}

_PDF_EXTS = {'pdf'}

_EXT_TO_MIME = {
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
    'tif': 'image/tiff', 'tiff': 'image/tiff', 'bmp': 'image/bmp',
    'webp': 'image/webp', 'gif': 'image/gif',
    'pdf': 'application/pdf', 'txt': 'text/plain', 'json': 'application/json',
    'csv': 'text/csv', 'xml': 'application/xml', 'md': 'text/markdown',
    'yaml': 'text/yaml', 'yml': 'text/yaml',
    'mp4': 'video/mp4', 'mkv': 'video/x-matroska', 'mov': 'video/quicktime',
    'webm': 'video/webm', 'avi': 'video/x-msvideo',
    'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'flac': 'audio/flac',
    'ogg': 'audio/ogg', 'aac': 'audio/aac', 'm4a': 'audio/mp4',
}

def _fanout_categorize(ext: str) -> str:
    ext = (ext or '').lower().lstrip('.')
    if ext in _IMAGE_EXTS:
        return 'image'
    if ext in _MODEL3D_EXTS:
        return 'model3d'
    if ext in _VIDEO_EXTS:
        return 'video'
    if ext in _AUDIO_EXTS:
        return 'audio'
    if ext in _TEXT_EXTS:
        return 'text'
    if ext in _PDF_EXTS:
        return 'pdf'
    return 'other'

def _fanout_preview(minio_client, content_hash, file_ext, logical_name):
    """Presigned preview object for one content-addressed blob, or None."""
    if not content_hash:
        return None
    ext = (file_ext or '').lower().lstrip('.')
    mime = _EXT_TO_MIME.get(ext, 'application/octet-stream')
    name = logical_name or f"{content_hash[:8]}.{ext}" if ext else (logical_name or content_hash[:8])
    try:
        url = minio_client.presigned_client.presigned_get_object(
            bucket_name=minio_client.bucket_name,
            object_name=content_hash,
            expires=timedelta(seconds=900),
            response_headers={
                'response-content-type': mime,
                'response-content-disposition': f'inline; filename="{name}"',
            },
        )
    except Exception:
        return None
    return {
        'url': url,
        'file_extension': ext,
        'category': _fanout_categorize(ext),
        'content_hash': content_hash,
        'logical_name': logical_name or '',
    }

def _fanout_status(raw) -> str:
    """Normalize an ApiTaskQueue status (enum or str) to a lowercase string."""
    val = raw.value if hasattr(raw, 'value') else raw
    return str(val or '').lower()

def _fanout_key(payload):
    """The input FILE a task ran against, used to distinguish a true fan-out
    (many distinct source cogs) from re-run attempts (same source cog, new
    attempt). Root fan-out tasks each carry one source cog id in
    ProjectInfo.cog_ids[0]; tasks with no source cog (interior/aggregating
    nodes) all collapse to a single key so a re-run of them is never a fan-out.
    """
    if not isinstance(payload, dict):
        return "__no_cog__"
    pi = payload.get("ProjectInfo") or {}
    cogs = pi.get("cog_ids")
    if cogs is None:
        cogs = payload.get("cog_ids") or []
    if isinstance(cogs, (list, tuple)) and cogs:
        return ("cog", cogs[0])
    return "__no_cog__"

def _attachment_url(minio_client, content_hash, file_ext, logical_name):
    """Presigned URL with attachment disposition — clicking DOWNLOADS the file
    with a real filename (vs the inline preview URL which opens in the tab)."""
    if not content_hash:
        return None
    ext = (file_ext or '').lower().lstrip('.')
    mime = _EXT_TO_MIME.get(ext, 'application/octet-stream')
    name = logical_name or (f"{content_hash[:8]}.{ext}" if ext else content_hash[:8])
    try:
        return minio_client.presigned_client.presigned_get_object(
            bucket_name=minio_client.bucket_name,
            object_name=content_hash,
            expires=timedelta(seconds=900),
            response_headers={
                'response-content-type': mime,
                'response-content-disposition': f'attachment; filename="{name}"',
            },
        )
    except Exception:
        return None

__all__ = [
    '_enforce_user_run_view',
    '_fanout_categorize',
    '_fanout_preview',
    '_fanout_status',
    '_fanout_key',
    '_attachment_url',
    '_IMAGE_EXTS',
    '_MODEL3D_EXTS',
    '_VIDEO_EXTS',
    '_AUDIO_EXTS',
    '_TEXT_EXTS',
    '_PDF_EXTS',
    '_EXT_TO_MIME',
]

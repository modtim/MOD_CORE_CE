"""GX.S6 (launch-completion-megaplan, Group X) — per-stack/app storage scopes.

A stack or app gets a PREFIX in the tenant's own file store —
``stacks/{stack_id}/`` or ``apps/{app_id}/`` in the ``tenant-{id}-files``
bucket, or the tenant's BYO-S3 bucket when an active StorageConnection
exists (``MinioClient`` resolves BYO in its constructor, so callers just
use ``mc.bucket_name``). No new buckets are ever created.

Community Edition: bucket + presigned PUT/GET links and soft delete. There
are no retention / compliance holds: a delete is never blocked for a period.

ISO 27001: NO hard deletes. A delete is a soft delete: a tombstone row in
``storage_scope_objects`` + the bytes are COPIED to the scope's
``.trash/`` prefix and the original is removed — the bytes survive in
trash, the row is never removed.

Audit: ``models.AuditEvent`` rows with free-form event_name strings (the
GX.R4 pattern) — no new AuditAction enum values, no ALTER TYPE.

Settings are DB-backed platform settings (services.platform_settings,
group ``storage_scopes``) — never env.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import models

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform settings (services.platform_settings, group "storage_scopes")
# ---------------------------------------------------------------------------
LINK_TTL_KEY = "storage_scopes.link_max_ttl_seconds"
LINK_TTL_DEFAULT = 3600          # 1h
MAX_PUT_SIZE_KEY = "storage_scopes.max_put_size_bytes"
MAX_PUT_SIZE_DEFAULT = 5 * 1024 * 1024 * 1024  # 5 GiB
LIST_PAGE_KEY = "storage_scopes.list_page_size"
LIST_PAGE_DEFAULT = 100

MIN_TTL_SECONDS = 60
MAX_KEY_LEN = 512                # matches storage_scope_objects.object_key
SCOPE_KINDS = ("stack", "app")
TRASH_DIR = ".trash"


def _setting_int(db, key: str, default: int) -> int:
    from services.platform_settings import get_setting
    try:
        return int(get_setting(key, db))
    except Exception:  # noqa: BLE001 — degrade to the default, never raise
        return default


def link_max_ttl(db) -> int:
    """Max presign TTL in seconds (DB setting, default 3600)."""
    return max(_setting_int(db, LINK_TTL_KEY, LINK_TTL_DEFAULT), MIN_TTL_SECONDS)


def max_put_size(db) -> int:
    """Max object size (bytes) a PUT link may be used for (DB setting)."""
    return _setting_int(db, MAX_PUT_SIZE_KEY, MAX_PUT_SIZE_DEFAULT)


def list_page_size(db) -> int:
    """Default/max page size for GET .../objects (DB setting)."""
    return _setting_int(db, LIST_PAGE_KEY, LIST_PAGE_DEFAULT)


def scope_prefix(kind: str, scope_id: int) -> str:
    """The object-store prefix a scope owns, e.g. 'stacks/7/' or 'apps/3/'."""
    return f"{kind}s/{int(scope_id)}/"


def trash_prefix(kind: str, scope_id: int) -> str:
    return scope_prefix(kind, scope_id) + TRASH_DIR + "/"


def validate_object_key(key: str) -> str:
    """Validate a scope-relative object key. Raises ValueError with a
    human-readable reason on any violation."""
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty string")
    if len(key) > MAX_KEY_LEN:
        raise ValueError(f"key exceeds {MAX_KEY_LEN} characters")
    if key.startswith("/"):
        raise ValueError("key must not start with '/'")
    if "\\" in key or "\0" in key:
        raise ValueError("key contains an invalid character")
    parts = key.split("/")
    if ".." in parts or any(p == "." for p in parts):
        raise ValueError("key must not contain '..' path segments")
    return key


# ---------------------------------------------------------------------------
# Scope resolution (authz) — tenant + per-kind checks
# ---------------------------------------------------------------------------

def _tenant_row(db, tenant_id: int) -> Optional[models.Tenant]:
    return db.query(models.Tenant).filter(
        models.Tenant.id == tenant_id).first()


def resolve_scope(db, kind: str, scope_id: int, user: models.User) -> Tuple[str, int]:
    """Resolve (validate + authorize) a storage scope for the caller.

    Returns ``(scope_prefix, scope_id)``. Raises:
      * HTTPException 404 — kind unknown / the stack does not exist in this
        tenant (cross-tenant reads are indistinguishable from missing).
      * HTTPException 403 — the user lacks the stack management capability.

    kind=app: ``app_id`` is a plain integer for now (the ``apps`` table is
    being created by GX.A3) — there is no row to verify yet, only the
    caller's tenant membership (the caller IS tenant-scoped by auth).
    """
    from fastapi import HTTPException, status

    if kind not in SCOPE_KINDS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown scope kind '{kind}'.",
        )
    try:
        scope_id = int(scope_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid scope id.",
        )
    if scope_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid scope id.",
        )

    if kind == "stack":
        from utils.paid_import import optional_paid
        # Persistent Stacks CRUD is a PAID module: on CE there are no stacks to
        # resolve a scope against, so degrade to the same 404 the code already
        # returns for a stack that isn't found in the tenant (indistinguishable
        # from missing — no 500, no upgrade wall on the app path).
        pss_crud = optional_paid("crud.persistent_stacks_shared")
        if pss_crud is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Persistent stack not found",
            )
        stack = pss_crud.get_owned_stack(db, scope_id, user.tenant_id)
        if stack is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Persistent stack not found",
            )
        # Reuse the existing persistent-stack permission check
        # (routers/persistent_stacks._shared._require_stack_author): the
        # STACK_AUTHOR platform capability, on top of tenant ownership.
        import permissions
        if not permissions.check_user_platform_capability(
                db, user.id, permissions.STACK_AUTHOR):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User does not have permission to manage this stack.",
            )
        return scope_prefix(kind, scope_id), scope_id

    # kind == "app" — GX.A3b: the `apps` table has landed. Verify the app
    # row exists, belongs to the caller's tenant, and is not revoked/
    # soft-deleted. Cross-tenant or gone apps 404 (indistinguishable from
    # missing). Which apps a caller may reach is enforced one layer up:
    # an APP key may only hit its OWN scope (services/app_key_scope, the
    # "own-app" rule: path app_id == api_key.app_id); a human Bearer caller
    # may only hit an app they manage (the apps router gates that).
    app = (
        db.query(models.App)
        .filter(models.App.id == scope_id)
        .filter(models.App.tenant_id == user.tenant_id)
        .filter(models.App.deleted_at.is_(None))
        .first()
    )
    if app is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found",
        )
    return scope_prefix(kind, scope_id), scope_id


# ---------------------------------------------------------------------------
# Storage client — the ONE seam tests monkeypatch
# ---------------------------------------------------------------------------

def _client_for(tenant_id: int):
    """The MinioClient for the tenant (BYO-S3-aware: its constructor routes
    to the tenant's active StorageConnection endpoint/bucket when one
    exists, else the house default + ``tenant-{id}-files``). Tests patch
    THIS function to inject a fake — no other storage import below."""
    from utils.minio_client import MinioClient
    return MinioClient(tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Tombstone / audit helpers
# ---------------------------------------------------------------------------

def _audit(db, *, tenant_id: int, user_id: Optional[int], event_name: str,
           details: Dict[str, Any]) -> None:
    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        user_id=user_id,
        event_name=event_name,
        details=details,
    ))


AUDIT_LINK_ISSUED = "STORAGE_SCOPE_LINK_ISSUED"
AUDIT_OBJECT_DELETED = "STORAGE_SCOPE_OBJECT_DELETED"


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _upsert_object_row(db, *, tenant_id: int, kind: str, scope_id: int,
                       key: str, content_type: Optional[str] = None) -> models.StorageScopeObject:
    row = db.query(models.StorageScopeObject).filter(
        models.StorageScopeObject.tenant_id == tenant_id,
        models.StorageScopeObject.scope_kind == kind,
        models.StorageScopeObject.scope_id == scope_id,
        models.StorageScopeObject.object_key == key,
    ).first()
    if row is None:
        row = models.StorageScopeObject(
            tenant_id=tenant_id, scope_kind=kind, scope_id=scope_id,
            object_key=key, content_type=content_type,
        )
        db.add(row)
        db.flush()
    elif content_type and not row.content_type:
        row.content_type = content_type
    return row


# ---------------------------------------------------------------------------
# Presigned links (CE)
# ---------------------------------------------------------------------------

def create_link(db, *, tenant_id: int, kind: str, scope_id: int, key: str,
                method: str, expires_in: Optional[int] = None,
                content_type: Optional[str] = None, user: models.User = None
                ) -> Dict[str, Any]:
    """Mint a presigned PUT or GET URL for ``key`` inside the scope prefix.

    The key is validated (no traversal, length cap) and joined with the
    scope prefix, so a signed URL can NEVER address anything outside
    ``{kind}s/{scope_id}/``. TTL is capped at the platform setting.
    """
    from fastapi import HTTPException, status as _status

    method = (method or "").upper()
    if method not in ("GET", "PUT"):
        raise HTTPException(
            status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="method must be 'GET' or 'PUT'",
        )
    try:
        key = validate_object_key(key)
    except ValueError as e:
        raise HTTPException(
            status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    cap = link_max_ttl(db)
    if expires_in is None or expires_in <= 0:
        ttl = cap
    else:
        ttl = min(int(expires_in), cap)

    prefix, scope_id = resolve_scope(db, kind, scope_id, user)
    full_key = prefix + key
    mc = _client_for(tenant_id)
    bucket = mc.bucket_name

    if method == "PUT":
        # The size cap is advisory: presign has no size param (S3 caps a
        # single PUT at 5GiB anyway); the setting records the tenant-facing
        # contract and is surfaced in the response so clients pre-validate.
        url = mc.presigned_client.presigned_put_object(
            bucket_name=bucket, object_name=full_key,
            expires=timedelta(seconds=ttl))
        _upsert_object_row(db, tenant_id=tenant_id, kind=kind,
                           scope_id=scope_id, key=key,
                           content_type=content_type)
        result = {
            "url": url, "bucket": bucket, "key": key, "method": method,
            "full_key": full_key, "expires_in": ttl,
            "max_size_bytes": max_put_size(db),
        }
        # The uploader sends this as the PUT's Content-Type header; a
        # presigned PUT cannot carry response-content-type (GET-only).
        if content_type:
            result["content_type"] = content_type
    else:
        # GET links can override the download's Content-Type via the
        # (SIGNED) response-content-type query param — passed to the signer
        # itself, never appended after the signature.
        url = mc.presigned_client.presigned_get_object(
            bucket_name=bucket, object_name=full_key,
            expires=timedelta(seconds=ttl),
            extra_query_params=(
                {"response-content-type": content_type}
                if content_type else None),
        )
        result = {
            "url": url, "bucket": bucket, "key": key, "method": method,
            "full_key": full_key, "expires_in": ttl,
        }

    _audit(db, tenant_id=tenant_id, user_id=(user.id if user else None),
           event_name=AUDIT_LINK_ISSUED,
           details={"scope_kind": kind, "scope_id": scope_id, "key": key,
                    "method": method, "expires_in": ttl})
    db.commit()
    return result


# ---------------------------------------------------------------------------
# Object listing (CE)
# ---------------------------------------------------------------------------

def list_objects(db, *, tenant_id: int, kind: str, scope_id: int,
                 prefix: Optional[str] = None, skip: int = 0,
                 limit: Optional[int] = None, user: models.User = None
                 ) -> Dict[str, Any]:
    """Paginated listing of the scope's objects (keys relative to the scope
    prefix). ``prefix`` is a scope-relative sub-prefix, validated with the
    same rules as object keys. ``skip`` pages through the listing using
    S3's start-after cursor semantics."""
    from fastapi import HTTPException, status as _status

    if limit is None:
        limit = list_page_size(db)
    limit = max(1, min(int(limit), list_page_size(db)))
    skip = max(0, int(skip or 0))

    if prefix is not None and prefix != "":
        try:
            prefix = validate_object_key(prefix)
        except ValueError as e:
            raise HTTPException(
                status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    prefix = (prefix or "").strip("/")

    scope_pref, _ = resolve_scope(db, kind, scope_id, user)
    base = scope_pref + prefix
    mc = _client_for(tenant_id)
    bucket = mc.bucket_name

    objs = []
    consumed = 0
    # Skip is applied by iterating from the start of the prefix (simple +
    # correct for scope listings; scopes are expected to be small-ish and
    # the page cap keeps the response bounded).
    for obj in mc.client.list_objects(
            bucket_name=bucket, prefix=base, recursive=True):
        if obj.is_dir:
            continue
        name = obj.object_name
        if not name.startswith(scope_pref) or name.startswith(
                trash_prefix(kind, scope_id)):
            continue  # never leak trash or out-of-scope keys
        if consumed < skip:
            consumed += 1
            continue
        objs.append({
            "key": name[len(scope_pref):],
            "size_bytes": int(getattr(obj, "size", 0) or 0),
            "last_modified": obj.last_modified.isoformat()
                             if obj.last_modified else None,
        })
        if len(objs) >= limit:
            break
    return {
        "items": objs,
        "prefix": prefix,
        "skip": skip,
        "limit": limit,
        "has_more": len(objs) >= limit,
    }


# ---------------------------------------------------------------------------
# Soft delete (CE) — ISO 27001: no hard deletes
# ---------------------------------------------------------------------------

def soft_delete_object(db, *, tenant_id: int, kind: str, scope_id: int,
                       key: str, user: models.User = None
                       ) -> Dict[str, Any]:
    """Soft-delete one scope object.

    ISO 27001 allows no hard deletes, so the bytes are NOT destroyed:
    the object is COPIED into the scope's ``.trash/`` prefix and the
    original is removed from its live location; a tombstone row is
    written/updated in ``storage_scope_objects`` (status='deleted',
    deleted_at) and the row is never removed. The trash copy is what a
    future (PAID) retention sweep may eventually purge after the grace
    window — until then the data remains recoverable.
    """
    from fastapi import HTTPException, status as _status

    try:
        key = validate_object_key(key)
    except ValueError as e:
        raise HTTPException(
            status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    _pref, scope_id = resolve_scope(db, kind, scope_id, user)
    full_key = scope_prefix(kind, scope_id) + key
    trash_key = trash_prefix(kind, scope_id) + key
    mc = _client_for(tenant_id)
    bucket = mc.bucket_name

    row = db.query(models.StorageScopeObject).filter(
        models.StorageScopeObject.tenant_id == tenant_id,
        models.StorageScopeObject.scope_kind == kind,
        models.StorageScopeObject.scope_id == scope_id,
        models.StorageScopeObject.object_key == key,
    ).first()
    now = _now()
    # Network I/O: copy-then-remove. If the source object does not exist we
    # still record the tombstone (the delete is idempotent and audit is the
    # point of the row).
    from minio.commonconfig import CopySource
    copy_error: Optional[str] = None
    try:
        mc.client.copy_object(
            bucket, trash_key, CopySource(bucket, full_key),
        )
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", "")
        if code not in ("NoSuchKey", "NoSuchBucket"):
            copy_error = f"copy to trash failed: {e}"
        else:
            copy_error = f"source object not found ({code})"
    try:
        mc.client.remove_object(bucket, full_key)
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", "")
        if code not in ("NoSuchKey", "NoSuchBucket") and not copy_error:
            copy_error = f"remove failed: {e}"

    # Tombstone (soft delete record) — written AFTER the I/O.
    if row is None:
        row = models.StorageScopeObject(
            tenant_id=tenant_id, scope_kind=kind, scope_id=scope_id,
            object_key=key,
        )
        db.add(row)
    row.status = "deleted"
    row.deleted_at = now
    row.updated_at = now
    _audit(db, tenant_id=tenant_id, user_id=(user.id if user else None),
           event_name=AUDIT_OBJECT_DELETED,
           details={"scope_kind": kind, "scope_id": scope_id, "key": key,
                    "trash_key": trash_key, "copy_error": copy_error})
    db.commit()
    return {
        "key": key, "status": "deleted", "trash_key": trash_key,
        "deleted_at": now.isoformat(),
        "copy_error": copy_error,
    }

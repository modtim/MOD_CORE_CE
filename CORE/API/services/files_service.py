"""Business logic for the Files upload domain (Gate 2b, routers/files.py).

Owns the policy the audit found hand-rolled inline in the router: folder
upload permission (404-vs-403), the storage-limit gate, and the
dedupe-vs-create decision for ContentBlob. Data access goes through
`crud/files.py` (the only place `db.query` lives for this domain).

The raw upload mechanics (reading the multipart body, hashing, the MinIO
call, and the Cog/File record creation + compensating rollback on a failed
upload) stay in `routers/files.py` -- this is a Gate 2b "partial migration"
for an upload handler whose transaction is a single tightly-coupled unit;
see the module docstring note left at the call site.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy.orm import Session

import models
from crud import files as files_crud
from services._base import NotFoundError, ServiceError

logger = logging.getLogger(__name__)

#: S14: legacy `/upload` endpoint file-size cap (500MB). Larger uploads must
#: use the multipart upload flow instead.
MAX_UPLOAD_SIZE = 500 * 1024 * 1024


class ForbiddenError(ServiceError):
    """Caller is authenticated but lacks a required capability on an existing
    resource (router maps -> 403). Kept distinct from `NotFoundError` so a
    legit permission failure never collapses into a 404."""

    def __init__(self, message: str = "Forbidden", *, details=None):
        super().__init__(message, code="forbidden", details=details)


def authorize_upload_folder(db: Session, *, cog_id: int, user: "models.User") -> None:
    """Ensure `cog_id` is an existing, non-deleted folder in `user`'s tenant
    that `user` may upload into.

    Raises `NotFoundError` if the folder doesn't exist (or belongs to another
    tenant -- existence is never leaked across tenants), `ForbiddenError` if
    the folder exists but the user lacks `can_upload`.
    """
    from permissions import check_user_capability_on_cog

    cog = files_crud.get_active_folder(db, cog_id, user.tenant_id)
    if not cog:
        raise NotFoundError("Folder not found")

    if not check_user_capability_on_cog(db, user.id, cog_id, "can_upload"):
        raise ForbiddenError("You don't have permission to upload files to this folder")


def enforce_storage_limit(db: Session, *, user: "models.User", file_size_bytes: int) -> None:
    """Enforce the tenant's storage cap for an incoming upload.

    Platform admins acting on the platform tenant (tenant_id == 1) bypass the
    cap via the shared Q.2 gate, which raises `HTTPException(413)` when the
    cap would be exceeded (existing `utils.usage_enforcement` behavior,
    reused as-is rather than re-wrapped in a ServiceError).
    """
    from utils.usage_enforcement import enforce_storage_quota

    enforce_storage_quota(db, user.tenant_id, file_size_bytes, user=user)


class MalwareInfectedError(ServiceError):
    """Upload content was flagged infected by the malware scanner.

    Router maps -> 422. Kept distinct from `ForbiddenError` so an
    anti-virus verdict never reads as a permission failure."""

    def __init__(self, message: str = "File blocked by malware scan", *, details=None):
        super().__init__(message, code="malware_infected", details=details)


class MalwareScanUnavailableError(ServiceError):
    """The malware scan errored and `malware_scan.fail_closed` is true, so
    the upload must be refused. Router maps -> 503."""

    def __init__(self, message: str = "Malware scan unavailable", *, details=None):
        super().__init__(message, code="malware_scan_unavailable", details=details)


def gate_upload_malware_scan(
    db: Session,
    *,
    user: "models.User",
    filename: Optional[str],
    content: bytes,
) -> None:
    """FX.90 part 1 (Q76=A) — scan an upload BEFORE it is stored.

    No-op while `malware_scan.enabled` is False (default) — the upload path
    is unchanged. When enabled:
      clean    -> return (store the file)
      infected -> 422 (MalwareInfectedError) + `MALWARE_UPLOAD_BLOCKED` audit
      error    -> fail mode: `fail_closed` True (default) -> 503
                  (MalwareScanUnavailableError); False -> accept with a
                  WARNING + `MALWARE_SCAN_ERROR` audit.
    Both audit rows record tenant, uploader, filename, size and signature /
    error detail — never file content.
    """
    # 1788 CE carve: malware_scan is paid; with it absent the scan gate is a
    # no-op (its own default posture is disabled) — uploads store unchanged.
    from utils.paid_import import optional_paid
    malware_scan = optional_paid("services.malware_scan")
    if malware_scan is None:
        return
    from audit import log_security_event

    cfg = malware_scan.get_config(db=db)
    if not cfg.enabled:
        return

    result = malware_scan.scan_bytes(content, db=db)
    metadata = {
        "filename": filename,
        "size_bytes": len(content),
        "scanner": "none",
    }
    if result.status == "infected":
        log_security_event(
            db,
            models.AuditAction.MALWARE_UPLOAD_BLOCKED,
            user_id=user.id,
            tenant_id=user.tenant_id,
            metadata={**metadata, "signature": result.signature},
        )
        raise MalwareInfectedError(
            f"Upload blocked: file contains malware "
            f"({result.signature or 'signature unknown'})"
        )
    if result.status == "error":
        metadata = {**metadata, "detail": result.detail,
                    "fail_closed": cfg.fail_closed}
        log_security_event(
            db,
            models.AuditAction.MALWARE_SCAN_ERROR,
            user_id=user.id,
            tenant_id=user.tenant_id,
            metadata=metadata,
        )
        if cfg.fail_closed:
            raise MalwareScanUnavailableError(
                "Upload blocked: malware scanner unavailable "
                f"({result.detail})"
            )
        logger.warning(
            "FX.90 malware scan error accepted under fail-open "
            "tenant=%s user=%s file=%s detail=%s",
            user.tenant_id, user.id, filename, result.detail,
        )



def gate_artefact_malware_scan(
    db: Session,
    *,
    path: Optional[str] = None,
    dir_root: Optional[str] = None,
    source: str,
    artefact: Optional[str],
    user: Optional["models.User"] = None,
    tenant_id: Optional[int] = None,
) -> None:
    """FX.90 p2a (Q76=A) — scan a DOWNLOADED marketplace/model artefact
    before it is registered or made usable.

    `path` is one downloaded file, `dir_root` a staged artefact directory;
    exactly one is required. The scan streams (services/malware_scan.py
    `scan_file`/`scan_artefact_dir`), so a multi-GB model package never has
    to fit in RAM.

    No-op while `malware_scan.enabled` is False (the default) — the
    marketplace/model download paths are unchanged. When enabled:
      clean    -> return (the caller may register the artefact)
      infected -> `MALWARE_UPLOAD_BLOCKED` audit (same action as the upload
                  path, metadata carries source + artefact + signature) and
                  MalwareInfectedError (-> 422). The caller must NOT
                  register; it deletes only the temp download and soft-flags
                  the DB row if one exists (ISO 27001: never a hard delete).
      error    -> the upload path's fail mode verbatim: `fail_closed` True
                  (default) -> MalwareScanUnavailableError (-> 503);
                  False -> accept with a WARNING + MALWARE_SCAN_ERROR audit.
    """
    # 1788 CE carve: malware_scan is paid; with it absent the artefact gate is
    # a no-op (same disabled-default posture). SaaS unchanged when present.
    from utils.paid_import import optional_paid
    malware_scan = optional_paid("services.malware_scan")
    if malware_scan is None:
        return
    from audit import log_security_event

    cfg = malware_scan.get_config(db=db)
    if not cfg.enabled:
        return
    if not (path or dir_root):
        raise ServiceError("artefact scan needs a path or dir_root", code="bad_request")

    if dir_root:
        result = malware_scan.scan_artefact_dir(dir_root, db=db)
        size = None
    else:
        result = malware_scan.scan_file(path, db=db)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None

    metadata = {
        "source": source,
        "artefact": artefact,
        "size_bytes": size,
        "scanner": "none",
    }
    if result.status == "infected":
        log_security_event(
            db,
            models.AuditAction.MALWARE_UPLOAD_BLOCKED,
            user_id=getattr(user, "id", None),
            tenant_id=tenant_id if tenant_id is not None else getattr(
                user, "tenant_id", None),
            metadata={**metadata,
                      "signature": result.signature,
                      "detail": result.detail},
        )
        raise MalwareInfectedError(
            f"Artefact blocked: downloaded {source} artefact "
            f"'{artefact or 'unnamed'}' contains malware "
            f"({result.signature or 'signature unknown'})"
        )
    if result.status == "error":
        metadata = {**metadata, "detail": result.detail,
                    "fail_closed": cfg.fail_closed}
        log_security_event(
            db,
            models.AuditAction.MALWARE_SCAN_ERROR,
            user_id=getattr(user, "id", None),
            tenant_id=tenant_id if tenant_id is not None else getattr(
                user, "tenant_id", None),
            metadata=metadata,
        )
        if cfg.fail_closed:
            raise MalwareScanUnavailableError(
                "Artefact blocked: malware scanner unavailable "
                f"({result.detail})"
            )
        logger.warning(
            "FX.90 p2a artefact scan error accepted under fail-open "
            "source=%s artefact=%s detail=%s",
            source, artefact, result.detail,
        )


def upsert_content_blob(
    db: Session,
    *,
    tenant_id: int,
    content_hash: str,
    storage_path: str,
    mime_type: Optional[str],
    file_extension: str,
    cog_id: int,
    size_bytes: int,
) -> models.ContentBlob:
    """Create-or-update the `ContentBlob` for `content_hash` (content-addressed
    dedupe). Does not commit -- the caller (router) commits alongside the
    `File`/`Cog` writes it owns in the same transaction.

    On an update, `size_bytes` is intentionally left untouched (matches the
    original inline behavior -- only storage_path/cog_id/mime_type/extension
    are refreshed for content that already exists).
    """
    existing_blob = files_crud.get_content_blob(db, content_hash, tenant_id)

    if existing_blob:
        existing_blob.storage_path = storage_path
        existing_blob.cog_id = cog_id
        existing_blob.mime_type = mime_type
        existing_blob.file_extension = file_extension
        return existing_blob

    content_blob = models.ContentBlob(
        id=content_hash,
        tenant_id=tenant_id,
        size_bytes=size_bytes,
        mime_type=mime_type,
        file_extension=file_extension,
        storage_path=storage_path,
        cog_id=cog_id
    )
    db.add(content_blob)
    return content_blob


# =============================================================================
# FX.90 p2b (Q76=A) — quarantine: a soft flag on the artefact that blocks
# downloads and workflow runs, never a delete (ISO 27001: no hard delete).
# =============================================================================


class QuarantinedArtefactError(ServiceError):
    """The artefact carries the malware-quarantine flag, so the requested
    delivery/run is refused (router maps -> 423 Locked — the content exists
    but is locked pending admin release). Kept distinct from ForbiddenError
    so a scan verdict never reads as a permission failure."""

    def __init__(self, message: str = "Artefact quarantined", *, details=None):
        super().__init__(message, code="malware_quarantined", details=details)


def get_quarantine(cog) -> Optional[dict]:
    """The cog's malware-quarantine record, or None when not quarantined.

    Backed by the additive nullable ``Cog.malware_quarantine`` JSONB column
    (models.py + migrations/20261007zj_fx90_p2b_malware_quarantine.py, NOT
    applied by this task — the test schema gets it from create_all). NULL =
    not quarantined. Shape::

        {"flagged_at": iso, "signature": str|None, "source": str,
         "artefact": str|None, "detail": str|None,
         "released_at": iso, "released_by": int}   # set on release only

    A released record keeps the trail (flag removed, history retained under
    ``history``) — quarantine is a flag, never a delete.
    """
    rec = getattr(cog, "malware_quarantine", None)
    return rec if isinstance(rec, dict) and rec.get("flagged_at") else None


def quarantine_artefact(
    db: Session,
    *,
    cog_id: int,
    tenant_id: int,
    source: str,
    artefact: Optional[str] = None,
    signature: Optional[str] = None,
    detail: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Optional[dict]:
    """Soft-flag `cog_id` as malware-quarantined + audit it.

    Idempotent: re-quarantining refreshes the record (newest signature/detail
    win) and keeps the original `flagged_at`. Returns the record, or None when
    the cog is missing/soft-deleted in another tenant (existence never leaked).
    Does not commit — the caller owns the transaction (the audit helper does
    its own commit, matching every other security hook here).
    """
    from datetime import datetime as _dt, timezone as _tz
    from crud import cogs_assets_cogs_files as acf_crud
    from audit import log_security_event
    from models import AuditAction

    cog = acf_crud.get_active_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=tenant_id)
    if cog is None:
        return None

    prior = getattr(cog, "malware_quarantine", None)
    flagged_at = (prior or {}).get("flagged_at") or _dt.now(_tz.utc).isoformat()
    rec = {
        "flagged_at": flagged_at,
        "signature": signature,
        "source": source,
        "artefact": artefact,
        "detail": detail,
    }
    cog.malware_quarantine = rec

    log_security_event(
        db,
        AuditAction.MALWARE_UPLOAD_BLOCKED,
        user_id=user_id,
        tenant_id=tenant_id,
        metadata={"source": source, "artefact": artefact, "cog_id": cog_id,
                  "signature": signature, "detail": detail,
                  "quarantined": True},
    )
    return rec


def release_quarantined_artefact(
    db: Session,
    *,
    cog_id: int,
    tenant_id: int,
    admin_user: "models.User",
    reason: Optional[str] = None,
) -> Optional[dict]:
    """Platform-admin release: clear the quarantine flag, keep the trail, audit.

    Returns the released record (with `released_at`/`released_by` stamped and
    appended to ``custom_metadata['malware_quarantine_history']``) or None when
    the cog is missing / not quarantined. No hard delete: the flag is cleared,
    the history row is retained, and the audit row is the authoritative record.
    """
    from datetime import datetime as _dt, timezone as _tz
    from crud import cogs_assets_cogs_files as acf_crud
    from audit import log_security_event
    from models import AuditAction

    cog = acf_crud.get_active_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=tenant_id)
    if cog is None:
        return None
    rec = get_quarantine(cog)
    if rec is None:
        return None

    released = {
        **rec,
        "released_at": _dt.now(_tz.utc).isoformat(),
        "released_by": getattr(admin_user, "id", None),
        "reason": reason,
    }
    # The flag is cleared; the released record + reason live in the audit row
    # (append-only, ISO 27001) — the artefact row itself is never deleted.
    cog.malware_quarantine = None

    log_security_event(
        db,
        AuditAction.ADMIN_SECURITY_OVERRIDE,
        user_id=getattr(admin_user, "id", None),
        tenant_id=tenant_id,
        metadata={"cog_id": cog_id, "artefact": rec.get("artefact"),
                  "signature": rec.get("signature"), "source": rec.get("source"),
                  "flagged_at": rec.get("flagged_at"), "reason": reason,
                  "released": True},
    )
    return released


def raise_if_quarantined(cog, *, cog_id: Optional[int] = None) -> None:
    """Refuse delivery/run use of a quarantined cog (423 via the caller's map)."""
    rec = get_quarantine(cog)
    if rec is None:
        return
    raise QuarantinedArtefactError(
        f"Download blocked: this artefact is malware-quarantined "
        f"(flagged {rec.get('flagged_at') or 'earlier'}, "
        f"signature {rec.get('signature') or 'unknown'}); "
        "a platform admin must release it before it can be downloaded or used "
        "as a workflow input"
    )


def check_run_quarantined_inputs(
    db: Session,
    *,
    tenant_id: int,
    cog_ids: "Optional[list]",
    user_id: Optional[int] = None,
) -> list:
    """FX.90 p2b (Q76=A) — workflow-start input gate.

    A run whose input cogs include a malware-quarantined artefact is refused
    BEFORE any WorkflowRun/credit is created (same position as the drain /
    overage / data-marking gates in routers/workflows_domains/workflows.py).
    Returns the offending cog ids and raises QuarantinedArtefactError (-> 423)
    with them in `details`; also writes a security audit row naming the
    quarantined inputs so the uploader/admin trail is complete.
    """
    from crud import cogs_assets_cogs_files as acf_crud
    from audit import log_security_event
    from models import AuditAction

    if not cog_ids:
        return []
    flagged = []
    for cid in cog_ids:
        cog = acf_crud.get_active_cog_by_id_and_tenant(
            db, cog_id=cid, tenant_id=tenant_id)
        if cog is not None and get_quarantine(cog) is not None:
            flagged.append(cid)
    if not flagged:
        return []

    log_security_event(
        db,
        AuditAction.MALWARE_UPLOAD_BLOCKED,
        user_id=user_id,
        tenant_id=tenant_id,
        metadata={"quarantined_cog_ids": flagged, "workflow_run": True},
    )
    raise QuarantinedArtefactError(
        f"Run refused: input artefact(s) {flagged} are malware-quarantined; "
        "a platform admin must release them before they can be used as "
        "workflow inputs",
        details={"cog_ids": flagged},
    )


def list_quarantined_artefacts(
    db: Session, *, tenant_id: int
) -> list:
    """Every malware-quarantined artefact (live Cog rows) in `tenant_id`."""
    rows = db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
        # SQL `IS NOT NULL` alone is not enough: SQLAlchemy 2.0's JSON type
        # serialises a Python None into JSONB `null`, which still satisfies
        # IS NOT NULL. Keep it as a cheap pre-filter and let get_quarantine
        # (the same test every other surface uses) be authoritative.
        models.Cog.malware_quarantine.isnot(None),
    ).all()
    out = []
    for c in rows:
        rec = get_quarantine(c)
        if rec is None:
            continue
        out.append({
            "cog_id": c.id,
            "name": c.name,
            "flagged_at": rec.get("flagged_at"),
            "signature": rec.get("signature"),
            "source": rec.get("source"),
            "artefact": rec.get("artefact"),
        })
    return out


def quarantine_release_state(
    db: Session, *, cog_id: int, tenant_id: int
) -> str:
    """"absent" (no live cog / other tenant), "quarantined", or "clear"."""
    from crud import cogs_assets_cogs_files as acf_crud

    cog = acf_crud.get_active_cog_by_id_and_tenant(
        db, cog_id=cog_id, tenant_id=tenant_id)
    if cog is None:
        return "absent"
    return "quarantined" if get_quarantine(cog) is not None else "clear"

"""GX.R4 (launch-completion-megaplan, Group X) — ephemeral run inputs.

Owner decision OQ-5 = (a):
  * TTL 24h, then a 7-day grace, then the BYTES are purged in ALL modes
    (even ISO). The RECORD (row) is kept forever — status/timestamps advance
    (soft) but the row is never deleted (ISO 27001: NO hard delete).
  * Audit events on create / expire / purge.
  * TTL + grace are DB platform settings (services.platform_settings),
    never env.

Design (see openspec .../gx-a1-app-design.md §4/§5.3): the upload materializes
the bytes into the tenant's existing content-addressed store (ContentBlob) plus
a HIDDEN, root-level Cog + Version (``cog_id`` on the row). That lets a run's
input node consume the file through the UNCHANGED cog pipeline
(``COG_IDS`` → ``resolve-cog-hash`` → content-hash staging) with no worker or
container changes. The hidden Cog is filtered out of the library listing, so
the input is invisible to the user's asset browser.

This module is the R4 user-facing (``app_id NULL``) axis. The app namespace
(GX.A2) reuses the same table with a real ``app_id`` FK.
"""
from __future__ import annotations

import hashlib
import io
import re
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import models
from crud.cogs_assets_cogs_files import get_content_blob_by_id_and_tenant
from utils.minio_client import MinioClient, get_tenant_bucket_name
from services._base import ServiceError

logger = logging.getLogger(__name__)

# DB-backed platform settings (services.platform_settings) — never env.
TTL_KEY = "ephemeral_inputs.ttl_hours"
GRACE_KEY = "ephemeral_inputs.purge_grace_days"
TTL_DEFAULT_HOURS = 24
GRACE_DEFAULT_DAYS = 7

# AuditEvent.event_name values (append-only; free-form string column, no enum).
AUDIT_CREATED = "EPHEMERAL_INPUT_CREATED"
AUDIT_EXPIRED = "EPHEMERAL_INPUT_EXPIRED"
AUDIT_REUSED = "EPHEMERAL_INPUT_REUSED"
AUDIT_BYTES_PURGED = "EPHEMERAL_INPUT_BYTES_PURGED"
AUDIT_SCAN_BLOCKED_INPUT = "MALWARE_SCAN_BLOCKED_RUN_INPUT"

# 30.6.12 c12c — the wire format of ``content_hash``: lowercase sha256 hex,
# exactly 64 chars. Uppercase is INVALID (we never fold case: an uppercase
# lookup would be an oracle for "this hash exists, you just typed it wrong").
CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# FX.90 p3b — workflow input gate (the async-scan verdict layer).
class InputScanBlockedError(ServiceError):
    """A run input's malware scan has not passed, so the run is refused.

    Router maps -> 409 Conflict (the file exists and is usable, but its scan
    state conflicts with starting a run on it right now — unlike p2b's 423
    Locked, which is the admin-release verdict for a QUARANTINED artefact).
    `details` names every offending file and its state so the UI can say
    "wait for the scan / this file is infected" per file."""

    def __init__(self, message: str = "Run refused: input scan not clean",
                 *, details: Any = None):
        super().__init__(message, code="input_scan_blocked", details=details)


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def ttl_hours(db) -> int:
    """TTL from the DB platform setting (falls back to the registry default)."""
    from services.platform_settings import get_setting
    try:
        return int(get_setting(TTL_KEY, db))
    except Exception:  # noqa: BLE001 — degrade to the default, never raise
        return TTL_DEFAULT_HOURS


def purge_grace_days(db) -> int:
    """Grace (bytes kept after expiry) from the DB platform setting."""
    from services.platform_settings import get_setting
    try:
        return int(get_setting(GRACE_KEY, db))
    except Exception:  # noqa: BLE001
        return GRACE_DEFAULT_DAYS


def _audit(db, *, tenant_id: int, user_id: Optional[int], event_name: str,
           details: Dict[str, Any]) -> None:
    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        user_id=user_id,
        event_name=event_name,
        details=details,
    ))


# ---------------------------------------------------------------------------
# 30.6.12 c12a — the upload itself, as a service (was inline in
# routers/ephemeral_run_inputs.py:79-235). Behaviour is UNCHANGED: same hidden
# root Cog + Version + File + ContentBlob, same ``app_id=None`` stamp (c12b
# stamps the app FK), same server-side sha256, same TTL, one
# EPHEMERAL_INPUT_CREATED audit row per created input.
# ---------------------------------------------------------------------------
BATCH_MAX_FILES_KEY = "ephemeral_inputs.batch_max_files"
BATCH_MAX_TOTAL_BYTES_KEY = "ephemeral_inputs.batch_max_total_bytes"
BATCH_MAX_FILES_DEFAULT = 16
BATCH_MAX_TOTAL_BYTES_DEFAULT = 250 * 1024 * 1024  # 250 MiB
UPLOAD_CAP_SETTING_KEY = "uploads.max_bytes_cog"    # middleware/upload_size_cap.py:105-106 class "cog"
_READ_CHUNK = 1 << 20                               # 1 MiB — today's chunk size


def _setting_int(db, key: str, default: int) -> int:
    """DB-backed tunable (tenant row > platform row > registry default)."""
    from services.platform_settings import get_setting
    try:
        value = int(get_setting(key, db))
    except Exception:  # noqa: BLE001 — degrade to the default, never raise
        return default
    return value if value > 0 else default


def batch_max_files(db) -> int:
    return _setting_int(db, BATCH_MAX_FILES_KEY, BATCH_MAX_FILES_DEFAULT)


def batch_max_total_bytes(db) -> int:
    return _setting_int(db, BATCH_MAX_TOTAL_BYTES_KEY, BATCH_MAX_TOTAL_BYTES_DEFAULT)


def per_file_cap_bytes(db, tenant_id: Optional[int] = None) -> int:
    """The per-file cap for this route = the upload_size_cap "cog" class cap.

    middleware/upload_size_cap.py:105-106 classifies ``/v1/runs/ephemeral-inputs``
    as ``"cog"`` (→ ``uploads.max_bytes_cog``); the middleware only sees the
    declared Content-Length, so a multipart route re-checks each part against
    the SAME cap. Read through the settings registry with the CALLER's session
    (never the middleware's db=None lookup, which would open its own session).
    """
    platform_cap = _setting_int(db, UPLOAD_CAP_SETTING_KEY, 1024 * 1024 * 1024)
    if tenant_id is None:
        return platform_cap
    from services.platform_settings import get_setting
    try:
        raw = get_setting(UPLOAD_CAP_SETTING_KEY, db, tenant_id=tenant_id)
        tenant_cap = int(raw) if raw is not None else None
    except Exception:  # noqa: BLE001 — no readable tenant row => platform cap
        tenant_cap = None
    if tenant_cap is None or tenant_cap <= 0:
        return platform_cap
    return min(platform_cap, tenant_cap)


def _get_row(db, ref_id: int, tenant_id: int) -> Optional["models.AppEphemeralInput"]:
    return db.query(models.AppEphemeralInput).filter(
        models.AppEphemeralInput.id == ref_id,
        models.AppEphemeralInput.tenant_id == tenant_id,
        models.AppEphemeralInput.deleted_at.is_(None),
    ).first()


def resolve_for_run(db, tenant_id: int,
                    inputs: Optional[Dict[str, Any]]) -> Tuple[List[int], Dict[str, Any]]:
    """Scan a run's per-node ``inputs`` for ``ephemeral_input_id`` references.

    Returns ``(cog_ids, sanitized_inputs)``:
      * ``cog_ids`` — the backing Cogs to append to the run's input files
        (deduped, order-preserving). Feeding these into the run's ``cog_ids``
        makes the input node receive each file exactly like an uploaded cog.
      * ``sanitized_inputs`` — the same ``inputs`` with every
        ``ephemeral_input_id`` key removed, so it is never sprayed into a
        node's extraVariables / container env.

    Raises:
      * HTTPException 404 — the id does not exist in this tenant (cross-tenant
        reads are indistinguishable from missing — no existence oracle).
      * HTTPException 410 — the input is expired / purged (bytes gone) or its
        TTL has already lapsed (the retention job may not have flipped it yet).
    """
    from fastapi import HTTPException, status as _http_status

    inputs = inputs if isinstance(inputs, dict) else {}
    now = _now()
    cog_ids: List[int] = []
    seen = set()
    for _node_key, vals in inputs.items():
        if not isinstance(vals, dict) or "ephemeral_input_id" not in vals:
            continue
        raw_ref = vals.get("ephemeral_input_id")
        try:
            ref_id = int(raw_ref)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=_http_status.HTTP_400_BAD_REQUEST,
                detail="ephemeral_input_id must be an integer",
            )
        row = _get_row(db, ref_id, tenant_id)
        if row is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail="Ephemeral input not found",
            )
        if row.status != "active" or row.expires_at <= now:
            raise HTTPException(
                status_code=_http_status.HTTP_410_GONE,
                detail=f"Ephemeral input {ref_id} has expired and is no longer available.",
            )
        if row.cog_id is not None and row.cog_id not in seen:
            seen.add(row.cog_id)
            cog_ids.append(row.cog_id)

    # Strip the reference key so it never reaches a node's env/extraVariables.
    sanitized: Dict[str, Any] = {}
    for k, v in inputs.items():
        if isinstance(v, dict) and "ephemeral_input_id" in v:
            v = {kk: vv for kk, vv in v.items() if kk != "ephemeral_input_id"}
            if not v:
                continue
        sanitized[k] = v
    return cog_ids, sanitized


# ---------------------------------------------------------------------------
# FX.90 p3b — workflow input gate for the ASYNC scan layer (p3a's marker)
# ---------------------------------------------------------------------------


def scan_state(cog) -> str:
    """One cog's async-scan state: "pending" | "infected" | "allowed".

    Reads the SAME `Cog.malware_quarantine` JSONB p2b/p3a share: a record with
    `scan_pending` is awaiting its async verdict, a record with `flagged_at`
    is the infected verdict. LEAD DECISION: a cog with NO marker at all is
    ALLOWED — a multipart upload under 64 MiB (`malware_scan.async_above_bytes`)
    never takes the async path and has no inline scan either, so "no marker"
    means "never scanned", not "clean"; refusing those would break every
    ordinary small upload. Only an explicit pending/infected marker blocks.
    """
    rec = getattr(cog, "malware_quarantine", None)
    if not isinstance(rec, dict):
        return "allowed"
    if rec.get("flagged_at"):
        return "infected"
    if rec.get("scan_pending"):
        return "pending"
    return "allowed"


def check_run_input_scans(db, tenant_id: int,
                          cog_ids: Optional[List[int]],
                          *, user_id: Optional[int] = None) -> list:
    """FX.90 p3b — refuse a run whose input file has a PENDING async scan.

    A file whose malware scan is still running (p3a queued it asynchronously
    because it is too large to scan inline) or already INFECTED must not feed
    a workflow run: the run would process bytes whose verdict the scanner has
    not delivered yet (or has condemned). Raises InputScanBlockedError (->
    409 naming every file + its state) and writes an append-only AuditEvent
    so the uploader/admin trail is complete.

    Byte-identical no-op when `malware_scan.enabled` is False (the default):
    nothing is marked then, and this reads the setting once and returns.
    Quarantined (infected) inputs are normally caught earlier by p2b's
    423 gate; they are named here too so the gate is correct standalone.
    """
    # 1788 CE carve: malware_scan is paid; with it absent no input ever has a
    # pending/infected scan verdict, so the gate returns nothing (SaaS unchanged).
    from utils.paid_import import optional_paid
    malware_scan = optional_paid("services.malware_scan")
    if malware_scan is None:
        return []
    from crud import cogs_assets_cogs_files as acf_crud

    if not cog_ids:
        return []
    if not bool(malware_scan._get("enabled")):
        return []

    blocked = []
    for cid in cog_ids:
        cog = acf_crud.get_active_cog_by_id_and_tenant(
            db, cog_id=cid, tenant_id=tenant_id)
        if cog is None:
            continue  # existence never leaked; other gates 404 these
        state = scan_state(cog)
        if state != "allowed":
            blocked.append({"cog_id": cid, "name": cog.name, "state": state})
    if not blocked:
        return []

    # Append-only AuditEvent, committed here (the run is refused, so no
    # later request transaction exists to carry it — same self-contained
    # write as account_security_alerts._emit_audit).
    db.add(models.AuditEvent(
        tenant_id=tenant_id,
        user_id=user_id,
        event_name=AUDIT_SCAN_BLOCKED_INPUT,
        details={"blocked_inputs": blocked, "workflow_run": True},
    ))
    db.commit()
    names = ", ".join(f"{b['name'] or ('#' + str(b['cog_id']))} "
                      f"({b['state']}, cog #{b['cog_id']})" for b in blocked)
    raise InputScanBlockedError(
        f"Run refused: input file(s) {names} have a malware scan that has "
        "not passed — wait for the scan to finish (or have a platform admin "
        "release the artefact if it is infected) and run again.",
        details={"blocked_inputs": blocked},
    )


# ---------------------------------------------------------------------------
# Retention sweep (expire at TTL, then purge bytes after grace) — mirrors the
# proven two-stage pattern in services/output_retention.py.
# ---------------------------------------------------------------------------

def _still_referenced(db, row) -> bool:
    """True if another not-yet-purged ephemeral row (same tenant) shares these
    bytes — content-addressed storage dedups on (content_hash, tenant), so
    purging this row's object would corrupt the sibling."""
    other = db.query(models.AppEphemeralInput.id).filter(
        models.AppEphemeralInput.tenant_id == row.tenant_id,
        models.AppEphemeralInput.content_hash == row.content_hash,
        models.AppEphemeralInput.id != row.id,
        models.AppEphemeralInput.bytes_purged_at.is_(None),
    ).first()
    return other is not None


def _purge_ephemeral_bytes(*, tenant_id: int, content_hash: str) -> bool:
    """Delete one content-addressed object's bytes from tenant storage.

    Isolated into its own module-level function so tests can monkeypatch it
    with a stub instead of touching real versitygw/MinIO (same shape as
    services.output_retention._purge_blob_bytes). Reuses MinioClient
    (BYO-S3-aware). Returns True on success, including "object already gone".
    """
    from utils.minio_client import MinioClient, get_tenant_bucket_name
    mc = MinioClient(tenant_id=tenant_id)
    # No `db` threaded in on purpose: stage2_purge() commits BEFORE this loop
    # so storage network I/O never runs inside an open DB transaction
    # (memory: external fetch inside a DB txn pins PgBouncer).
    bucket = mc.bucket_name or get_tenant_bucket_name(tenant_id)
    try:
        mc.client.remove_object(bucket, content_hash)
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", "")
        if code in ("NoSuchKey", "NoSuchBucket"):
            return True
        raise
    return True


def _held_skip(db) -> "Optional[set]":
    """GY.C15 — set of held tenant ids for a batch (fail closed: on a
    lookup error returns ``None`` which callers treat as 'every tenant
    held'). A ``None`` return is distinct from an empty set (nothing held).
    """
    from utils.paid_import import optional_paid
    _lhold = optional_paid("services.legal_hold")
    try:
        if _lhold is None:  # FX.162.p5 — CE carve: no legal holds exist
            return set()
        platform_wide, tenant_ids = _lhold.held_tenants(db)
    except Exception as e:  # noqa: BLE001 — fail closed
        logger.error("ephemeral_run_inputs: legal-hold lookup failed — "
                     "failing closed (treating all tenants as held): %s", e)
        return None
    return None if platform_wide else tenant_ids


def stage1_expire(db, *, now: Optional[datetime] = None) -> int:
    """Stage 1 (soft expire): flip past-TTL active rows to ``expired`` and
    append an audit tombstone per row. The row is NEVER deleted.

    GY.C15 — rows of a legally-held tenant are skipped (bytes + record
    preserved); supports preservation requirements (e.g. DFARS 252.204-7012
    90-day). Fails closed: a hold-lookup error skips the batch."""
    now = _now(now)
    rows = db.query(models.AppEphemeralInput).filter(
        models.AppEphemeralInput.status == "active",
        models.AppEphemeralInput.expires_at <= now,
        models.AppEphemeralInput.deleted_at.is_(None),
    ).all()
    held = _held_skip(db)  # None => all held (fail closed); set => held ids
    count = 0
    for r in rows:
        if held is None or r.tenant_id in held:
            logger.info("ephemeral_run_inputs: skipping stage1 expire id=%s "
                        "(tenant %s) — legal hold active; row left in place",
                        r.id, r.tenant_id)
            continue
        r.status = "expired"
        r.expired_at = now
        _audit(db, tenant_id=r.tenant_id, user_id=r.owner_user_id,
               event_name=AUDIT_EXPIRED,
               details={"id": r.id, "content_hash": r.content_hash,
                        "expires_at": r.expires_at.isoformat() if r.expires_at else None})
        count += 1
    if count:
        db.commit()
    return count


def stage2_purge(db, *, now: Optional[datetime] = None,
                 grace_days: Optional[int] = None) -> dict:
    """Stage 2 (byte reclaim): for expired rows past the grace period, delete
    the storage object bytes (ALL modes — owner decision OQ-5(a)) and stamp
    ``bytes_purged_at`` + a second audit tombstone.

    Network I/O rule: the read transaction is committed BEFORE any
    ``remove_object`` call, and the tombstone writes happen only AFTER all I/O
    completes — the same rule services/output_retention._purge_stage enforces.
    Rows are NEVER deleted (only ``bytes_purged_at`` is set).
    """
    now = _now(now)
    if grace_days is None:
        grace_days = purge_grace_days(db)
    cutoff = now - timedelta(days=grace_days)

    rows = db.query(models.AppEphemeralInput).filter(
        models.AppEphemeralInput.status == "expired",
        models.AppEphemeralInput.expired_at.isnot(None),
        models.AppEphemeralInput.expired_at <= cutoff,
        models.AppEphemeralInput.bytes_purged_at.is_(None),
        models.AppEphemeralInput.deleted_at.is_(None),
    ).all()

    # GY.C15 — legal hold: bytes of a held tenant are preserved. Resolve
    # the held set in the read phase (before the commit below). Fail
    # closed: a lookup error => every tenant held.
    held = _held_skip(db)  # None => all held (fail closed); set => held ids

    # Close the read transaction BEFORE any storage network I/O.
    db.commit()

    purged = 0
    purged_bytes = 0
    skipped = 0
    skipped_legal_hold = 0
    succeeded = []
    for r in rows:
        if held is None or r.tenant_id in held:
            logger.info("ephemeral_run_inputs: skipping stage2 byte purge "
                        "id=%s (tenant %s) — legal hold active; bytes "
                        "preserved", r.id, r.tenant_id)
            skipped_legal_hold += 1
            continue
        if _still_referenced(db, r):
            skipped += 1
            continue
        try:
            ok = _purge_ephemeral_bytes(tenant_id=r.tenant_id, content_hash=r.content_hash)
        except Exception as e:  # noqa: BLE001
            logger.warning("ephemeral_run_inputs: byte purge failed id=%s: %s", r.id, e)
            continue
        if ok:
            purged += 1
            purged_bytes += int(r.size_bytes or 0)
            succeeded.append(r)

    for r in succeeded:
        r.bytes_purged_at = now
        _audit(db, tenant_id=r.tenant_id, user_id=r.owner_user_id,
               event_name=AUDIT_BYTES_PURGED,
               details={"id": r.id, "content_hash": r.content_hash})
    if succeeded:
        db.commit()

    return {
        "purged": purged,
        "bytes_purged": purged_bytes,
        "skipped_still_referenced": skipped,
        "skipped_legal_hold": skipped_legal_hold,
    }


def apply_ephemeral_retention(db, *, now: Optional[datetime] = None,
                              grace_days: Optional[int] = None) -> dict:
    """Run the two-stage sweep. Runs in ALL modes (owner decision OQ-5(a));
    rows are never deleted. Returns a summary for the scheduler log."""
    now = _now(now)
    expired = stage1_expire(db, now=now)
    purge = stage2_purge(db, now=now, grace_days=grace_days)
    return {
        "mode": "all_modes",
        "generated_at": now.isoformat(),
        "expired": expired,
        **purge,
    }


class EphemeralUploadFailedError(ServiceError):
    """A single upload in a batch failed AFTER its pre-checks passed.

    The router maps this to 500 (single-file mode, today's exact status +
    detail) or to ``items[i].error = {code, detail}`` (batch mode)."""

    def __init__(self, message: str = "Failed to store the file.",
                 *, code: str = "storage_write_failed", details: Any = None):
        super().__init__(message, code=code, details=details)


class EphemeralFileTooLargeError(ServiceError):
    """One part breached the per-file cap (the upload_size_cap "cog" class)."""

    def __init__(self, message: str = "File exceeds the per-file upload cap.",
                 *, code: str = "file_too_large", details: Any = None):
        super().__init__(message, code=code, details=details)


def measure_upload(upload, cap_bytes: int) -> int:
    """Exact byte count of one part, streamed + DISCARDED, then rewound.

    Batch mode needs the summed size BEFORE any DB write or storage put (S2);
    a multipart part has no trustworthy declared length, so the only way to
    know it is to stream it. Nothing is retained (the chunks are dropped) and
    the read hard-stops at ``cap_bytes`` +1, so a 2 GiB part never lands in
    memory. ``file.seek(0)`` leaves the part re-readable by ``upload_one()``.
    """
    declared = getattr(upload, "size", None)
    if declared is not None and declared > cap_bytes:
        raise EphemeralFileTooLargeError(
            details={"cap_bytes": cap_bytes, "declared_bytes": int(declared)})
    total = 0
    while True:
        chunk = upload.file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap_bytes:
            raise EphemeralFileTooLargeError(
                details={"cap_bytes": cap_bytes, "streamed_bytes": total})
    upload.file.seek(0)
    return total


def _spool_upload(upload, cap_bytes: int) -> Tuple[bytes, str, int]:
    """Stream an ``UploadFile`` into memory, HARD-STOPPING at ``cap_bytes``.

    Today's single-file path bounded the read only by the middleware's
    declared-Content-Length check (middleware/upload_size_cap.py:246
    ``_declared_content_length``), so a lying/absent Content-Length plus a
    chunked body could stream unbounded. Here the read itself stops at the cap
    +1 byte, so a 2 GiB part never lands in memory. Returns
    ``(data, sha256_hex, size_bytes)`` — the hash is ALWAYS computed
    server-side from the bytes (S3).
    """
    declared = getattr(upload, "size", None)
    if declared is not None and declared > cap_bytes:
        raise EphemeralFileTooLargeError(
            details={"cap_bytes": cap_bytes, "declared_bytes": int(declared)})
    sha256 = hashlib.sha256()
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = upload.file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap_bytes:
            raise EphemeralFileTooLargeError(
                details={"cap_bytes": cap_bytes, "streamed_bytes": total})
        sha256.update(chunk)
        chunks.append(chunk)
    data = b"".join(chunks)
    return data, sha256.hexdigest(), len(data)


def _response_for(row) -> Dict[str, Any]:
    """The response dict for an ephemeral input (fresh or reused).

    Extracted verbatim from the tail of the fresh path in c12b: every value is
    read off the row, so the fresh path's output is byte-identical (its
    ``name``/``content_hash``/``size_bytes``/``cog_id`` are exactly the row's).
    """
    return {
        "ephemeral_input_id": row.id,
        "cog_id": row.cog_id,
        "name": row.name,
        "content_hash": row.content_hash,
        "size_bytes": row.size_bytes,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def _reusable_row(db, *, tenant_id: int, content_hash: str,
                  app_id: Optional[int], user_id: Optional[int],
                  now: datetime, ttl: timedelta):
    """30.6.12 c12b — the ONE row a re-upload of identical bytes may reuse.

    S1 isolation: the caller's tenant is ALWAYS required; an App-key caller
    (``app_id`` set) only ever sees rows stamped with that SAME app; a
    human/API-key caller (``app_id IS NULL``) additionally only sees rows it
    owns (``owner_user_id == user.id``) so per-user RBAC on the backing cog
    is never crossed. S2: only a live row whose bytes are still present —
    ``status == "active"``, not yet expired, never bytes-purged, never
    soft-deleted.

    Plain ``first()`` (not ``with_for_update(skip_locked=True)``): the reuse
    path only bumps ``expires_at`` on a row it already scoped to one principal,
    and locking a row for the rest of the request would hold a Postgres lock
    across the audit write for no benefit. Two simultaneous identical uploads
    may both go fresh (2 rows, no corruption) — accepted, not fixed.
    """
    return (db.query(models.AppEphemeralInput)
            .filter(models.AppEphemeralInput.content_hash == content_hash,
                    *_scope_filter(tenant_id=tenant_id, app_id=app_id,
                                   user_id=user_id),
                    models.AppEphemeralInput.status == "active",
                    models.AppEphemeralInput.expires_at > now,
                    models.AppEphemeralInput.bytes_purged_at.is_(None),
                    models.AppEphemeralInput.deleted_at.is_(None))
            .order_by(models.AppEphemeralInput.id.desc())
            .first())


def _scope_filter(*, tenant_id: int, app_id: Optional[int],
                  user_id: Optional[int]):
    """30.6.12 c12c — the ONE principal-scope filter, shared by reuse and GET.

    S1 isolation, identical to c12b's reuse predicate: the caller's tenant is
    ALWAYS required; an App-key caller (``app_id`` set) only ever sees rows
    stamped with that SAME app; a human/API-key caller (``app_id IS NULL``)
    additionally only sees rows it owns (``owner_user_id == user.id``), so
    per-user RBAC on the backing cog is never crossed. ``IS NULL`` is matched
    with ``is_(None)`` — a bare ``== None`` would build a no-op Python
    comparison and silently drop the isolation filter.
    """
    filters = [
        models.AppEphemeralInput.tenant_id == tenant_id,
        models.AppEphemeralInput.deleted_at.is_(None),
    ]
    if app_id is None:
        filters.append(models.AppEphemeralInput.app_id.is_(None))
        filters.append(models.AppEphemeralInput.owner_user_id == user_id)
    else:
        filters.append(models.AppEphemeralInput.app_id == app_id)
    return filters


def query_by_hash(db, *, tenant_id: int, app_id: Optional[int],
                  user_id: Optional[int], content_hash: str) -> List["models.AppEphemeralInput"]:
    """30.6.12 c12c — every live row of ``content_hash`` THIS principal may see.

    Same scope filter as c12b's reuse query (S1) and the same liveness
    predicates (``status == "active"``, not yet expired, never bytes-purged,
    never soft-deleted) — so a hash that belongs to another tenant, another
    App, or another human's NULL-app rows is indistinguishable from an
    unknown hash: same status, same body (S2, no existence oracle).

    READ-ONLY (S3): no ``expires_at`` refresh (a GET is not a reuse) and no
    audit row. Newest first, capped at 50.
    """
    return (db.query(models.AppEphemeralInput)
            .filter(models.AppEphemeralInput.content_hash == content_hash,
                    *_scope_filter(tenant_id=tenant_id, app_id=app_id,
                                   user_id=user_id),
                    models.AppEphemeralInput.status == "active",
                    models.AppEphemeralInput.expires_at > _now(),
                    models.AppEphemeralInput.bytes_purged_at.is_(None))
            .order_by(models.AppEphemeralInput.id.desc())
            .limit(50)
            .all())


def upload_one(db, *, tenant_id: int, user, upload, name: Optional[str] = None,
               cap_bytes: Optional[int] = None, bucket: Optional[str] = None,
               client_factory=None,
               app_id: Optional[int] = None) -> Dict[str, Any]:
    """One ephemeral-input upload — today's router body, verbatim behaviour.

    Materializes the bytes as a ``ContentBlob`` (dedup by (hash, tenant)) plus a
    HIDDEN root Cog + ``Version`` + ``File`` and one ``AppEphemeralInput`` row
    (``app_id`` stamped from the caller's App ApiKey in c12b — NULL for human
    callers), then one ``EPHEMERAL_INPUT_CREATED`` audit row. Returns EXACTLY
    the response dict ``POST /v1/runs/ephemeral-inputs`` has always returned.

    30.6.12 c12b adds scoped reuse: identical bytes from the SAME principal
    (tenant + app_id, plus owner_user_id when app_id is NULL) reuse the live,
    un-purged row — no storage put, no new Cog/Version/File/row, just a fresh
    ``expires_at`` and one ``EPHEMERAL_INPUT_REUSED`` audit.

    ``tenant_id`` comes from the verified principal only (S1); it is never read
    from the body. Raises ``EphemeralFileTooLargeError`` (per-file cap) and
    ``EphemeralUploadFailedError`` (storage write failed) — the router maps
    those; nothing is committed for a failed item.
    """
    from utils.usage_enforcement import enforce_storage_quota
    from utils.paid_import import optional_paid

    # Existing byte-ingress quota gate (raises 413 on breach) — unchanged, and
    # still BEFORE the body is read.
    enforce_storage_quota(db, tenant_id, upload.size or 0, user=user)

    cap = cap_bytes if cap_bytes is not None else per_file_cap_bytes(db, tenant_id)
    data, content_hash, size_bytes = _spool_upload(upload, cap)

    # ---- 30.6.12 c12b — scoped, purge-aware reuse (BEFORE any storage put) --
    # Identical bytes from the SAME principal (tenant + app, or tenant + user
    # when app_id is NULL) reuse the live row: no storage put, no new
    # Cog/Version/File/row, no new blob — the row just gets a fresh TTL.
    # ISO: nothing is deleted; the record row is never touched by purge either.
    _now_dt = datetime.now(timezone.utc)
    _ttl = timedelta(hours=ttl_hours(db))
    _prior = _reusable_row(db, tenant_id=tenant_id, content_hash=content_hash,
                           app_id=app_id, user_id=getattr(user, "id", None),
                           now=_now_dt, ttl=_ttl)
    if _prior is not None:
        _prior.expires_at = _now_dt + _ttl
        _audit(db, tenant_id=tenant_id, user_id=user.id, event_name=AUDIT_REUSED,
               details={
                   "id": _prior.id,
                   "content_hash": _prior.content_hash,
                   "app_id": _prior.app_id,
                   "expires_at": _prior.expires_at.isoformat() if _prior.expires_at
                   else None,
               })
        db.commit()
        db.refresh(_prior)
        return _response_for(_prior)

    if not name:
        name = getattr(upload, "filename", None) or "ephemeral_input"

    file_extension = ""
    if "." in name:
        file_extension = os.path.splitext(name)[1].lstrip(".").lower()

    ctype = getattr(upload, "content_type", None) or "application/octet-stream"

    existing_blob = get_content_blob_by_id_and_tenant(
        db, blob_id=content_hash, tenant_id=tenant_id)
    is_new_blob = existing_blob is None

    bucket = bucket or get_tenant_bucket_name(tenant_id, db)
    if is_new_blob:
        client = (client_factory or MinioClient)(tenant_id=tenant_id)
        try:
            client.ensure_bucket_exists()
            client.client.put_object(
                bucket_name=client.bucket_name,
                object_name=content_hash,
                data=io.BytesIO(data),
                length=size_bytes,
                content_type=ctype,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("ephemeral upload to storage failed: %s", e)
            raise EphemeralUploadFailedError(details={"content_hash": content_hash}) from e
        storage_scheme = os.getenv("STORAGE_SCHEME", "s3")
        existing_blob = models.ContentBlob(
            id=content_hash,
            tenant_id=tenant_id,
            size_bytes=size_bytes,
            mime_type=ctype,
            file_extension=file_extension or None,
            storage_path=f"{storage_scheme}://{client.bucket_name or bucket}/{content_hash}",
        )
        db.add(existing_blob)
        db.flush()

    cog = models.Cog(
        tenant_id=tenant_id,
        owner_id=user.id,
        name=name,
        cog_type=models.CogType.FILE,
        is_system_generated=True,
        is_hidden=True,
    )
    db.add(cog)
    db.flush()

    existing_blob.cog_id = cog.id

    version = models.Version(
        cog_id=cog.id,
        tenant_id=tenant_id,
        content_id=content_hash,
        version_number=1,
        name=name,
        author_user_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(version)
    db.flush()
    cog.latest_version_id = version.id

    db.add(models.File(
        tenant_id=tenant_id,
        user_id=user.id,
        cog_id=cog.id,
        name=name,
        size_bytes=size_bytes,
        mime_type=ctype,
        storage_path=existing_blob.storage_path,
    ))

    ttl = _ttl
    now = _now_dt
    row = models.AppEphemeralInput(
        tenant_id=tenant_id,
        app_id=app_id,  # c12b: App-key callers stamp the app; humans stay NULL
        owner_user_id=user.id,
        name=name,
        file_extension=file_extension or None,
        mime_type=ctype,
        size_bytes=size_bytes,
        content_hash=content_hash,
        storage_path=existing_blob.storage_path,
        cog_id=cog.id,
        expires_at=now + ttl,
        status="active",
    )
    db.add(row)
    db.flush()
    _audit(db, tenant_id=tenant_id, user_id=user.id, event_name=AUDIT_CREATED,
           details={
               "id": row.id,
               "name": row.name,
               "content_hash": row.content_hash,
               "size_bytes": row.size_bytes,
               "cog_id": row.cog_id,
               "app_id": row.app_id,
               "expires_at": row.expires_at.isoformat() if row.expires_at else None,
           })

    _egress = optional_paid("services.egress_metering")
    if _egress is not None:
        _egress.apply_storage_change(db, tenant_id,
                                     new_blob_bytes=size_bytes if is_new_blob else 0)

    db.commit()
    db.refresh(row)
    db.refresh(cog)

    return _response_for(row)


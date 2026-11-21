"""StorageConnection CRUD — per-tenant BYO-S3 connections (Gate 5, D6).

Replaces the dead `Drive` stub. The secret key is NEVER stored on the
`storage_connections` row: it is persisted through the established tenant-secret
pattern (`crud/secret_cud.py` → Fernet + per-tenant key + Vault dual-write) under
the Secret name held in `StorageConnection.secret_ref`. Only that non-secret
reference lives on the row, and no read path ever returns the secret.

Activation is gated on a passing connection test (`last_test_ok`); at most one
active connection per tenant. When none is active, callers fall back to the
house-default backend + `tenant-{id}-files` (see `utils/storage.resolve_backend`).
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

import models, schemas
from crud import secret_cud
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Fields whose change invalidates a prior passing test (re-gates activation).
_CREDENTIAL_FIELDS = {"endpoint", "region", "bucket", "access_key", "secure", "secret_key"}


def _secret_name_for(conn_id: int) -> str:
    """Deterministic Secret name that holds this connection's secret key."""
    return f"__storage_conn_{conn_id}_secret"


def _byo_holds_objects_or_false(db: Session, conn: models.StorageConnection) -> bool:
    """BYO-DEACTIVATE-BLOCK (REV-BYO-REVIEW-CHANGES 1+2): the shared "does this
    connection's own store still hold objects?" check, same rule as the
    deactivate route (routers/storage_connections.py).

    Fail OPEN on an unresolvable store (endpoint down, secret gone, Vault
    unreachable, listing denied): unknown is not empty, and a tenant must
    never be locked out of their own connection. FAIL CLOSED on
    ``utils.residency.ResidencyPinError`` — the one class the pin documents as
    must-not-swallow ("use the house default" would silently move data).
    FAIL CLOSED on ``utils.storage.ByoKeyCheckUnavailable`` (FIX-BYO-CUD-
    FAIL-CLOSED): the store WAS resolved and the per-key walk RAN, but a MOD
    key could not be confirmed gone, so the caller must refuse (409), never
    deactivate. Returns False for platform/house rows (MOD-owned, never
    orphaned).
    """
    from utils import residency
    from utils.storage import resolve_backend, ByoKeyCheckUnavailable
    from services import storage_migration as stmig

    try:
        cfg = resolve_backend(conn.tenant_id, db)
        if not cfg.is_byo:
            # Fell back to the house default: the tenant's own store is not
            # resolvable (Vault down / no secret / bad config) — unknown, so
            # fail open rather than block.
            raise ValueError("the tenant's own store is not resolvable")
        # BYO move-out owner default: after a VERIFIED move the guard is
        # MOD-key-scoped (foreign files informational); before any verified
        # move it is the whole-bucket check, byte-for-byte as before.
        return stmig.byo_deactivate_guard_holds(db, conn, cfg)
    except (residency.ResidencyPinError, ByoKeyCheckUnavailable):
        # pin = fail closed, never swallowed (utils/storage.py:445-449);
        # ByoKeyCheckUnavailable = the walk ran, keys unconfirmed: fail closed.
        raise
    except Exception as exc:  # noqa: BLE001 — fail OPEN on an unresolvable store
        logger.warning(
            "BYO store for connection %d (tenant %s) unresolvable: %s; "
            "treating as unknown, not empty (never lock a tenant out).",
            conn.id, conn.tenant_id, exc,
        )
        return False


def _refuse_byo_not_empty(conn: models.StorageConnection) -> None:
    """The ONE 409 vocabulary for "your own store still holds objects" —
    identical body to the deactivate route so every is_active-flipping path
    refuses alike."""
    raise HTTPException(
        status_code=409,
        detail={
            "error": "byo_storage_not_empty",
            "message": (
                "Your bring-your-own S3 storage still holds at least one "
                f"object in bucket '{conn.bucket}'. Deactivating now would "
                "move MOD back to its own storage and leave those objects "
                "unreachable. Move them into MOD storage with MOD's move "
                "tool, or delete them yourself, then deactivate."
            ),
        },
    )


def _refuse_byo_check_unavailable(conn: models.StorageConnection) -> None:
    """The ONE 409 vocabulary for "could not confirm your MOD keys are gone":
    identical body to the deactivate route's ByoKeyCheckUnavailable clause.
    Carries no endpoint, key or credential."""
    raise HTTPException(
        status_code=409,
        detail={
            "error": "byo_key_check_unavailable",
            "message": (
                "Could not confirm your files have left this storage "
                "(storage error); nothing was changed: try again in a "
                "moment."
            ),
        },
    ) from None


def get_connections_for_tenant(db: Session, tenant_id: int) -> List[models.StorageConnection]:
    return (
        db.query(models.StorageConnection)
        .filter(models.StorageConnection.tenant_id == tenant_id)
        .order_by(models.StorageConnection.created_at.desc())
        .all()
    )


def get_connection(db: Session, conn_id: int, tenant_id: int) -> Optional[models.StorageConnection]:
    return (
        db.query(models.StorageConnection)
        .filter(
            models.StorageConnection.id == conn_id,
            models.StorageConnection.tenant_id == tenant_id,
        )
        .first()
    )


def get_active_connection(db: Session, tenant_id: int) -> Optional[models.StorageConnection]:
    """The tenant's active BYO-S3 connection, if any. Tenant-scoped by construction."""
    return (
        db.query(models.StorageConnection)
        .filter(
            models.StorageConnection.tenant_id == tenant_id,
            models.StorageConnection.is_active.is_(True),
        )
        .first()
    )


# --- BYO move-out job rows (IMPL-BYO-MOVE-OUT-A-API, design s6) -------------
# storage_move_jobs rows are HISTORY: nothing here ever deletes or soft-
# deletes them (ISO 27001 no-hard-delete; the job record stays visible
# forever, which is why the table carries no deleted_at column).

def create_move_job(
    db: Session,
    conn: models.StorageConnection,
    *,
    target_bucket: str,
    total_objects: int,
    chunks_total: int,
) -> models.StorageMoveJob:
    """Insert the job row for a move-out (tenant/connection from the row, so
    every later lookup is tenant-scoped by construction)."""
    job = models.StorageMoveJob(
        tenant_id=conn.tenant_id,
        connection_id=conn.id,
        target_bucket=target_bucket,
        status="pending",
        total_objects=total_objects,
        chunks_total=chunks_total,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_move_job(
    db: Session,
    conn: models.StorageConnection,
    *,
    job_id: Optional[int] = None,
    status: Optional[str] = None,
    statuses: Optional[tuple] = None,
) -> Optional[models.StorageMoveJob]:
    """The tenant's move-out job for this connection (tenant_id filter on
    EVERY query — isolation). Default: the latest one. `status`/`statuses`
    narrow the search; `job_id` addresses one exact row."""
    filters = [
        models.StorageMoveJob.tenant_id == conn.tenant_id,
        models.StorageMoveJob.connection_id == conn.id,
    ]
    if job_id is not None:
        filters.append(models.StorageMoveJob.id == job_id)
    if status is not None:
        filters.append(models.StorageMoveJob.status == status)
    if statuses is not None:
        filters.append(models.StorageMoveJob.status.in_(statuses))
    return (
        db.query(models.StorageMoveJob)
        .filter(*filters)
        .order_by(models.StorageMoveJob.created_at.desc())
        .first()
    )


def get_connection_secret(db: Session, conn: models.StorageConnection) -> Optional[str]:
    """Decrypt the connection's secret key from Vault (fresh per call). Never cached."""
    if not conn.secret_ref:
        return None
    return secret_cud.get_decrypted_secret_by_name(
        db, secret_name=conn.secret_ref, tenant_id=conn.tenant_id
    )


def create_connection(
    db: Session,
    payload: schemas.StorageConnectionCreate,
    user: models.User,
) -> models.StorageConnection:
    """Create a connection. The secret is stored via the Secret/Vault pattern and
    the row keeps only `secret_ref`. Never auto-activates (a test must pass first)."""
    # GY.V3a — data-residency gate: refuse (audit row + ResidencyPinError)
    # BEFORE any DB mutation when the pair maps outside the platform pin
    # or to an unknown zone. Pin off = no-op, byte-identical behaviour.
    from utils import residency
    residency.enforce_for_create_update(
        db, user.tenant_id, user,
        region=payload.region, endpoint=payload.endpoint, name=payload.name,
    )
    # iso-soft-delete-sweep (2026-09-22): (tenant_id, name) is UNIQUE (plain
    # constraint), so a soft-deleted connection still owns its name. Re-adding a
    # connection with the name of a soft-deleted one reactivates the tombstone in
    # place instead of colliding on INSERT. (Query WITH tombstones — the
    # soft-delete listener hides them from the normal lookups.)
    existing = (
        db.query(models.StorageConnection)
        .execution_options(include_deleted=True)
        .filter(
            models.StorageConnection.tenant_id == user.tenant_id,
            models.StorageConnection.name == payload.name,
        )
        .first()
    )
    if existing is not None and existing.deleted_at is not None:
        existing.deleted_at = None
        existing.storage_type = models.StorageType.S3
        existing.endpoint = payload.endpoint
        existing.region = payload.region
        existing.bucket = payload.bucket
        existing.access_key = payload.access_key
        existing.secure = payload.secure
        existing.sts_enabled = payload.sts_enabled
        existing.is_active = False
        existing.last_test_ok = False
        existing.created_by_user_id = user.id
        secret_name = existing.secret_ref or _secret_name_for(existing.id)
        secret_cud.create_or_update_secret(
            db,
            secret_name=secret_name,
            value=payload.secret_key,
            user=user,
            description=f"BYO-S3 secret key for storage connection '{payload.name}'",
        )
        existing.secret_ref = secret_name
        db.commit()
        db.refresh(existing)
        logger.info(
            "StorageConnection %d reactivated for tenant %s (endpoint=%s bucket=%s)",
            existing.id, existing.tenant_id, existing.endpoint, existing.bucket,
        )
        return existing

    conn = models.StorageConnection(
        tenant_id=user.tenant_id,
        name=payload.name,
        storage_type=models.StorageType.S3,
        endpoint=payload.endpoint,
        region=payload.region,
        bucket=payload.bucket,
        access_key=payload.access_key,
        secure=payload.secure,
        sts_enabled=payload.sts_enabled,
        is_active=False,
        last_test_ok=False,
        created_by_user_id=user.id,
    )
    db.add(conn)
    db.flush()  # assign conn.id for the deterministic secret name

    secret_name = _secret_name_for(conn.id)
    secret_cud.create_or_update_secret(
        db,
        secret_name=secret_name,
        value=payload.secret_key,
        user=user,
        description=f"BYO-S3 secret key for storage connection '{payload.name}'",
    )
    conn.secret_ref = secret_name
    db.commit()
    db.refresh(conn)
    logger.info(
        "StorageConnection %d created for tenant %s (endpoint=%s bucket=%s)",
        conn.id, conn.tenant_id, conn.endpoint, conn.bucket,
    )
    return conn


def update_connection(
    db: Session,
    conn: models.StorageConnection,
    update: schemas.StorageConnectionUpdate,
    user: models.User,
) -> models.StorageConnection:
    """Update fields. Any credential/endpoint change re-arms the test gate
    (last_test_ok=False, is_active=False) so a stale pass can't keep a changed
    connection live."""
    data = update.model_dump(exclude_unset=True)
    # GY.V3a — data-residency gate on the EFFECTIVE (merged) region/endpoint:
    # an update that moves a connection outside the platform pin — or the
    # pin being set after the connection existed — is refused (audit row +
    # ResidencyPinError) before any field is touched. Pin off = no-op.
    from utils import residency
    residency.enforce_for_create_update(
        db, conn.tenant_id, user,
        region=data.get("region", conn.region),
        endpoint=data.get("endpoint", conn.endpoint),
        name=conn.name,
    )
    # BYO-DEACTIVATE-BLOCK (REV-BYO-REVIEW-CHANGES 2): a credential/endpoint
    # edit deactivates the ACTIVE connection below — same orphaning as
    # deactivate. Compute the predicate BEFORE any mutation and refuse with the
    # deactivate route's 409 while the store still holds objects (checked
    # against the OLD config, which is where the objects actually live).
    _credential_change_would_apply = (
        data.get("secret_key") is not None
        or any(
            getattr(conn, key) != value
            for key, value in data.items()
            # secret_key is not a row column (the row keeps only secret_ref).
            if key in _CREDENTIAL_FIELDS and key != "secret_key"
        )
    )
    if _credential_change_would_apply and conn.is_active:
        from utils.storage import ByoKeyCheckUnavailable
        try:
            _holds = _byo_holds_objects_or_false(db, conn)
        except ByoKeyCheckUnavailable as exc:
            # FIX-BYO-CUD-FAIL-CLOSED: the walk ran, keys unconfirmed: refuse
            # with the router's 409; nothing has been mutated yet.
            logger.warning(
                "update: BYO MOD-key check for connection %d (tenant %s) "
                "could not confirm its keys (%s); refusing (fail closed, "
                "nothing changed).", conn.id, conn.tenant_id, exc)
            _refuse_byo_check_unavailable(conn)
        if _holds:
            _refuse_byo_not_empty(conn)

    credential_changed = False

    new_secret = data.pop("secret_key", None)
    if new_secret is not None:
        secret_name = conn.secret_ref or _secret_name_for(conn.id)
        secret_cud.create_or_update_secret(
            db, secret_name=secret_name, value=new_secret, user=user,
            description=f"BYO-S3 secret key for storage connection '{conn.name}'",
        )
        conn.secret_ref = secret_name
        credential_changed = True

    for key, value in data.items():
        if getattr(conn, key) != value and key in _CREDENTIAL_FIELDS:
            credential_changed = True
        setattr(conn, key, value)

    if credential_changed:
        conn.is_active = False
        conn.last_test_ok = False
        conn.last_test_error = None
        conn.last_test_at = None

    db.commit()
    db.refresh(conn)
    return conn


def mark_test_result(
    db: Session, conn: models.StorageConnection, ok: bool, reason: str
) -> models.StorageConnection:
    conn.last_test_ok = ok
    conn.last_test_error = None if ok else reason
    conn.last_test_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conn)
    return conn


def activate_connection(db: Session, conn: models.StorageConnection) -> models.StorageConnection:
    """Make this the tenant's active connection. Caller MUST have verified
    `conn.last_test_ok` first (a connection cannot activate until a test passes)."""
    # Deactivate any other active connection for the tenant (single active).
    # BYO-DEACTIVATE-BLOCK (REV-BYO-REVIEW-CHANGES 2): activating B while A is
    # active used to orphan A's objects silently. Refuse with the deactivate
    # route's 409 while A's own store still provably holds objects (same
    # fail-open rule for an unresolvable store; ResidencyPinError propagates).
    # DOCUMENTED EXCEPTION: the migrate-on-activate completion hook
    # (services/storage_migration.py) lands here with
    # migration_status='verified' AND migrated_object_count>0 — the worker
    # copied AND per-object-checksum-verified every object from A's bucket
    # into B's before this call, so dropping A cannot orphan anything (the
    # copy is what the tenant reads; the house duplicate is guarded
    # separately by byo_house_copy_erasure). An under-reported completion
    # marks the migration 'failed' and never reaches activate_connection
    # (test_storage_connection_migration.py::test_completion_under_report_
    # marks_failed_not_active). The zero-object immediate activate stamps
    # 'verified' with migrated_object_count==0 — no copy happened, so the
    # check still runs there (test_byo_deactivate_block.py::
    # test_activate_second_connection_refused_while_first_holds_objects).
    _landing_of_verified_migration = (
        conn.migration_status == "verified"
        and (conn.migrated_object_count or 0) > 0
    )
    if not _landing_of_verified_migration:
        from utils.storage import ByoKeyCheckUnavailable
        for other in (
            db.query(models.StorageConnection)
            .filter(
                models.StorageConnection.tenant_id == conn.tenant_id,
                models.StorageConnection.id != conn.id,
                models.StorageConnection.is_active.is_(True),
            )
            .all()
        ):
            try:
                _other_holds = _byo_holds_objects_or_false(db, other)
            except ByoKeyCheckUnavailable as exc:
                # FIX-BYO-CUD-FAIL-CLOSED: walk ran, keys unconfirmed: refuse
                # before the bulk deactivate below (nothing written).
                logger.warning(
                    "activate: BYO MOD-key check for connection %d (tenant "
                    "%s) could not confirm its keys (%s); refusing (fail "
                    "closed, nothing changed).",
                    other.id, other.tenant_id, exc)
                _refuse_byo_check_unavailable(other)
            if _other_holds:
                _refuse_byo_not_empty(other)
    (
        db.query(models.StorageConnection)
        .filter(
            models.StorageConnection.tenant_id == conn.tenant_id,
            models.StorageConnection.id != conn.id,
            models.StorageConnection.is_active.is_(True),
        )
        .update({models.StorageConnection.is_active: False})
    )
    conn.is_active = True
    db.commit()
    db.refresh(conn)
    logger.info("StorageConnection %d activated for tenant %s", conn.id, conn.tenant_id)
    return conn


def deactivate_connection(db: Session, conn: models.StorageConnection) -> models.StorageConnection:
    conn.is_active = False
    db.commit()
    db.refresh(conn)
    return conn


def delete_connection(db: Session, conn: models.StorageConnection) -> None:
    """Delete the connection and best-effort remove its Vault/DB secret."""
    if conn.secret_ref:
        try:
            db_secret = secret_cud.get_secret_by_name(
                db, secret_name=conn.secret_ref, tenant_id=conn.tenant_id
            )
            if db_secret:
                secret_cud.delete_secret(db, db_secret)
        except Exception as exc:  # noqa: BLE001 — never block delete on secret cleanup
            logger.warning(
                "StorageConnection %d: failed to delete secret '%s': %s",
                conn.id, conn.secret_ref, exc,
            )
    # iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    # iso-no-hard-delete). Soft-delete the connection (SoftDeleteMixin + in the
    # soft-delete listener) and flip is_active so it stops being selected.
    # `create_connection` reactivates the tombstone on re-create by name. The
    # Vault/DB secret is still best-effort removed above.
    conn.deleted_at = datetime.now(timezone.utc)
    conn.is_active = False
    db.commit()

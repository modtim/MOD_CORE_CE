"""Per-tenant BYO-S3 storage connections (remove-minio-versitygw Gate 5).

A tenant admin points MOD at an S3-compatible endpoint they control. The secret
key is stored via the Secret/Vault pattern and is NEVER returned by any read
endpoint. A connection cannot become active until a connection test passes.

Auth: tenant-admin capabilities (`TENANT:view_settings` to read,
`TENANT:manage_settings` to mutate); platform admins bypass.
"""
import logging
import os
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import schemas, models
from crud import storage_connection_cud as sc_crud
from dependencies import get_db, require_capability

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Storage Connections"])

_view = require_capability(models.ResourceType.TENANT, "view_settings")
_manage = require_capability(models.ResourceType.TENANT, "manage_settings")


def _byo_s3_activation_enabled() -> bool:
    """Server-side gate for making a BYO-S3 connection the tenant's live backend.

    mega-qa security — the Dashboard hid Option-2 (BYO-S3) activation behind a
    build-time gate, but that is a UI-only control: a crafted POST to
    /storage-connections/{id}/activate bypassed it entirely and could re-home a
    tenant's object storage onto an untrusted endpoint.

    Self-host / CE: always enabled. There is one tenant (the operator's own),
    the operator chooses the endpoint, and a licence-style gate has no place in
    the open core (owner decisions 10-01, WS4.Q17, 11-09). SaaS: unchanged —
    the env flag BYO_S3_DATA_PLANE_READY (default OFF) must be exactly "true"
    before any hosted tenant may re-home its storage."""
    from utils.entitlements import is_saas
    if not is_saas():
        return True
    return os.getenv("BYO_S3_DATA_PLANE_READY", "false").strip().lower() == "true"


def _require_byo_s3_entitlement(db: Session, current_user: models.User) -> None:
    """VC.SUS.AUTHZ2 — tenant-plan gate for BYO-S3 activation.

    Activation is a plan capability: `Cap.BYO_S3` is part of the baseline (free in
    every self-host edition, incl. CE) and is granted on SaaS only by a
    plan's capabilities.
    Without this check, a tenant whose plan doesn't include BYO-S3 could
    activate via a crafted POST even though the Dashboard hides the control —
    same UI-only-gate class as the data-plane flag above. Resolves through the
    SAME `get_entitlements(db, tenant)` call as every other Cap.* gate
    (routers/burst_connect.py `_require_burst`), auto-picking the SaaS-plan or
    self-host-license branch. A denial is
    402 like the other entitlement gates (require_entitlement / burst /
    gpu_pool)."""
    from utils.entitlements import get_entitlements, evaluate_feature, Cap
    tenant = getattr(current_user, "tenant", None)
    if not evaluate_feature(Cap.BYO_S3, get_entitlements(db, tenant)).allowed:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "byo_s3_required",
                "message": (
                    "Bring-your-own S3 storage is not available in this deployment. "
                    ""
                ),
            },
        )


def _require_conn(db: Session, conn_id: int, tenant_id: int) -> models.StorageConnection:
    conn = sc_crud.get_connection(db, conn_id=conn_id, tenant_id=tenant_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Storage connection not found")
    return conn


def _cors_port_ok(value: str) -> bool:
    """True for a decimal port in 1..65535 (same rule as main._cors_port_ok)."""
    return value.isascii() and value.isdigit() and 1 <= int(value) <= 65535


def _cors_host_ok(value: str) -> bool:
    """True for a bare hostname/IP: ASCII alnum plus `.-_` only."""
    return bool(value) and value.isascii() and all(
        c.isalnum() or c in ".-_" for c in value)


def _dashboard_origins() -> List[str]:
    """Browser origins that talk directly to a tenant's S3 endpoint via presigned
    URLs. A BYO endpoint MUST allow CORS from these origins or uploads/downloads
    fail in the browser.

    Same rules the CORS middleware in main.py uses (main imports the routers, so
    importing it back here would be circular): a BASE_DOMAIN that ALREADY carries
    the published port (the CE single-host compose: `203.0.113.67:3000`) has NO
    subdomains - the Dashboard origin is that exact `scheme://host:port` and the
    WorkflowBuilder lives on its OWN published host port (`WORKFLOW_BUILDER_PORT`,
    default 3001) on the same host, never a subdomain. A bare domain keeps the
    `proto://sub.base` forms. An explicitly EMPTY subdomain env means "no
    subdomain" (the `dashboard`/`workflows` defaults apply only when UNSET), so
    the advice never contains a stray dot (`http://.203.0.113.67:3000`) or a
    duplicate; a non-numeric/out-of-range port is skipped, never emitted.
    """
    proto = (os.getenv("PROTOCOL", "https") or "").strip()
    base = (os.getenv("BASE_DOMAIN", "localhost") or "").strip()
    dash = (os.getenv("DASHBOARD_SUBDOMAIN", "dashboard") or "").strip()
    wf = (os.getenv("WORKFLOWS_SUBDOMAIN", "workflows") or "").strip()
    out: List[str] = []

    def _add(origin: str) -> None:
        if origin and origin not in out:
            out.append(origin)

    host, _, port = base.partition(":")
    if port:
        # Mirrors main._cors_origins() port branch exactly (main is the live
        # CORS source; importing it here would be circular). No subdomain form
        # on a port base: the dashboard is the bare host:port origin, and the
        # WB is host:WORKFLOW_BUILDER_PORT only when WORKFLOWS_SUBDOMAIN is empty.
        if not _cors_host_ok(host) or not _cors_port_ok(port):
            return out
        p_proto = (os.getenv("PROTOCOL", "http") or "").strip()
        p_scheme = "https" if p_proto == "https" else "http"
        _add(f"{p_scheme}://{host}:{port}")
        if not wf:
            wb_port = (os.getenv("WORKFLOW_BUILDER_PORT", "3001") or "").strip()
            if _cors_port_ok(wb_port):
                _add(f"{p_scheme}://{host}:{wb_port}")
        return out
    if not base:
        return out
    if dash:
        _add(f"{proto}://{dash}.{base}")
    else:
        _add(f"{proto}://{base}")
    if wf:
        _add(f"{proto}://{wf}.{base}")
    else:
        _add(f"{proto}://{base}")
    return out


@router.get("/cors-requirements")
def get_cors_requirements(
    current_user: models.User = Depends(_view),
):
    """The CORS rule a tenant must apply to their BYO-S3 bucket so the browser can
    upload/download directly. Surfaced in the UI (D7); if the browser is rejected
    the upload fails visibly rather than silently losing data."""
    origins = _dashboard_origins()
    return {
        "required": True,
        "allowed_origins": origins,
        "allowed_methods": ["GET", "PUT", "HEAD", "POST", "DELETE"],
        "allowed_headers": ["*"],
        "expose_headers": ["ETag"],
        "note": (
            "Apply this CORS rule on the BYO-S3 bucket. Without it, the browser's "
            "direct presigned upload/download is blocked and the Files tab shows a "
            "clear CORS error (no silent data loss)."
        ),
        "example_s3_cors_json": [
            {
                "AllowedOrigins": origins,
                "AllowedMethods": ["GET", "PUT", "HEAD", "POST", "DELETE"],
                "AllowedHeaders": ["*"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 3000,
            }
        ],
        "versitygw_house_default_hint": (
            "The house-default versitygw serves CORS via VGW_CORS_ALLOW_ORIGIN "
            "(--cors-allow-origin); set it to the Dashboard/Workflows origins."
        ),
    }


@router.post("/", response_model=schemas.StorageConnection,
             status_code=status.HTTP_201_CREATED)
def create_storage_connection(
    payload: schemas.StorageConnectionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_manage),
):
    """Register a BYO-S3 connection. The secret is encrypted (Fernet+Vault) and
    never echoed back. Does NOT activate — run a test first, then activate.

    GY.V3a: with the platform data-residency pin set, a region/endpoint that
    maps outside the pin (or to an unknown zone) is refused with 403 + an
    audit row; the connection is NOT created."""
    from utils import residency
    # FX.162.p5 — CE carve: no DLP output allowlist -> no restriction.
    from utils.paid_import import optional_paid
    _dlp = optional_paid("services.dlp_output_allowlist")
    if _dlp is not None:
        _dlp.check_destination(
            db, current_user.tenant_id, payload.endpoint,
            kind="storage_connection", user_id=current_user.id,
        )
    try:
        conn = sc_crud.create_connection(db, payload=payload, user=current_user)
    except residency.ResidencyPinError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "residency_pin_violation",
                "pin": exc.pin,
                "zone": exc.zone,
                "message": (
                    f"Region {exc.region!r} maps to residency zone "
                    f"{exc.zone!r}, outside the platform data-residency "
                    f"pin {exc.pin!r}. Storage connections must stay "
                    f"inside the pinned region."
                ),
            },
        ) from exc
    return conn


@router.get("/", response_model=List[schemas.StorageConnection])
def list_storage_connections(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_view),
):
    return sc_crud.get_connections_for_tenant(db, tenant_id=current_user.tenant_id)


@router.get("/{conn_id}", response_model=schemas.StorageConnection)
def get_storage_connection(
    conn_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_view),
):
    return _require_conn(db, conn_id, current_user.tenant_id)


@router.patch("/{conn_id}", response_model=schemas.StorageConnection)
def update_storage_connection(
    conn_id: int,
    update: schemas.StorageConnectionUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_manage),
):
    """Update a connection. Any credential/endpoint change re-arms the test gate
    (deactivates + clears the last passing test).

    GY.V3a: with the platform data-residency pin set, an update whose
    EFFECTIVE region/endpoint maps outside the pin (or to an unknown zone)
    is refused with 403 + an audit row; no field is changed."""
    from utils import residency
    # FX.162.p5 — CE carve: no DLP output allowlist -> no restriction.
    from utils.paid_import import optional_paid
    _dlp = optional_paid("services.dlp_output_allowlist")
    conn = _require_conn(db, conn_id, current_user.tenant_id)
    effective_endpoint = update.endpoint if update.endpoint is not None else conn.endpoint
    if _dlp is not None:
        _dlp.check_destination(
            db, current_user.tenant_id, effective_endpoint,
            kind="storage_connection", user_id=current_user.id,
        )
    try:
        return sc_crud.update_connection(db, conn=conn, update=update,
                                         user=current_user)
    except residency.ResidencyPinError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "residency_pin_violation",
                "pin": exc.pin,
                "zone": exc.zone,
                "message": (
                    f"Region {exc.region!r} maps to residency zone "
                    f"{exc.zone!r}, outside the platform data-residency "
                    f"pin {exc.pin!r}. Storage connections must stay "
                    f"inside the pinned region."
                ),
            },
        ) from exc


@router.delete("/{conn_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_storage_connection(
    conn_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_manage),
):
    conn = _require_conn(db, conn_id, current_user.tenant_id)
    sc_crud.delete_connection(db, conn=conn)
    return


@router.post("/test-config", response_model=schemas.StorageConnectionTestResult)
def test_storage_connection_config(
    cfg: schemas.StorageConnectionTestConfig,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_manage),
):
    """Test an UNSAVED connection config (UI 'Test' before Save). Does not persist.
    GSEC R-066: the endpoint is checked against the SSRF guard before any
    Minio call (private/internal targets refused unless the on-prem setting
    ``storage.byo_allow_private_endpoints`` is on; metadata always blocked)."""
    from utils.storage_connection_test import test_connection, CORS_NOTE
    ok, reason = test_connection(
        endpoint=cfg.endpoint, region=cfg.region, bucket=cfg.bucket,
        access_key=cfg.access_key, secret_key=cfg.secret_key, secure=cfg.secure,
        db=db,
    )
    return schemas.StorageConnectionTestResult(ok=ok, reason=reason, cors_note=CORS_NOTE)


@router.get("/{conn_id}/migration-status",
            response_model=schemas.StorageConnectionMigrationStatus)
def get_storage_connection_migration_status(
    conn_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_view),
):
    """WS4.Q17.17 — read-only progress for a connection's migrate-on-activate
    copy job: the migration_* bookkeeping plus ``total_objects`` (the size of
    the enqueued copy manifest) so the Dashboard can render objects/bytes
    progress and poll until ``verified``/``failed``. Tenant-scoped like every
    other read here — a foreign tenant's id is a 404."""
    from services import storage_migration as stmig
    conn = _require_conn(db, conn_id, current_user.tenant_id)
    return stmig.migration_status_for_connection(db, conn)


@router.post("/{conn_id}/test", response_model=schemas.StorageConnectionTestResult)
def test_storage_connection(
    conn_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_manage),
):
    """Test a SAVED connection using its stored (Vault) secret. On pass, the
    connection becomes eligible to activate; on fail it cannot activate."""
    from utils.storage_connection_test import test_connection, CORS_NOTE
    conn = _require_conn(db, conn_id, current_user.tenant_id)
    secret = sc_crud.get_connection_secret(db, conn)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connection has no stored secret; update it with a secret key first.",
        )
    ok, reason = test_connection(
        endpoint=conn.endpoint, region=conn.region, bucket=conn.bucket,
        access_key=conn.access_key, secret_key=secret, secure=conn.secure,
        db=db,
    )
    sc_crud.mark_test_result(db, conn=conn, ok=ok, reason=reason)
    return schemas.StorageConnectionTestResult(ok=ok, reason=reason, cors_note=CORS_NOTE)


@router.post("/{conn_id}/activate", response_model=schemas.StorageConnection)
def activate_storage_connection(
    conn_id: int,
    skip_missing_source: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_manage),
):
    """Activate a connection as the tenant's backend. Requires a passing
    connection test. Self-host/CE: no plan or env gate. SaaS: the tenant's
    plan must include BYO-S3 (Cap.BYO_S3) and the operator flag
    BYO_S3_DATA_PLANE_READY=true must be set.

    FIX-BYO-MIGRATION-SKIP-MISSING — optional query parameter
    ``skip_missing_source`` (default false, so every existing client is
    unchanged): when true, a source object the OLD backend no longer holds
    (a dangling content_blobs row — PROOF-BOX-J-2 F1 left 12 of 15) is
    skipped instead of aborting the whole migration, and every skipped key
    is counted and listed in the migration-status response. Default false
    keeps the fail-closed behaviour: silent skipping would hide data loss."""
    conn = _require_conn(db, conn_id, current_user.tenant_id)
    _require_byo_s3_entitlement(db, current_user)
    # Server-side BYO-S3 gate (WS4.Q18): SaaS-only (see _byo_s3_activation_enabled).
    # It runs before any activation logic: when the gate is closed, the refusal
    # is the same explainer no matter what the connection's test state is.
    # Creating and testing a connection stay allowed; activation does not.
    if not _byo_s3_activation_enabled():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "byo_storage_not_available",
                "message": (
                    "Bring-your-own S3 storage isn't enabled on this installation "
                    "yet. Your files stay on MOD storage; contact support to "
                    "enable it."
                ),
            },
        )
    if not conn.last_test_ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connection cannot be activated until a connection test passes.",
        )
    # WS4.Q17.16 — a tenant with existing objects in their current backend
    # (house bucket or a prior BYO connection) cannot flip straight to this
    # connection: the objects have to be copied over first. Route through
    # the migration service, which enqueues a Go-worker copy+verify task and
    # leaves is_active False until the worker's completion hook verifies
    # success. Zero objects (new tenant, or nothing uploaded yet) activates
    # immediately with migration_status='verified' — same as before this
    # task, just with the bookkeeping fields stamped.
    from services import storage_migration as stmig
    return stmig.activate_with_migration(db, conn, skip_missing_source=skip_missing_source)


@router.post("/{conn_id}/deactivate", response_model=schemas.StorageConnection)
def deactivate_storage_connection(
    conn_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_manage),
):
    """Deactivate a connection; the tenant falls back to the house default.

    BYO-DEACTIVATE-BLOCK (data safety, not a cap or a licence gate): an ACTIVE
    BYO connection that still holds objects cannot be deactivated — falling back
    to the house default would orphan (hide) everything the tenant owns in their
    own store, with no MOD-side path back to it. The objects must be moved out
    or deleted first. An empty BYO connection deactivates as before, and
    platform/house storage is never inspected here (it is MOD-owned, and
    deactivating a BYO row never orphans it).

    An unresolvable store (endpoint down, secret gone, Vault unreachable) is
    NOT blocked: same "never fail closed on a config lookup" convention as
    utils.storage.resolve_backend, so a tenant whose endpoint is temporarily
    down is never locked out of their own connection. The refusal fires only
    on a POSITIVE object hit — the case where orphaning is proven.
    """
    conn = _require_conn(db, conn_id, current_user.tenant_id)
    if not conn.is_active:
        # Already inactive: nothing to orphan, and no network call.
        return sc_crud.deactivate_connection(db, conn=conn)

    from utils.storage import resolve_backend
    # FIX-BYO-MOD-KEYS-PRESENT-FAIL-CLOSED: the "walk ran, keys unconfirmed"
    # class (raised ONLY by utils.storage.byo_mod_keys_present) — handled
    # CLOSED below, never by the fail-open catch-all.
    from utils.storage import ByoKeyCheckUnavailable
    from utils import residency
    from services import storage_migration as stmig

    try:
        # The ONE resolution path (utils.storage.resolve_backend): the tenant's
        # active BYO config, secret decrypted fresh from Vault, residency pin
        # honoured. It never raises for resolution failures — it falls back to
        # the house default — so `is_byo` False below means "their own store
        # could not be resolved", which is unknown, not empty.
        cfg = resolve_backend(conn.tenant_id, db)
        if not cfg.is_byo:
            raise ValueError(
                "the tenant's own store is not resolvable (fell back to MOD "
                "storage: Vault down, no secret, or bad connection config)")
        holds = stmig.byo_deactivate_guard_holds(db, conn, cfg)
    except residency.ResidencyPinError as exc:
        # CLOSED (fail closed, propagate): the data-residency pin is the ONE
        # class utils/storage.py documents as must-not-swallow — falling back
        # to the house default here would silently move data out of the pinned
        # region, so a pinned tenant cannot deactivate a store that still
        # holds objects. Rendered as 403 like the create/PATCH pin refusals.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "residency_pin_violation",
                "pin": exc.pin,
                "zone": exc.zone,
                "message": (
                    f"Region {exc.region!r} maps to residency zone "
                    f"{exc.zone!r}, outside the platform data-residency "
                    f"pin {exc.pin!r}. Deactivating a pinned tenant's "
                    "BYO store would move MOD back to its own storage; "
                    "move the objects out or delete them first."
                ),
            },
        )
    except ByoKeyCheckUnavailable as exc:
        # CLOSED (FIX-BYO-MOD-KEYS-PRESENT-FAIL-CLOSED, data safety): the
        # store WAS resolved and the per-key walk RAN, but a MOD key could
        # not be confirmed as gone (transport error, timeout, 5xx,
        # AccessDenied, ...). "Unknown" is not "moved": clearing the guard
        # here would deactivate the connection while a MOD object may still
        # sit at the BYO bucket (the data-loss path the review found).
        # Nothing is touched; the user retries. The wrapper carries no
        # endpoint, key or credential; the original error is only the
        # __cause__ and is never rendered.
        logger.warning(
            "deactivate: BYO MOD-key check for connection %d (tenant %s) "
            "could not confirm its keys (%s); refusing (fail closed, "
            "nothing changed).",
            conn.id, conn.tenant_id, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "byo_key_check_unavailable",
                "message": (
                    "Could not confirm your files have left this storage "
                    "(storage error); nothing was changed: try again in a "
                    "moment."
                ),
            },
        ) from None
    except Exception as exc:  # noqa: BLE001 — fail OPEN on an unresolvable store
        # OPEN (documented trade): resolution/transport classes only — minio
        # S3Error/ServerError/InvalidResponseError (endpoint down, bucket gone,
        # listing permission-denied), requests/urllib3 transport errors, Vault
        # + DB errors, and the ValueError above. Unknown is not empty, and a
        # tenant whose endpoint is temporarily down must never be locked out of
        # their own connection; the refusal fires only on a POSITIVE object hit.
        # FIX-BYO-MOD-KEYS-PRESENT-FAIL-CLOSED: the "walk ran and errored"
        # class is handled CLOSED above, so it can never clear the guard here.
        logger.warning(
            "deactivate: BYO store for connection %d (tenant %s) unresolvable: %s; "
            "proceeding (never lock a tenant out of their own connection).",
            conn.id, conn.tenant_id, exc)
        holds = False

    if holds:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
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
    return sc_crud.deactivate_connection(db, conn=conn)


# =============================================================================
# BYO move-out (IMPL-BYO-MOVE-OUT-A-API, design results-1110/DESIGN-BYO-
# MOVE-OUT-FLOW.md s3). Data safety, not a cap and not a licence gate: the
# move copies the tenant's own objects INTO MOD storage (or another
# connection they own), keeps the source ACTIVE, and never deletes anything
# (soft delete only). No BYO_S3 activation gate here: it moves data OUT of
# a store the tenant already has; the SaaS activation gate stays only on
# /activate (the existing decision, unchanged).
# =============================================================================

@router.post("/{conn_id}/move-out",
             response_model=schemas.StorageConnectionMoveOutResult,
             status_code=status.HTTP_202_ACCEPTED)
def start_storage_connection_move_out(
    conn_id: int,
    request: schemas.StorageConnectionMoveOutRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_manage),
):
    """Move the tenant's own objects out of their BYO bucket into MOD
    storage (default) or another connection they own, so the connection
    can later be deactivated without orphaning anything.

    The 409 ``byo_storage_not_empty`` stays the answer of a plain
    deactivate (unchanged refusal, one new sentence pointing here).
    Refusals: 404 unknown/foreign id (row resolved FIRST, zero storage
    work), 409 ``move_out_target_unsupported`` (both targets given /
    unknown target), 409 ``connection_test_required`` (target conn
    lacks a passing test — same sentence /activate uses), 409
    ``move_out_already_running`` (a move chunk is still in flight),
    403 ``residency_pin_violation`` (the pin propagates, never
    swallowed — same body as deactivate).
    """
    from services import storage_migration as stmig
    from utils import residency

    conn = _require_conn(db, conn_id, current_user.tenant_id)

    # "Both targets given" means the caller EXPLICITLY sent both fields —
    # `target` defaults to "mod_storage", so a body carrying only
    # target_connection_id is a valid conn-to-conn move (design s3).
    both = request.target_connection_id is not None
    target_given = "target" in request.model_fields_set
    target_mod = not target_given or request.target == "mod_storage"
    if (both and target_given) or (not both and not target_mod):
        # Both given (mod_storage + a connection id) or an unknown target
        # name: one refusal vocabulary, per the design's table.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "move_out_target_unsupported",
                "message": (
                    "Pick exactly one move-out target: MOD storage "
                    "(\"mod_storage\") or one of your own connections via "
                    "target_connection_id."
                ),
            },
        )

    dest_connection = None
    if both:
        # The TARGET gets the same ownership resolution as the source: a
        # foreign id is a 404 with zero storage work.
        dest_connection = _require_conn(
            db, request.target_connection_id, current_user.tenant_id)
        if dest_connection.id == conn.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "move_out_target_unsupported",
                    "message": "The move-out target must be a different "
                               "connection than the one being emptied.",
                },
            )
        if not dest_connection.last_test_ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "connection_test_required",
                    "message": (
                        "Connection cannot be used as a move-out target until "
                        "a connection test passes."
                    ),
                },
            )

    if stmig.move_out_already_running(db, conn):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "move_out_already_running",
                "message": (
                    "A move-out for this connection is already in flight. "
                    "Watch it with move-status, or cancel it first."
                ),
            },
        )

    target_bucket = (
        dest_connection.bucket if dest_connection is not None
        else stmig._house_bucket(conn.tenant_id)
    )
    try:
        job, first_task = stmig.start_move_out(
            db, conn, target_bucket=target_bucket,
            dest_connection_id=(dest_connection.id
                                if dest_connection is not None else None),
        )
    except residency.ResidencyPinError as exc:
        # CLOSED (fail closed, propagate): same body as the deactivate pin
        # refusal — a move-out that would cross the pin is refused.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "residency_pin_violation",
                "pin": exc.pin,
                "zone": exc.zone,
                "message": (
                    f"Region {exc.region!r} maps to residency zone "
                    f"{exc.zone!r}, outside the platform data-residency "
                    f"pin {exc.pin!r}. Moving a pinned tenant's objects "
                    "out of their own store would cross the pin."
                ),
            },
        )
    except ValueError:
        # The source is not a BYO row / its own store is not resolvable:
        # the design reuses the deactivate refusal VERBATIM — the 409
        # vocabulary is shared through crud.storage_connection_cud.
        sc_crud._refuse_byo_not_empty(conn)
    return {
        "connection_id": conn.id,
        "status": job.status,
        "total_objects": int(job.total_objects or 0),
        "task_id": first_task.id if first_task is not None else None,
    }


@router.get("/{conn_id}/move-status",
            response_model=schemas.StorageConnectionMoveStatus)
def get_storage_connection_move_status(
    conn_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_view),
):
    """Read-only progress for a connection's BYO move-out (the storage_move_jobs
    row plus the skip report across its chunk tasks). Tenant-scoped like every
    other read here — a foreign tenant's id is a 404. ``move_status`` is
    'none' when no move was ever started."""
    from services import storage_migration as stmig
    conn = _require_conn(db, conn_id, current_user.tenant_id)
    return stmig.move_status_for_connection(db, conn)


@router.post("/{conn_id}/move-out/cancel", response_model=schemas.StorageConnection)
def cancel_storage_connection_move_out(
    conn_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_manage),
):
    """Stop an in-flight move-out. The job row goes to 'cancelled' (history —
    never deleted, soft delete only) and every still-pending chunk task is
    cancelled; a chunk already claimed by a worker finishes its copy, which
    is harmless (the destination is MOD-owned and content-addressed). The
    connection itself stays exactly as it was (still ACTIVE, serving
    backend unchanged). 409 ``move_out_not_running`` when nothing is."""
    from services import storage_migration as stmig
    conn = _require_conn(db, conn_id, current_user.tenant_id)
    job = stmig.cancel_move_out(db, conn)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "move_out_not_running",
                "message": "No move-out is in flight for this connection.",
            },
        )
    return conn

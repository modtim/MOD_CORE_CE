"""Tenant calibration-fixture migration state (GC.PFM.7a/7b/14).

Pure-DB soft state on `tenants.fixture_status / fixture_version /
fixture_checked_at` (migration 20260925_add_tenant_fixture_status).
No deletes anywhere (ISO 27001 soft-state rule).

State machine: pending -> ready | failed (via record_result from the
migrator callback), any -> pending (mark_pending), ready -> pending
only (mark_drift — alert-only drift, never touches non-ready rows).
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, List, Optional

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models import Tenant

logger = logging.getLogger(__name__)

# ============================================================================
# Prometheus metrics (PFM.14) — registered at module level with the same
# duplicate-registration guard services/wizard_metrics.py uses (re-import or
# a second REGISTRY scrape re-registration raises ValueError).
# ============================================================================
FIXTURE_MIGRATIONS_TOTAL = None
FIXTURE_DRIFT_TENANTS = None
try:
    from prometheus_client import Counter, Gauge
    try:
        FIXTURE_MIGRATIONS_TOTAL = Counter(
            "mod_fixture_migrations_total",
            "Calibration fixture migration results per tenant.",
            ["result"],
        )
    except ValueError:
        logger.debug("fixture_service: metric already registered: mod_fixture_migrations_total")
    try:
        FIXTURE_DRIFT_TENANTS = Gauge(
            "mod_fixture_drift_tenants",
            "Tenants currently marked drifted by the fixture drift check.",
        )
    except ValueError:
        logger.debug("fixture_service: metric already registered: mod_fixture_drift_tenants")
except Exception:
    logger.debug("fixture_service: prometheus_client unavailable; metrics disabled")


def _bump_metric(result: str) -> None:
    if FIXTURE_MIGRATIONS_TOTAL is not None:
        FIXTURE_MIGRATIONS_TOTAL.labels(result=result).inc()


def _get_tenant(db: Session, tenant_id: int) -> Optional[Tenant]:
    return db.query(Tenant).filter(Tenant.id == tenant_id).first()


def list_all_tenants_ordered(db: Session) -> List[Tenant]:
    """Every tenant, ordered by id ascending. Verbatim relocation of the
    query in `admin_fixtures.list_fixture_status` (PFM.8a)."""
    return db.query(Tenant).order_by(Tenant.id.asc()).all()


def record_result(db: Session, tenant_id: int, ok: bool, version: Optional[str]) -> bool:
    """PFM.7a — record a migrator callback result for a tenant.

    Sets fixture_status to ready/failed plus fixture_version and
    fixture_checked_at. Soft update only — no deletes. Returns True if a
    tenant row was updated, False if the tenant does not exist.
    """
    tenant = _get_tenant(db, tenant_id)
    if tenant is None:
        logger.warning("fixture_service.record_result: no tenant %s", tenant_id)
        _bump_metric("failure")
        return False
    tenant.fixture_status = "ready" if ok else "failed"
    if version is not None:
        tenant.fixture_version = str(version)[:32]
    tenant.fixture_checked_at = datetime.now(timezone.utc)
    db.commit()
    _bump_metric("success" if ok else "failure")
    return True


def is_pending(db: Session, tenant_id: int) -> bool:
    """FIX-FIXTURE-GATE-2 — True only while a migration is queued for the
    tenant (fixture_status == 'pending', the marker `dispatch_migration`
    sets). The migrator callback accepts a result only in this state."""
    tenant = _get_tenant(db, tenant_id)
    return tenant is not None and tenant.fixture_status == "pending"


def list_tenant_exists(db: Session, tenant_id: int) -> bool:
    """FIX-FIXTURE-GATE-2 — True when a tenant row exists (lets the callback
    answer 404 for an unknown tenant and 409 for a known non-pending one)."""
    return _get_tenant(db, tenant_id) is not None


def fixtures_ready(db: Session, tenant_id: int) -> bool:
    """PFM.7a — True only when the tenant's fixtures are marked ready."""
    tenant = _get_tenant(db, tenant_id)
    return tenant is not None and tenant.fixture_status == "ready"


def mark_pending(db: Session, tenant_id: int) -> bool:
    """PFM.7a — reset a tenant to the pending state (soft update only)."""
    tenant = _get_tenant(db, tenant_id)
    if tenant is None:
        logger.warning("fixture_service.mark_pending: no tenant %s", tenant_id)
        return False
    tenant.fixture_status = "pending"
    db.commit()
    return True


def mark_drift(db: Session, tenant_ids: Iterable[int]) -> int:
    """PFM.7a — flip ONLY 'ready' tenants to pending (alert-only drift).

    Returns the number of rows actually flipped. Rows in any other state
    are left untouched; nothing is ever deleted.
    """
    ids = list(tenant_ids)
    if not ids:
        if FIXTURE_DRIFT_TENANTS is not None:
            FIXTURE_DRIFT_TENANTS.set(0)
        return 0
    rows = (
        db.query(Tenant)
        .filter(Tenant.id.in_(ids), Tenant.fixture_status == "ready")
        .all()
    )
    for tenant in rows:
        tenant.fixture_status = "pending"
    db.commit()
    if FIXTURE_DRIFT_TENANTS is not None:
        FIXTURE_DRIFT_TENANTS.set(len(rows))
    return len(rows)


# ============================================================================
# PFM.7b — dispatch the migrator container task
# ============================================================================

FIXTURE_MIGRATOR_IMAGE = "mod/fixture-migrator"


def _migrator_env(tenant_id: int, mode: str = "sync") -> dict:
    """Build the env for the fixture-migrator container.

    STORAGE_ENDPOINT points at the platform's own object storage and
    CALLBACK_URL at the API's fixture callback route. Credentials are NOT
    put here: this env is persisted in ApiTaskQueue.task_payload, so the
    service/root storage secret would sit in the queue table in plaintext.
    The migrator must get scoped per-task credentials instead (GC.PFM.7c).
    """
    endpoint = (
        os.getenv("STORAGE_ENDPOINT", "").strip()
        or os.getenv("VERSITYGW_INTERNAL_URL", "").strip()
        or os.getenv("S3_INTERNAL_URL") or os.getenv("MINIO_INTERNAL_URL", "http://versitygw:7070")
    )
    use_ssl = os.getenv("STORAGE_USE_SSL", "").strip()
    callback_url = os.getenv("CALLBACK_URL", "").strip() or os.getenv(
        "FIXTURE_CALLBACK_URL",
        "{}://api.{}{}/v1/admin/fixtures/callback".format(
            os.getenv("PROTOCOL", "https"),
            os.getenv("BASE_DOMAIN", "localhost"),
            os.getenv("API_PATH_PREFIX", ""),
        ),
    )
    env = {
        "STORAGE_ENDPOINT": endpoint,
        "CALLBACK_URL": callback_url,
        # PFM §2.2 — the migrator is mode-driven (sync | seed-empty | verify).
        "MODE": mode,
        # The callback route requires the tenant id in its body (PFM.3.4); the
        # container only knows it from here.
        "TENANT_ID": str(tenant_id),
    }
    if use_ssl:
        env["STORAGE_USE_SSL"] = use_ssl
    return env


def shared_bucket_enabled(db: Session = None) -> bool:
    """PFM.19 cutover flag — the same accessor the calibration gate, the
    create-tenant provisioning hook (users_router_service) and the daily
    drift audit (scheduler) use. On Community Edition the fixture-migrator
    image is private (never published to a public Harbor project), so the
    default-off flag is what keeps anything from dispatching it at all."""
    from services.platform_settings import get_setting

    try:
        return bool(get_setting("fixture_shared_bucket_enabled", db=db))
    except Exception as exc:
        # FIX-FIXTURE-GATE-2: a malformed flag value fails to the default
        # (off) instead of raising out of the route / gate / drift tick.
        logger.warning(
            "fixture_shared_bucket_enabled unreadable (%s); treating as off",
            type(exc).__name__,
        )
        return False


def dispatch_migration(db: Session, tenant_id: int, mode: str = "sync") -> Optional[int]:
    """PFM.7b — mark the tenant pending and enqueue the fixture-migrator
    container task. Returns the enqueued ApiTaskQueue row id, or `None` when
    the shared-bucket flag is off (the dispatch is skipped).

    Gated (FIX-FIXTURE-GATE, Gate 6 deviation D9) on the PFM.19 cutover
    flag: with `fixture_shared_bucket_enabled` off — the CE default — this
    is a no-op returning `None`, and nothing is marked pending and no task
    row is created. That is what stops the worker from re-pulling (and being
    denied) the private `mod/fixture-migrator` image on every heartbeat
    forever: the backfill migration and the provisioning hook both call this
    service, and a skipped dispatch leaves the tenant at its existing
    fixture_status, so the sweep neither errors nor retries. A skipped
    result is `None`; a dispatched task is always a positive row id, so
    callers can tell the two apart.

    `mode` is the PFM §2.2 migrator mode: provisioning + the weekly drift
    repair use `sync`; a fresh-tenant seed may pass `seed-empty` (never
    overwrites), an admin health check `verify` (read-only).
    """
    if not shared_bucket_enabled(db):
        logger.info(
            "fixture migration dispatch skipped for tenant %s: shared "
            "fixture bucket disabled (fixture_shared_bucket_enabled off)",
            tenant_id,
        )
        _bump_metric("skipped")
        return None
    mark_pending(db, tenant_id)
    # Lazy import: the helper lives in a heavy router _shared module and we
    # don't want it on this module's import path (PFM.7b spec).
    from routers.node_definitions_domains._shared import _enqueue_container_task

    return _enqueue_container_task(
        db,
        tenant_id,
        FIXTURE_MIGRATOR_IMAGE,
        _migrator_env(tenant_id, mode),
        node_kind_label="fixture_migrator",
    )

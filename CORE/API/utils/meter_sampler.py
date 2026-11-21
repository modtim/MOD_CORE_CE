"""sz-payg.9 (payg-billing-hardening §3.1) — hourly meter sampling.

Writes one `BillingMeterSample` row per (tenant, meter, sampled_at) for the
two **state** meters billed on peak-in-period (design.md §2): `storage` and
`concurrent`. Seat count is NOT sampled here — it's a point-in-time
subscription fact read directly off `Tenant`/`Plan` at invoice time, not
something hourly-sampled (see `models.BillingMeterSample` docstring).

Measurement sources — deliberately reused, not reinvented:

* storage: `Tenant.storage_used_bytes`, the running total already kept
  current by `services/storage_measure.py` (`measure_tenant_storage` /
  `recompute_all`). Converted to GB using the same 1024**3 (GiB) convention
  as `utils/storage_limits.py:storage_limit_bytes_from_plan` — the postpaid/
  overage price getters are per-GB-month (design.md line 11/14), so storing
  GB (not bytes) here keeps `monthly_billing_processor.py` a plain
  `MAX(value) * price_per_gb_month` read with no unit conversion at billing
  time.
* concurrent: a live COUNT of `WorkflowRun` rows for the tenant with
  `status == TaskStatus.RUNNING` (not soft-deleted). This is the same
  "running work" concept `services/burst_auto.py` and
  `services/accelerators_service.py` use elsewhere
  (`TaskStatus.RUNNING`/`TaskStatus.QUEUED` checks) — no new definition of
  "concurrent" is introduced.

Idempotency: `billing_meter_samples` has a UNIQUE(tenant_id, meter,
sampled_at) index (`ux_billing_meter_samples_tenant_meter_sampled_at`). Both
sample functions upsert via `INSERT ... ON CONFLICT ... DO NOTHING` against
that index, so a re-fired hourly job (restart/overlap at the same
`sampled_at`) never double-counts a tenant. Per the model docstring this
table is append-only in intent — a bad sample is superseded by a NEW row at
a later `sampled_at`, never UPDATEd — so DO NOTHING (not DO UPDATE) is
correct here.

`sample_all(db, sampled_at)` is the per-tick sweep sz-payg.10 (not built
here) will call: one `commit()` per tenant per meter so a single tenant's
failure cannot roll back or abort the rest of the batch (design/tasks.md
§3.2: "per-tenant error logged but does not abort the batch").
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

_BYTES_PER_GB = 1024 ** 3
_SAMPLE_INDEX_ELEMENTS = ["tenant_id", "meter", "sampled_at"]


def truncate_to_hour(ts: datetime) -> datetime:
    """Floor a timestamp to the top of its hour (naive-UTC and aware-UTC
    both supported). `sampled_at` is always stored truncated to the hour so
    the hourly sampler job and the UNIQUE(tenant_id, meter, sampled_at)
    index agree on one row per tenant per meter per hour."""
    return ts.replace(minute=0, second=0, microsecond=0)


def _upsert_sample(
    db: Session, tenant_id: int, meter: str, sampled_at: datetime, value: Decimal
) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING against the (tenant_id, meter,
    sampled_at) unique index. Returns True if a new row was written, False
    if an identical-key row already existed (idempotent re-run)."""
    stmt = pg_insert(models.BillingMeterSample).values(
        tenant_id=tenant_id,
        meter=meter,
        sampled_at=sampled_at,
        value=value,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=_SAMPLE_INDEX_ELEMENTS
    ).returning(models.BillingMeterSample.id)
    result = db.execute(stmt)
    db.commit()
    return result.first() is not None


def sample_storage(db: Session, tenant_id: int, sampled_at: datetime) -> Optional[Decimal]:
    """Sample the `storage` meter for one tenant at `sampled_at` (truncated
    to the hour). Reads the existing `Tenant.storage_used_bytes` total
    (maintained by `services/storage_measure.py`) rather than re-measuring
    object storage itself. Returns the GB value written, or None if the
    tenant doesn't exist or no longer exists (soft-deleted)."""
    tenant = (
        db.query(models.Tenant)
        .filter(models.Tenant.id == tenant_id, models.Tenant.deleted_at.is_(None))
        .first()
    )
    if tenant is None:
        return None

    used_bytes = int(tenant.storage_used_bytes or 0)
    gb = (Decimal(used_bytes) / Decimal(_BYTES_PER_GB)).quantize(Decimal("0.0001"))
    hour = truncate_to_hour(sampled_at)
    _upsert_sample(db, tenant_id, "storage", hour, gb)
    return gb


def sample_concurrent(db: Session, tenant_id: int, sampled_at: datetime) -> Optional[int]:
    """Sample the `concurrent` meter for one tenant at `sampled_at`
    (truncated to the hour): a live COUNT of that tenant's non-deleted
    `WorkflowRun` rows with `status == TaskStatus.RUNNING`. Returns the
    count written, or None if the tenant doesn't exist or is soft-deleted."""
    tenant = (
        db.query(models.Tenant)
        .filter(models.Tenant.id == tenant_id, models.Tenant.deleted_at.is_(None))
        .first()
    )
    if tenant is None:
        return None

    count = (
        db.query(models.WorkflowRun)
        .filter(
            models.WorkflowRun.tenant_id == tenant_id,
            models.WorkflowRun.status == models.TaskStatus.RUNNING,
            models.WorkflowRun.deleted_at.is_(None),
        )
        .count()
    )
    hour = truncate_to_hour(sampled_at)
    _upsert_sample(db, tenant_id, "concurrent", hour, Decimal(count))
    return count


_ABSENT_LOGGED = False


def paid_models_present() -> bool:
    """True when the paid billing model this sampler writes exists in the
    loaded models module (the paid tree). On CE it is absent and the sweep
    becomes a logged-once no-op. Keys on the model's presence only: no
    licence gate and no quantity cap."""
    return getattr(models, "BillingMeterSample", None) is not None


def sample_all(db: Session, sampled_at: Optional[datetime] = None) -> dict:
    """Sweep every non-deleted tenant, sampling both meters for each.
    Per-tenant failures are logged and skipped — they never abort the
    batch (tasks.md §3.2). Returns a summary dict of counts for callers
    (e.g. the sz-payg.10 scheduler job) to log. When the paid billing
    models are absent (CE) it returns zero counts with ``skipped`` set and
    touches neither the DB nor the tenant table."""
    global _ABSENT_LOGGED
    sampled_at = sampled_at or datetime.now(timezone.utc)
    hour = truncate_to_hour(sampled_at)
    if not paid_models_present():
        if not _ABSENT_LOGGED:
            _ABSENT_LOGGED = True
            logger.info(
                "meter_sampler: paid billing models absent (CE); sweep is a no-op"
            )
        return {
            "sampled_at": hour,
            "tenants_total": 0,
            "tenants_sampled": 0,
            "tenants_errored": 0,
            "skipped": "paid_models_absent",
        }

    tenant_ids = [
        row[0]
        for row in db.query(models.Tenant.id)
        .filter(models.Tenant.deleted_at.is_(None))
        .all()
    ]

    sampled = 0
    errored = 0
    for tenant_id in tenant_ids:
        try:
            sample_storage(db, tenant_id, hour)
            sample_concurrent(db, tenant_id, hour)
            sampled += 1
        except Exception:  # noqa: BLE001 - one tenant must not sink the sweep
            db.rollback()
            errored += 1
            logger.exception(
                "meter_sampler: sample_all failed for tenant_id=%s at %s",
                tenant_id, hour,
            )

    return {
        "sampled_at": hour,
        "tenants_total": len(tenant_ids),
        "tenants_sampled": sampled,
        "tenants_errored": errored,
    }

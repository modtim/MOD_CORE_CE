"""GH.9 — burst platform-fee pricing lookup.

Burst = MOD stands up ephemeral workers inside the customer's OWN cloud
account (decomp/gh-burst.md). The customer pays the cloud provider directly
for the compute; MOD earns a management fee in CREDITS, per managed
burst-GPU-hour, tiered by GPU class. The fee is debited from the customer's
prepaid balance — never floated (Group H "no float" rule).

The fee table lives on the singleton `BillingSettings` row in the same
pricing config the admin edits through GET/PUT /v1/pricing/config
(`doc["burst_fees"]`), as `burst_fee_credits_per_gpu_hour` (JSONB, keyed by
GPU class, e.g. {"default": 5, "t4": 2, "l4": 4, "a10g": 5, "a100": 15,
"h100": 25}). A NULL column (or a missing class in a partially-configured
table) falls back to `DEFAULT_BURST_FEE_CREDITS_PER_GPU_HOUR` below, so the
lookup is total: it never raises, never returns None.

Values are credits (not cents) — the same unit the prepaid ledger debits —
and may be fractional, hence float.
"""
from __future__ import annotations

from typing import Dict, Optional

from sqlalchemy.orm import Session

import models
from utils.paid_import import optional_paid

# CE carve: `utils/pricing.py` is DENIED (open-core/manifest.yaml:1550), so a
# direct import of it in this CE-KEPT module is a boot crash. Bind it through
# `optional_paid` and DEGRADE to this module's own documented fallback posture
# - NOT a 402: the two read paths are display/lookup and already degrade on the
# same condition (`_config` returns None on an unseeded install, and the module
# docstring says "A NULL column ... falls back to DEFAULT_BURST_FEE_CREDITS_PER_GPU_HOUR
# ... the lookup is total: it never raises, never returns None"). The write path
# (`set_burst_fee_rates`) is the admin primitive for the DENIED /v1/pricing/config
# PUT (open-core/manifest.yaml:1622 routers/pricing.py, :1623 services/pricing_service.py)
# and has no caller anywhere in CORE/API, so a 402 here would invent a status code
# on a surface CE does not have; it keeps its own existing failure shape
# (`raise ValueError` on a malformed fee, audit `except Exception: pass`).
_paid_pricing = optional_paid("utils.pricing")
if _paid_pricing is not None:
    from utils.pricing import get_pricing_config, invalidate_pricing_cache
else:
    def get_pricing_config(db=None, *args, **kwargs):  # CE: no pricing row
        return None  # -> `_config` returns None -> pure DEFAULT_ table

    def invalidate_pricing_cache(*args, **kwargs):  # CE: nothing cached
        return None

# Default credits per managed burst-GPU-hour by GPU class. `default` is the
# fallback for any class not listed (including unknown ones).
DEFAULT_BURST_FEE_CREDITS_PER_GPU_HOUR: Dict[str, float] = {
    "default": 5.0,
    "t4": 2.0,
    "a10g": 5.0,
    "l4": 4.0,
    "a100": 15.0,
    "h100": 25.0,
}


def _config(db: Session) -> Optional[models.BillingSettings]:
    """The singleton pricing row, or None when not seeded (fresh install)."""
    try:
        return get_pricing_config(db)
    except ValueError:
        return None


def get_burst_fee_rates(db: Session) -> Dict[str, float]:
    """The effective burst fee table: admin-configured entries (from the
    BillingSettings row) layered over the defaults, so an admin who only set
    `{"h100": 30}` still gets the default rates for every other class.

    Returns a flat {gpu_class: credits_per_hour} dict with at least
    `default`. This is the admin READ surface (the `burst_fees` field of the
    /v1/pricing/config document). Safe on an unseeded install (pure defaults).
    """
    rates = {k: float(v) for k, v in DEFAULT_BURST_FEE_CREDITS_PER_GPU_HOUR.items()}
    settings = _config(db)
    stored = (
        getattr(settings, "burst_fee_credits_per_gpu_hour", None)
        if settings is not None
        else None
    )
    if isinstance(stored, dict):
        for key, value in stored.items():
            try:
                rates[str(key).strip().lower()] = float(value)
            except (TypeError, ValueError):
                continue  # ignore a malformed entry rather than break the doc
    return rates


def burst_fee_credits_per_hour(db: Session, gpu_class: str) -> float:
    """Credits MOD charges per managed burst-GPU-hour for `gpu_class`.

    Case-insensitive; unknown/blank classes fall back to `default`. Never
    raises — the metering tick must not be able to crash on an exotic class
    name.
    """
    rates = get_burst_fee_rates(db)
    key = (gpu_class or "").strip().lower()
    value = rates.get(key)
    if value is None:
        value = rates.get("default")
    if value is None:  # unreachable (defaults always carry `default`)
        return float(DEFAULT_BURST_FEE_CREDITS_PER_GPU_HOUR["default"])
    return float(value)


def set_burst_fee_rates(
    db: Session,
    *,
    fees: Optional[Dict[str, float]] = None,
    tenant_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Dict[str, float]:
    """Admin WRITE surface — persist the burst fee table onto the singleton
    BillingSettings row (creating it if needed), invalidate the pricing
    cache so the very next metering tick sees it, and audit the change like
    the other rate editors. Only the classes present in `fees` are stored;
    omitted classes keep their stored/default values. Returns the effective
    merged table.

    Authorization is enforced by the calling router (platform admin, same as
    the rest of the /v1/pricing/config PUT); this is the pure persistence
    primitive.
    """
    settings = db.query(models.BillingSettings).order_by(models.BillingSettings.id).first()
    if settings is None:
        settings = models.BillingSettings()
        db.add(settings)

    before = dict(getattr(settings, "burst_fee_credits_per_gpu_hour", None) or {})
    if fees is not None:
        cleaned: Dict[str, float] = {}
        for key, value in fees.items():
            try:
                cleaned[str(key).strip().lower()] = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"invalid burst fee for class {key!r}: {value!r}")
        settings.burst_fee_credits_per_gpu_hour = cleaned
    after = dict(settings.burst_fee_credits_per_gpu_hour or {})

    try:
        db.add(models.AuditEvent(
            tenant_id=tenant_id, user_id=user_id,
            event_name="BURST_FEES_UPDATED",
            details={"before": before, "after": after}))
    except Exception:  # noqa: BLE001 — never block the settings write on audit
        pass

    db.commit()
    invalidate_pricing_cache()
    return after

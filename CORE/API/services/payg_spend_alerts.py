"""PAYG spend ALERTS (payg-billing-hardening §sz-payg.20) — the 15-minute sweep.

`utils/usage_alerts.py` grew `send_soft_warn_alert` (once per configured
threshold per billing period) and `send_hard_pause_alert` (once per period)
for postpaid tenants, but nothing ever called them: the thresholds live on
`BillingSettings` (`soft_warn_thresholds_cents`, `hard_pause_cents`, cached
by `utils/pricing.get_pricing_config` under the same field names) and the
accrued-period spend is exactly the number the overage cap / billing summary
already show — `utils/overage_cap.current_overage_cents(db, tenant)`. This
module is the missing caller: it walks every live POSTPAID tenant
(`Tenant.billing_mode == 'postpaid'`, the same column billing_summary and
the billing services switch on) and fires the emails as their spend crosses
each threshold.

EMAILS ONLY — this sweep NEVER pauses, blocks, suspends or charges anything.
Hard-pause ENFORCEMENT (actually stopping new runs) is a separate design
item; the alert here only tells the tenant's root admin that the spend
crossed the configured hard-pause line.

Kill switch: the DB-backed platform setting
`billing.payg_spend_alerts_enabled` (bool, default False, group "billing",
no env_fallback), registered at import exactly like
services/payg_auto_recharge.py does for its own sweep. Default OFF until the
owner verifies the email flow.

Dedupe is NOT re-implemented here: `utils.usage_alerts` keys its
`UsageAlertTracker` per threshold-per-period
(`payg_soft_warn_<cents>` / `payg_hard_pause`), so re-running the sweep
inside the same billing period sends nothing new. Recipient resolution is
imported from services/payg_auto_recharge (`_tenant_recipients`) rather
than duplicated — same ACTIVE root-admin lookup everywhere.

Failure posture: a per-tenant error (e.g. the spend lookup raising) is
logged and counted `errors`, and the sweep continues with the next tenant.
"""
from __future__ import annotations

import logging
from typing import Any

from services import platform_settings as psr
import models
try:
    from utils import overage_cap
    from utils import pricing
    from utils import usage_alerts
except ImportError:
    overage_cap = None
    pricing = None
    usage_alerts = None
from services.payg_auto_recharge import _tenant_recipients

logger = logging.getLogger(__name__)

SPEND_ALERTS_ENABLED_KEY = "billing.payg_spend_alerts_enabled"
DEFAULT_SPEND_ALERTS_ENABLED = False


def _validate_spend_alerts_enabled(value: bool) -> None:  # noqa: F704 — type gate only
    """Registry validator: the switch is strictly boolean."""
    if not isinstance(value, bool):
        raise ValueError(
            "billing.payg_spend_alerts_enabled must be a bool, "
            f"got {value!r}")


psr.register(
    SPEND_ALERTS_ENABLED_KEY,
    bool,
    DEFAULT_SPEND_ALERTS_ENABLED,
    validator=_validate_spend_alerts_enabled,
    group="billing",
    description="Master switch for the 15-minute PAYG spend-alerts sweep "
                "(services/payg_spend_alerts.sweep): live tenants on "
                "postpaid billing (Tenant.billing_mode == 'postpaid') get "
                "one soft-warn email per BillingSettings.soft_warn_thresholds_cents "
                "their accrued period spend (utils.overage_cap.current_overage_cents) "
                "has crossed, plus one hard-pause email once it reaches "
                "BillingSettings.hard_pause_cents. Emails ONLY — this sweep "
                "never pauses or blocks anything. Default False = OFF "
                "everywhere (kill switch) until the owner verifies the flow. "
                "No env fallback: the switch is DB-only.",
)


def sweep_enabled(db: Any = None) -> bool:
    """The kill switch, resolved per read (DB > default False).

    A settings-layer error answers OFF (fail closed): a broken settings
    store must never start emailing tenants. Only the exception CLASS name
    reaches the log — never the message body, which can carry DB/SQL detail.
    """
    try:
        return bool(psr.get_setting(SPEND_ALERTS_ENABLED_KEY, db=db))
    except Exception as exc:  # noqa: BLE001 — fail closed (sweep off)
        logger.warning(
            "payg_spend_alerts: %s unreadable (%s); sweep stays OFF",
            SPEND_ALERTS_ENABLED_KEY, type(exc).__name__)
        return False


def _thresholds(raw: Any) -> list[int]:
    """Sanitized, sorted soft-warn thresholds (ints > 0 only).

    `BillingSettings.soft_warn_thresholds_cents` is JSONB, so it can carry
    junk (strings, None, bools, negatives) written by hand; anything that is
    not a positive int is skipped rather than sent. bool is excluded
    deliberately (True would mean a 1-cent alert).
    """
    out: list[int] = []
    for value in (raw or []):
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value <= 0:
            continue
        out.append(value)
    return sorted(out)


def sweep(db, *, now=None) -> dict:
    """15-minute PAYG spend-alerts sweep.

    Returns `{"enabled", "tenants", "soft_warn_sent", "hard_pause_sent",
    "skipped", "errors"}`; while the kill switch is off it answers
    `{"enabled": False}` without touching the DB again. Candidates are live
    (`deleted_at IS NULL`) postpaid tenants; spend is
    `utils.overage_cap.current_overage_cents(db, tenant)` — the same number
    the cap status / billing summary show, never a re-derived one.

    For every threshold the spend has reached, `send_soft_warn_alert` is
    called with that `threshold_cents` and `balance_cents=spend`; a
    positive `hard_pause_cents` the spend reached adds one
    `send_hard_pause_alert`. Both dedupe themselves per billing period, so
    a second sweep in the same period sends nothing. A tenant with no
    resolvable root admin counts `skipped`; a tenant whose lookup raises
    counts `errors` and never aborts the sweep.
    """
    if not sweep_enabled(db):
        return {"enabled": False}
    if overage_cap is None or pricing is None:
        return {"enabled": False}

    summary = {"enabled": True, "tenants": 0, "soft_warn_sent": 0,
               "hard_pause_sent": 0, "skipped": 0, "errors": 0}

    config = pricing.get_pricing_config(db)
    thresholds = _thresholds(getattr(config, "soft_warn_thresholds_cents", None))
    hard_pause = getattr(config, "hard_pause_cents", None)
    hard_pause = hard_pause if (isinstance(hard_pause, int)
                                and not isinstance(hard_pause, bool)
                                and hard_pause > 0) else None

    tenants = db.query(models.Tenant).filter(
        models.Tenant.deleted_at.is_(None),
        models.Tenant.billing_mode == "postpaid",
    ).all()

    for tenant in tenants:
        summary["tenants"] += 1
        try:
            spend = overage_cap.current_overage_cents(db, tenant)
        except Exception as exc:  # noqa: BLE001 — one tenant never aborts the sweep
            summary["errors"] += 1
            logger.warning(
                "payg_spend_alerts: tenant %s spend lookup failed (%s)",
                tenant.id, type(exc).__name__)
            continue

        recipients = _tenant_recipients(db, tenant)
        if not recipients:
            summary["skipped"] += 1
            continue

        for threshold_cents in thresholds:
            if spend < threshold_cents:
                continue
            if usage_alerts.send_soft_warn_alert(
                    db, tenant_id=tenant.id, threshold_cents=threshold_cents,
                    balance_cents=spend, **recipients):
                summary["soft_warn_sent"] += 1

        if hard_pause and spend >= hard_pause:
            if usage_alerts.send_hard_pause_alert(
                    db, tenant_id=tenant.id, hard_pause_cents=hard_pause,
                    balance_cents=spend, **recipients):
                summary["hard_pause_sent"] += 1

    return summary

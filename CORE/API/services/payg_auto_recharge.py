"""PAYG 4.6 (payg-billing-hardening §5) — the 5-minute auto-recharge sweep.

A tenant that turned on PAYG auto-recharge (sz-payg.4 columns, written by
`PUT /billing/auto-recharge` in routers/billing.py — which already verified
the payment method belongs to THIS tenant's checkout customer) gets a
top-up whenever their prepaid balance drops below their threshold: this
sweep creates an off-session PaymentIntent on the saved card, which the payment processor
settles against the Customer and whose `payment_intent.succeeded` webhook
(task 332) credits `Tenant.prepaid_balance_cents` back.

THIS SWEEP NEVER CREDITS THE BALANCE ITSELF — crediting is the webhook's
job (332); the sweep only ever *spends*. Because the credit has not landed
yet when the next 5-minute tick fires, the PaymentIntent carries a
per-tenant idempotency key bucketed to 30 minutes
(`mod-autorecharge-{tenant_id}-{unix_time // 1800}`): every tick inside one
bucket replays the SAME processor idempotency request, so the processor returns the
original intent instead of charging the card twice before the webhook
lands. Two buckets = two genuinely different attempts.

Kill switch: the DB-backed platform setting
`billing.auto_recharge_sweep_enabled` (bool, default False, group "billing",
no env_fallback). Default OFF until the owner verifies the flow with a checkout
test card. Registration happens at import, same idiom as
services/drift_phase1_settings.py.

Concurrency: every read-then-spend of the prepaid balance takes the same
per-tenant `utils.prepaid_lock.prepaid_lock` as
`utils/monthly_billing_processor.charge_invoice` and
`services/prepaid_refund_service.py`, so a sweep charge and an invoice
charge/refund for one tenant never race on the same processor balance read.
`pg_advisory_xact_lock` is transaction-scoped, so the sweep commits the
same session inside each lock to release it.

Failure posture: a declined card (or any checkout error) for one tenant is
best-effort alerted via `utils.usage_alerts.send_auto_recharge_failure_alert`
and counted `failed`; it never aborts the rest of the sweep. The processor's own
key/egress gate (`utils.stripe_api._ensure_stripe_api_key`) raising is an
install-wide condition, not a per-tenant one, so it propagates to the
scheduler tick, which logs it. Nothing here logs a card/PM id beyond the
last 4 chars, and never the API key.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from services import platform_settings as psr  # leaf module: no audit cycle
import models
try:
    from utils.prepaid_lock import prepaid_lock
    from utils import stripe_api
    from utils import usage_alerts
except ImportError:
    prepaid_lock = None
    stripe_api = None
    usage_alerts = None

AUTO_RECHARGE_SWEEP_ENABLED_KEY = "billing.auto_recharge_sweep_enabled"
DEFAULT_AUTO_RECHARGE_SWEEP_ENABLED = False

#: Idempotency bucket width (seconds). One auto-recharge attempt per tenant
#: per bucket, so the 5-minute sweep cannot double-charge a card while the
#: webhook credit leg (task 332) is still in flight.
IDEMPOTENCY_BUCKET_SECONDS = 1800


def _validate_sweep_enabled(value: bool) -> None:  # noqa: F704 — type gate only
    """Registry validator: the switch is strictly boolean."""
    if not isinstance(value, bool):
        raise ValueError(
            "billing.auto_recharge_sweep_enabled must be a bool, "
            f"got {value!r}")


psr.register(
    AUTO_RECHARGE_SWEEP_ENABLED_KEY,
    bool,
    DEFAULT_AUTO_RECHARGE_SWEEP_ENABLED,
    validator=_validate_sweep_enabled,
    group="billing",
    description="PAYG 4.6 — master switch for the 5-minute auto-recharge "
                "sweep (services/payg_auto_recharge.sweep): tenants whose "
                "prepaid balance is under their auto_recharge_threshold_cents "
                "get an off-session PaymentIntent on their saved card, "
                "credited back by the payment_intent.succeeded webhook. "
                "Default False = OFF everywhere (kill switch) until the "
                "owner verifies the flow with a checkout test card. No env "
                "fallback: the switch is DB-only.",
)


def sweep_enabled(db: Any = None) -> bool:
    """The kill switch, resolved per read (DB > default False).

    A settings-layer error answers OFF (fail closed): a broken settings
    store must never start charging cards. Only the exception CLASS name
    reaches the log — never the message body, which can carry DB/SQL detail.
    """
    try:
        return bool(psr.get_setting(AUTO_RECHARGE_SWEEP_ENABLED_KEY, db=db))
    except Exception as exc:  # noqa: BLE001 — fail closed (sweep off)
        logger.warning(
            "payg_auto_recharge: %s unreadable (%s); sweep stays OFF",
            AUTO_RECHARGE_SWEEP_ENABLED_KEY, type(exc).__name__)
        return False


def _default_stripe_create(customer_id: str, payment_method_id: str,
                           amount_cents: int, idempotency_key: str,
                           metadata: dict):
    """Thin wrapper around `stripe.PaymentIntent.create` (the injectable
    default for `sweep(stripe_create=...)`).

    Goes through `utils.stripe_api._ensure_stripe_api_key()` first, exactly
    like every other Stripe call site in this codebase, so the key is set
    and `stripe.default_http_client` is pinned to the bounded
    STRIPE_HTTP_TIMEOUT_SECONDS timeout (and sealed-mode egress is refused
    with a 503 rather than hanging). The idempotency key is passed as
    `idempotency_key=`, so the processor itself dedupes a replayed attempt.
    """
    stripe_api._ensure_stripe_api_key()
    return stripe_api.stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        customer=customer_id,
        payment_method=payment_method_id,
        off_session=True,   # the customer is not present
        confirm=True,       # attempt the charge immediately
        idempotency_key=idempotency_key,
        metadata=metadata,
    )


def _tenant_recipients(db, tenant) -> Optional[dict]:
    """Recipient context for the failure alert, or None if unresolvable.

    Same admin resolution as utils.usage_alerts.process_usage_alerts: the
    tenant's ACTIVE root admin.
    """
    admin = db.query(models.User).filter(
        models.User.tenant_id == tenant.id,
        models.User.parent_user_id.is_(None),
        models.User.status == models.UserStatus.ACTIVE,
    ).first()
    if not admin:
        return None
    return {
        "tenant_name": tenant.name or "",
        "recipient_email": admin.email,
        "recipient_name": (
            f"{admin.first_name or ''} {admin.last_name or ''}".strip()),
        "first_name": admin.first_name or admin.username or "there",
    }


def _idempotency_key(tenant_id: int, now_ts: float) -> str:
    """One attempt per tenant per 30-min bucket (see module docstring)."""
    return (f"mod-autorecharge-{tenant_id}-"
            f"{int(now_ts) // IDEMPOTENCY_BUCKET_SECONDS}")


def sweep(db, *, now=None, stripe_create=None) -> dict:
    """5-minute auto-recharge sweep (PAYG 4.6, §5).

    Returns `{"checked", "charged", "failed", "skipped"}`. No-ops (all
    zeros) while `billing.auto_recharge_sweep_enabled` is off. Candidates
    are live tenants with auto_recharge_enabled, a threshold/amount/payment
    method on file, amount > 0, a checkout customer (Subscription.stripe_customer_id)
    and a prepaid balance strictly below the threshold.

    Each candidate is charged under `prepaid_lock` with the 30-min-bucket
    idempotency key and the metadata contract task 332's webhook leg keys on:
    `{"purpose": "auto_recharge", "tenant_id": str, "amount_cents": str}`.
    The sweep NEVER credits the balance — the webhook does.
    """
    summary = {"checked": 0, "charged": 0, "failed": 0, "skipped": 0}
    if not sweep_enabled(db):
        return summary
    if prepaid_lock is None:
        return summary

    create = stripe_create or _default_stripe_create
    now_ts = (now.timestamp() if now is not None
              else datetime.now(timezone.utc).timestamp())

    tenants = db.query(models.Tenant).filter(
        models.Tenant.deleted_at.is_(None)).all()
    for tenant in tenants:
        sub = getattr(tenant, "subscription", None)
        customer_id = sub.stripe_customer_id if sub else None
        threshold = tenant.auto_recharge_threshold_cents
        amount = tenant.auto_recharge_amount_cents
        pm_id = tenant.auto_recharge_payment_method_id

        if not (tenant.auto_recharge_enabled):
            continue
        summary["checked"] += 1
        if not (threshold and amount and amount > 0 and pm_id and customer_id):
            summary["skipped"] += 1
            continue
        balance = tenant.prepaid_balance_cents or 0
        if balance >= threshold:
            summary["skipped"] += 1
            continue

        idem = _idempotency_key(tenant.id, now_ts)
        metadata = {"purpose": "auto_recharge",
                    "tenant_id": str(tenant.id),
                    "amount_cents": str(amount)}
        try:
            with prepaid_lock(db, tenant.id):
                create(customer_id=customer_id,
                       payment_method_id=pm_id,
                       amount_cents=amount,
                       idempotency_key=idem,
                       metadata=metadata)
                db.commit()
            summary["charged"] += 1
        except Exception as exc:  # noqa: BLE001 — one tenant never aborts the sweep
            summary["failed"] += 1
            logger.warning(
                "payg_auto_recharge: tenant %s charge failed (%s); PM ...%s",
                tenant.id, type(exc).__name__, (pm_id or "")[-4:])
            recipients = _tenant_recipients(db, tenant)
            if recipients:
                try:
                    usage_alerts.send_auto_recharge_failure_alert(
                        db, tenant_id=tenant.id,
                        tenant_name=recipients["tenant_name"],
                        amount_cents=amount,
                        failure_reason=getattr(exc, "user_message", None)
                        or getattr(exc, "code", None) or "",
                        recipient_email=recipients["recipient_email"],
                        recipient_name=recipients["recipient_name"],
                        first_name=recipients["first_name"])
                except Exception as alert_exc:  # noqa: BLE001 — best effort
                    logger.warning(
                        "payg_auto_recharge: failure alert skipped for tenant %s (%s)",
                        tenant.id, type(alert_exc).__name__)
    return summary

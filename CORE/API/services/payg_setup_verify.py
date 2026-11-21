"""PAYG 6.6 (payg-billing-hardening §7 / tasks 6.6) — SetupIntent -> $1 verification charge.

A new PAYG signup must prove its card can actually be *charged*, not merely
attached: when checkout reports `setup_intent.succeeded`, we run a 100-cent
off-session PaymentIntent on the customer's freshly saved card, confirm it,
and immediately refund it. Only a succeeded charge stamps
`Tenant.payg_setup_verified_at` (models.py ~443 — the one column this module
ever writes); the first workflow run is gated on that stamp elsewhere.

Kill switch: the DB-backed platform setting `billing.payg_setup_verify_enabled`
(bool, default False, group "billing", NO env fallback). Default OFF until the
owner verifies the flow with a checkout test card. A settings-layer error answers
OFF (fail closed): a broken settings store must never start charging cards.

Refund posture (deliberate): if the refund call fails AFTER a successful
charge we do NOT mark the tenant verified — we log an ERROR carrying only the
tenant id + PaymentIntent id (never card data) and return "failed". Ops refunds
that intent manually and the tenant re-verifies on the next SetupIntent; leaving
the stamp unset is the fail-closed choice, because an unmarked tenant is
re-verifiable while a wrongly marked one hides a live $1 on the card.

Nothing here logs a card number, a full PM id, or the API key — only the
exception CLASS name and the checkout's own `code`.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from services import platform_settings as psr  # leaf module: no audit cycle
import models
try:
    from utils import stripe_api
except ImportError:
    stripe_api = None

PAYG_SETUP_VERIFY_ENABLED_KEY = "billing.payg_setup_verify_enabled"
DEFAULT_PAYG_SETUP_VERIFY_ENABLED = False

#: The verification amount, in cents. Refunded immediately, so it never lands
#: on the tenant's prepaid balance.
VERIFY_AMOUNT_CENTS = 100

RESULT_SKIPPED_DISABLED = "skipped_disabled"
RESULT_ALREADY_VERIFIED = "already_verified"
RESULT_VERIFIED = "verified"
RESULT_FAILED = "failed"


def _validate_enabled(value: Any) -> None:  # noqa: F704 — type gate only
    """Registry validator: the switch is strictly boolean."""
    if not isinstance(value, bool):
        raise ValueError(
            "billing.payg_setup_verify_enabled must be a bool, got "
            f"{value!r}")


psr.register(
    PAYG_SETUP_VERIFY_ENABLED_KEY,
    bool,
    DEFAULT_PAYG_SETUP_VERIFY_ENABLED,
    validator=_validate_enabled,
    group="billing",
    description="PAYG 6.6 — master switch for the card verification "
                "charge (services/payg_setup_verify.verify_payment_method): a "
                "new PAYG signup gets a 100-cent off-session PaymentIntent on "
                "its freshly saved card, refunded immediately, and only a "
                "succeeded charge stamps Tenant.payg_setup_verified_at. "
                "Default False = OFF everywhere (kill switch) until the owner "
                "verifies the flow with a checkout test card. No env fallback: "
                "the switch is DB-only.",
)


def enabled(db: Any = None) -> bool:
    """The kill switch, resolved per read (DB > default False).

    A settings-layer error answers OFF (fail closed). Only the exception CLASS
    name reaches the log — never the message body, which can carry DB/SQL detail.
    """
    try:
        return bool(psr.get_setting(PAYG_SETUP_VERIFY_ENABLED_KEY, db=db))
    except Exception as exc:  # noqa: BLE001 — fail closed (verification off)
        logger.warning(
            "payg_setup_verify: %s unreadable (%s); verification stays OFF",
            PAYG_SETUP_VERIFY_ENABLED_KEY, type(exc).__name__)
        return False


def _intent_id(intent: Any) -> Optional[str]:
    """The PaymentIntent's checkout id, from an SDK object or a plain mapping."""
    if intent is None:
        return None
    value = getattr(intent, "id", None)
    if value is None and isinstance(intent, dict):
        value = intent.get("id")
    return str(value) if value else None


def _intent_status(intent: Any) -> Optional[str]:
    """The PaymentIntent's payment processor `status`, from an SDK object or a mapping."""
    if intent is None:
        return None
    value = getattr(intent, "status", None)
    if value is None and isinstance(intent, dict):
        value = intent.get("status")
    return str(value) if value else None


def _default_stripe_cancel(payment_intent_id: str):
    """Thin wrapper around the payment processor's PaymentIntent cancel (the injectable default).

    Only reached for the `requires_capture` edge case below: an authorized-but-
    uncaptured $1 hold left on the card. Not expected in the happy path since
    the verification PaymentIntent never sets `capture_method="manual"`.
    """
    stripe_api._ensure_stripe_api_key()
    return stripe_api.stripe.PaymentIntent.cancel(payment_intent_id)


def _default_stripe_create(customer_id: str, payment_method_id: str,
                           amount_cents: int, idempotency_key: str,
                           metadata: dict):
    """Thin wrapper around the payment processor's PaymentIntent create (the injectable default).

    Goes through `utils.stripe_api._ensure_stripe_api_key()` first, exactly
    like every other payment-processor call site in this codebase, so the key is set and
    the payment processor's default HTTP client is pinned to the bounded STRIPE_HTTP_TIMEOUT_SECONDS
    timeout (sealed-mode egress refused rather than hanging).
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


def _default_stripe_refund(payment_intent_id: str, idempotency_key: str):
    """Thin wrapper around the payment processor's Refund create (the injectable default)."""
    stripe_api._ensure_stripe_api_key()
    return stripe_api.stripe.Refund.create(
        payment_intent=payment_intent_id,
        idempotency_key=idempotency_key,
    )


def verify_payment_method(db, tenant, *, customer_id: str,
                          payment_method_id: str, setup_intent_id: str,
                          stripe_create: Optional[Callable] = None,
                          stripe_refund: Optional[Callable] = None,
                          stripe_cancel: Optional[Callable] = None) -> str:
    """PAYG 6.6 — charge $1 on the tenant's card, refund it, stamp verified_at.

    Returns one of `"skipped_disabled"` (kill switch off — no checkout call at
    all), `"already_verified"` (the stamp is set — idempotent, no checkout call),
    `"verified"` (charge succeeded and refunded; `tenant.payg_setup_verified_at`
    is set, the CALLER commits), or `"failed"`.

    NEVER raises: a declined card / checkout error / settings error / missing
    event id is a warning (exception CLASS + checkout `code` only) and a
    `"failed"` result. A refund that fails after a successful charge is an
    ERROR log with the tenant + PaymentIntent ids and `"failed"` WITHOUT the
    stamp — ops refunds that intent manually and the tenant re-verifies
    (see the module docstring for why fail-closed wins here).
    """
    if tenant is None:
        logger.warning("payg_setup_verify: no tenant resolved; nothing to verify")
        return RESULT_FAILED

    if tenant.payg_setup_verified_at is not None:
        return RESULT_ALREADY_VERIFIED

    if not enabled(db):
        return RESULT_SKIPPED_DISABLED

    if stripe_api is None:
        return RESULT_SKIPPED_DISABLED

    if not (customer_id and payment_method_id and setup_intent_id):
        logger.warning(
            "payg_setup_verify: tenant %s setup event missing customer/PM/"
            "setup-intent id; no verification charge",
            tenant.id)
        return RESULT_FAILED

    create = stripe_create or _default_stripe_create
    refund = stripe_refund or _default_stripe_refund
    cancel = stripe_cancel or _default_stripe_cancel
    pi_id = None

    try:
        intent = create(customer_id=customer_id,
                        payment_method_id=payment_method_id,
                        amount_cents=VERIFY_AMOUNT_CENTS,
                        idempotency_key=f"mod-payg-verify-{setup_intent_id}",
                        metadata={"purpose": "payg_setup_verify",
                                  "tenant_id": str(tenant.id)})
        pi_id = _intent_id(intent)
        if not pi_id:
            logger.error(
                "payg_setup_verify: tenant %s verification intent returned no "
                "id; cannot refund, not marked verified",
                tenant.id)
            return RESULT_FAILED

        pi_status = _intent_status(intent)
        if pi_status == "requires_capture":
            # Authorized but not (yet) captured — nothing to refund; cancel the
            # hold instead so no money is ever actually taken from the card.
            cancel(pi_id)
            logger.warning(
                "payg_setup_verify: tenant %s verification intent %s came back "
                "requires_capture; canceled the authorization, not marked "
                "verified", tenant.id, pi_id)
            return RESULT_FAILED
        if pi_status != "succeeded":
            # requires_action / processing / requires_confirmation / etc: the
            # card was never actually charged, so there is nothing to refund
            # and nothing for ops to chase manually.
            logger.warning(
                "payg_setup_verify: tenant %s verification intent %s ended in "
                "status %s (not succeeded); not marked verified",
                tenant.id, pi_id, pi_status)
            return RESULT_FAILED

        refund(payment_intent_id=pi_id,
               idempotency_key=f"mod-payg-verify-refund-{pi_id}")
    except Exception as exc:  # noqa: BLE001 — verification never aborts the PM save
        code = getattr(exc, "code", None)
        if pi_id:
            logger.error(
                "payg_setup_verify: tenant %s charged the verification amount (intent %s) but the "
                "refund failed (%s %s); NOT marked verified — ops must refund "
                "intent %s manually",
                tenant.id, pi_id, type(exc).__name__, code or "", pi_id)
        else:
            logger.warning(
                "payg_setup_verify: tenant %s verification charge failed (%s %s)",
                tenant.id, type(exc).__name__, code or "")
        return RESULT_FAILED

    tenant.payg_setup_verified_at = datetime.now(timezone.utc)
    return RESULT_VERIFIED

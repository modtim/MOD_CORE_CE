"""services/payg_setup_gate.py — payg 6.7 (payg-billing-hardening §7).

A tenant that switched to PAYG postpaid billing (``Tenant.billing_mode ==
'postpaid'``, models.py) has no prepaid balance to draw on: every run is
billed against their Stripe Customer after the fact. That only makes sense
once they have actually attached a payment method — the SetupIntent webhook
(payg 6.6, task 340) verifies one with a $1 charge that is refunded
immediately and stamps ``Tenant.payg_setup_verified_at``. Until that column
is set, a postpaid tenant can queue runs they can never pay for, so
``POST /workflows/workflow-versions/{id}/run`` refuses them with a 402 whose
detail is ``{"code": "payment_setup_required", "message": ...}`` BEFORE any
``WorkflowRun`` row exists (same pre-flight position as the purchase-required
and run-budget gates).

THIS MODULE ONLY READS ``payg_setup_verified_at`` — the writer is task 340
(the SetupIntent webhook), landed behind its own flag. Nothing here creates
a SetupIntent, charges a card, or touches Stripe.

Kill switch: the DB-backed platform setting ``billing.payg_setup_gate_enabled``
(bool, default False, group "billing", no env fallback), registered at import
with the same idiom as services/payg_auto_recharge.py. Default OFF until the
owner verifies the flow with a Stripe test card.

Scope notes: prepaid tenants are never gated (they pay from their balance), a
tenant row that cannot be resolved is never gated, and platform admins are
NOT special-cased here — the router decides who is exempt.

Error posture: this helper does not swallow errors; the router wraps the call
exactly like the run-budget gate (log warning + ``db.rollback()`` + run
proceeds), so a broken gate query can neither block Execute nor leave the
request session in ``InFailedSqlTransaction``.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from typing import Any

import models
from services import platform_settings as psr  # leaf module: no audit cycle

logger = logging.getLogger(__name__)

PAYG_SETUP_GATE_ENABLED_KEY = "billing.payg_setup_gate_enabled"
DEFAULT_PAYG_SETUP_GATE_ENABLED = False


def _validate_gate_enabled(value: bool) -> None:  # noqa: F704 — type gate only
    """Registry validator: the switch is strictly boolean."""
    if not isinstance(value, bool):
        raise ValueError(
            "billing.payg_setup_gate_enabled must be a bool, "
            f"got {value!r}"
        )


psr.register(
    PAYG_SETUP_GATE_ENABLED_KEY,
    bool,
    DEFAULT_PAYG_SETUP_GATE_ENABLED,
    validator=_validate_gate_enabled,
    group="billing",
    description="PAYG 6.7 — master switch for the payment-setup-required "
                "pre-flight on POST /workflows/workflow-versions/{id}/run "
                "(services/payg_setup_gate.setup_required): postpaid tenants "
                "whose SetupIntent was never verified "
                "(Tenant.payg_setup_verified_at NULL, written by the "
                "SetupIntent webhook) get a 402 payment_setup_required before "
                "any WorkflowRun exists. Default False = OFF everywhere (kill "
                "switch) until the owner verifies the flow with a Stripe test "
                "card. No env fallback: the switch is DB-only.",
)


def gate_enabled(db: Any = None) -> bool:
    """True when the payg 6.7 gate switch is on. Fail-safe: any registry error
    means OFF (the gate stays off, the run stays allowed)."""
    if db is None:
        return DEFAULT_PAYG_SETUP_GATE_ENABLED
    try:
        return bool(psr.get_setting(PAYG_SETUP_GATE_ENABLED_KEY, db=db))
    except Exception as exc:  # noqa: BLE001 — a broken registry read = OFF
        logger.warning("payg setup gate switch unreadable (%s): treating as OFF",
                       type(exc).__name__)
        return DEFAULT_PAYG_SETUP_GATE_ENABLED


def setup_required(db: Session, tenant_id) -> bool:
    """True when this tenant must add a payment method before running.

    True only when ALL of: the ``billing.payg_setup_gate_enabled`` switch is
    on, the tenant's ``billing_mode`` is ``'postpaid'``, and
    ``payg_setup_verified_at`` is NULL (no verified SetupIntent yet).
    Prepaid tenants, flag off, and an unresolvable tenant row are all False.
    """
    if tenant_id is None:
        return False
    if not gate_enabled(db):
        return False
    tenant = (
        db.query(models.Tenant)
        .filter(models.Tenant.id == int(tenant_id))
        .first()
    )
    if tenant is None:
        return False
    if (tenant.billing_mode or "") != "postpaid":
        return False
    return tenant.payg_setup_verified_at is None

"""GH.4 — burst metering + prepaid holds (launch-completion-megaplan group H,
decomp/gh-burst.md).

Group H's "no float" rule (gh-burst.md "Safety rules"):
  - before scale-up, a prepaid HOLD = the estimated fee for the requested
    window; refused outright if the balance can't cover it;
  - on each metering tick, debit elapsed burst-GPU-time × fee, then
    re-check the balance;
  - at zero, drain then teardown.

This module is the meter + the hold gate. It reuses the EXISTING prepaid
ledger rails end to end:
  - `utils.credit_manager.CreditManager.deduct_credits` for every debit
    (same row-locked, never-negative primitive `stack_billing.py` uses for
    persistent-stack reservations) — `allow_advance=False` so a burst
    tenant can never float past its balance the way overage billing can;
  - `models.LedgerEntryType.BURST_MANAGEMENT_FEE` for the ledger entry type,
    so burst spend is auditable separately from workflow/reservation spend;
  - `services.burst_pricing.burst_fee_credits_per_hour` (GH.9) for the rate;
  - `models.AuditEvent` for hold refusals and zero-balance teardowns
    (ISO 27001 — append-only, never blocks the operation it describes).

ANCHORED / IDEMPOTENT, same shape as `stack_billing._meter_stack`:
`BurstSession.last_debited_at` is the watermark. A tick charges
`rate · (now − last_debited_at)` and advances the watermark in the SAME
transaction as the deduct (set BEFORE `deduct_credits`, whose own commit
carries both). A missed tick self-heals (elapsed spans the gap); a
double/overlapping tick is a no-op (`SELECT … FOR UPDATE` on the session row
serialises two runners, and an elapsed of <=0 rolls back without charging).

CAPPED, NEVER NEGATIVE: the tick first attempts the full computed charge.
`InsufficientCreditsError.available` — read under the SAME row lock
`deduct_credits` takes on `tenants` — is the exact remaining balance; the
tick immediately retries for exactly that amount, which cannot fail (it is
by construction <= what's on the row), draining the balance to exactly 0.
The session is marked 'draining' in that same follow-up transaction and
`_teardown_session` is called once, outside the lock.

CE posture (owner Q36, 11-05): Core/open-core ships `utils.credit_manager`
as a no-op stub — there is NO MOD management fee and NO prepaid hold in CE.
`_credits_available(db)` detects that (the Cloud module carries a real
`credits_enabled(db)`; the stub's module-level `__getattr__` fallback returns
None, which is falsy) and the meter then runs in fee-less mode: the hold
passes with 0.0 required, ticks debit nothing and write no ledger row, but
the metering sweep still runs and the max-lifetime drain net still advances
the watermark and tears instances down, so nothing can leave a cloud instance
running unmanaged. A failure reading the credits state fails toward the
no-fee side (CE), never toward provisioning-without-a-gate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

import models
from crud.secret_cud import resolve_decrypted_secret_for_tenant
from models import LedgerEntryType
from services import burst_accounts, burst_bootstrap, burst_pricing, burst_provider
from utils.paid_import import optional_paid

# CE carve (owner Q36, 11-05): `utils/credit_manager.py` is DENIED
# (open-core/manifest.yaml:1548), so a direct import of it in this CE-KEPT
# module is a boot crash. Bind it through `optional_paid` and DEGRADE to the
# fee-less posture this module already documents (its own other failure paths:
# `_credits_available` resolves any read failure to False = fee-less, and both
# `reserve_burst_hold` and `_charge_and_advance` return before any credit move
# when it is False). NOT a 402: burst-to-SaaS must work in CE
# (open-core/manifest.yaml:1062) and every use of these two names sits behind
# that fee-less gate, so the CE-safe degrade is "no debit, no ledger row" -
# never a pretend-served fee. `CreditManager` stays None in CE so an
# unreachable-by-construction call fails toward no-spend (AttributeError ->
# the per-session guard in `debit_tick`), never toward a silent charge.
_paid_credit_manager = optional_paid("utils.credit_manager")
if _paid_credit_manager is not None:
    from utils.credit_manager import CreditManager, InsufficientCreditsError
else:
    class InsufficientCreditsError(Exception):  # CE stand-in: never raised
        """No credit ledger to exhaust in CE, so nothing raises this."""

    CreditManager = None

logger = logging.getLogger(__name__)

EVENT_HOLD_REFUSED = "BURST_HOLD_REFUSED"
EVENT_ZERO_BALANCE_TEARDOWN = "BURST_ZERO_BALANCE_TEARDOWN"
EVENT_TEARDOWN_ERROR = "BURST_SESSION_TEARDOWN_ERROR"


def _credits_available(db: Session) -> bool:
    """True when this build has a credit ledger to meter against (Cloud/SaaS),
    False in Core/CE where `utils.credit_manager` ships as a no-op stub.

    Detection, not a flag: the Cloud module carries a real `credits_enabled(db)`
    (the `billing.credits_enabled` platform setting, default True); the CE stub
    has no such attribute, so its module-level `__getattr__` fallback returns a
    no-op whose value is None — falsy. Any read failure (module absent,
    registry unreadable) resolves to False: fee-less. That is the safe
    direction — a meter that cannot tell whether it may charge charges NOTHING
    and still runs its teardown nets, so no failure mode here can provision
    spend or leave an instance running unmanaged.
    """
    try:
        from utils.credit_manager import credits_enabled
        return bool(credits_enabled(db))
    except Exception:  # noqa: BLE001 — absent/unreadable = no credit ledger
        return False


def _max_lifetime_expired(db: Session, session: "models.BurstSession",
                          now: datetime) -> bool:
    """True when `session`'s instance is past its max lifetime.

    The lifetime cap is the account's JSONB `limits.max_lifetime_hours`
    (plain dict access — never `.astext`), falling back to
    `burst_bootstrap.DEFAULT_MAX_LIFETIME_HOURS` (12 h, the same cap the
    cloud-init self-destruct arms). An unreadable account/limit resolves to
    the DEFAULT cap, not to "no cap": the safety net must fail toward
    cleaning up, never toward leaving a cloud instance running.
    """
    hours = float(burst_bootstrap.DEFAULT_MAX_LIFETIME_HOURS)
    try:
        account = None
        if session.account_id is not None:
            account = (db.query(models.TenantBurstAccount)
                       .filter(models.TenantBurstAccount.id == session.account_id)
                       .first())
        if account is not None:
            limits = account.limits or {}
            raw = limits.get("max_lifetime_hours")
            if raw is not None:
                hours = float(raw)
    except Exception:  # noqa: BLE001 — unreadable limit -> default cap
        hours = float(burst_bootstrap.DEFAULT_MAX_LIFETIME_HOURS)
    if hours <= 0:
        return True
    joined = session.joined_at or session.last_debited_at
    if joined is None:
        return True  # no anchor to age against -> tear down
    return (now - _as_utc(joined)).total_seconds() >= hours * 3600.0


class BurstHoldRefused(Exception):
    """Typed refusal for `reserve_burst_hold` — the prepaid balance cannot
    cover the estimated fee for the requested burst window. GH.5 (the scale
    controller) catches this and does not scale up."""

    def __init__(self, *, tenant_id: int, gpu_class: str, hours: float,
                 required: float, available: float):
        self.tenant_id = tenant_id
        self.gpu_class = gpu_class
        self.hours = hours
        self.required = required
        self.available = available
        super().__init__(
            f"burst hold refused: tenant {tenant_id} needs {required:.4f} "
            f"credits for {hours:.4f}h of {gpu_class!r}, has {available:.4f}"
        )


def _audit(db: Session, *, tenant_id: int, event: str, **details: Any) -> None:
    """Never let an audit failure break the operation it describes."""
    try:
        db.add(models.AuditEvent(tenant_id=tenant_id, user_id=None,
                                 event_name=event, details=details))
        db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("burst_metering: audit emit failed for %s", event,
                       exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Sessions — the burst-hour meter's join -> leave bracket
# ---------------------------------------------------------------------------

def open_session(
    db: Session, *, tenant_id: int, gpu_class: str = "default",
    account_id: Optional[int] = None, instance_id: Optional[str] = None,
    worker_name: Optional[str] = None,
) -> models.BurstSession:
    """Open a burst-hour meter session at worker JOIN (first heartbeat /
    enrollment approval). Anchors `last_debited_at` to now so no
    pre-join interval is ever billed."""
    now = datetime.now(timezone.utc)
    session = models.BurstSession(
        tenant_id=tenant_id, account_id=account_id, instance_id=instance_id,
        worker_name=worker_name, gpu_class=(gpu_class or "default").strip().lower(),
        status="active", joined_at=now, last_debited_at=now,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def close_session(db: Session, session_id: int) -> Optional[models.BurstSession]:
    """Close a session at worker LEAVE (graceful drain, not exhaustion): one
    final partial charge for the interval since the last tick, then the
    meter stops (`left_at` set, `status='closed'`). Idempotent — closing an
    already-closed session just returns it unchanged. Returns None only if
    the session does not exist at all."""
    session = db.query(models.BurstSession).filter(models.BurstSession.id == session_id).first()
    if session is None:
        return None
    if session.status == "closed":
        return session
    now = datetime.now(timezone.utc)
    _charge_and_advance(db, session_id, now)  # best-effort final tick
    session = (db.query(models.BurstSession)
               .filter(models.BurstSession.id == session_id)
               .with_for_update().first())
    if session is None or session.status == "closed":
        return session
    session.status = "closed"
    session.left_at = now
    db.commit()
    db.refresh(session)
    return session


# ---------------------------------------------------------------------------
# Hold — the prepaid gate GH.5 calls before scaling a burst worker up
# ---------------------------------------------------------------------------

def reserve_burst_hold(db: Session, tenant_id: int, gpu_class: str,
                       hours: float) -> float:
    """The prepaid hold GH.5 takes before provisioning a burst instance.

    No credits move here — the tick debits as usage actually accrues (§the
    module docstring). This is the "no float" GATE: refuse up-front when the
    balance plainly cannot cover `hours` of `gpu_class` at the current
    management-fee rate, so MOD never provisions infra it already knows the
    tenant can't pay for.

    Returns the estimated required credits on success. Raises
    `BurstHoldRefused` (and audits the refusal) when the tenant is prepaid
    and its balance is below that estimate. A postpaid tenant has no
    balance dimension to gate on here (mirrors stack_billing's postpaid
    posture) and always passes.
    """
    if not _credits_available(db):
        # CE (owner Q36): no MOD fee, no prepaid hold — the gate passes with
        # 0.0 required and NOTHING is read from or written to the tenant row
        # (no lock, no ledger, no audit). Cost bounding in CE lives in the
        # default-ON limits (instance cap, monthly GPU-hours, idle shutdown,
        # max lifetime), enforced by the scale controller, not here.
        return 0.0

    rate = burst_pricing.burst_fee_credits_per_hour(db, gpu_class)
    required = rate * float(hours)

    tenant = (db.query(models.Tenant).filter(models.Tenant.id == tenant_id)
              .with_for_update().first())
    if tenant is None:
        db.rollback()
        raise BurstHoldRefused(tenant_id=tenant_id, gpu_class=gpu_class,
                               hours=hours, required=required, available=0.0)

    if getattr(tenant, "billing_mode", None) == "postpaid":
        db.rollback()  # release the lock; nothing to check
        return required

    # OWNER-DECISIONS 09-29 — the up-front gate is plan + purchased pools
    # combined; purchased (one-time top-up) credits cover a burst hold too.
    available = float((tenant.credits_remaining or 0) + (tenant.purchased_credits_remaining or 0))
    db.rollback()  # read-only check — never hold the row past this function

    if available < required:
        _audit(db, tenant_id=tenant_id, event=EVENT_HOLD_REFUSED,
              gpu_class=gpu_class, hours=hours, required=required,
              available=available)
        raise BurstHoldRefused(tenant_id=tenant_id, gpu_class=gpu_class,
                               hours=hours, required=required, available=available)
    return required


# ---------------------------------------------------------------------------
# The tick — per-session debit, idempotent, capped, never negative
# ---------------------------------------------------------------------------

def _charge_and_advance(db: Session, session_id: int, now: datetime) -> Optional[dict]:
    """Meter one session under a row lock. Returns
    {charged, exhausted, elapsed_h} or None if the session isn't
    chargeable (missing / not active). Any state change commits (or rolls
    back) before returning, so the lock is never held across sessions."""
    session = (db.query(models.BurstSession)
               .filter(models.BurstSession.id == session_id,
                       models.BurstSession.status == "active")
               .with_for_update().first())
    if session is None:
        return None

    anchor = session.last_debited_at or session.joined_at
    elapsed_h = (now - _as_utc(anchor)).total_seconds() / 3600.0
    if elapsed_h <= 0:
        db.rollback()  # double tick / clock skew — release the lock, no-op
        return {"charged": 0, "exhausted": False, "elapsed_h": 0.0}

    if not _credits_available(db):
        # CE fee-less metering (owner Q36): no debit, no ledger row — but the
        # tick still runs, and when the instance is past its max lifetime the
        # watermark advances and the session drains + tears down, so an
        # instance can never run past its cap because the fee is absent.
        db.rollback()  # release the row lock taken above; nothing to charge
        if not _max_lifetime_expired(db, session, now):
            return {"charged": 0, "exhausted": False, "elapsed_h": elapsed_h}
        session = (db.query(models.BurstSession)
                   .filter(models.BurstSession.id == session_id)
                   .with_for_update().first())
        if session is None:
            return None
        session.last_debited_at = now
        session.status = "draining"
        db.commit()
        return {"charged": 0, "exhausted": True, "elapsed_h": elapsed_h}

    rate = burst_pricing.burst_fee_credits_per_hour(db, session.gpu_class)
    charge = int(round(rate * elapsed_h))
    if charge < 1:
        # Sub-credit: do not advance the anchor, so a low rate accumulates
        # across ticks until it crosses one whole credit (mirrors
        # stack_billing._meter_stack's floor-to-zero guard).
        db.rollback()
        return {"charged": 0, "exhausted": False, "elapsed_h": elapsed_h}

    # Advance the anchor + running total BEFORE the deduct so its own commit
    # carries both — the watermark and the ledger move atomically.
    session.last_debited_at = now
    session.credits_debited_total = (session.credits_debited_total or 0) + charge
    try:
        CreditManager.deduct_credits(
            db, session.tenant_id, charge,
            notes=f"Burst management fee: {charge} credits (session "
                  f"{session.id}, {elapsed_h:.4f}h x {rate:.4f} cr/hr)",
            metadata={"source": "burst_management_fee", "session_id": session.id},
            entry_type=LedgerEntryType.BURST_MANAGEMENT_FEE,
            allow_advance=False)
        return {"charged": charge, "exhausted": False, "elapsed_h": elapsed_h}
    except InsufficientCreditsError as e:
        db.rollback()  # discard the failed full-charge attempt entirely
        available = max(int(e.available or 0), 0)

        session = (db.query(models.BurstSession)
                   .filter(models.BurstSession.id == session_id).with_for_update().first())
        if session is None:
            return None
        session.last_debited_at = now
        session.status = "draining"
        if available <= 0:
            db.commit()
            return {"charged": 0, "exhausted": True, "elapsed_h": elapsed_h}

        session.credits_debited_total = (session.credits_debited_total or 0) + available
        # `available` was read under the SAME lock deduct_credits takes on
        # `tenants`, so this exact amount is guaranteed to succeed — it
        # drains the balance to precisely 0, never negative.
        CreditManager.deduct_credits(
            db, session.tenant_id, available,
            notes=f"Burst management fee (capped at balance): {available} "
                  f"credits (session {session.id})",
            metadata={"source": "burst_management_fee", "session_id": session.id,
                     "capped": True},
            entry_type=LedgerEntryType.BURST_MANAGEMENT_FEE,
            allow_advance=False)
        return {"charged": available, "exhausted": True, "elapsed_h": elapsed_h}


def _resolve_teardown_provider(db: Session, account: models.TenantBurstAccount):
    """Build the BurstProvider for a teardown. A separate, monkeypatchable
    seam (tests substitute a FakeBurstProvider / recorder here instead of
    touching real cloud credentials)."""
    secret = resolve_decrypted_secret_for_tenant(
        db, secret_name=account.secret_ref, tenant_id=account.tenant_id)
    if secret is None:
        raise RuntimeError(f"secret '{account.secret_ref}' not found in the tenant secret store")
    return burst_provider.get_provider(account.provider, **burst_accounts._creds_from_secret(secret))


def _teardown_session(db: Session, session_id: int) -> None:
    """Zero-balance drain: tear the burst instance down through the
    existing burst_reconciler/burst_provider abstractions, then close the
    session. Never raises — a failure is audited and the session stays
    'draining' so the next tick (or the orphan reconciler) retries it."""
    session = db.query(models.BurstSession).filter(models.BurstSession.id == session_id).first()
    if session is None or session.status == "closed":
        return

    account = None
    if session.account_id is not None:
        account = (db.query(models.TenantBurstAccount)
                   .filter(models.TenantBurstAccount.id == session.account_id).first())
    if account is None:
        _audit(db, tenant_id=session.tenant_id, event=EVENT_TEARDOWN_ERROR,
              session_id=session.id,
              error="no burst account on session; cannot resolve a provider for teardown")
        return

    secret = None
    try:
        secret = resolve_decrypted_secret_for_tenant(
            db, secret_name=account.secret_ref, tenant_id=account.tenant_id)
        provider = _resolve_teardown_provider(db, account)
        node_ids = [session.instance_id] if session.instance_id else []
        terminated = provider.teardown(node_ids) if node_ids else []

        session = (db.query(models.BurstSession)
                   .filter(models.BurstSession.id == session_id).with_for_update().first())
        session.status = "closed"
        session.left_at = datetime.now(timezone.utc)
        db.commit()
        _audit(db, tenant_id=session.tenant_id, event=EVENT_ZERO_BALANCE_TEARDOWN,
              session_id=session.id, account_id=account.id,
              instance_id=session.instance_id, terminated=len(terminated))
    except Exception as e:  # noqa: BLE001 — one bad teardown must not raise
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        error = burst_accounts._redact(str(e), secret)
        _audit(db, tenant_id=session.tenant_id, event=EVENT_TEARDOWN_ERROR,
              session_id=session.id, account_id=account.id, error=error)
        logger.warning("burst_metering: teardown failed for session %s: %s",
                       session_id, error)


def debit_tick(db: Session, now: Optional[datetime] = None) -> dict:
    """One metering pass over every active burst session, plus a retry pass
    over sessions still 'draining' (a previous teardown attempt failed).

    A per-session error is logged and skipped — one bad session must never
    abort the sweep. Returns a summary dict for the caller/logs.
    """
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {"charged": [], "drained": [], "errors": 0}

    active_ids = [(row[0], row[1]) for row in
                 db.query(models.BurstSession.id, models.BurstSession.status)
                 .filter(models.BurstSession.status.in_(["active", "draining"])).all()]
    for sid, status in active_ids:
        if status == "draining":
            # Retry teardown on sessions left in draining state by a
            # previous failed teardown — retry on the NEXT tick, not
            # the same one, so a transient failure does not spin.
            try:
                _teardown_session(db, sid)
            except Exception as e:  # noqa: BLE001
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                result["errors"] += 1
                logger.exception("burst_metering: teardown retry for session %s raised: %s", sid, e)
        else:
            try:
                outcome = _charge_and_advance(db, sid, now)
                if outcome is None:
                    continue
                if outcome["charged"]:
                    result["charged"].append({"session_id": sid, "credits": outcome["charged"]})
                if outcome["exhausted"]:
                    result["drained"].append(sid)
                    _teardown_session(db, sid)
            except Exception as e:  # noqa: BLE001
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                result["errors"] += 1
                logger.exception("burst_metering: metering session %s raised: %s", sid, e)

    if result["charged"] or result["drained"]:
        logger.info("burst_metering: charged=%d drained=%d",
                    len(result["charged"]), len(result["drained"]))
    return result

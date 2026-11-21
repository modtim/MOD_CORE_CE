"""burst-to-saas slice 3 — the production caller for the spill decision.

`burst_policy.decide` is a pure function; this is where a real queued run meets
it. The hook is the ONE moment a run is known to be waiting: it has just been
**capacity-queued** because the tenant's concurrency slots are full. That is
exactly "my queue backed up" — the condition burst exists to relieve — so it is
the only place the wait-threshold check has a real wait to weigh.

**Layered on top of the safe queue, never in place of it.** The run is left
capacity-queued (it will run locally when a slot frees) and a background task is
offered the chance to spill it instead. If the spill happens, the run is marked
terminal + ran_remotely so the local dispatcher skips it; if it refuses or
fails, the run is handed back to the queue and runs locally as it would have.
The run is therefore never orphaned and never run twice:

  - The claim (`is_capacity_queued = False`, taken under the row lock) removes
    the run from the dispatcher's selection
    (`services/scheduler.py:_run_concurrency_dispatch`, which filters
    `is_capacity_queued.is_(True), status == QUEUED` with `skip_locked`).
  - If the dispatcher wins the lock first, this task's `skip_locked` claim
    returns nothing and it bails — the run simply runs locally. No double run.

Default-off is the safety floor: with `TenantBurstSettings.enabled` False (the
default for every tenant that has not opted in), `decide` returns on its first
check and no background task is even scheduled. This code is dormant until a
tenant deliberately switches burst on.
"""
from __future__ import annotations

import logging
import math
import os
import secrets as py_secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

import models
from crud.secret_cud import resolve_decrypted_secret_for_tenant
from services import burst_accounts, burst_bootstrap, burst_metering, burst_policy, burst_provider
from services import platform_settings
from services.platform_settings import get_setting
from utils.paid_import import optional_paid
from services.burst_metering import BurstHoldRefused
from services.burst_policy import BurstSettings, Decision
from services.burst_reconciler import BURST_INSTANCE_TAG_KEY, BURST_LAUNCHED_AT_TAG_KEY

logger = logging.getLogger(__name__)


# --- inputs to the decision, each from an EXISTING signal --------------------

def _settings(db: Session, tenant_id: int) -> Optional[BurstSettings]:
    row = (
        db.query(models.TenantBurstSettings)
        .filter(models.TenantBurstSettings.tenant_id == tenant_id)
        .first()
    )
    if row is None:
        return None  # never configured — decide() reads this as off
    return BurstSettings(
        enabled=bool(row.enabled),
        spill_threshold_minutes=row.spill_threshold_minutes,
        spend_ceiling=Decimal(str(row.spend_ceiling)) if row.spend_ceiling is not None else None,
        spend_period_days=row.spend_period_days,
    )


def _projected_wait_minutes(
    db: Session, tenant_id: int, exclude_run_id: int,
) -> Optional[float]:
    """How long until this run would start locally, from the scheduler's own
    per-run estimates (`estimated_duration_seconds`) — NOT a second duration
    model (design D3, memo: reuse the existing estimate, don't invent one).

    Approximated as the work ahead of this run — everything RUNNING plus every
    capacity-queued run older than it — spread across the tenant's concurrency
    slots. It is a conservative proxy, honest about being one, and is the number
    to replace the day the AI scheduler exposes a real queue-wait ETA. Returns
    None when nothing ahead carries an estimate; `decide` treats None as "not
    worth spilling on a guess" and stays local.
    """
    ahead = (
        db.query(func.coalesce(func.sum(models.WorkflowRun.estimated_duration_seconds), 0))
        .filter(
            models.WorkflowRun.tenant_id == tenant_id,
            models.WorkflowRun.id != exclude_run_id,
            models.WorkflowRun.status.in_(
                [models.TaskStatus.RUNNING, models.TaskStatus.QUEUED]
            ),
        )
        .scalar()
    )
    ahead_seconds = float(ahead or 0)
    if ahead_seconds <= 0:
        return None

    slots = 1
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    sub = getattr(tenant, "subscription", None)
    plan = getattr(sub, "plan", None)
    if plan is not None and (plan.max_concurrent_processes or 0) > 0:
        slots = plan.max_concurrent_processes
    return (ahead_seconds / slots) / 60.0


# --- GH.5 scale controller ----------------------------------------------------
#
# Retargets the SAME "a run just went capacity-queued" hook: instead of (or as
# well as) offering the run to MOD's SaaS pool, grow the tenant's OWN-cloud
# burst pool by one instance, bounded by the account's policy cap, gated by
# the prepaid hold (services.burst_metering), and reconciled/torn down by the
# existing GH.3/GH.6 machinery (burst_bootstrap, burst_reconciler).
#
# The platform setting `burst.scale_enabled` (platform-settings registry,
# default ON) is its OWN kill switch, checked BEFORE
# TenantBurstSettings.enabled: with it off, `consider_capacity_spill` below
# is byte-for-byte the old burst-to-saas spill path, behind its existing
# per-tenant kill switch. With it on (the default) the hook is repurposed for every
# tenant in this deployment at once — an operator-level decision, not a
# per-tenant one (per-tenant consent is still `TenantBurstSettings.enabled`,
# which `_scale_policy_decision` checks via `burst_policy.decide`, same
# order as the old spill decision).
# "min_billable_window" (gh-burst.md Safety rules): the hold taken before
# scale-up covers at least this many hours, so a burst instance that runs
# for minutes is still held (and later billed) for a whole billable unit.
DEFAULT_HOLD_HOURS = 1.0

# Default policy value when TenantBurstAccount.limits carries no "max_instances"
# (or carries 0/an unreadable value, which _instance_cap treats as unset): 1
# cloud instance at a time. This constant is the fallback value only; the value
# actually used is _default_max_instances(), evaluated at CALL time:
#   * the default is the CE ceiling burst_accounts.MAX_INSTANCES_LIMIT (100000,
#     effectively unlimited: no quantity caps), and an explicit 0 means that same
#     ceiling. An explicit POSITIVE account value wins.
# A failing probe falls back to this constant (fail-closed).
DEFAULT_MAX_BURST_INSTANCES = 1


def _default_max_instances() -> int:
    """Default instance cap, evaluated at call time (never
    frozen at import): the fallback DEFAULT_MAX_BURST_INSTANCES (1); the
    effective cap is the shared burst_accounts.MAX_INSTANCES_LIMIT."""
    return platform_settings._ce_unlimited(
        DEFAULT_MAX_BURST_INSTANCES,
        unlimited=burst_accounts.MAX_INSTANCES_LIMIT,
    )

# "idle > the window" — a burst session with zero tenant capacity-queued
# backlog for at least this long is scaled back down. Session age
# (`joined_at`) is the idle-duration proxy: BurstSession carries no
# per-worker "last had work" column, and adding one is avoidable (see
# task note "prefer not adding any migration"); the instance's own
# cloud-init idle timer (burst_bootstrap) is the authoritative LOCAL
# backstop regardless, so this is only the API-side companion sweep.
DEFAULT_SCALE_DOWN_IDLE_MINUTES = 30

# Last-resort cloud GPU-hour budget: used only when TenantBurstAccount.limits
# carries no "max_gpu_hours_month" AND the platform setting
# `burst.default_max_gpu_hours_month` itself cannot be read. Scale-up is
# bounded, so this fail-safe must never default to unbounded; the SETTING is
# registered at 100000.0 (effectively unlimited: no quantity caps), and
# this constant is only the last-resort fallback for an UNREADABLE setting, so
# it stays bounded (fail-closed). 100 is the documented fixture
# value (tests/test_burst_accounts.py:85).
DEFAULT_MAX_GPU_HOURS_MONTH = 100

EVENT_SCALE_UP = "BURST_SCALE_UP"
EVENT_SCALE_DOWN = "BURST_SCALE_DOWN"
# Scale-up refusals, audited so they are visible (CLOUD-CE B4 / 17.C4b).
EVENT_REFUSED_LIMIT_UNREADABLE = "refused_limit_unreadable"
EVENT_REFUSED_BUDGET = "refused_budget_reached"

# Advisory-lock namespace (utils/prepaid_lock.py pattern: hashtext(ns) +
# tenant_id) serializing scale-up for one tenant, so two concurrent triggers
# can never both pass the cap check and both provision.
_SCALE_LOCK_NAMESPACE = "mod-burst-scale"


def _scale_controller_enabled() -> bool:
    try:
        return bool(get_setting("burst.scale_enabled"))
    except Exception:  # noqa: BLE001 — fail closed: nothing provisions without the setting
        return False


def _scale_up_lock(db: Session, tenant_id: int) -> None:
    """Per-tenant `pg_advisory_xact_lock`, released automatically at the
    next commit/rollback of `db`'s current transaction (see
    utils/prepaid_lock.py for the same pattern on the prepaid-spend path).
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:ns), :tenant_id)"),
        {"ns": _SCALE_LOCK_NAMESPACE, "tenant_id": tenant_id},
    )


def _live_burst_account(db: Session, tenant_id: int) -> Optional["models.TenantBurstAccount"]:
    return (
        db.query(models.TenantBurstAccount)
        .filter(
            models.TenantBurstAccount.tenant_id == tenant_id,
            models.TenantBurstAccount.deleted_at.is_(None),
            models.TenantBurstAccount.status == burst_accounts.STATUS_VALID,
        )
        .order_by(models.TenantBurstAccount.id.desc())
        .first()
    )


def _scale_policy_decision(
    db: Session, run: "models.WorkflowRun", account: Optional["models.TenantBurstAccount"],
) -> Decision:
    """Same pure `burst_policy.decide`, same check order, as the old spill
    decision (decomp/gh-burst.md: "reuses burst_policy, same order; the
    ceiling becomes the prepaid-balance/daily-cap check"). `connected` here
    means "is there a live own-cloud burst account to launch into" rather
    than "is this install paired with MOD"; the spend-ceiling/credit checks
    are left out (None) because the prepaid HOLD (`reserve_burst_hold`,
    called right after this) is the actual no-float gate for scale-up.
    """
    settings = _settings(db, run.tenant_id)
    workflow = getattr(run, "workflow", None)
    workflow_eligible = bool(getattr(workflow, "burst_eligible", False))
    return burst_policy.decide(
        settings=settings,
        workflow_eligible=workflow_eligible,
        connected=account is not None,
        target_can_run=True,
        projected_wait_minutes=_projected_wait_minutes(db, run.tenant_id, run.id),
        period_spend=None,
        credit_balance=None,
    )


def _active_launch_count(db: Session, tenant_id: int) -> int:
    """Active + draining BurstSessions for this tenant — the "how many
    burst instances does this tenant have right now" count the cap is
    checked against. Provisioning happens synchronously inside the
    advisory-lock section below (no separate in-flight-launch bookkeeping
    needed: a launch either has not started, or has already committed its
    BurstSession row and is counted here, by the time any other trigger
    for this tenant gets past the lock).
    """
    return (
        db.query(func.count(models.BurstSession.id))
        .filter(
            models.BurstSession.tenant_id == tenant_id,
            models.BurstSession.status.in_(("active", "draining")),
        )
        .scalar()
    ) or 0


def _limits_dict(account: Optional["models.TenantBurstAccount"]) -> dict:
    """`account.limits` as a dict for the tolerant (non-budget) readers; a
    non-dict value reads as empty here. The GPU-hour budget reader does NOT use
    this: it treats a present-but-non-dict block as unreadable and refuses."""
    limits = account.limits if account is not None else None
    return limits if isinstance(limits, dict) else {}


class LimitUnreadable(Exception):
    """The tenant's `limits` are PRESENT but cannot be read as a budget (not a
    JSON object, or `max_gpu_hours_month` is not a finite non-negative
    number). A scale-up must be refused — never guessed at, never unbounded."""


def _instance_cap(account: Optional["models.TenantBurstAccount"]) -> int:
    """Concurrent-instance cap for the account. An explicit positive
    `max_instances` wins. Unset/{}/unreadable -> the call-time
    default. Explicit 0: unlimited (the default IS the unlimited ceiling)."""
    limits = _limits_dict(account)
    default = _default_max_instances()
    try:
        cap = int(limits.get("max_instances") or default)
    except (TypeError, ValueError):
        cap = default
    return max(cap, 0)


def _default_gpu_hours_month() -> float:
    """Fallback budget for an account with NO `max_gpu_hours_month`: the
    platform setting `burst.default_max_gpu_hours_month`. An unreadable or
    out-of-range setting value falls back to the bounded constant, never to
    "unlimited"."""
    try:
        value = float(get_setting("burst.default_max_gpu_hours_month"))
    except Exception:  # noqa: BLE001 — fail bounded
        logger.warning("burst-scale: burst.default_max_gpu_hours_month unreadable; "
                       "using %s", DEFAULT_MAX_GPU_HOURS_MONTH, exc_info=True)
        return float(DEFAULT_MAX_GPU_HOURS_MONTH)
    if not math.isfinite(value) or value < 0:
        return float(DEFAULT_MAX_GPU_HOURS_MONTH)
    return value


def _gpu_hours_month_limit(account: Optional["models.TenantBurstAccount"]) -> float:
    """Resolve the monthly cloud GPU-hour budget, FAIL SAFE (never unbounded).

    * limits absent (None / {}) or `max_gpu_hours_month` absent / null ->
      the platform setting `burst.default_max_gpu_hours_month` (default 100).
    * valid number (numeric strings accepted) -> that value; an explicit 0
      means "none allowed" and refuses every scale-up — never "no limit".
    * limits PRESENT but unreadable (not a dict; or `max_gpu_hours_month`
      present but a bool / non-numeric / negative / nan / inf) -> raises
      `LimitUnreadable`; the caller refuses the scale-up and audits
      `refused_limit_unreadable`.
    """
    limits = account.limits if account is not None else None
    if limits is None:
        return _default_gpu_hours_month()
    if not isinstance(limits, dict):
        raise LimitUnreadable(f"limits is {type(limits).__name__}, not an object")
    raw = limits.get("max_gpu_hours_month", None)
    if raw is None:
        return _default_gpu_hours_month()
    if isinstance(raw, bool):
        raise LimitUnreadable("max_gpu_hours_month is a boolean")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise LimitUnreadable(
            f"max_gpu_hours_month is not numeric ({type(raw).__name__})") from None
    # nan/inf would make `used >= limit` never true (unbounded).
    if not math.isfinite(value) or value < 0:
        raise LimitUnreadable("max_gpu_hours_month is not a finite non-negative number")
    return value


def _burst_gpu_hours_used_this_month(db: Session, tenant_id: int) -> float:
    """Cloud GPU-hours this tenant has already spent on burst sessions in the
    current UTC calendar month — the meter behind
    `TenantBurstAccount.limits["max_gpu_hours_month"]`.

    Summed from `models.BurstSession` rows (the burst-hour meter GH.4 made
    authoritative: `joined_at` -> `left_at` brackets the session), NOT from
    `utils.usage_enforcement.get_gpu_hours_used_this_month`, which counts
    `ApiTaskQueue` GPU-queue task durations — a different spend entirely.
    A still-active/draining session counts up to now; a closed one counts to
    `left_at`. Only rows whose interval overlaps the current month count, and
    each is clamped to the month, so last month's sessions never bleed in.
    Naive timestamps (pre-timezone-migration rows) are read as UTC.
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = 0.0
    for joined_at, left_at in (
        db.query(models.BurstSession.joined_at, models.BurstSession.left_at)
        .filter(models.BurstSession.tenant_id == tenant_id)
        .all()
    ):
        if joined_at is None:
            continue
        started = joined_at if joined_at.tzinfo else joined_at.replace(tzinfo=timezone.utc)
        ended = now
        if left_at is not None:
            left = left_at if left_at.tzinfo else left_at.replace(tzinfo=timezone.utc)
            ended = min(ended, left)
        started = max(started, month_start)
        if ended <= started:
            continue
        used += (ended - started).total_seconds() / 3600.0
    return used


def _instance_type_for(account: Optional["models.TenantBurstAccount"]) -> str:
    limits = _limits_dict(account)
    allowed = limits.get("allowed_instance_types") or ["default"]
    return str(allowed[0]) if allowed else "default"


def _gpu_class_for(account: Optional["models.TenantBurstAccount"]) -> str:
    limits = _limits_dict(account)
    return str(limits.get("gpu_class") or "default")


def _new_instance_label(tenant_id: int) -> str:
    """Matches burst_bootstrap._sanitize_instance_label's charset
    ([A-Za-z0-9._-], starting alnum)."""
    return f"t{tenant_id}-{py_secrets.token_hex(4)}"


def _provider_for_account(db: Session, account: "models.TenantBurstAccount"):
    """Build the BurstProvider for `account`. A separate, monkeypatchable
    seam (tests substitute a FakeBurstProvider here instead of touching
    real cloud credentials) — mirrors burst_metering._resolve_teardown_provider."""
    secret = resolve_decrypted_secret_for_tenant(
        db, secret_name=account.secret_ref, tenant_id=account.tenant_id,
    )
    if secret is None:
        raise RuntimeError(f"secret '{account.secret_ref}' not found in the tenant secret store")
    return burst_provider.get_provider(account.provider, **burst_accounts._creds_from_secret(secret))


def _api_base_url() -> str:
    """Public API base for burst scale-up (FIX-BURST-AUTO-API-LOCALHOST-SENTINEL).

    Explicit-or-empty (the `host_lockdown.py:148` / `routers/worker_install.py:236`
    pattern): an explicitly set `API_EXTERNAL_URL` (then `API_URL`) wins verbatim
    — only a trailing slash is trimmed, so the paid posture is unchanged when the
    env var is set — and neither set yields the EMPTY string, never the
    old unreachable api.localhost sentinel (a CE default install sets no
    such var, so the old fallback handed the bootstrap an address nothing can
    reach). The caller skips scale-up on an empty base (see `_scale_up_worker`).
    """
    return (os.getenv("API_EXTERNAL_URL") or os.getenv("API_URL") or "").strip().rstrip("/")


def _refuse_on_gpu_budget(db: Session, tenant_id: int,
                          account: "models.TenantBurstAccount", *, phase: str) -> bool:
    """True (after committing `db` and writing a visible audit row) when this
    scale-up must be refused on the cloud GPU-hour budget: either the limits
    are present but unreadable (`refused_limit_unreadable`) or the month's
    budget is already spent (`refused_budget_reached`). Never raises: the audit
    write is best-effort (`_audit_scale`) and any unexpected failure while
    resolving the budget refuses (fail closed) instead of provisioning."""
    try:
        gpu_limit = _gpu_hours_month_limit(account)
    except LimitUnreadable as e:
        logger.warning("burst-scale: tenant %s limits unreadable (%s) — refusing "
                       "scale-up (%s)", tenant_id, e, phase)
        db.commit()
        _audit_scale(db, tenant_id=tenant_id, event=EVENT_REFUSED_LIMIT_UNREADABLE,
                     account_id=account.id, phase=phase, reason=str(e))
        return True
    gpu_used = _burst_gpu_hours_used_this_month(db, tenant_id)
    if gpu_used >= gpu_limit:
        logger.info("burst-scale: tenant %s at cloud GPU-hour budget "
                    "max_gpu_hours_month (%s/%s this month) (%s)",
                    tenant_id, gpu_used, gpu_limit, phase)
        db.commit()
        _audit_scale(db, tenant_id=tenant_id, event=EVENT_REFUSED_BUDGET,
                     account_id=account.id, phase=phase,
                     used_hours=round(gpu_used, 4), limit_hours=gpu_limit)
        return True
    return False


def scale_up_if_needed(db: Session, run: "models.WorkflowRun") -> Optional["models.BurstSession"]:
    """Evaluate, and if allowed launch, one bounded scale-up for
    `run.tenant_id`. Returns the opened BurstSession, or None if the
    policy refused, the hold was refused, or the instance cap was already
    reached. Never raises for an ordinary refusal — only a genuinely
    unexpected error propagates (the caller, `_scale_up_worker`, is a
    background thread with its own broad catch).

    Order: policy decision -> prepaid hold -> [advisory lock] -> cap check
    -> render user-data -> [advisory lock, re-verified] -> provision ->
    open_session. Two helpers called along this path
    (`burst_metering.reserve_burst_hold` and, via
    `burst_bootstrap.render_burst_user_data`,
    `worker_management_service.create_enrollment_token_row`) commit or
    roll back `db` internally as part of their own read/write, which ends
    whatever transaction is open and silently releases any advisory lock
    taken on it — so the lock is (re-)taken fresh immediately after each of
    those calls, with the cap re-checked each time, and is held
    uninterrupted from its final acquisition through the provider call and
    the BurstSession commit. That final lock+cap-check window is the
    atomic section two concurrent triggers for the same tenant can never
    both pass.
    """
    tenant_id = run.tenant_id
    account = _live_burst_account(db, tenant_id)
    decision = _scale_policy_decision(db, run, account)
    if not decision.spill:
        burst_policy.stamp_stay_local(
            run, decision.reason.value if decision.reason else "unknown"
        )
        db.commit()
        logger.info("burst-scale: tenant %s stays local (%s)", tenant_id,
                    decision.reason.value if decision.reason else "?")
        return None

    gpu_class = _gpu_class_for(account)
    try:
        required = burst_metering.reserve_burst_hold(db, tenant_id, gpu_class, DEFAULT_HOLD_HOURS)
    except BurstHoldRefused as e:
        logger.info("burst-scale: hold refused for tenant %s: %s", tenant_id, e)
        return None

    # The hold check above ended its own transaction; take the lock fresh
    # and hold it, uninterrupted, through the cap check + provision + commit.
    _scale_up_lock(db, tenant_id)
    account = _live_burst_account(db, tenant_id)
    if account is None:
        db.commit()
        return None

    # CE burst bounds: the cloud GPU-hour budget is checked BEFORE the
    # instance cap and before anything provisions, so a tenant that spent its
    # month's budget cannot provision even one more instance.
    if _refuse_on_gpu_budget(db, tenant_id, account, phase="pre_mint"):
        return None

    cap = _instance_cap(account)
    active = _active_launch_count(db, tenant_id)
    if active >= cap:
        logger.info("burst-scale: tenant %s at instance cap (%s/%s)", tenant_id, active, cap)
        db.commit()
        return None

    label = _new_instance_label(tenant_id)
    launched_at = datetime.now(timezone.utc)
    tags = {
        **burst_accounts.MOD_TAG,
        BURST_INSTANCE_TAG_KEY: label,
        BURST_LAUNCHED_AT_TAG_KEY: launched_at.isoformat().replace("+00:00", "Z"),
    }
    api_base_url = _api_base_url()
    if not api_base_url:
        # No public API base configured (CE default): the bootstrap install
        # command would point nowhere, so skip scale-up — the same "missing
        # value is simply omitted (logged once)" posture as host_lockdown.py.
        logger.warning("burst-scale: tenant %s has no API public URL "
                       "(API_EXTERNAL_URL/API_URL unset) — skipping scale-up",
                       tenant_id)
        db.commit()
        return None
    user_data = burst_bootstrap.render_burst_user_data(
        db, tenant_id, instance_label=label, api_base_url=api_base_url,
    )

    # render_burst_user_data mints the enrollment token via
    # worker_management_service.create_enrollment_token_row, which commits
    # internally (same "helper commits its own transaction" shape as
    # reserve_burst_hold above) — that commit already ended the transaction
    # the advisory lock above was taken in, silently releasing it before the
    # provider was ever called. Re-take the lock and re-verify the cap fresh:
    # a second concurrent trigger could have slipped through that now-unlocked
    # window and already committed its own BurstSession. Nothing between this
    # point and open_session()'s commit below does its own internal
    # commit/rollback, so this second acquisition is the one actually held
    # through provision + open_session.
    _scale_up_lock(db, tenant_id)
    account = _live_burst_account(db, tenant_id)
    if account is None:
        db.commit()
        return None
    if _refuse_on_gpu_budget(db, tenant_id, account, phase="post_mint"):
        return None
    cap = _instance_cap(account)
    active = _active_launch_count(db, tenant_id)
    if active >= cap:
        logger.info("burst-scale: tenant %s at instance cap (%s/%s) after token mint", tenant_id, active, cap)
        db.commit()
        return None

    try:
        provider = _provider_for_account(db, account)
        nodes = provider.provision(1, _instance_type_for(account), user_data, tags)
    except Exception:
        db.rollback()
        logger.exception("burst-scale: provision failed for tenant %s", tenant_id)
        return None
    if not nodes:
        db.commit()
        logger.warning("burst-scale: provider returned no nodes for tenant %s", tenant_id)
        return None
    node = nodes[0]

    session = burst_metering.open_session(
        db, tenant_id=tenant_id, gpu_class=gpu_class, account_id=account.id,
        instance_id=str(node.get("id") or ""), worker_name=f"burst-{label}",
    )
    _audit_scale(db, tenant_id=tenant_id, event=EVENT_SCALE_UP, account_id=account.id,
                session_id=session.id, instance_id=session.instance_id,
                gpu_class=gpu_class, hold_credits=required, tags=tags)
    logger.info("burst-scale: tenant %s scaled up instance %s (session %s)",
                tenant_id, session.instance_id, session.id)
    return session


def _audit_scale(db: Session, *, tenant_id: int, event: str, **details) -> None:
    try:
        db.add(models.AuditEvent(tenant_id=tenant_id, user_id=None,
                                 event_name=event, details=details))
        db.commit()
    except Exception:  # noqa: BLE001 — never let an audit failure break scaling
        logger.warning("burst-scale: audit emit failed for %s", event, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _scale_up_worker(session_factory: Callable[[], Session], run_id: int) -> None:
    db = session_factory()
    try:
        run = db.query(models.WorkflowRun).filter(models.WorkflowRun.id == run_id).first()
        if run is None:
            return
        scale_up_if_needed(db, run)
    except Exception:
        logger.exception("burst-scale: unexpected error scaling up for run %s", run_id)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _capacity_queued_count(db: Session, tenant_id: int) -> int:
    return (
        db.query(func.count(models.WorkflowRun.id))
        .filter(
            models.WorkflowRun.tenant_id == tenant_id,
            models.WorkflowRun.is_capacity_queued.is_(True),
            models.WorkflowRun.status == models.TaskStatus.QUEUED,
        )
        .scalar()
    ) or 0


def _teardown_and_close(db: Session, session: "models.BurstSession") -> None:
    """Actually destroy the cloud instance (best-effort — a failure is
    logged, not fatal: the orphan reconciler will catch it next sweep),
    then stop the meter via the existing `burst_metering.close_session`."""
    account = None
    if session.account_id is not None:
        account = (
            db.query(models.TenantBurstAccount)
            .filter(models.TenantBurstAccount.id == session.account_id)
            .first()
        )
    if account is not None and session.instance_id:
        try:
            provider = _provider_for_account(db, account)
            provider.teardown([session.instance_id])
        except Exception:
            logger.exception("burst-scale: teardown failed for session %s", session.id)
    burst_metering.close_session(db, session.id)
    _audit_scale(db, tenant_id=session.tenant_id, event=EVENT_SCALE_DOWN,
                session_id=session.id, instance_id=session.instance_id)


def scale_down_idle(
    db: Session, now: Optional[datetime] = None,
    idle_window: Optional[timedelta] = None,
) -> list[int]:
    """Scale down (teardown + close_session) every active burst session
    whose tenant has had zero capacity-queued backlog for at least
    `idle_window` (default DEFAULT_SCALE_DOWN_IDLE_MINUTES). Returns the
    ids of the sessions closed. One bad session is logged and skipped —
    never aborts the sweep (same shape as burst_metering.debit_tick)."""
    now = now or datetime.now(timezone.utc)
    window = idle_window or timedelta(minutes=DEFAULT_SCALE_DOWN_IDLE_MINUTES)
    closed: list[int] = []
    sessions = (
        db.query(models.BurstSession)
        .filter(models.BurstSession.status == "active")
        .all()
    )
    for s in sessions:
        try:
            joined = s.joined_at
            if joined.tzinfo is None:
                joined = joined.replace(tzinfo=timezone.utc)
            if now - joined < window:
                continue
            if _capacity_queued_count(db, s.tenant_id) > 0:
                continue
            _teardown_and_close(db, s)
            closed.append(s.id)
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            logger.exception("burst-scale: scale-down failed for session %s", s.id)
    return closed


def consider_capacity_spill(db: Session, run: "models.WorkflowRun",
                            background_tasks=None) -> None:
    """Request-path entry. Cheap: launches the background spiller, or nothing.

    A single indexed settings read decides whether to bother. With burst off —
    the default — this returns without launching anything, so the hot path pays
    one query and existing behaviour is untouched.

    The spiller runs in a daemon THREAD, not a FastAPI BackgroundTask: it blocks
    in `poll_until_done` for as long as the remote run takes (up to hours), and a
    threadpool worker held for hours would starve request handling. This mirrors
    how `scheduler._run_concurrency_dispatch` launches `start_workflow_execution`
    (`threading.Thread(daemon=True)`). `background_tasks` is accepted for call
    compatibility but intentionally unused.

    GH.5 retarget: the platform setting `burst.scale_enabled` (platform-
    settings registry, default ON) is checked FIRST, ahead
    of and independent from `TenantBurstSettings.enabled`. Off (an explicit
    admin decision — deployments that never touched the registry stay ON):
    this function is byte-for-byte the old spill-to-MOD
    path below, behind its own existing per-tenant kill switch. On (the
    default): the SAME
    "just went capacity-queued" hook instead launches the GH.5 scale-up
    evaluation (`scale_up_if_needed`), which does its own per-tenant policy
    check (`_scale_policy_decision`, same `burst_policy.decide` order as the
    spill path). This is how "kill switch off -> old spill path; policy
    disallows -> no scale" both hold at once: they are two different switches,
    checked in that order. An unreadable setting fails closed (no scale-up
    thread; the old spill path runs).
    """
    if _scale_controller_enabled():
        import threading

        from database import SessionLocal
        threading.Thread(
            target=_scale_up_worker,
            args=(SessionLocal, run.id),
            daemon=True,
        ).start()
        logger.info("burst-scale: run %s launched for scale-up evaluation", run.id)
        return

    # The MOD-hosted spill path is not part of the Community Edition: with the
    # module absent there is nothing to spill to and nothing runs here.
    saas_spill = optional_paid("services.burst_saas_spill")
    if saas_spill is None:
        return
    settings = _settings(db, run.tenant_id)
    if settings is None or not settings.enabled:
        return
    import threading

    from database import SessionLocal
    threading.Thread(
        target=saas_spill.spill_capacity_queued_run,
        args=(SessionLocal, run.id),
        daemon=True,
    ).start()
    logger.info("burst-auto: run %s launched for spill evaluation", run.id)

"""GH.6 — burst orphan reconciler (launch-completion-megaplan group H,
decomp/gh-burst.md).

"Never leave infra running unmanaged": every N minutes, for each LIVE
tenant burst account (status 'valid'), list the MOD-tagged instances in
the customer's cloud and terminate any instance MOD no longer knows
about (or whose registration has expired). Every terminate and every
error writes an AuditEvent (ISO 27001 audit trail; nothing is ever
hard-deleted).

The "known instance" rule
-------------------------
GH.3 (burst worker bootstrap) has not landed an instance/lease table yet,
so the known set is derived from the worker registry:

  An instance is KNOWN and kept iff its tag
  `mod-burst-instance` (see BURST_INSTANCE_TAG_KEY) matches the
  `worker_name` or `hostname` of a `WorkerEnrollment` row that:
    * is APPROVED (not pending/revoked),
    * belongs to the SAME tenant — scoped via the enrollment's
      `WorkerEnrollmentToken.owner_tenant_id` (the enrollment row itself
      has no tenant column; the cloud-burst token is minted
      `owner_tenant_id`-owned, which is exactly the row a burst worker
      enrolls with),
    * has `expires_at` NULL or in the future (not an expired token), and
    * has `last_seen_at` within `liveness_window` (default 15 minutes)
      of `now` (still heartbeating).

Everything else with the MOD tag is an orphan and is terminated. When GH.3
lands a first-class instance/lease table, only `_known_instance_ids`
needs to change.

Safety:
  * only instances returned by `provider.list_tagged(MOD_TAG)` (i.e.
    carrying mod-burst=mod-core) are ever passed to `teardown` — the
    provider's own tag guard is the second line of defence;
  * provider errors are redacted (secret value stripped) before they
    touch an audit row or a log line;
  * one account's failure writes a `BURST_RECONCILE_ERROR` audit row and
    is swallowed — it must never stop the remaining accounts.

Not wired into scheduler.py by design (a separate task owns the cadence).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

import models
from crud.secret_cud import resolve_decrypted_secret_for_tenant
from services import burst_accounts, burst_provider

logger = logging.getLogger(__name__)

# Tag the GH.3 cloud-init is going to stamp on every burst worker, so the
# reconciler can match the cloud instance back to the enrolled worker.
BURST_INSTANCE_TAG_KEY = "mod-burst-instance"

EVENT_ORPHAN_TERMINATED = "BURST_ORPHAN_TERMINATED"
EVENT_RECONCILE_ERROR = "BURST_RECONCILE_ERROR"

# A burst worker that stops heartbeating for this long is treated as gone:
# its instance is an orphan again even if the enrollment row is APPROVED.
DEFAULT_LIVENESS_WINDOW = timedelta(minutes=15)

# Tag the launcher (GH.5) stamps with the launch time (ISO-8601 UTC). An
# instance younger than the boot grace hasn't had time to install + enroll,
# so it is not an orphan yet. A missing/unparseable tag gets no grace.
BURST_LAUNCHED_AT_TAG_KEY = "mod-burst-launched-at"
DEFAULT_BOOT_GRACE = timedelta(minutes=20)

# Tim Q38: a burst worker's hard max lifetime. Kept in sync with
# burst_bootstrap.DEFAULT_MAX_LIFETIME_HOURS (asserted equal in the tests);
# defined here so the reconciler's import graph stays free of burst_bootstrap.
DEFAULT_MAX_LIFETIME = timedelta(hours=12)

# Provider node fields the RunPod adapter (B8) fills in: the creation time
# (ISO-8601 UTC or None) and its own over-lifetime flag. A pod cannot
# self-destruct, so this reconciler is the only reaper of max lifetime.
CREATED_FIELD = "created"
EXPIRED_FIELD = "expired"

# GH.3 enrolls the worker as f"burst-{instance_label}" while the instance tag
# carries the bare label; both forms identify the same instance.
_WORKER_NAME_PREFIX = "burst-"

# Node states that are no longer consuming cloud resources.
_INACTIVE_STATES = {"terminated", "terminating", "deleted", "destroyed"}


def _audit(db: Session, *, tenant_id: int, event: str, **details: Any) -> None:
    """Never let an audit failure break the reconciliation it describes."""
    try:
        db.add(models.AuditEvent(tenant_id=tenant_id, user_id=None,
                                 event_name=event, details=details))
    except Exception:
        logger.warning("burst reconciler: audit emit failed for %s", event,
                       exc_info=True)


def _known_instance_ids(
    db: Session, *, tenant_id: int, now: datetime,
    liveness_window: timedelta,
) -> set[str]:
    """Instance ids the reconciler must KEEP (the "known" set).

    See the module docstring for the rule: APPROVED enrollment, not
    expired, heartbeating within `liveness_window`, same tenant.
    """
    min_seen = now - liveness_window
    rows = (
        db.query(models.WorkerEnrollment, models.WorkerEnrollmentToken)
        .join(
            models.WorkerEnrollmentToken,
            models.WorkerEnrollment.enrollment_token_id
            == models.WorkerEnrollmentToken.id,
        )
        .filter(models.WorkerEnrollment.status
                == models.WorkerEnrollmentStatus.APPROVED)
        .filter(models.WorkerEnrollmentToken.owner_tenant_id == tenant_id)
        .all()
    )
    known: set[str] = set()
    for row, _token in rows:
        if row.expires_at is not None and row.expires_at < now:
            continue  # expired registration → its instances are orphans
        if row.last_seen_at is None or row.last_seen_at < min_seen:
            continue  # not heartbeating → treat as gone
        for ident in (row.worker_name, row.hostname):
            if ident:
                known.add(ident)
                if ident.startswith(_WORKER_NAME_PREFIX):
                    known.add(ident[len(_WORKER_NAME_PREFIX):])
    return known


def _parse_ts(raw: Any) -> Optional[datetime]:
    """ISO-8601 string -> tz-aware UTC datetime; None on empty/unparseable."""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _launched_at(inst: dict[str, Any]) -> Optional[datetime]:
    """Launch time: the launcher tag if parseable, else the provider's
    `created`. A missing/unparseable value grants no grace."""
    launched = _parse_ts((inst.get("tags") or {}).get(BURST_LAUNCHED_AT_TAG_KEY))
    if launched is not None:
        return launched
    return _parse_ts(inst.get(CREATED_FIELD))


def _in_boot_grace(
    tags: dict[str, Any], now: datetime, created: Any = None,
) -> bool:
    raw = tags.get(BURST_LAUNCHED_AT_TAG_KEY)
    launched = _parse_ts(raw) if raw else None
    if launched is None:
        launched = _parse_ts(created)
    if launched is None:
        return False
    # A `created` in the FUTURE by more than the grace is a clock lie: a
    # forged value must not shield an instance forever, so it gets no grace.
    if launched - now > DEFAULT_BOOT_GRACE:
        return False
    return now - launched < DEFAULT_BOOT_GRACE


def _lifetime_exceeded(inst: dict[str, Any], now: datetime) -> bool:
    """True when the provider says the instance is over its max lifetime
    (`expired`, set by the RunPod adapter) or its `created` is older than
    DEFAULT_MAX_LIFETIME. Unknown/unparseable created is NOT exceeded."""
    if inst.get(EXPIRED_FIELD) is True:
        return True
    created = _parse_ts(inst.get(CREATED_FIELD))
    return created is not None and now - created >= DEFAULT_MAX_LIFETIME


def reconcile_account(
    db: Session,
    account: models.TenantBurstAccount,
    provider: Optional[burst_provider.BurstProvider] = None,
    now: Optional[datetime] = None,
    liveness_window: timedelta = DEFAULT_LIVENESS_WINDOW,
) -> dict:
    """Reconcile one tenant burst account against its cloud.

    Returns a per-account summary dict:
      {account_id, tenant_id, provider, listed, kept, terminated,
       lifetime_terminated, lifetime_terminated_ids, skipped_untagged,
       terminated_ids, errors, ok}

    Any exception (secret resolution, provider list/teardown) is caught,
    audited as `BURST_RECONCILE_ERROR` (redacted), rolled back, and
    reported in the summary — it never propagates.
    """
    now = now or datetime.now(timezone.utc)
    secret: Optional[str] = None
    summary: dict[str, Any] = {
        "account_id": account.id,
        "tenant_id": account.tenant_id,
        "provider": account.provider,
        "listed": 0,
        "kept": 0,
        "terminated": 0,
        "lifetime_terminated": 0,
        "lifetime_terminated_ids": [],
        "skipped_untagged": 0,
        "terminated_ids": [],
        "errors": [],
        "ok": False,
    }
    try:
        if provider is None:
            # Mirror burst_accounts.probe_account: secret value resolved
            # here, used only to build the provider, stripped from every
            # error string before it is persisted.
            secret = resolve_decrypted_secret_for_tenant(
                db, secret_name=account.secret_ref,
                tenant_id=account.tenant_id,
            )
            if secret is None:
                raise RuntimeError(
                    f"secret '{account.secret_ref}' not found in the "
                    f"tenant secret store"
                )
            provider = burst_provider.get_provider(
                account.provider, **burst_accounts._creds_from_secret(secret),
            )

        instances = provider.list_tagged(burst_accounts.MOD_TAG)
        summary["listed"] = len(instances)
        known = _known_instance_ids(db, tenant_id=account.tenant_id,
                                    now=now, liveness_window=liveness_window)

        to_terminate: list[dict[str, Any]] = []
        reasons: dict[str, str] = {}
        for inst in instances:
            state = str(inst.get("state") or "").lower()
            if state in _INACTIVE_STATES:
                continue  # already gone on the provider side
            tags = inst.get("tags") or {}
            if not burst_provider.has_mod_tag(tags):
                # A foreign/untagged node must never reach teardown, even
                # with expired=True or an old `created`.
                summary["skipped_untagged"] += 1
                continue
            created = inst.get(CREATED_FIELD)
            if _in_boot_grace(tags, now, created):
                summary["kept"] += 1
                continue
            if _lifetime_exceeded(inst, now):
                # max lifetime wins over "known": a heartbeating worker
                # past 12 h is reaped here (RunPod pods cannot
                # self-destruct, so this is the only lifetime reaper).
                reasons[str(inst.get("id") or "")] = "max_lifetime"
                to_terminate.append(inst)
                continue
            if str(tags.get(BURST_INSTANCE_TAG_KEY) or "") in known:
                if _parse_ts(created) is None and inst.get(EXPIRED_FIELD) is not True:
                    # A known, heartbeating instance with no usable age
                    # signal: the lifetime rule cannot see it. Name the
                    # instance only (no ip/tags).
                    logger.warning(
                        "burst reconciler: known instance %s has no "
                        "parseable created time; lifetime not enforced",
                        inst.get("id"),
                    )
                summary["kept"] += 1
                continue
            reasons[str(inst.get("id") or "")] = "orphan"
            to_terminate.append(inst)

        if to_terminate:
            terminated = provider.teardown([i["id"] for i in to_terminate])
            for i in terminated:
                summary["terminated"] += 1
                iid = str(i.get("id") or "")
                reason = reasons.get(iid, "orphan")
                if reason == "max_lifetime":
                    summary["lifetime_terminated"] += 1
                    summary["lifetime_terminated_ids"].append(iid)
                summary["terminated_ids"].append(iid)
                _audit(
                    db, tenant_id=account.tenant_id,
                    event=EVENT_ORPHAN_TERMINATED,
                    account_id=account.id,
                    provider=account.provider,
                    instance_id=iid,
                    state=i.get("state"),
                    public_ip=i.get("public_ip"),
                    tags=i.get("tags") or {},
                    reason=reason,
                    created=i.get(CREATED_FIELD),
                )
        db.commit()
        summary["ok"] = True
    except Exception as e:  # noqa: BLE001 — one account must not stop the rest
        try:
            db.rollback()
        except Exception:
            logger.warning("burst reconciler: rollback failed", exc_info=True)
        error = burst_accounts._redact(str(e), secret)
        summary["errors"].append(error)
        _audit(
            db, tenant_id=account.tenant_id,
            event=EVENT_RECONCILE_ERROR,
            account_id=account.id, provider=account.provider,
            error=error,
        )
        db.commit()
        logger.warning("burst reconcile failed for tenant=%s account=%s: %s",
                       account.tenant_id, account.id, error)
    return summary


def reconcile_all(
    db: Session,
    now: Optional[datetime] = None,
    liveness_window: timedelta = DEFAULT_LIVENESS_WINDOW,
) -> list[dict]:
    """Reconcile every LIVE account whose probe status is 'valid'.

    Accounts whose provider build/list fails are audited and skipped;
    the loop always continues to the next account.
    """
    now = now or datetime.now(timezone.utc)
    accounts = (
        db.query(models.TenantBurstAccount)
        .filter(
            models.TenantBurstAccount.deleted_at.is_(None),
            models.TenantBurstAccount.status == burst_accounts.STATUS_VALID,
        )
        .order_by(models.TenantBurstAccount.id)
        .all()
    )
    results: list[dict] = []
    for acct in accounts:
        try:
            results.append(
                reconcile_account(db, acct, now=now,
                                  liveness_window=liveness_window)
            )
        except Exception as e:  # noqa: BLE001 — belt and braces; the inner
            # function already swallows, but a DB-level blowup on one
            # account must not kill the remaining ones.
            db.rollback()
            logger.warning("burst reconcile: uncaught failure for "
                           "account %s: %s", acct.id, e)
    return results

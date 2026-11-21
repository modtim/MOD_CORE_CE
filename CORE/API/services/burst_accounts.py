"""GH.2 — per-tenant cloud credential accounts for bursting into the
CUSTOMER's own cloud (launch-completion-megaplan group H, decomp/gh-burst.md).

This module owns the `TenantBurstAccount` lifecycle:

  * `create_account` / `list_accounts` / `update_account` /
    `soft_delete_account` — tenant-scoped CRUD. One LIVE account per
    (tenant, provider) — enforced by the partial unique index.
  * `probe_account` — the dry-run permission probe: resolve the tenant
    secret named by `secret_ref`, build the provider via
    `burst_provider.get_provider(provider, **creds)`, and call
    `list_tagged({"mod-burst": "mod-core"})` (read-only). Success →
    status='valid'; failure → status='invalid' with a REDACTED error
    string (the credential value is stripped before it is stored).

Credential rules (non-negotiable, per decomp/gh-burst.md):
  * the account stores the secret's NAME, never the value;
  * the secret value is resolved from the existing tenant secret store
    (crud.secret_cud) at probe/provision time only;
  * secret values are never logged and never returned by any reader in
    this module (`account_dict` is the ONLY serialization surface and it
    carries no secret material).

ISO 27001: soft-delete only (deleted_at); every mutation emits an
AuditEvent. An audit failure never breaks the operation it describes.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from crud.secret_cud import resolve_decrypted_secret_for_tenant
from services import burst_provider
from services import platform_settings  # leg A's shared _ce_unlimited helper

logger = logging.getLogger(__name__)

# The read-only probe lists exactly what MOD owns: instances tagged
# mod-burst=mod-core.
MOD_TAG = {burst_provider.MOD_TAG_KEY: burst_provider.mod_tag_value()}

EVENT_CREATED = "BURST_ACCOUNT_CREATED"
EVENT_UPDATED = "BURST_ACCOUNT_UPDATED"
EVENT_DELETED = "BURST_ACCOUNT_DELETED"
EVENT_PROBED = "BURST_ACCOUNT_PROBED"

STATUS_PENDING = "pending"
STATUS_VALID = "valid"
STATUS_INVALID = "invalid"


# --- helpers -----------------------------------------------------------------

def _audit(db: Session, *, tenant_id: int, user_id: Optional[int], event: str,
           **details: Any) -> None:
    """Never let an audit failure break the operation it describes."""
    try:
        db.add(models.AuditEvent(
            tenant_id=tenant_id, user_id=user_id,
            event_name=event, details=details,
        ))
    except Exception:
        logger.warning("burst accounts: audit emit failed for %s", event,
                       exc_info=True)


def _iso(dt: Any) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def account_dict(account: models.TenantBurstAccount) -> dict:
    """The ONLY serialization surface for a burst account.

    Deliberately contains NO secret material: `secret_ref` is the secret's
    NAME (safe to show), and nothing else can carry credential bytes.
    """
    return {
        "id": account.id,
        "tenant_id": account.tenant_id,
        "provider": account.provider,
        "secret_ref": account.secret_ref,
        "region": account.region,
        "limits": account.limits or {},
        "status": account.status,
        "last_probe_at": _iso(account.last_probe_at),
        "last_probe_error": account.last_probe_error,
        "created_at": _iso(account.created_at),
        "updated_at": _iso(account.updated_at),
    }


def _creds_from_secret(value: str) -> dict[str, Any]:
    """Parse the credential secret value into provider kwargs.

    Convention: the secret is a JSON object of provider kwargs (e.g.
    {"provider_key": "...", "secret_key": "..."} for libcloud, {} for the
    fake provider). A non-JSON plain string is treated as a bare
    `provider_key`. This function only ever returns the Parsed value —
    callers must redact it out of any error string.
    """
    try:
        data = json.loads(value)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass
    return {"provider_key": value}


def _redact(message: str, secret: Optional[str], limit: int = 500) -> str:
    """Strip the credential out of an error message before it is stored."""
    s = str(message)
    if secret:
        s = s.replace(secret, "[REDACTED]")
    return s[:limit]


# --- limits validation -------------------------------------------------------

# FIX-CE-UNLIMITED-E (CE has NO quantity caps; paid keeps every
# current value). This is a VALIDATION ceiling for a tenant's own
# `limits.max_instances`, not a default: on CE a tenant admin can raise the
# ceiling to the file's honest range top (100000, the same top MAX_GPU_HOURS_MONTH_LIMIT
# already uses); paid keeps 20 byte-for-byte. Leg A's shared `_ce_unlimited`
# helper (platform_settings.py) is REUSED - it asks is_ce_edition() and ANY
# probe failure returns the PAID value, so a broken probe can never loosen the
# paid ceiling. Leg C represents unlimited burst budgets as the validator
# ceiling (not 0: 0 is taken by "refuse every scale-up" in paid semantics), so
# the CE ceiling here is the same honest top, not a sentinel.
MAX_INSTANCES_LIMIT = platform_settings._ce_unlimited(20, unlimited=100000)
MAX_GPU_HOURS_MONTH_LIMIT = 100000
MAX_LIFETIME_HOURS_LIMIT = 100000
MAX_INSTANCE_TYPES = 50
MAX_NAME_LEN = 100


def _is_finite_number(value: Any) -> bool:
    """True for real int/float only: bool (an int subclass), NaN and +/-inf
    are all rejected. Never coerces, never accepts a str."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _validate_limits(limits: Optional[dict]) -> None:
    """Fail-closed validation of the `limits` JSONB policy block (B4 item 2).

    Unknown keys are rejected. Rules:
      max_instances            int, 0..MAX_INSTANCES_LIMIT (the validation
                               ceiling: 20 for paid, 100000 on Community
                               Edition; chosen by the edition probe
                               _ce_unlimited, not a platform setting)
      max_gpu_hours_month      int or float, 0..100000
      max_lifetime_hours       int or float, 0..100000
      allowed_instance_types   list of non-empty str (<=50 items, <=100 chars)
      gpu_class, resource_group  non-empty str (<=100 chars)
    bool, None, NaN, +/-inf, a str where a number is required, and negatives
    are rejected; nothing is ever coerced. An empty block is valid. Messages
    name the offending KEY and the rule only - never the submitted value.
    """
    if limits is None:
        return
    if not isinstance(limits, dict):
        raise ValueError("limits must be a JSON object")
    for key, value in limits.items():
        if not isinstance(key, str):
            raise ValueError("limits: keys must be strings")
        if value is None or isinstance(value, bool):
            raise ValueError(
                f"limits.{key}: must be a number, a string, or a list of "
                f"strings (not null or a boolean)"
            )
        if key == "max_instances":
            if not isinstance(value, int) or value < 0 or \
                    value > MAX_INSTANCES_LIMIT:
                raise ValueError(
                    f"limits.max_instances: must be an integer "
                    f"0..{MAX_INSTANCES_LIMIT}"
                )
        elif key in ("max_gpu_hours_month", "max_lifetime_hours"):
            ceiling = (MAX_GPU_HOURS_MONTH_LIMIT
                       if key == "max_gpu_hours_month"
                       else MAX_LIFETIME_HOURS_LIMIT)
            if not _is_finite_number(value) or value < 0 or value > ceiling:
                raise ValueError(
                    f"limits.{key}: must be a finite number 0..{ceiling}"
                )
        elif key == "allowed_instance_types":
            if not isinstance(value, list):
                raise ValueError(
                    "limits.allowed_instance_types: must be a list of strings"
                )
            if len(value) > MAX_INSTANCE_TYPES:
                raise ValueError(
                    f"limits.allowed_instance_types: at most "
                    f"{MAX_INSTANCE_TYPES} items"
                )
            for item in value:
                if not isinstance(item, str) or not item or len(item) > MAX_NAME_LEN:
                    raise ValueError(
                        "limits.allowed_instance_types: each item must be a "
                        f"non-empty string of at most {MAX_NAME_LEN} characters"
                    )
        elif key in ("gpu_class", "resource_group"):
            if not isinstance(value, str) or not value or len(value) > MAX_NAME_LEN:
                raise ValueError(
                    f"limits.{key}: must be a non-empty string of at most "
                    f"{MAX_NAME_LEN} characters"
                )
        else:
            raise ValueError(f"limits.{key}: unknown key")


# --- CRUD --------------------------------------------------------------------

def create_account(db: Session, *, tenant_id: int, user: models.User,
                   provider: str, secret_ref: str,
                   region: Optional[str] = None,
                   limits: Optional[dict] = None) -> models.TenantBurstAccount:
    """Create a burst account. `limits` is a JSONB policy block
    ({max_instances int 0..MAX_INSTANCES_LIMIT (the validation ceiling: 20
    for paid, 100000 on Community Edition; chosen by the edition probe
    _ce_unlimited, not a platform setting), max_gpu_hours_month number
    0..100000,
    max_lifetime_hours number 0..100000, allowed_instance_types list of
    non-empty str (<=50), gpu_class str, resource_group str}; unknown keys
    and out-of-range/odd-typed values are rejected).

    Raises ValueError when a LIVE account for (tenant, provider) already
    exists (soft-deleted tombstones don't block recreation), or when a
    `limits` value fails validation (before any row or audit event is
    written).
    """
    _validate_limits(limits)
    acct = models.TenantBurstAccount(
        tenant_id=tenant_id,
        provider=provider,
        secret_ref=secret_ref,
        region=region,
        limits=limits or {},
        status=STATUS_PENDING,
    )
    db.add(acct)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ValueError(
            f"a live burst account for provider '{provider}' already "
            f"exists for this tenant"
        )
    _audit(db, tenant_id=tenant_id, user_id=user.id, event=EVENT_CREATED,
           account_id=acct.id, provider=provider, secret_ref=secret_ref)
    db.commit()
    db.refresh(acct)
    return acct


def list_accounts(db: Session, *, tenant_id: int) -> list[models.TenantBurstAccount]:
    """All LIVE burst accounts for the tenant (never other tenants')."""
    return (
        db.query(models.TenantBurstAccount)
        .filter(
            models.TenantBurstAccount.tenant_id == tenant_id,
            models.TenantBurstAccount.deleted_at.is_(None),
        )
        .order_by(models.TenantBurstAccount.created_at)
        .all()
    )


def get_account(db: Session, *, account_id: int, tenant_id: int
                ) -> Optional[models.TenantBurstAccount]:
    """Tenant-scoped lookup — a foreign tenant's account reads as absent."""
    return (
        db.query(models.TenantBurstAccount)
        .filter(
            models.TenantBurstAccount.id == account_id,
            models.TenantBurstAccount.tenant_id == tenant_id,
            models.TenantBurstAccount.deleted_at.is_(None),
        )
        .first()
    )


def update_account(db: Session, *, account_id: int, tenant_id: int,
                   user: models.User,
                   provider: Optional[str] = None,
                   secret_ref: Optional[str] = None,
                   region: Optional[str] = None,
                   limits: Optional[dict] = None) -> models.TenantBurstAccount:
    """Update mutable fields. `None` means "leave unchanged".

    Changing provider / secret_ref / region invalidates any previous
    probe result (status → 'pending') — a probe only proves the
    credentials it ran with.
    """
    acct = get_account(db, account_id=account_id, tenant_id=tenant_id)
    if acct is None:
        return None
    _validate_limits(limits)

    changed = []
    credentials_changed = False
    if provider is not None and provider != acct.provider:
        acct.provider = provider
        changed.append("provider")
        credentials_changed = True
    if secret_ref is not None and secret_ref != acct.secret_ref:
        acct.secret_ref = secret_ref
        changed.append("secret_ref")
        credentials_changed = True
    if region is not None and region != acct.region:
        acct.region = region
        changed.append("region")
        credentials_changed = True
    if limits is not None and limits != (acct.limits or {}):
        acct.limits = limits
        changed.append("limits")

    if credentials_changed:
        acct.status = STATUS_PENDING
        acct.last_probe_error = None

    _audit(db, tenant_id=tenant_id, user_id=user.id, event=EVENT_UPDATED,
           account_id=acct.id, fields=changed)
    db.commit()
    db.refresh(acct)
    return acct


def soft_delete_account(db: Session, *, account_id: int, tenant_id: int,
                        user: models.User) -> Optional[models.TenantBurstAccount]:
    """Soft-delete (ISO 27001 — never a hard delete). Frees the
    (tenant, provider) name for recreation via the partial unique index."""
    acct = get_account(db, account_id=account_id, tenant_id=tenant_id)
    if acct is None:
        return None
    acct.deleted_at = datetime.now(timezone.utc)
    _audit(db, tenant_id=tenant_id, user_id=user.id, event=EVENT_DELETED,
           account_id=acct.id, provider=acct.provider)
    db.commit()
    db.refresh(acct)
    return acct


# --- probe -------------------------------------------------------------------

def probe_account(db: Session, account: models.TenantBurstAccount,
                  *, user: Optional[models.User] = None) -> models.TenantBurstAccount:
    """Dry-run permission probe (read-only).

    Resolves the tenant secret named by `secret_ref`, builds the provider
    via `burst_provider.get_provider(provider, **creds)`, and lists the
    MOD-tagged instances. Success → status='valid'; ANY exception →
    status='invalid' with a redacted error string (the credential value
    is stripped out before it touches the row or a log line).
    """
    now = datetime.now(timezone.utc)
    secret: Optional[str] = None
    try:
        secret = resolve_decrypted_secret_for_tenant(
            db, secret_name=account.secret_ref, tenant_id=account.tenant_id,
        )
        if secret is None:
            raise RuntimeError(
                f"secret '{account.secret_ref}' not found in the tenant "
                f"secret store"
            )
        provider = burst_provider.get_provider(
            account.provider, **_creds_from_secret(secret),
        )
        provider.list_tagged(MOD_TAG)
        new_status = STATUS_VALID
        error: Optional[str] = None
    except Exception as e:  # noqa: BLE001 — every failure is "invalid"
        new_status = STATUS_INVALID
        error = _redact(str(e), secret)
        # Redacted message is safe to log; the raw exception is NOT
        # (its traceback may contain the credential).
        logger.warning(
            "burst probe invalid for tenant=%s account=%s: %s",
            account.tenant_id, account.id, error,
        )

    account.status = new_status
    account.last_probe_at = now
    account.last_probe_error = error

    _audit(db, tenant_id=account.tenant_id,
           user_id=user.id if user is not None else None,
           event=EVENT_PROBED,
           account_id=account.id, provider=account.provider,
           status=new_status, probe_error=error)
    db.commit()
    db.refresh(account)
    return account


# --- read-only dashboard surface (GH.8a) --------------------------------------
# Sessions / fee ledger / summary for the cloud-burst Dashboard. Read-only,
# tenant-scoped on `current_user.tenant_id` — a foreign tenant's session or
# fee reads as absent. No secret material ever leaves this module.

# The meter (services/burst_metering._charge_and_advance) writes each fee
# debit's ledger note as "... (session {id}, ..." — `CreditLedgerEntry` has
# no session_id column, so the note is the only session linkage. Match it
# anchored so session 1 never matches the "session 12" note of 12.
_SESSION_NOTE_RE = re.compile(r"\(session (\d+)")


def _session_fee_sums(db: Session, *, tenant_id: int,
                      session_ids: list[int]) -> dict[int, int]:
    """ONE query: {session_id: total fee credits} for the tenant's
    BURST_MANAGEMENT_FEE ledger rows. The session id is parsed in Python
    from the ledger note the meter writes (anchored, so note "session 12"
    never lands on session 1); fee debits are stored negative
    (`CreditManager.deduct_credits` writes amount_in_credits = -charge), so
    credits CHARGED = sum of ABS(amount) per session. A session with no
    debits gets 0; exactly one query regardless of session count — no N+1."""
    if not session_ids:
        return {}
    rows = (
        db.query(
            models.CreditLedgerEntry.notes,
            func.abs(models.CreditLedgerEntry.amount_in_credits),
        )
        .filter(
            models.CreditLedgerEntry.tenant_id == tenant_id,
            models.CreditLedgerEntry.entry_type
            == models.LedgerEntryType.BURST_MANAGEMENT_FEE,
            models.CreditLedgerEntry.notes.isnot(None),
        )
        .all()
    )
    out: dict[int, int] = {sid: 0 for sid in session_ids}
    wanted = set(session_ids)
    for note, amt in rows:
        m = _SESSION_NOTE_RE.search(note)
        if m is None:
            continue
        sid = int(m.group(1))
        if sid in wanted:
            out[sid] += int(amt or 0)
    return out


def list_sessions(db: Session, *, tenant_id: int,
                  status: str = "active", limit: int = 50) -> list[dict]:
    """Tenant-scoped burst sessions, newest first.

    `status`: 'active' (the live meters only) or 'all' (any status).
    `provider` is joined from TenantBurstAccount — a soft-deleted account
    still names the provider of a session it launched. Each row's
    `fees_charged_credits` is the sum of that session's management-fee
    debits (0 if the meter never fired one)."""
    limit = max(1, min(int(limit), 1000))
    q = (
        db.query(
            models.BurstSession,
            models.TenantBurstAccount.provider.label("acct_provider"),
        )
        .outerjoin(
            models.TenantBurstAccount,
            models.TenantBurstAccount.id == models.BurstSession.account_id,
        )
        .filter(models.BurstSession.tenant_id == tenant_id)
    )
    if status != "all":
        q = q.filter(models.BurstSession.status == "active")
    sessions = (
        q.order_by(models.BurstSession.joined_at.desc(),
                   models.BurstSession.id.desc())
        .limit(limit).all()
    )
    fee_sums = _session_fee_sums(
        db, tenant_id=tenant_id,
        session_ids=[s.id for s, _ in sessions],
    )
    out = []
    for s, provider in sessions:
        out.append({
            "id": s.id,
            "account_id": s.account_id,
            "provider": provider,
            "instance_id": s.instance_id,
            "worker_name": s.worker_name,
            "gpu_class": s.gpu_class,
            "status": s.status,
            "joined_at": _iso(s.joined_at),
            "left_at": _iso(s.left_at),
            "fees_charged_credits": fee_sums.get(s.id, 0),
        })
    return out


def list_fee_ledger(db: Session, *, tenant_id: int,
                    limit: int = 100) -> list[dict]:
    """The tenant's burst management-fee ledger entries, newest first.

    `session_id` is recovered from the ledger note the meter writes
    (see `_SESSION_NOTE_RE`); `amount_credits` is the positive charge
    (debits are stored negative)."""
    limit = max(1, min(int(limit), 1000))
    rows = (
        db.query(models.CreditLedgerEntry)
        .filter(
            models.CreditLedgerEntry.tenant_id == tenant_id,
            models.CreditLedgerEntry.entry_type
            == models.LedgerEntryType.BURST_MANAGEMENT_FEE,
        )
        .order_by(models.CreditLedgerEntry.created_at.desc(),
                  models.CreditLedgerEntry.id.desc())
        .limit(limit)
        .all()
    )
    out = []
    for r in rows:
        m = _SESSION_NOTE_RE.search(r.notes or "")
        out.append({
            "id": r.id,
            "created_at": _iso(r.created_at),
            "session_id": int(m.group(1)) if m else None,
            "amount_credits": abs(int(r.amount_in_credits or 0)),
            "description": r.notes,
        })
    return out


def summary(db: Session, *, tenant_id: int) -> dict:
    """Cloud-burst Dashboard rollup: live accounts, active sessions, and
    management fees charged in the current UTC month."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    accounts = (
        db.query(func.count(models.TenantBurstAccount.id))
        .filter(
            models.TenantBurstAccount.tenant_id == tenant_id,
            models.TenantBurstAccount.deleted_at.is_(None),
        )
        .scalar() or 0
    )
    active_sessions = (
        db.query(func.count(models.BurstSession.id))
        .filter(
            models.BurstSession.tenant_id == tenant_id,
            models.BurstSession.status == "active",
        )
        .scalar() or 0
    )
    fees = (
        db.query(func.sum(func.abs(models.CreditLedgerEntry.amount_in_credits)))
        .filter(
            models.CreditLedgerEntry.tenant_id == tenant_id,
            models.CreditLedgerEntry.entry_type
            == models.LedgerEntryType.BURST_MANAGEMENT_FEE,
            models.CreditLedgerEntry.created_at >= month_start,
        )
        .scalar() or 0
    )
    return {
        "accounts": int(accounts),
        "active_sessions": int(active_sessions),
        "fees_this_month_credits": int(fees),
    }

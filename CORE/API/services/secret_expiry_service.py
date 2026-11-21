"""FX.117 p4a/p4b — per-secret expiry (the RECORD, the gate, the admin API, the reminder job).

FX.117 (owner Q104=A) ships the customer secret store. p3b gave a tenant admin
a per-secret allowed-workflows scope as a NEW table (`secret_workflow_scopes`)
mirroring the p2 `Secret.allowed_workflow_ids` bag; p4 does exactly the same
shape for expiry: the expiry date an admin sets lives in the NEW table
`secret_expiries` (models.SecretExpiry) and the p2 `Secret.expires_at` bag stays
as the admin-facing view this table mirrors.

Why a new table: models.py is bind-mounted LIVE and shared by every lane, so
NO column may be added to the existing `secrets` table — an expiry is a
RECORD the compliance trail can read (when the expiry was set, when it was
cleared, when the owner was reminded), and ISO 27001 forbids hard deletes, so
clearing an expiry soft-deletes its row and stamps `deleted_at`.

Four halves, one module:

  1. The RECORD — `models.SecretExpiry` (table `secret_expiries`), at most ONE
     live row per secret (partial unique index WHERE deleted_at IS NULL), the
     secret_id FK carrying NO `ON DELETE CASCADE` clause.
  2. The GATE — `assert_run_expiry` at the run's secret-resolution point
     (`routers/worker_tasks.py` resolve_task_secrets, right beside p3b's
     `assert_run_scope`). An EXPIRED secret is refused the same way an
     out-of-scope one is: the value is NOT injected, a WARNING names the
     secret only (never the value), an audited SECRET_ACCESS_DENIED row with
     reason=expired is written, and the node fails with
     `SecretExpiryError.detail` ("secret <name> has expired"). Gated by the
     `secrets.block_expired` platform setting (default true).
  3. The ADMIN API — GET/PUT /secrets/{secret_id}/expiry (routers/secrets.py,
     the same per-secret gate the existing secrets routes use; the tenant
     comes from the token, another tenant's secret is a 404). PUT null clears
     the expiry via a soft delete.
  4. The REMINDER JOB — `expiry_reminder_tick` (services/scheduler.py job
     `fx117_secret_expiry_reminders`): secrets expiring within
     `secrets.expiry_reminder_days` (int, default 14) or already expired get
     their tenant admins notified through the existing in-app notification +
     email path, AT MOST ONCE per `secrets.expiry_reminder_interval_hours`
     (int, default 24) via `last_reminded_at`, each send audited
     SECRET_EXPIRY_REMINDER_SENT. The message carries the secret NAME only,
     never the value.

Masking (FX.117 p3a, `services.secret_masking.run_secret_values`) is
deliberately NOT touched here: a secret whose value was refused must still be
masked in persisted node text — masking protects the log, it does not
authorise the value.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone

import models
from services._base import NotFoundError, ServiceError

logger = logging.getLogger(__name__)

# Audit labels carried by models.AuditAction (and the live `auditaction` PG
# enum via migrations/20261007zzzf_fx117_p4_secret_expiries.py). AuditEvent
# rows use the same UPPERCASE name in `event_name` — the label the enum-sync
# guard requires.
EXPIRY_AUDIT_EVENT = "SECRET_EXPIRY_SET"
REMINDER_AUDIT_EVENT = "SECRET_EXPIRY_REMINDER_SENT"
DENIAL_AUDIT_EVENT = "SECRET_ACCESS_DENIED"
DENIAL_REASON = "expired"

# Platform settings (services/platform_settings.py, group "secrets"). No env
# vars: DB-backed platform settings, re-read every pass so switching needs no
# restart.
BLOCK_KEY = "secrets.block_expired"
REMINDER_DAYS_KEY = "secrets.expiry_reminder_days"
INTERVAL_HOURS_KEY = "secrets.expiry_reminder_interval_hours"
DEFAULT_REMINDER_DAYS = 14
DEFAULT_INTERVAL_HOURS = 24

# FX.117 p4c (gov wrap) — the gov/DoD secret-lifetime ceiling. The profile
# list is the SAME one services/credential_lifetime.GOV_PROFILES,
# services/tenant_alert_rules.GOV_PROFILES and
# services/security_incidents.GOV_PROFILES use: an install on a gov profile
# caps EVERY tenant, so the read is platform-level (no tenant override of the
# profile list).
GOV_PROFILES = ("cmmc", "fedramp_high_il5")
GOV_MAX_LIFETIME_KEY = "secrets.gov_max_lifetime_days"
DEFAULT_GOV_MAX_LIFETIME_DAYS = 365
MISSING_EXPIRY_AUDIT_EVENT = "SECRET_MISSING_EXPIRY_REPORTED"
MISSING_EXPIRY_REASON = "missing_expiry"


class BadRequestError(ServiceError):
    """Payload shape the caller must fix (router maps -> 400)."""

    def __init__(self, message: str = "Bad request", *, details: None = None):
        super().__init__(message, code="bad_request", details=details)


class ForbiddenError(ServiceError):
    """Caller lacks the permission for this action (router maps -> 403)."""

    def __init__(self, message: str = "Forbidden", *, details: None = None):
        super().__init__(message, code="forbidden", details=details)


def _utc(value) -> Optional[datetime]:
    """Normalise a DB timestamp to UTC (a naive timestamptz read back is
    naive; treat it as UTC so a comparison never raises)."""
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _live_rows(db: Session, secret_id: int) -> list:
    """Every LIVE expiry row for one secret (tombstones excluded).

    `SecretExpiry` is not in the global soft-delete filter list in database.py,
    so the `deleted_at` predicate is written here explicitly — and every reader
    of this table goes through this helper, so no callsite can accidentally
    honour a cleared (soft-deleted) expiry.
    """
    return (
        db.query(models.SecretExpiry)
        .filter(
            models.SecretExpiry.secret_id == int(secret_id),
            models.SecretExpiry.deleted_at.is_(None),
        )
        .all()
    )


def get_expiry(db: Session, secret_id: int) -> Optional[datetime]:
    """The secret's current expiry (None = no expiry).

    The TABLE is authoritative; the p2 `Secret.expires_at` bag mirrors it for
    the admin UI. With the (unexpected) presence of several live rows the
    FURTHEST one wins, so a partially-written replace can never shorten a
    tenant's protection.
    """
    rows = _live_rows(db, secret_id)
    if not rows:
        return None
    stamps = [_utc(r.expires_at) for r in rows]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return None
    return max(stamps)


def is_expired(db: Session, secret_id: int, *, now: Optional[datetime] = None) -> bool:
    """True when the secret's live expiry is at or before `now`.

    No live row (or a NULL/absent expires_at) = no expiry = never expired.
    """
    exp = get_expiry(db, secret_id)
    if exp is None:
        return False
    return exp <= (now or _now())


def days_left(db: Session, secret_id: int, *, now: Optional[datetime] = None) -> Optional[float]:
    """Whole+fractional days until expiry (negative once past). None = no expiry."""
    exp = get_expiry(db, secret_id)
    if exp is None:
        return None
    return (exp - (now or _now())).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# The GATE — run-time injection refusal (p4a enforcement)
# ---------------------------------------------------------------------------


def _block_expired_on(db: Session) -> bool:
    """The `secrets.block_expired` switch (default TRUE: an expired secret is
    refused at run-time injection). A settings read failure keeps enforcement
    ON — silently injecting an expired secret is the unsafe direction, so a
    broken read must never open the gate.

    FX.117 p4c — under a gov/DoD compliance profile the refusal is FORCED:
    the profile force is re-derived from the active profile list here, so
    even a platform-scope write that turned the switch off (which the
    profile lock already refuses) cannot open the gate."""
    if active_gov_profile(db) is not None:
        return True
    import services.platform_settings as psr
    try:
        return bool(psr.get_setting(BLOCK_KEY, db=db))
    except Exception as e:  # noqa: BLE001 — fail closed (enforce)
        logger.warning("secret_expiry_service: block_expired read failed "
                       "(enforcing): %s", e)
        return True


def active_gov_profile(db: Session) -> Optional[str]:
    """The active gov/DoD compliance profile id, or None (FX.117 p4c).

    The SAME mechanism services/credential_lifetime.active_gov_profile and
    services/tenant_alert_rules.active_gov_profile use: the profile list is
    platform-level (an install on a gov profile caps every tenant), the
    profile ids are the shared GOV_PROFILES tuple, and the CE carve (no
    services/compliance_profiles.py module) means no gov profile."""
    from utils.paid_import import optional_paid
    cprof = optional_paid("services.compliance_profiles")
    if cprof is None:
        return None
    try:
        for profile in cprof.active_profiles(db):
            if profile in GOV_PROFILES:
                return profile
    except Exception:  # noqa: BLE001 — read-side guard must not raise
        logger.warning("secret_expiry_service: gov profile lookup failed")
    return None


def gov_max_lifetime_days(db: Session) -> Optional[int]:
    """The gov/DoD secret-lifetime ceiling in days (None = no gov profile).

    `secrets.gov_max_lifetime_days` (int, default 365). A failed/garbage read
    falls back to the default: silently letting a gov install carry an
    unbounded secret lifetime is the unsafe direction, so the ceiling never
    disappears because the setting could not be read."""
    if active_gov_profile(db) is None:
        return None
    import services.platform_settings as psr
    try:
        value = int(psr.get_setting(GOV_MAX_LIFETIME_KEY, db=db))
    except Exception as e:  # noqa: BLE001 — fail closed (default ceiling)
        logger.warning("secret_expiry_service: gov-max-lifetime read failed "
                       "(using default %d): %s",
                       DEFAULT_GOV_MAX_LIFETIME_DAYS, e)
        return DEFAULT_GOV_MAX_LIFETIME_DAYS
    if value < 1:
        logger.warning("secret_expiry_service: gov-max-lifetime read %s "
                       "(using default %d)", value,
                       DEFAULT_GOV_MAX_LIFETIME_DAYS)
        return DEFAULT_GOV_MAX_LIFETIME_DAYS
    return value


class GovExpiryError(ServiceError):
    """FX.117 p4c — a secret write that would leave the tenant secret without
    a compliant expiry while a gov/DoD profile is active. The ROUTER maps it
    to 422 (the payload is what the install's posture forbids)."""

    def __init__(self, message: str, *, details: None = None):
        super().__init__(message, code="gov_secret_expiry_required",
                         details=details)


def assert_gov_expiry(db: Session, tenant_id: Optional[int],
                      expires_at: Optional[datetime],
                      *, secret_name: Optional[str] = None,
                      now: Optional[datetime] = None,
                      action: str = "Creating") -> None:
    """The p4c gov gate for a secret CREATE/UPDATE: raise
    ``GovExpiryError`` (router -> 422) when a gov/DoD profile is active and
    the secret would end up with NO expiry, or one further out than
    ``secrets.gov_max_lifetime_days``.

    Never fires when no gov profile is active (a non-gov install is
    unchanged: expiry stays optional, the p2 ``secrets.max_secret_age_days``
    ceiling is the only limit). Pure validation — it runs BEFORE the write,
    so nothing is half-committed when it refuses."""
    max_days = gov_max_lifetime_days(db)
    if max_days is None:
        return None
    now = now or _now()
    name = secret_name or "the secret"
    if expires_at is None:
        raise GovExpiryError(
            f"{action} {name} without an expiry is refused on a gov/DoD "
            f"compliance profile: every tenant secret must carry an expiry "
            f"at most {max_days} days out "
            f"(secrets.gov_max_lifetime_days). Supply expires_at."
        )
    exp = _utc(expires_at)
    if exp is None:
        raise GovExpiryError(
            f"{action} {name} with an unreadable expiry is refused on a "
            f"gov/DoD compliance profile: supply expires_at as an ISO-8601 "
            f"timestamp at most {max_days} days out."
        )
    if exp <= now:
        raise GovExpiryError(
            f"{action} {name} with an expiry in the past is refused on a "
            f"gov/DoD compliance profile: the secret would be dead on "
            f"arrival. Supply a future expires_at at most {max_days} days "
            f"out."
        )
    if exp > now + timedelta(days=max_days):
        raise GovExpiryError(
            f"{action} {name} with an expiry of {exp.isoformat()} exceeds "
            f"the gov/DoD secret-lifetime ceiling of {max_days} days "
            f"(secrets.gov_max_lifetime_days). Latest allowed expiry is "
            f"{(now + timedelta(days=max_days)).isoformat()}."
        )
    return None


def _audit_denial(db: Session, db_secret: "models.Secret", secret_name: str,
                  *, workflow_run_id: Optional[int] = None,
                  node_id: Optional[str] = None) -> None:
    """Audit an expiry DENIAL. Best-effort: an audit failure never changes the
    decision (the value is withheld either way).

    Mirrors `services.secret_workflow_scope_service._audit_denial` and
    `services.secrets_service._audit_secret_access_denied` — same event_name,
    same shape — so the p2 by-name gate, the p3b table gate and this p4 gate
    produce one indistinguishable denial row in the audit trail. Carries the
    secret NAME, its id and the reason only; the value is never in an audit
    row.
    """
    details = {
        "secret_id": db_secret.id,
        "secret_name": secret_name,
        "reason": DENIAL_REASON,
    }
    if workflow_run_id is not None:
        details["workflow_run_id"] = workflow_run_id
    if node_id:
        details["node_id"] = node_id
    try:
        db.add(
            models.AuditEvent(
                tenant_id=getattr(db_secret, "tenant_id", None),
                user_id=None,
                event_name=DENIAL_AUDIT_EVENT,
                details=details,
            )
        )
        db.commit()
    except Exception as e:  # noqa: BLE001 — audit is a nicety, the denial stands
        logger.debug("Expiry-denial audit failed for secret %s: %s",
                     db_secret.id, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def assert_run_expiry(db: Session,
                      db_secret: "models.Secret",
                      secret_name: str,
                      *,
                      workflow_run_id: Optional[int] = None,
                      node_id: Optional[str] = None) -> bool:
    """The p4a run-expiry gate. Returns True when the value may be injected.

    Called at the run's secret-resolution point (`routers/worker_tasks.py`
    resolve_task_secrets, right beside p3b's `assert_run_scope`). When the
    secret has a live expiry row at or before now and `secrets.block_expired`
    is on: a WARNING naming the secret ONLY (never the value), an audited
    SECRET_ACCESS_DENIED row with reason=expired, and False — the caller
    withholds the value and fails the node with `SecretExpiryError.detail`
    ("secret <name> has expired"). Never raises: the caller is a batch
    resolver that must keep resolving the other refs of the same task.
    """
    if not _block_expired_on(db):
        return True
    if not is_expired(db, db_secret.id):
        return True

    logger.warning(
        "Secret '%s' (id %s) has expired (expires_at %s) — run %s is NOT "
        "allowed its value; value NOT injected.",
        secret_name, db_secret.id,
        (get_expiry(db, db_secret.id) or _now()).isoformat(),
        workflow_run_id,
    )
    _audit_denial(db, db_secret, secret_name,
                  workflow_run_id=workflow_run_id, node_id=node_id)
    return False


class SecretExpiryError(ServiceError):
    """The node-facing refusal (the worker fails the node with `.message`).

    Raised nowhere — the resolver returns it as a value so one expired ref
    cannot abort the resolution of the task's other refs.
    """

    def __init__(self, message: str, *, details: None = None):
        super().__init__(message, code="secret_expired", details=details)


def node_error(secret_name: str) -> str:
    """The exact node-failure message the worker reports (secret NAME only)."""
    return f"secret {secret_name} has expired"


# ---------------------------------------------------------------------------
# The ADMIN API: read / set / clear the expiry
# ---------------------------------------------------------------------------


def _crud_get_secret_by_id(db: Session, secret_id: int, tenant_id: int):
    """Tenant-scoped secret lookup (crud/secret_cud.get_secret_by_id)."""
    from crud.secret_cud import get_secret_by_id
    return get_secret_by_id(db, secret_id=secret_id, tenant_id=tenant_id)


def _load_secret_for_admin(db: Session, secret_id: int,
                           current_user: "models.User") -> "models.Secret":
    """The tenant-scoped lookup + the same per-secret gate the existing
    secrets routes use (services/secrets_service.get_secret / update_secret).

    Another tenant's secret is a 404 (NotFoundError), never a 403 — the
    tenant comes from the token, so a foreign secret is simply not there.
    """
    import services.secrets_service as secrets_service
    db_secret = _crud_get_secret_by_id(db, secret_id, current_user.tenant_id)
    if db_secret is None:
        raise NotFoundError("Secret not found.")
    if not secrets_service.can_access_secret(db, current_user, secret_id, "edit"):
        raise ForbiddenError(
            "You don't have edit access to this secret."
        )
    return db_secret


def get_expiry_for_admin(db: Session, secret_id: int,
                         current_user: "models.User") -> Optional[datetime]:
    """Admin read: the secret's expiry (None = no expiry).

    Same per-secret gate the existing GET /secrets/{id} route uses ('view');
    another tenant's secret is a 404 (tenant from the token).
    """
    import services.secrets_service as secrets_service
    db_secret = _crud_get_secret_by_id(db, secret_id, current_user.tenant_id)
    if db_secret is None:
        raise NotFoundError("Secret not found.")
    if not secrets_service.can_access_secret(db, current_user, secret_id, "view"):
        raise ForbiddenError("You don't have access to this secret.")
    return get_expiry(db, db_secret.id)


def _parse_expires_at(value) -> Optional[datetime]:
    """Coerce a JSON payload value to a UTC datetime (None = clear).

    A string is parsed as ISO-8601 (a trailing Z accepted); a naive timestamp
    is taken as UTC. Anything else is a payload shape the caller must fix
    (BadRequestError).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        out = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            out = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise BadRequestError(
                "expires_at must be an ISO-8601 timestamp (or null to clear)."
            )
    else:
        raise BadRequestError(
            "expires_at must be an ISO-8601 timestamp (or null to clear)."
        )
    return _utc(out)


def set_expiry_for_admin(db: Session, secret_id: int, expires_at,
                         current_user: "models.User"):
    """Admin write: (db_secret, new_expiry, previous_expiry, added, removed).

    `expires_at` null clears the expiry (soft delete of the live row). A
    timestamp SETS it — including one already in the past, which cuts the
    secret off from run-time injection immediately (the value is never
    hard-deleted; expiry is a soft state, ISO 27001).
    """
    db_secret = _load_secret_for_admin(db, secret_id, current_user)
    new_exp = _parse_expires_at(expires_at)
    # FX.117 p4c (gov wrap) — under a gov/DoD profile the expiry may not be
    # cleared and may not exceed secrets.gov_max_lifetime_days (GovExpiryError
    # -> 422 at the router). Checked before any row is soft-deleted, so a
    # refusal never disturbs the existing record.
    assert_gov_expiry(db, db_secret.tenant_id, new_exp,
                      secret_name=db_secret.name, action="Updating")
    previous = get_expiry(db, db_secret.id)
    added, removed = set_expiry(db, db_secret, new_exp, current_user)
    return db_secret, new_exp, previous, added, removed


def set_expiry(db: Session,
               db_secret: "models.Secret",
               new_expiry: Optional[datetime],
               current_user: "models.User") -> tuple:
    """Replace a secret's expiry: soft-delete the live row, insert the new one.

    `new_expiry` None clears it (the live row is soft-deleted and stamped, so
    the compliance trail keeps the expiry the secret once had — nothing is
    hard-deleted). Every other live row for the secret is soft-deleted too, so
    the table keeps at most ONE live row per secret (the partial unique index
    enforces it at the DB level, and a re-set never collides with its own
    tombstone).

    Also mirrors the value into `Secret.expires_at` (the p2 admin-facing bag
    read by the admin UI and the p2 by-name gate) so the table and that bag
    can never disagree. Audits one SECRET_EXPIRY_SET row with the old/new
    expiry, the added/removed row ids and the acting user — never a secret
    value. Returns (added_row_ids, removed_row_ids).
    """
    tenant_id = int(db_secret.tenant_id)
    existing = _live_rows(db, db_secret.id)
    old = get_expiry(db, db_secret.id)
    now = _now()

    removed_ids = []
    for row in existing:
        row.deleted_at = now
        row.deleted_by_user_id = (current_user.id if current_user else None)
        removed_ids.append(int(row.id))

    added_ids = []
    if new_expiry is not None:
        fresh = models.SecretExpiry(
            tenant_id=tenant_id,
            secret_id=int(db_secret.id),
            expires_at=new_expiry,
            created_by_user_id=(current_user.id if current_user else None),
        )
        db.add(fresh)
        db.commit()
        try:
            added_ids.append(int(fresh.id))
        except Exception:  # noqa: BLE001 — id not resolved: keep the rest
            pass
    db_secret.expires_at = new_expiry
    db.commit()

    try:
        db.add(
            models.AuditEvent(
                tenant_id=tenant_id,
                user_id=(current_user.id if current_user else None),
                event_name=EXPIRY_AUDIT_EVENT,
                details={
                    "secret_id": db_secret.id,
                    "secret_name": db_secret.name,
                    "previous_expires_at": (old.isoformat() if old else None),
                    "expires_at": (new_expiry.isoformat() if new_expiry
                                   else None),
                    "cleared": new_expiry is None,
                    "added_row_ids": added_ids,
                    "removed_row_ids": removed_ids,
                },
            )
        )
        db.commit()
    except Exception as e:  # noqa: BLE001 — audit is a nicety
        logger.warning("Expiry audit write failed for secret %s: %s",
                       db_secret.id, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return added_ids, removed_ids


# ---------------------------------------------------------------------------
# The REMINDER JOB (p4b) — tenant admins, once per interval, name only
# ---------------------------------------------------------------------------


def _reminder_days(db: Session) -> int:
    """`secrets.expiry_reminder_days` (int, default 14).

    The p2 setting is a LIST of reminder day-marks; p4b wants a single lead
    band, so the WIDEST mark is the band. A non-int/failed read falls back to
    the default so a settings hiccup never silences the reminders."""
    import services.platform_settings as psr
    try:
        value = psr.get_setting(REMINDER_DAYS_KEY, db=db)
    except Exception as e:  # noqa: BLE001 — default band
        logger.warning("secret_expiry_service: reminder-days read failed "
                       "(using default %d): %s", DEFAULT_REMINDER_DAYS, e)
        return DEFAULT_REMINDER_DAYS
    if isinstance(value, bool):
        return DEFAULT_REMINDER_DAYS
    if isinstance(value, int):
        return max(0, int(value))
    if isinstance(value, list):
        marks = [d for d in value
                 if isinstance(d, int) and not isinstance(d, bool) and d >= 0]
        if marks:
            return max(marks)
    return DEFAULT_REMINDER_DAYS


def _interval_hours(db: Session) -> int:
    """`secrets.expiry_reminder_interval_hours` (int, default 24)."""
    import services.platform_settings as psr
    try:
        value = psr.get_setting(INTERVAL_HOURS_KEY, db=db)
    except Exception as e:  # noqa: BLE001 — default interval
        logger.warning("secret_expiry_service: interval-hours read failed "
                       "(using default %d): %s", DEFAULT_INTERVAL_HOURS, e)
        return DEFAULT_INTERVAL_HOURS
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_INTERVAL_HOURS
    return max(1, int(value))


def _tenant_admins(db: Session, tenant_id: int) -> List["models.User"]:
    """Tenant admins = active inviter-chain roots with an email (the codebase
    convention services/log_streams.py::_tenant_admin_emails and
    services/node_deprecation_notices.py::_tenant_admins use verbatim).

    1788f — CE carve: crud.stripe_webhooks is a paid module, so CE has no Stripe
    helper to resolve the inviter-chain roots. CE default = no admins resolved
    (the caller notifies nobody); the expiry scan itself still runs and no
    notification rows are fabricated."""
    from utils.paid_import import optional_paid
    _stripe = optional_paid("crud.stripe_webhooks")
    if _stripe is None:
        return []
    return [u for u in _stripe.list_active_tenant_admin_users(db, tenant_id) if u.email]


def _due_secrets(db: Session, now: datetime, lead_days: int) -> list:
    """(secret, expiry) pairs the tenant admins must be told about.

    The band is `expires_at <= now + lead` — INCLUDING already-expired
    secrets (the task says "expiring within N days or already expired"), so a
    secret that went dark keeps its owners in the loop until they replace it.
    Secrets with no live expiry row are never candidates.

    The join is EXPLICIT (`secret_id == secrets.id`): a two-collection query
    without it is a cartesian product (the FX.117 runs-list bug class), and
    the ix_secret_expiries_expires_at index is what makes the band scan cheap.
    Returns (secret, expiry, live_row) triples so the caller never re-queries.
    """
    upper = now + timedelta(days=max(0, int(lead_days)))
    rows = (
        db.query(models.SecretExpiry, models.Secret)
        .filter(models.SecretExpiry.deleted_at.is_(None),
                models.Secret.deleted_at.is_(None),
                models.SecretExpiry.expires_at <= upper,
                models.SecretExpiry.secret_id == models.Secret.id)
        .all()
    )
    out = []
    for exp_row, sec in rows:
        stamp = _utc(exp_row.expires_at)
        if stamp is None:
            continue
        out.append((sec, stamp, exp_row))
    return out


def _reminder_due(row, now: datetime, interval_hours: int) -> bool:
    """At most ONE reminder per `interval_hours` per secret (via last_reminded_at).

    NULL last_reminded_at = never reminded = due. A row whose stamp is
    unreadable is treated as due (a broken record must not silence a warning
    forever; the fresh send repairs it).
    """
    last = _utc(getattr(row, "last_reminded_at", None))
    if last is None:
        return True
    return (now - last) >= timedelta(hours=max(1, int(interval_hours)))


def _reminder_message(sec: "models.Secret", expiry: datetime,
                      now: datetime) -> str:
    """The reminder text: the secret NAME + the date only, NEVER the value."""
    days_left = int((expiry - now).total_seconds() // 86400)
    if expiry <= now:
        return (
            f"Secret '{sec.name}' has EXPIRED (expiry {expiry.isoformat()}). "
            f"MOD no longer injects it into workflow runs — runs that need it "
            f"fail with 'secret {sec.name} has expired'. Replace it "
            f"(PATCH /secrets/{sec.id} or PUT /secrets/{sec.id}/expiry) to "
            f"restore run-time access."
        )
    return (
        f"Secret '{sec.name}' expires on {expiry.isoformat()} "
        f"({max(days_left, 0)} day(s) away). Runs that need it will fail once "
        f"the date passes. Replace it (PATCH /secrets/{sec.id} or PUT "
        f"/secrets/{sec.id}/expiry) to keep run-time access."
    )


def _notify_admins(db: Session, tenant_id: int, subject: str, body: str) -> int:
    """In-app notification + email to every tenant admin (the existing path:
    models.Notification rows + services/email_send_api.prepare_send, the same
    pair services/log_streams.py::_alert_tenant_admins uses).

    Returns the number of admins notified. Never raises: a failed email leaves
    the in-app notification in place and is logged.
    """
    admins = _tenant_admins(db, tenant_id)
    sent = 0
    for admin in admins:
        try:
            db.add(models.Notification(
                recipient_user_id=admin.id,
                notification_type="secret_expiring",
                message=body,
            ))
            db.commit()
            sent += 1
        except Exception as e:  # noqa: BLE001 — one admin's row must not stop others
            db.rollback()
            logger.warning("secret_expiry_service: in-app notify failed for "
                           "admin %s (tenant %s): %s", admin.id, tenant_id, e)
    if not admins:
        return 0
    try:
        from services import email_send_api
        email_send_api.prepare_send(
            db,
            tenant_id=tenant_id,
            user_id=None,
            app_id=None,
            to=sorted({a.email for a in admins if a.email}),
            cc=[],
            bcc=[],
            subject=subject,
            text=body,
            html=f"<p>{body}</p>",
            attachments=None,
        )
    except Exception as e:  # noqa: BLE001 — no SMTP config (409) / limits: in-app still stands
        logger.info("secret_expiry_service: email send not completed for "
                    "tenant %s: %s", tenant_id, e)
    return sent


def _audit_reminder(db: Session, sec: "models.Secret", expiry: datetime,
                    *, days_left: Optional[int], expired: bool,
                    admins_notified: int) -> None:
    """One SECRET_EXPIRY_REMINDER_SENT row per send (the compliance evidence
    AND, together with `last_reminded_at`, the exactly-once record). Carries
    the secret NAME, its id, the expiry date, the lead and how many admins
    were told — NEVER a secret value."""
    try:
        db.add(
            models.AuditEvent(
                tenant_id=sec.tenant_id,
                user_id=None,
                event_name=REMINDER_AUDIT_EVENT,
                details={
                    "secret_id": sec.id,
                    "secret_name": sec.name,
                    "expires_at": expiry.isoformat(),
                    "days_left": days_left,
                    "expired": expired,
                    "admins_notified": admins_notified,
                },
            )
        )
        db.commit()
    except Exception as e:  # noqa: BLE001 — audit is best-effort
        logger.warning("secret_expiry_service: reminder audit write failed "
                       "for secret %s: %s", sec.id, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _missing_expiry_secrets(db: Session) -> list:
    """FX.117 p4c — live tenant secrets that carry NO expiry (gov report).

    Only candidates while a gov/DoD profile is active (the caller checks).
    A secret with a tombstoned (cleared) expiry counts as missing — the
    record keeps the history, the live state has none. Ordered by id so the
    report is deterministic. Returns Secret rows; the report carries the
    NAME only, never a value."""
    live_ids = {
        int(r.secret_id)
        for r in db.query(models.SecretExpiry)
        .filter(models.SecretExpiry.deleted_at.is_(None)).all()
    }
    rows = (
        db.query(models.Secret)
        .filter(models.Secret.deleted_at.is_(None))
        .all()
    )
    return sorted(
        [sec for sec in rows
         if int(sec.id) not in live_ids and _utc(sec.expires_at) is None],
        key=lambda s: int(s.id),
    )


def _missing_expiry_report(db: Session, tenant_id: int,
                           names: List[str]) -> str:
    """The gov "missing expiry" body: secret NAMES only, never values."""
    listed = ", ".join(names)
    return (
        f"Secrets in this tenant have NO expiry, which a gov/DoD compliance "
        f"profile forbids: {listed}. They are NOT auto-expired and their "
        f"values are still injected — set an expiry (PUT "
        f"/secrets/{{id}}/expiry, or PATCH /secrets/{{id}}) within "
        f"secrets.gov_max_lifetime_days to satisfy the posture."
    )


def expiry_reminder_tick(db: Session, *, now: Optional[datetime] = None
                         ) -> dict:
    """One scheduled reminder pass (job id fx117_secret_expiry_reminders).

    Finds every secret whose live expiry is within `secrets.expiry_reminder_days`
    (or already past), notifies the tenant admins through the in-app
    notification + email path AT MOST ONCE per
    `secrets.expiry_reminder_interval_hours` — the `last_reminded_at` stamp on
    the live `secret_expiries` row is the dedupe record — and audits each send
    SECRET_EXPIRY_REMINDER_SENT. The message carries the secret NAME only,
    never the value.

    Returns counters; never raises per-secret (a failed send leaves the stamp
    unwritten so the next pass retries).
    """
    now = now or _now()
    lead = _reminder_days(db)
    interval = _interval_hours(db)
    out = {"considered": 0, "sent": 0, "skipped": 0, "expired": 0}
    seen: set = set()
    for sec, expiry, primary in _due_secrets(db, now, lead):
        if int(sec.id) in seen:
            # The partial unique index keeps ONE live row per secret, but a
            # secret is reminded at most once per pass regardless.
            continue
        seen.add(int(sec.id))
        out["considered"] += 1
        if not _reminder_due(primary, now, interval):
            out["skipped"] += 1
            continue
        expired = expiry <= now
        body = _reminder_message(sec, expiry, now)
        subject = (f"MOD: secret '{sec.name}' "
                   f"{'has expired' if expired else 'is expiring'}")
        notified = _notify_admins(db, int(sec.tenant_id), subject, body)
        primary.last_reminded_at = now
        db.commit()
        _audit_reminder(db, sec, expiry,
                        days_left=int((expiry - now).total_seconds() // 86400),
                        expired=expired, admins_notified=notified)
        out["sent"] += 1
        if expired:
            out["expired"] += 1
        logger.info("fx117_secret_expiry_reminders: secret %s (%s) reminded "
                    "(expired=%s, admins=%d)",
                    sec.id, sec.name, expired, notified)

    # FX.117 p4c — the gov half of the reminder job: secrets that carry NO
    # expiry at all are reported to the tenant admins as "missing expiry"
    # (name only). They are NEVER auto-expired: the value keeps being
    # injected, the compliance trail just learns the posture gap exists.
    # There is no live `secret_expiries` row to stamp for these, so the
    # exactly-once record is the audit row itself: at most one report per
    # tenant per secrets.expiry_reminder_interval_hours.
    out["missing_expiry_reported"] = 0
    if active_gov_profile(db) is not None:
        by_tenant: dict = {}
        for sec in _missing_expiry_secrets(db):
            by_tenant.setdefault(int(sec.tenant_id), []).append(sec)
        for tenant_id, secs in sorted(by_tenant.items()):
            if _missing_expiry_recently_reported(db, tenant_id, now,
                                                 interval):
                continue
            names = [str(s.name) for s in secs]
            body = _missing_expiry_report(db, tenant_id, names)
            notified = _notify_admins(
                db, tenant_id,
                "MOD: secrets missing an expiry (gov compliance profile "
                "active)", body)
            _audit_missing_expiry(db, tenant_id, names, notified)
            out["missing_expiry_reported"] += len(secs)
            logger.info("fx117_secret_expiry_reminders: tenant %s reported "
                        "%d secret(s) missing an expiry (gov posture)",
                        tenant_id, len(secs))
    return out


def _missing_expiry_recently_reported(db: Session, tenant_id: int,
                                      now: datetime, interval_hours: int) \
        -> bool:
    """The dedupe for the gov "missing expiry" report: at most one per tenant
    per `secrets.expiry_reminder_interval_hours` (the audit row is the record,
    since these secrets have no live expiry row to stamp)."""
    since = now - timedelta(hours=max(1, int(interval_hours)))
    return bool(
        db.query(models.AuditEvent)
        .filter(models.AuditEvent.event_name == MISSING_EXPIRY_AUDIT_EVENT,
                models.AuditEvent.tenant_id == tenant_id,
                models.AuditEvent.timestamp >= since)
        .first()
    )


def _audit_missing_expiry(db: Session, tenant_id: int,
                          names: List[str], admins_notified: int) -> None:
    """One SECRET_MISSING_EXPIRY_REPORTED row per report (names only)."""
    try:
        db.add(
            models.AuditEvent(
                tenant_id=tenant_id,
                user_id=None,
                event_name=MISSING_EXPIRY_AUDIT_EVENT,
                details={
                    "secret_names": names,
                    "missing_expiry": True,
                    "reason": MISSING_EXPIRY_REASON,
                    "admins_notified": admins_notified,
                },
            )
        )
        db.commit()
    except Exception as e:  # noqa: BLE001 — audit is best-effort
        logger.warning("secret_expiry_service: missing-expiry audit write "
                       "failed for tenant %s: %s", tenant_id, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass

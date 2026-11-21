"""services/oauth_refresh.py — sz-4.2c OAuth pre-expiry refresh tick +
sz-4.4 refresh-failure "reconnect required" notification (service only:
the scheduler job wiring is the lead's, see LEAD STEPS).

Reuses services/oauth_broker.py (committed 9c64e12a1) — never edited here:
the frozen PROVIDERS registry, the httpx `_TEST_TRANSPORT` client seam, the
Vault client-credential reader, the tenant-key path and the CODE-ONLY error
redactor are imported by name. The refresh POST mirrors the code-exchange
POST EXACTLY: streamed `iter_bytes()` with the 256 KiB cap enforced WHILE
streaming (an oversized token response is refused before it is ever fully
buffered), `follow_redirects=False`, `trust_env=False`, 15 s timeout, and
provider failures reduce to a bare error code — never a response body,
never a token — into `OAuthConnection.last_error_code`, the audit details,
the log and the notification text.

Failure classification (status strings are exactly the model CHECK's
`status IN ('active','refresh_failed','reconnect_required','revoked')`):
* provider `invalid_grant`-family or an HTTP 400/401 -> "reconnect_required"
  immediately (the customer must re-run the connect flow);
* network / timeout / 5xx / unparseable / oversized -> transient: the row
  stays "active" and only after `oauth.refresh_max_failures` CONSECUTIVE
  transient failures does it become "refresh_failed".

Failure counter: models.OAuthConnection has NO JSON metadata column (only
`scopes`, which is semantically the granted scope list), so the consecutive
transient-failure counter is DERIVED from the audit trail — trailing
`oauth.connection.refresh_transient` rows for this connection_id since the
last `refresh_success` / transition row. No new column, no models.py edit.

On a transition to reconnect_required / refresh_failed exactly one in-app
notification is fanned out through services/app_notifications.py (dedupe
key `oauth-reconnect:<conn.id>:<status>`, so the tick never re-spams) to the
connecting user, or to the tenant's admins for a tenant-wide connection
(user_id NULL), plus one audit row. Tokens are never logged or returned.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import json
import httpx
from sqlalchemy.orm import Session

import models
from services import app_notifications
from services import http_triggers
from services import oauth_broker
from services import platform_settings as psr
from utils import encryption
from utils.paid_import import optional_paid
key_provider = optional_paid("services.key_provider")

logger = logging.getLogger(__name__)

# --- Settings (DB platform settings, never env) -----------------------------

LEAD_KEY = "oauth.refresh_lead_s"
BATCH_KEY = "oauth.refresh_batch"
MAX_FAILURES_KEY = "oauth.refresh_max_failures"

DEFAULT_LEAD_S = 600
DEFAULT_BATCH = 100
DEFAULT_MAX_FAILURES = 3


def _validate_positive(name: str):
    def _check(value) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be an int >= 0, got {value!r}")
    return _check


psr.register(
    LEAD_KEY, int, DEFAULT_LEAD_S, group="oauth",
    validator=_validate_positive(LEAD_KEY),
    description="sz-4.2c — refresh an OAuth access token this many seconds "
                "BEFORE it expires. Default 600.",
)
psr.register(
    BATCH_KEY, int, DEFAULT_BATCH, group="oauth",
    validator=_validate_positive(BATCH_KEY),
    description="sz-4.2c — max connections the refresh tick processes per "
                "run (oldest-expiry first). Default 100.",
)
psr.register(
    MAX_FAILURES_KEY, int, DEFAULT_MAX_FAILURES, group="oauth",
    validator=_validate_positive(MAX_FAILURES_KEY),
    description="sz-4.2c — consecutive transient refresh failures before an "
                "OAuth connection is marked refresh_failed. Default 3.",
)

# Status strings the models.OAuthConnection CHECK allows.
STATUS_ACTIVE = "active"
STATUS_REFRESH_FAILED = "refresh_failed"
STATUS_RECONNECT_REQUIRED = "reconnect_required"

# Provider error codes that mean "the customer must reconnect" (RFC 6749
# permanent-grant failures). Everything else that the provider actually
# answered is treated as transient unless it is a 400/401.
_PERMANENT_CODES = frozenset({
    "invalid_grant", "unauthorized_client", "invalid_client",
    "unsupported_grant_type", "revoked_token", "token_expired",
})

# Notification link: Dashboard deep-link hash the SPA router
# (CORE/Dashboard/js/ui.js:navigateToPage) actually understands —
# "#<page>?tab=<subtab>" selects the sub-tab via getActiveTab()/the
# `_requestedTab` fallback. "settings" is the real pageId (pages/settings.html)
# and "integrations" matches `id="main-tab-integrations"` in that page
# (CORE/Dashboard/js/settings.js reads it via `main-tab-${wantedTab}`). A
# bare "#integrations/oauth" path segment is NOT a registered pageId and
# would not land anywhere.
NOTIFICATION_LINK = "#settings?tab=integrations"
NOTIFICATION_SEVERITY = "warning"

_TRANSIENT_CODES = frozenset({
    "network_unavailable", "response_too_large", "response_unparseable",
})

# Some providers answer a TRANSIENT condition (rate limiting / temporary
# outage) with the same HTTP 400/401 status Google uses for a permanent
# invalid_grant — e.g. Microsoft `temporarily_unavailable`, Slack/other
# `slow_down` / `ratelimited`. These codes must stay transient (retry, never
# an immediate reconnect_required) regardless of the HTTP status they rode
# in on.
_TRANSIENT_DESPITE_4XX_CODES = frozenset({
    "temporarily_unavailable", "slow_down", "ratelimited", "rate_limited",
})

# Audit event names this service emits (details = provider + id + CODE only).
AUDIT_REFRESH_SUCCESS = "oauth.connection.refresh_success"
AUDIT_REFRESH_TRANSIENT = "oauth.connection.refresh_transient"
AUDIT_REFRESH_FAILED = "oauth.connection.refresh_failed"
AUDIT_RECONNECT_REQUIRED = "oauth.connection.reconnect_required"
_AUDIT_COUNTER_EVENTS = (
    AUDIT_REFRESH_SUCCESS, AUDIT_REFRESH_TRANSIENT, AUDIT_REFRESH_FAILED,
    AUDIT_RECONNECT_REQUIRED,
)
_COUNTER_SCAN_LIMIT = 50


class _RefreshFailure(Exception):
    """Internal: provider refused the refresh. Carries a CODE ONLY plus the
    permanent/transient flag. Never a body, never a token."""

    def __init__(self, code: str, *, permanent: bool) -> None:
        super().__init__(code)
        self.code = code
        self.permanent = permanent


def _refresh_exchange(spec: Dict, client_id: str, client_secret: str,
                      refresh_token: str) -> dict:
    """POST the provider token endpoint with grant_type=refresh_token.

    Byte-for-byte the same HTTP shape as oauth_broker._exchange_code (same
    client seam, same 256 KiB cap enforced WHILE streaming via iter_bytes,
    same basic/body auth rules, same code-only redaction) — the only
    difference is the grant. Raises _RefreshFailure(code, permanent=...).
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    token_auth = spec["token_auth"]
    use_basic = token_auth == "basic" or (
        token_auth == "basic_if_secret" and bool(client_secret))
    auth = (client_id, client_secret) if use_basic else None
    if not use_basic:
        data["client_id"] = client_id
        data["client_secret"] = client_secret

    client = oauth_broker._http_client_factory()
    post_kwargs = {"json": data} if spec.get("token_body") == "json" \
        else {"data": data}
    try:
        # ssrf-ok: URL is PROVIDERS[...]["token_url"], a static reviewed HTTPS
        # literal from the frozen registry — never user-supplied.
        request = client.build_request(
            "POST", spec["token_url"],
            headers={"Accept": "application/json"}, **post_kwargs)
        resp = client.send(request, auth=auth, stream=True)
    except httpx.HTTPError:
        client.close()
        raise _RefreshFailure("network_unavailable", permanent=False) from None

    body = bytearray()
    try:
        try:
            for chunk in resp.iter_bytes():
                body.extend(chunk)
                if len(body) > oauth_broker._MAX_RESPONSE_BYTES:
                    raise _RefreshFailure("response_too_large",
                                          permanent=False)
        except httpx.HTTPError:
            raise _RefreshFailure("network_unavailable",
                                  permanent=False) from None
        status_code = resp.status_code
    finally:
        resp.close()
        client.close()

    if status_code < 200 or status_code >= 300:
        # a 3xx is NOT followed (follow_redirects=False) and is a failure.
        code = oauth_broker._error_code_only(bytes(body))
        permanent = (
            code not in _TRANSIENT_DESPITE_4XX_CODES
            and (code in _PERMANENT_CODES or status_code in (400, 401)))
        raise _RefreshFailure(code, permanent=permanent)
    try:
        parsed = json.loads(bytes(body).decode())
    except (ValueError, UnicodeError):
        raise _RefreshFailure("response_unparseable", permanent=False) from None
    if not isinstance(parsed, dict):
        raise _RefreshFailure("response_unparseable", permanent=False)
    ok_field = spec.get("ok_field")
    if ok_field and parsed.get(ok_field) is False:
        # e.g. Slack: HTTP 200 + {"ok": false, "error": ...} is a failure.
        code = oauth_broker._error_code_only(parsed)
        raise _RefreshFailure(code, permanent=code in _PERMANENT_CODES)
    if "access_token" not in parsed:
        code = oauth_broker._error_code_only(parsed)
        raise _RefreshFailure(code, permanent=code in _PERMANENT_CODES)
    return parsed


# --- transient-failure counter (derived from the audit trail) ---------------


def _consecutive_transient_failures(db: Session, tenant_id: int,
                                    connection_id: int) -> int:
    """Trailing `oauth.connection.refresh_transient` rows for THIS
    connection since the last success / transition row. The model has no
    metadata column to hold a counter, so the audit trail is the counter —
    newest-first scan, bounded, JSONB read in Python only (never a JSONB
    expression in SQL)."""
    rows = (
        db.query(models.AuditEvent)
        .filter(models.AuditEvent.tenant_id == tenant_id,
                models.AuditEvent.event_name.in_(_AUDIT_COUNTER_EVENTS))
        .order_by(models.AuditEvent.timestamp.desc())
        .limit(_COUNTER_SCAN_LIMIT)
        .all()
    )
    count = 0
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        if details.get("connection_id") != connection_id:
            continue
        if row.event_name == AUDIT_REFRESH_TRANSIENT:
            count += 1
        else:
            break
    return count


def _audit(db: Session, tenant_id: int, user_id: Optional[int],
           event_name: str, provider: str, connection_id: int,
           error_code: Optional[str]) -> None:
    """One audit row: provider + connection id + CODE ONLY. Never a body,
    never a token."""
    try:
        db.add(models.AuditEvent(
            tenant_id=tenant_id, user_id=user_id, event_name=event_name,
            details={"provider": provider, "connection_id": connection_id,
                     "error_code": error_code},
        ))
    except Exception as exc:  # noqa: BLE001 — audit must never break the tick
        logger.warning("oauth_refresh: audit emit failed (non-fatal): %s",
                       type(exc).__name__)


def _notify_targets(db: Session, conn: models.OAuthConnection) -> List[int]:
    """Owner user for an owned connection; the tenant's admins for a
    tenant-wide one (user_id NULL)."""
    if conn.user_id is not None:
        return [conn.user_id]
    admins = (
        db.query(models.User)
        .filter(models.User.tenant_id == conn.tenant_id,
                models.User.deleted_at.is_(None))
        .all()
    )
    return [u.id for u in admins
            if getattr(u, "is_platform_admin", False)
            or getattr(u, "parent_user_id", None) is None]


def _notify_failure(db: Session, conn: models.OAuthConnection,
                    status_new: str) -> None:
    """One in-app notification per (connection, status) — the dedupe key
    makes the tick idempotent, so a later tick never re-spams."""
    targets = _notify_targets(db, conn)
    if not targets:
        return
    provider = conn.provider
    try:
        app_notifications.fan_out(
            db,
            tenant_id=conn.tenant_id,
            user_id=conn.user_id,
            targets={"user_ids": targets, "group_ids": []},
            title=f"Reconnect {provider}",
            body=("MOD could no longer refresh this integration's access "
                  "token. Open Settings -> Integrations and reconnect it."),
            severity=NOTIFICATION_SEVERITY,
            link=NOTIFICATION_LINK,
            channels=["in_app"],
            dedupe_key=f"oauth-reconnect:{conn.id}:{status_new}",
        )
    except Exception as exc:  # noqa: BLE001 — notification must never break
        logger.warning("oauth_refresh: notification fan-out failed (non-fatal): %s",
                       type(exc).__name__)


# --- refresh_connection -----------------------------------------------------


def refresh_connection(
    db: Session, conn: models.OAuthConnection,
    *, defer_notifications: Optional[List[Tuple[int, str]]] = None,
) -> str:
    """Refresh ONE connection. Caller owns the transaction (this flushes).

    When ``defer_notifications`` is a list, notification requests are
    appended as ``(connection_id, status_new)`` tuples and nothing is
    committed here.  When None (default), notification is sent immediately
    via ``_notify_failure`` (the legacy single-connection path).

    Returns the outcome: 'refreshed' | 'transient_failure' |
    'reconnect_required' | 'refresh_failed'. Tokens are never returned,
    logged, or stored in plaintext.
    """
    spec = oauth_broker.PROVIDERS.get(conn.provider)
    if spec is None:
        raise oauth_broker.OAuthStateError("provider unknown")

    if not conn.encrypted_refresh_token:
        # The provider/consent NEVER issued a refresh token (google without
        # offline consent, github/notion/linear/slack/airtable connections):
        # nothing was rotated, the customer just has to reconnect. CODE-ONLY.
        conn.last_error_code = "no_refresh_token"
        conn.status = STATUS_RECONNECT_REQUIRED
        conn.updated_at = datetime.now(timezone.utc)
        db.flush()
        _audit(db, conn.tenant_id, conn.user_id, AUDIT_RECONNECT_REQUIRED,
               conn.provider, conn.id, "no_refresh_token")
        if defer_notifications is None:
            _notify_failure(db, conn, STATUS_RECONNECT_REQUIRED)
        else:
            defer_notifications.append((conn.id, STATUS_RECONNECT_REQUIRED))
        return STATUS_RECONNECT_REQUIRED

    tenant, _key = oauth_broker._tenant_and_key(db, conn.tenant_id)

    refresh_plain = http_triggers.decrypt_secret(
        db, tenant, conn.encrypted_refresh_token)
    if not refresh_plain:
        # ciphertext present but undecryptable (tenant key rotated / vault):
        # the customer must reconnect.
        conn.last_error_code = "key_rotated"
        conn.status = STATUS_RECONNECT_REQUIRED
        conn.updated_at = datetime.now(timezone.utc)
        db.flush()
        _audit(db, conn.tenant_id, conn.user_id, AUDIT_RECONNECT_REQUIRED,
               conn.provider, conn.id, "key_rotated")
        if defer_notifications is None:
            _notify_failure(db, conn, STATUS_RECONNECT_REQUIRED)
        else:
            defer_notifications.append((conn.id, STATUS_RECONNECT_REQUIRED))
        return STATUS_RECONNECT_REQUIRED

    try:
        client_id, client_secret = oauth_broker._client_credentials(
            conn.tenant_id, conn.provider)
    except oauth_broker.OAuthNotConfigured:
        # Vault outage or credentials removed: transient — never force a
        # reconnect on a Vault hiccup.
        _audit(db, conn.tenant_id, conn.user_id, AUDIT_REFRESH_TRANSIENT,
               conn.provider, conn.id, "not_configured")
        db.flush()
        return "transient_failure"

    try:
        tokens = _refresh_exchange(spec, client_id, client_secret,
                                   str(refresh_plain))
    except _RefreshFailure as exc:
        code = exc.code
        if exc.permanent:
            conn.status = STATUS_RECONNECT_REQUIRED
            conn.last_error_code = code
            conn.updated_at = datetime.now(timezone.utc)
            db.flush()
            _audit(db, conn.tenant_id, conn.user_id,
                   AUDIT_RECONNECT_REQUIRED, conn.provider, conn.id, code)
            if defer_notifications is None:
                _notify_failure(db, conn, STATUS_RECONNECT_REQUIRED)
            else:
                defer_notifications.append(
                    (conn.id, STATUS_RECONNECT_REQUIRED))
            return STATUS_RECONNECT_REQUIRED

        max_failures = psr.get_setting(MAX_FAILURES_KEY, db, tenant_id=None)
        _audit(db, conn.tenant_id, conn.user_id, AUDIT_REFRESH_TRANSIENT,
               conn.provider, conn.id, code)
        db.flush()
        streak = _consecutive_transient_failures(
            db, conn.tenant_id, conn.id)
        if streak >= int(max_failures):
            conn.status = STATUS_REFRESH_FAILED
            conn.last_error_code = code
            conn.updated_at = datetime.now(timezone.utc)
            db.flush()
            # the transition row also RESETS the streak basis (it is not a
            # transient event).
            _audit(db, conn.tenant_id, conn.user_id, AUDIT_REFRESH_FAILED,
                   conn.provider, conn.id, code)
            if defer_notifications is None:
                _notify_failure(db, conn, STATUS_REFRESH_FAILED)
            else:
                defer_notifications.append(
                    (conn.id, STATUS_REFRESH_FAILED))
            return STATUS_REFRESH_FAILED
        return "transient_failure"

    # --- success -----------------------------------------------------------
    access = str(tokens["access_token"])
    conn.encrypted_access_token = http_triggers.encrypt_secret(
        db, tenant, access)
    rotated = tokens.get("refresh_token")
    if rotated:
        # provider rotated the refresh token too; otherwise KEEP the old
        # ciphertext (a refresh grant that returns none is not a failure).
        conn.encrypted_refresh_token = http_triggers.encrypt_secret(
            db, tenant, str(rotated))

    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(
            expires_in, bool) and expires_in > 0:
        conn.access_token_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=float(expires_in)))
    conn.last_refreshed_at = datetime.now(timezone.utc)
    conn.status = STATUS_ACTIVE
    conn.last_error_code = None
    conn.updated_at = datetime.now(timezone.utc)
    _kv = key_provider.ciphertext_key_version(tenant.encryption_key)
    conn.key_version = str(
        _kv if _kv is not None else encryption.get_key_version())[:32]
    db.flush()
    _audit(db, conn.tenant_id, conn.user_id, AUDIT_REFRESH_SUCCESS,
           conn.provider, conn.id, None)
    return "refreshed"


# --- refresh_tick -----------------------------------------------------------


# One-shot guard so the CE tick logs at most once per process.
_refresh_tick_logged_ce_once = False


def refresh_tick(db: Session, *, now: Optional[datetime] = None) -> Dict[str, int]:
    """Pre-expiry refresh sweep. Returns per-outcome counts; one failing
    connection NEVER aborts the tick.

    Selection: LIVE (`deleted_at IS NULL`), `status = active`, and either
    (A) HAS a refresh token and `access_token_expires_at <= now + oauth.refresh_lead_s`
    (a NULL expiry counts as due — refresh it), or (B) has NO refresh token and
    its access token has already EXPIRED (`access_token_expires_at` NOT NULL and
    <= now) — such rows can never be refreshed, so they go through
    `refresh_connection` and become reconnect_required / `no_refresh_token`
    instead of staying "active" forever. No-refresh rows with a NULL expiry
    (tokens that never expire, e.g. GitHub OAuth apps) or a FUTURE expiry stay
    skipped. Oldest expiry first, bounded by `oauth.refresh_batch`. Rows are
    taken with `for_update(skip_locked=True)` so a second API replica running
    the same tick simply skips the locked rows instead of double-refreshing;
    each connection then gets its own SAVEPOINT so a failure rolls back only
    that connection.
    """
    global _refresh_tick_logged_ce_once
    if key_provider is None:
        if not _refresh_tick_logged_ce_once:
            _refresh_tick_logged_ce_once = True
            logger.info("oauth_refresh: key_provider absent (CE); tick skipped")
        return {"refreshed": 0, "transient_failure": 0,
                "reconnect_required": 0, "refresh_failed": 0,
                "skipped_no_refresh_token": 0, "skipped_not_due": 0,
                "expired_no_refresh_token": 0, "error": 0}
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:  # naive settings/fixture times -> UTC
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    lead_s = int(psr.get_setting(LEAD_KEY, db, tenant_id=None))
    batch = int(psr.get_setting(BATCH_KEY, db, tenant_id=None))
    cutoff = now_dt + timedelta(seconds=lead_s)

    counts: Dict[str, int] = {
        "refreshed": 0, "transient_failure": 0,
        "reconnect_required": 0, "refresh_failed": 0,
        "skipped_no_refresh_token": 0, "skipped_not_due": 0,
        "expired_no_refresh_token": 0, "error": 0,
    }

    live = (
        models.OAuthConnection.deleted_at.is_(None),
        models.OAuthConnection.status == STATUS_ACTIVE,
    )
    has_refresh = models.OAuthConnection.encrypted_refresh_token.isnot(None)
    no_refresh = models.OAuthConnection.encrypted_refresh_token.is_(None)
    due = (
        (models.OAuthConnection.access_token_expires_at.is_(None))
        | (models.OAuthConnection.access_token_expires_at <= cutoff)
    )
    expired = (
        models.OAuthConnection.access_token_expires_at.isnot(None)
    ) & (
        models.OAuthConnection.access_token_expires_at <= now_dt
    )

    # "skipped" = no refresh token AND not expired (NULL expiry = the token
    # never expires, e.g. a GitHub OAuth app; a future expiry is not due).
    counts["skipped_no_refresh_token"] = (
        db.query(models.OAuthConnection.id)
        .filter(*live, no_refresh,
                (models.OAuthConnection.access_token_expires_at.is_(None))
                | (models.OAuthConnection.access_token_expires_at > now_dt))
        .count()
    )
    counts["skipped_not_due"] = (
        db.query(models.OAuthConnection.id)
        .filter(*live, has_refresh,
                models.OAuthConnection.access_token_expires_at > cutoff)
        .count()
    )

    rows = (
        db.query(models.OAuthConnection)
        .filter(*live, (has_refresh & due) | (no_refresh & expired))
        .order_by(models.OAuthConnection.access_token_expires_at.asc())
        .limit(max(0, batch))
        .with_for_update(skip_locked=True)
        .all()
    )

    # IMPORTANT: `rows` were locked by ONE `SELECT ... FOR UPDATE SKIP LOCKED`
    # above. A `db.commit()` here would COMMIT the outer transaction — ending
    # it and releasing the row locks on every OTHER row still waiting in this
    # loop (SAVEPOINTs nest inside one outer transaction; only the outer
    # COMMIT controls when its locks are released).  That would let a second
    # API replica's tick grab the remaining rows mid-sweep, defeating
    # `skip_locked`.  So `refresh_connection` is called with
    # ``defer_notifications`` so every notification is deferred and sent
    # AFTER the single `db.commit()` below — the outer transaction (and thus
    # every row lock) stays alive until that commit, which is the only one
    # that happens before we release the locks.
    defer: List[Tuple[int, str]] = []
    for conn in rows:
        conn_id = conn.id
        # a no-refresh row that was pulled in because its access token EXPIRED
        # gets its own count key (refresh_connection marks it
        # reconnect_required / no_refresh_token).
        expired_no_refresh = (
            not conn.encrypted_refresh_token
            and conn.access_token_expires_at is not None
            and conn.access_token_expires_at <= now_dt
        )
        savepoint = db.begin_nested()
        try:
            outcome = refresh_connection(db, conn, defer_notifications=defer)
            if savepoint.is_active:
                savepoint.commit()
            if expired_no_refresh:
                counts["expired_no_refresh_token"] = counts.get(
                    "expired_no_refresh_token", 0) + 1
            else:
                counts[outcome] = counts.get(outcome, 0) + 1
        except Exception as exc:  # noqa: BLE001 — one row never aborts a tick
            try:
                savepoint.rollback()
            except Exception:
                pass
            counts["error"] += 1
            logger.warning(
                "oauth_refresh: refresh of connection %s failed (non-fatal): %s",
                conn_id, type(exc).__name__)

    db.commit()

    # Send all deferred notifications now that the outer transaction
    # has committed and row locks are released.  Re-load each connection
    # fresh so we have current state; wrap each so one failure never
    # aborts the rest.  The dedupe_key (oauth-reconnect:<id>:<status>)
    # prevents re-spam on a subsequent tick.
    for conn_id, status_new in defer:
        try:
            reloaded = db.query(models.OAuthConnection).get(conn_id)
            if reloaded is not None and reloaded.deleted_at is None:
                _notify_failure(db, reloaded, status_new)
        except Exception as _exc:  # noqa: BLE001 — notification must never
            logger.warning(
                "oauth_refresh: deferred notification for conn %s "
                "failed (non-fatal): %s", conn_id, type(_exc).__name__)

    return counts

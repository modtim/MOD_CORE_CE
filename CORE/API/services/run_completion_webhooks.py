"""GX.R3 — run-completion webhooks for apps built on MOD.

External apps (e.g. the talk/voice-to-task app) run MOD workflows and need to
be told when a run reaches a terminal state instead of polling. This module
owns the durable delivery side of that contract:

* Subscription CRUD helpers — per-workflow (and per-run via the run request's
  `callback_url`), tenant-scoped, soft-delete, HMAC signing secret generated
  server-side, stored encrypted under the tenant encryption key (same pattern
  as secret values in crud/secret_cud.py), shown to the caller exactly once.
* Enqueue — called from the run-finalization paths with the run id; writes ONE
  `RunWebhookDelivery` row per target (workflow subscription + per-run
  callback) in the caller's already-committed state and returns. It performs
  NO network I/O and must be invoked only AFTER the run's terminal commit, so
  it never blocks finalization and never runs inside the run's transaction.
* A background sender (an APScheduler job, same pattern as
  services/scheduler.py) picks due rows, re-resolves DNS at send time behind
  the SSRF guard, POSTs with a 10s timeout and retries with exponential
  backoff (1m, 5m, 30m, 2h, 6h; max 5 attempts). The delivery log is
  append-only: one row per (delivery, attempt) — status, http status, no
  response bodies.

Payload shape (signed):
    {event: "run.completed"|"run.failed"|"run.cancelled", run_id,
     workflow_id, status, completed_at, error,
     result_url: "/v1/runs/{id}/result"}

Headers:
    X-MOD-Signature: sha256=<hex HMAC-SHA256 of `timestamp.body`>
    X-MOD-Timestamp: <unix seconds>
    X-MOD-Event: <event>
    X-MOD-Delivery: <delivery uuid>
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets as _secrets
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# --- Events -----------------------------------------------------------------

EVENT_RUN_COMPLETED = "run.completed"
EVENT_RUN_FAILED = "run.failed"
EVENT_RUN_CANCELLED = "run.cancelled"

_STATUS_TO_EVENT = {
    "completed": EVENT_RUN_COMPLETED,
    "success": EVENT_RUN_COMPLETED,
    "succeeded": EVENT_RUN_COMPLETED,
    "failed": EVENT_RUN_FAILED,
    "failure": EVENT_RUN_FAILED,
    "cancelled": EVENT_RUN_CANCELLED,
    "canceled": EVENT_RUN_CANCELLED,
}

# --- Delivery policy ---------------------------------------------------------

# Retry ladder in minutes after the previous attempt. Max attempts = 1 + len.
RETRY_BACKOFF_MINUTES = [1, 5, 30, 120, 360]
MAX_ATTEMPTS = len(RETRY_BACKOFF_MINUTES) + 1
DELIVERY_TIMEOUT_SECONDS = 10

DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED_PERMANENT = "failed"
DELIVERY_REJECTED = "rejected"  # SSRF/URL rejection at enqueue or send time

SIG_HEADER = "X-MOD-Signature"
TIMESTAMP_HEADER = "X-MOD-Timestamp"
EVENT_HEADER = "X-MOD-Event"
DELIVERY_HEADER = "X-MOD-Delivery"

from services import ssrf_guard as _ssrf_guard

# The guard itself lives in services/ssrf_guard.py (FX.151: one shared
# implementation for every outbound-URL site). These names are kept as
# re-exports so existing import sites and tests keep working.
_BLOCKED_INTERNAL_HOSTNAMES = _ssrf_guard.BLOCKED_INTERNAL_HOSTNAMES
ssrf_check = _ssrf_guard.ssrf_check
_ip_refused = _ssrf_guard._ip_refused


# --- Tunables (DB platform settings, never env vars) --------------------------

from services import platform_settings as _ps

_ps.register(
    "run_webhooks.allow_http",
    bool,
    False,
    group="webhooks",
    description="GX.R3: allow http (non-https) callback URLs for run-completion "
                "webhooks. Off = https only. Enable only for dev/on-prem beds.",
)
_ps.register(
    "run_webhooks.allow_private",
    bool,
    False,
    group="webhooks",
    description="GX.R3: allow run-completion webhook callbacks to resolve to "
                "private/loopback/link-local addresses. Off = refused (SSRF "
                "guard). On-prem deployments legitimately target internal "
                "hosts; the docker-internal hostname block still applies.",
)


# --- GX n9: tenant-owned host exemption ---------------------------------------
#
# A run-completion webhook may target a hostname the tenant provably owns
# (an ACTIVE App hostname, or an fqdn declared on one of its RUNNING stacks)
# without the global ``run_webhooks.allow_private`` opt-in. Exact-host only,
# fail closed. Private (RFC1918/ULA) addressing is the ONLY thing unlocked:
# loopback / link-local / metadata / unspecified / multicast / reserved are
# still refused (same decision as the http_triggers stack opt-in).

EXEMPT_REASON = "tenant_owned_host"
_STACK_LIVE_STATUSES = ("running", "active", "ready")
_METADATA_ADDRS = frozenset({"169.254.169.254", "fd00:ec2::254"})


def _normalize_host(host) -> Optional[str]:
    """Lowercase, strip one trailing dot, IDNA -> ASCII. None when the value
    is not a bare DNS hostname (port, userinfo, brackets, IP literal, ...)."""
    import ipaddress as _ip
    if not isinstance(host, str):
        return None
    h = host.strip().lower()
    if h.endswith("."):
        h = h[:-1]
    if not h or any(c in h for c in ":@/[]\\ \t?#"):
        return None
    try:
        h = h.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    try:
        _ip.ip_address(h)
        return None  # ownership is by hostname, never an IP literal
    except ValueError:
        pass
    return h if len(h) <= 253 else None


def is_tenant_owned_host(db, tenant_id, host) -> bool:
    """True only when ``host`` EXACTLY equals an active hostname owned by
    ``tenant_id``: a live (not soft-deleted) ``active`` App (stack-backed apps
    also need their stack in the same tenant and running), or an fqdn in the
    ``tls_config`` of one of the tenant's running stacks. No suffix/wildcard
    matching. Any DB error -> False (fail closed)."""
    h = _normalize_host(host)
    if h is None or tenant_id is None:
        return False
    try:
        import models
        for app in (db.query(models.App)
                    .filter(models.App.tenant_id == tenant_id,
                            models.App.status == "active",
                            models.App.deleted_at.is_(None),
                            models.App.hostname.isnot(None)).all()):
            if _normalize_host(app.hostname) != h:
                continue
            if app.stack_id is None:
                return True
            stack = (db.query(models.PersistentStack)
                     .filter(models.PersistentStack.id == app.stack_id,
                             models.PersistentStack.tenant_id == tenant_id)
                     .first())
            if stack is not None and stack.status in _STACK_LIVE_STATUSES:
                return True
        from services import app_hostname as _ah
        base = _ah.base_domain()
        # n9F: no declared zone (BASE_DOMAIN unset/empty, or the app_hostname
        # dev fallback "localhost") -> nothing counts as owned.
        zone = base if (base and base != "localhost") else None
        for stack in (db.query(models.PersistentStack)
                      .filter(models.PersistentStack.tenant_id == tenant_id,
                              models.PersistentStack.status.in_(_STACK_LIVE_STATUSES),
                              models.PersistentStack.tls_config.isnot(None)).all()):
            for svc in (stack.tls_config or {}).values():
                if not (isinstance(svc, dict)
                        and _normalize_host(svc.get("fqdn")) == h):
                    continue
                # x-mod.tls.fqdn is tenant free text: only ONE non-reserved
                # label under BASE_DOMAIN counts. Label-boundary (n9F): the
                # remainder must be a single slug label, so neither
                # ``evilBASE`` nor ``x.evilBASE`` matches. (n9R CRITICAL fix.)
                prefix = (h[: -(len(zone) + 1)]
                          if zone and h.endswith("." + zone) else "")
                if _ah.SLUG_RE.match(prefix) and not _ah.is_reserved_slug(prefix):
                    return True
    except Exception:  # noqa: BLE001 — fail closed
        return False
    return False


def _exempt_addr_ok(addr: str) -> bool:
    """An address an exempted delivery may use: private only."""
    import ipaddress as _ip
    try:
        ip = _ip.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if str(ip) in _METADATA_ADDRS:
        return False
    return bool(ip.is_private and not (
        ip.is_loopback or ip.is_unspecified or ip.is_link_local
        or ip.is_multicast or ip.is_reserved))


def check_target(db, tenant_id, url: str, *, allow_http: bool = False,
                 allow_private: bool = False, subscription_id=None,
                 phase: str = "delivery"):
    """Single rule for create AND delivery. Returns ``(refusal, exempt_host)``.

    ``refusal`` is None when allowed; ``exempt_host`` is the normalized host
    when (and only when) the tenant-owned-host exemption is what allowed it
    (the caller must then send with ``_ssrf_private_ok_host=exempt_host``)."""
    refusal = ssrf_check(url, allow_http=allow_http, allow_private=allow_private)
    if refusal is None:
        return None, None
    from urllib.parse import urlsplit
    import socket as _socket
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https") or (scheme == "http" and not allow_http):
            return refusal, None
        if parts.username is not None or parts.password is not None:
            return refusal, None
        if parts.port not in (None, 443 if scheme == "https" else 80):
            return refusal, None
        host = _normalize_host(parts.hostname or "")
    except ValueError:
        return refusal, None
    if (host is None or host in _BLOCKED_INTERNAL_HOSTNAMES
            or not is_tenant_owned_host(db, tenant_id, host)):
        return refusal, None
    try:
        addrs = [sa[0] for *_r, sa in _socket.getaddrinfo(host, None)]
    except Exception:  # noqa: BLE001 — unresolvable -> refuse
        return refusal, None
    if not addrs or not all(_exempt_addr_ok(a) for a in addrs):
        return refusal, None
    logger.info(
        "run_webhooks.audit: private target allowed phase=%s tenant=%s "
        "subscription=%s host=%s reason=%s",
        phase, tenant_id, subscription_id, host, EXEMPT_REASON)
    return None, host


# --- Signing -----------------------------------------------------------------

def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """`X-MOD-Signature` value: `sha256=<hex>` over HMAC-SHA256 of
    `<timestamp>.<body>` keyed by the subscription's signing secret."""
    msg = timestamp.encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_payload(run, status_override: Optional[str] = None) -> dict:
    """The signed JSON body for a terminal run (see module docstring).

    `status_override` lets a finalize path report a more precise terminal
    reason than the run row carries (e.g. a user cancel leaves
    `WorkflowRun.status = FAILED` — the cancel route passes "cancelled" so
    subscribers get `run.cancelled`, not `run.failed`).
    """
    status_str = (
        (status_override or "").lower()
        or (run.status.value if run.status is not None else "unknown")
    )
    event = _STATUS_TO_EVENT.get(status_str.lower(), EVENT_RUN_FAILED)
    completed_at = run.completed_at
    return {
        "event": event,
        "run_id": run.id,
        "workflow_id": run.workflow_id,
        "status": status_str,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "error": run.error_message,
        "result_url": f"/v1/runs/{run.id}/result",
    }


# --- Secret encryption (tenant-key pattern from crud/secret_cud.py) -----------

def _ce_key_provider():
    """1788e CE carve: services.key_provider is paid/denied in CE; the CE-safe
    provider is the shipped local-only module (same pattern as
    crud/secret_cud.py:20), byte-compatible for CE-wrapped envelopes."""
    from utils.paid_import import optional_paid
    from services import tenant_key_local
    return optional_paid("services.key_provider") or tenant_key_local


def _ensure_tenant_encryption_key(db, tenant) -> str:
    key_provider = _ce_key_provider()
    if not tenant.encryption_key:
        _plaintext, tenant.encryption_key = key_provider.generate_and_wrap_tenant_key(
            tenant.id, db=db)
        db.flush()
    return tenant.encryption_key


def encrypt_secret(db, tenant, plaintext: str) -> str:
    """Encrypt a signing secret under the tenant's key (local- or
    vault_transit-wrapped, per the Vault-transit KEK backend)."""
    from utils import encryption
    key_provider = _ce_key_provider()
    tenant_key_material = _ensure_tenant_encryption_key(db, tenant)
    decrypted_tenant_key = key_provider.unwrap_tenant_key(
        tenant_key_material, tenant.id, db=db)
    return encryption.encrypt_value(plaintext, decrypted_tenant_key)


def decrypt_secret(db, tenant, encrypted_value: str) -> Optional[str]:
    """Reverse of `encrypt_secret`; None on any decryption failure (a row
    whose key material rotated away is unusable — rotate the subscription)."""
    from utils import encryption
    key_provider = _ce_key_provider()
    if not encrypted_value or not tenant.encryption_key:
        return None
    try:
        decrypted_tenant_key = key_provider.unwrap_tenant_key(
            tenant.encryption_key, tenant.id, db=db)
        return encryption.decrypt_value(encrypted_value, decrypted_tenant_key)
    except Exception:  # noqa: BLE001 — corrupt/rotated key material
        logger.warning(
            "run_webhooks: failed to decrypt signing secret (tenant=%s)", tenant.id
        )
        return None


def generate_signing_secret() -> str:
    return "mod_whsec_" + _secrets.token_urlsafe(32)


# --- Subscription CRUD helpers (router-facing, no HTTP) -----------------------

def create_subscription(db, *, tenant_id: int, workflow_id: int, url: str,
                        user_id: Optional[int] = None) -> tuple:
    """Create a per-workflow subscription. Returns (subscription, raw_secret).

    The raw secret is returned ONCE; only the encrypted form is persisted.
    """
    import models
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if tenant is None:
        raise ValueError("tenant not found")
    raw_secret = generate_signing_secret()
    encrypted = encrypt_secret(db, tenant, raw_secret)
    sub = models.RunWebhookSubscription(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        url=url,
        secret_encrypted=encrypted,
        created_by_user_id=user_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub, raw_secret


def rotate_subscription_secret(db, *, tenant_id: int, sub_id: int) -> tuple:
    """Rotate the signing secret. Returns (subscription, raw_new_secret)."""
    import models
    sub = (
        db.query(models.RunWebhookSubscription)
        .filter(
            models.RunWebhookSubscription.id == sub_id,
            models.RunWebhookSubscription.tenant_id == tenant_id,
            models.RunWebhookSubscription.deleted_at.is_(None),
        )
        .first()
    )
    if sub is None:
        return None, None
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    raw_secret = generate_signing_secret()
    sub.secret_encrypted = encrypt_secret(db, tenant, raw_secret)
    db.commit()
    db.refresh(sub)
    return sub, raw_secret


def soft_delete_subscription(db, *, tenant_id: int, sub_id: int) -> bool:
    """ISO: soft-delete only (audit row is emitted by the router)."""
    import models
    sub = (
        db.query(models.RunWebhookSubscription)
        .filter(
            models.RunWebhookSubscription.id == sub_id,
            models.RunWebhookSubscription.tenant_id == tenant_id,
            models.RunWebhookSubscription.deleted_at.is_(None),
        )
        .first()
    )
    if sub is None:
        return False
    sub.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return True


def list_subscriptions(db, *, tenant_id: int, workflow_id: int) -> list:
    import models
    return (
        db.query(models.RunWebhookSubscription)
        .filter(
            models.RunWebhookSubscription.tenant_id == tenant_id,
            models.RunWebhookSubscription.workflow_id == workflow_id,
            models.RunWebhookSubscription.deleted_at.is_(None),
        )
        .order_by(models.RunWebhookSubscription.id)
        .all()
    )


def list_tenant_subscriptions(db, *, tenant_id: int,
                              skip: int = 0, limit: int = 100) -> list:
    """B36a — every live subscription in the tenant, across ALL workflows, so
    the merged Webhooks UI can list "one workflow" rows without iterating
    workflows. Soft-deleted subscriptions AND soft-deleted workflows are
    excluded (ISO 27001). Returns (sub, workflow) pairs — the caller applies
    the per-workflow permission gate and picks the public fields; the signing
    secret is NEVER part of the row.
    """
    import models
    return (
        db.query(models.RunWebhookSubscription, models.Workflow)
        .filter(
            models.RunWebhookSubscription.workflow_id == models.Workflow.id,
            models.RunWebhookSubscription.tenant_id == tenant_id,
            models.RunWebhookSubscription.deleted_at.is_(None),
            models.Workflow.deleted_at.is_(None),
        )
        .order_by(models.RunWebhookSubscription.id)
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_delivery(db, *, tenant_id: int, delivery_id: int):
    import models
    return (
        db.query(models.RunWebhookDelivery)
        .filter(
            models.RunWebhookDelivery.id == delivery_id,
            models.RunWebhookDelivery.tenant_id == tenant_id,
        )
        .first()
    )


def list_deliveries(db, *, tenant_id: int, workflow_id: Optional[int] = None,
                    sub_id: Optional[int] = None, skip: int = 0,
                    limit: int = 100) -> list:
    import models
    q = db.query(models.RunWebhookDelivery).filter(
        models.RunWebhookDelivery.tenant_id == tenant_id
    )
    if workflow_id is not None:
        q = q.filter(models.RunWebhookDelivery.workflow_id == workflow_id)
    if sub_id is not None:
        q = q.filter(models.RunWebhookDelivery.subscription_id == sub_id)
    return (
        q.order_by(models.RunWebhookDelivery.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


# --- Enqueue (called from run-finalization, AFTER the terminal commit) --------

def _lookup_run_for_enqueue(db, workflow_run_id: int):
    import models
    return (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.id == workflow_run_id)
        .first()
    )


def enqueue_run_completion(db, *, workflow_run_id: int, tenant_id: int,
                           status_str: str) -> int:
    """Create one delivery row per target for a terminal run. NO network I/O;
    the background sender does the POST. Must be called AFTER the caller has
    committed the run's terminal state (it only reads and writes its own rows,
    and a failure here must never affect the run).

    Returns the number of delivery rows created.
    """
    import models
    # Terminal-state guard: only fire for the three terminal outcomes. A
    # non-terminal status (queued/running/...) yields 0 — the run is not over.
    if _STATUS_TO_EVENT.get((status_str or "").lower()) is None:
        return 0
    run = _lookup_run_for_enqueue(db, workflow_run_id)
    if run is None:
        return 0
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if tenant is None:
        return 0

    payload = build_payload(run, status_override=status_str)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    event = payload["event"]

    targets: List[dict] = []
    subs = (
        db.query(models.RunWebhookSubscription)
        .filter(
            models.RunWebhookSubscription.tenant_id == tenant_id,
            models.RunWebhookSubscription.workflow_id == run.workflow_id,
            models.RunWebhookSubscription.is_active.is_(True),
            models.RunWebhookSubscription.deleted_at.is_(None),
        )
        .all()
    )
    for sub in subs:
        targets.append({"subscription_id": sub.id, "url": sub.url})

    # Per-run callback_url (carried in the run's stored conf / parameters).
    # The run-creation route generated a one-shot signing secret BEFORE
    # returning the run response (shown to the caller exactly once, like the
    # subscription secret) and stored its tenant-key-encrypted form in the
    # run's parameters under `run_webhook_secret_encrypted`. The delivery row
    # carries that encrypted blob so the sender can verify-sign later.
    params = run.parameters or {}
    callback_url = params.get("callback_url")
    if callback_url:
        targets.append(
            {
                "subscription_id": None,
                "url": callback_url,
                "secret_encrypted": params.get("run_webhook_secret_encrypted"),
            }
        )

    allow_private = bool(_ps.get_setting("run_webhooks.allow_private", db=db))
    allow_http = bool(_ps.get_setting("run_webhooks.allow_http", db=db))

    created = 0
    for t in targets:
        refusal, _exempt = check_target(
            db, tenant_id, t["url"], allow_http=allow_http,
            allow_private=allow_private,
            subscription_id=t.get("subscription_id"), phase="enqueue",
        )
        status = DELIVERY_REJECTED if refusal else DELIVERY_PENDING
        delivery = models.RunWebhookDelivery(
            tenant_id=tenant_id,
            workflow_id=run.workflow_id,
            subscription_id=t.get("subscription_id"),
            run_id=workflow_run_id,
            url=t["url"],
            event=event,
            payload_body=body,
            delivery_uuid=str(uuid.uuid4()),
            secret_encrypted=t.get("secret_encrypted"),
            status=status,
            attempts=0,
            next_attempt_at=datetime.now(timezone.utc),
            last_error=refusal,
        )
        db.add(delivery)
        created += 1
    db.commit()
    if created:
        logger.info(
            "run_webhooks: enqueued %d delivery(ies) for run %s (%s)",
            created, workflow_run_id, event,
        )
    return created


def _default_session_factory():
    """Create the DB session the background enqueue thread uses. A plain
    module-level function (not inlined) so tests can monkeypatch it to bind
    onto the test harness's own connection — `database.SessionLocal` requires
    `database.initialize()`, which the test app never calls."""
    from database import SessionLocal

    return SessionLocal()


def enqueue_run_completion_background(tenant_id: int, workflow_run_id: int,
                                      status_str: str) -> None:
    """Schedule `enqueue_run_completion` on a daemon thread with its own DB
    session so the finalize path (which may still be holding a session) never
    blocks on it and never pins its connection. Same pattern as
    services.outbound_webhooks.dispatch_workflow_completion_background.

    The caller has ALREADY committed the run's terminal state; this thread only
    reads it and writes the delivery rows.
    """

    def _run() -> None:
        try:
            db = _default_session_factory()
        except Exception:
            logger.exception(
                "run_webhooks: enqueue session creation failed (run=%s)",
                workflow_run_id,
            )
            return
        try:
            enqueue_run_completion(
                db, workflow_run_id=workflow_run_id, tenant_id=tenant_id,
                status_str=status_str,
            )
        except Exception:
            logger.exception(
                "run_webhooks: enqueue failed for run %s", workflow_run_id
            )
        finally:
            if db is not None:
                db.close()

    threading.Thread(
        target=_run, name=f"run-webhook-enqueue-{workflow_run_id}", daemon=True
    ).start()


# --- Background sender --------------------------------------------------------
# APScheduler job (registered in services/scheduler.py:start_scheduler). Runs
# on its own DB session, never inside any request or run-finalization
# transaction. The tick is a no-op when no delivery is due.

def _due_deliveries(db, now: datetime, limit: int = 50) -> list:
    import models
    return (
        db.query(models.RunWebhookDelivery)
        .filter(
            models.RunWebhookDelivery.status == DELIVERY_PENDING,
            models.RunWebhookDelivery.next_attempt_at <= now,
        )
        .order_by(models.RunWebhookDelivery.next_attempt_at)
        .limit(limit)
        .all()
    )


def _resolve_secret(db, delivery) -> Optional[str]:
    """The signing secret for a delivery row.

    Subscription rows: decrypt the stored tenant-key-encrypted secret.
    Per-run callback rows: the one-shot secret stored (encrypted, same
    tenant-key pattern) on the delivery row itself at enqueue time.
    """
    import models
    if delivery.subscription_id is not None:
        sub = (
            db.query(models.RunWebhookSubscription)
            .filter(models.RunWebhookSubscription.id == delivery.subscription_id)
            .first()
        )
        if sub is None:
            return None
        tenant = db.query(models.Tenant).filter(
            models.Tenant.id == sub.tenant_id
        ).first()
        if tenant is None:
            return None
        return decrypt_secret(db, tenant, sub.secret_encrypted)
    tenant = db.query(models.Tenant).filter(
        models.Tenant.id == delivery.tenant_id
    ).first()
    if tenant is None or not delivery.secret_encrypted:
        return None
    return decrypt_secret(db, tenant, delivery.secret_encrypted)


def _backoff_for_attempt(attempts: int) -> Optional[timedelta]:
    """Delay before attempt number `attempts + 1` (1-based ladder). None when
    the attempt cap is reached."""
    if attempts >= MAX_ATTEMPTS:
        return None
    # After attempt N, wait RETRY_BACKOFF_MINUTES[N-1]: first retry +1m, then
    # +5m, +30m, +2h, +6h (so MAX_ATTEMPTS attempts total).
    minutes = RETRY_BACKOFF_MINUTES[min(attempts - 1, len(RETRY_BACKOFF_MINUTES) - 1)]
    return timedelta(minutes=minutes)


def attempt_delivery(db, delivery) -> None:
    """Perform ONE send attempt for a delivery row and update the row in the
    caller's session (committed by the tick). Never raises: every failure is
    recorded on the row and logged.

    Success  -> status=delivered, next_attempt_at=None.
    Failure  -> attempts+1; next_attempt_at per the backoff ladder, or
                status=failed once MAX_ATTEMPTS is spent.
    """
    import models
    now = datetime.now(timezone.utc)
    delivery.last_attempted_at = now
    attempt_number = (delivery.attempts or 0) + 1

    def _record_attempt(outcome: str, http_status=None, error: str = None) -> None:
        """Append the append-only attempt row (ISO: never updated/deleted)."""
        db.add(models.RunWebhookDeliveryAttempt(
            delivery_id=delivery.id,
            attempt_number=attempt_number,
            outcome=outcome,
            http_status=http_status,
            error=error,
        ))

    allow_private = bool(_ps.get_setting("run_webhooks.allow_private", db=db))
    allow_http = bool(_ps.get_setting("run_webhooks.allow_http", db=db))
    refusal, exempt_host = check_target(
        db, delivery.tenant_id, delivery.url, allow_http=allow_http,
        allow_private=allow_private,
        subscription_id=delivery.subscription_id, phase="delivery",
    )
    if refusal:
        # DNS drifted to an internal address since enqueue — stop retrying.
        delivery.status = DELIVERY_REJECTED
        delivery.last_error = refusal[:500]
        delivery.last_http_status = None
        _record_attempt("rejected", error=refusal[:500])
        return

    # GSEC FX.19 R-042 — webhook egress disabled: mark rejected (no retry —
    # the platform admin must re-enable the switch) and skip the outbound
    # send. Audit-safe log line, never raises (the run already completed).
    from utils import egress_policy
    r042 = egress_policy.refuse_reason(egress_policy.CATEGORY_WEBHOOKS, db=db)
    if r042:
        delivery.status = DELIVERY_REJECTED
        delivery.last_error = r042[:500]
        delivery.last_http_status = None
        delivery.next_attempt_at = None
        _record_attempt("rejected", error=r042[:500])
        logger.info("run_completion_webhooks: send rejected (delivery=%s) — %s",
                    getattr(delivery, "id", None), r042)
        return

    # GSEC FX.161.p1b — a destination that drifted outside the tenant's
    # output allowlist since enqueue is rejected (no retry); audited by
    # check_destination itself, never raised into the delivery tick.
    from fastapi import HTTPException as _HTTPException
    # FX.162.p5 — CE carve: no DLP output allowlist -> no restriction.
    from utils.paid_import import optional_paid
    _dlp = optional_paid("services.dlp_output_allowlist")
    try:
        if _dlp is not None:
            _dlp.check_destination(
                db, delivery.tenant_id, delivery.url,
                kind="run_completion_webhook",
            )
    except _HTTPException:
        delivery.status = DELIVERY_REJECTED
        delivery.last_error = "destination not in tenant output allowlist"
        delivery.last_http_status = None
        delivery.next_attempt_at = None
        _record_attempt("rejected", error=delivery.last_error)
        return

    secret = _resolve_secret(db, delivery)
    if secret is None:
        # Subscription deleted or key material lost — no point retrying.
        delivery.status = DELIVERY_REJECTED
        delivery.last_error = "signing secret unavailable"
        delivery.last_http_status = None
        _record_attempt("rejected", error="signing secret unavailable")
        return

    body = delivery.payload_body
    if isinstance(body, str):
        body = body.encode("utf-8")
    timestamp = str(int(now.timestamp()))
    headers = {
        "Content-Type": "application/json",
        SIG_HEADER: compute_signature(secret, timestamp, body),
        TIMESTAMP_HEADER: timestamp,
        EVENT_HEADER: delivery.event,
        DELIVERY_HEADER: delivery.delivery_uuid,
    }
    from utils import safe_http
    extra = {"_ssrf_private_ok_host": exempt_host} if exempt_host else {}
    try:
        resp = safe_http.post(
            delivery.url,
            data=body,
            headers=headers,
            _ssrf_connect_timeout=DELIVERY_TIMEOUT_SECONDS,
            _ssrf_read_timeout=DELIVERY_TIMEOUT_SECONDS,
            tenant_id=delivery.tenant_id,
            **extra,
        )
        delivery.last_http_status = resp.status_code
        if 200 <= resp.status_code < 300:
            delivery.status = DELIVERY_DELIVERED
            delivery.attempts = attempt_number
            delivery.last_error = None
            delivery.next_attempt_at = None
            _record_attempt("sent_2xx", http_status=resp.status_code)
            return
        # Non-2xx: retryable (the subscriber may be temporarily unhealthy).
        delivery.last_error = f"HTTP {resp.status_code}"[:500]
        _record_attempt("sent_non2xx", http_status=resp.status_code,
                        error=delivery.last_error)
    except safe_http.SsrfBlocked:
        # DNS drifted to an internal address since enqueue — stop retrying
        # (host only in the record; the URL may carry tokens).
        delivery.status = DELIVERY_REJECTED
        delivery.last_error = "ssrf_guard"
        delivery.last_http_status = None
        delivery.next_attempt_at = None
        _record_attempt("rejected", error="ssrf_guard")
        return
    except Exception as e:  # noqa: BLE001 — network errors are retryable
        delivery.last_http_status = None
        delivery.last_error = f"{type(e).__name__}: {e}"[:500]
        _record_attempt("error", error=delivery.last_error)

    delivery.attempts = attempt_number
    backoff = _backoff_for_attempt(delivery.attempts)
    if backoff is None:
        delivery.status = DELIVERY_FAILED_PERMANENT
        delivery.next_attempt_at = None
    else:
        delivery.next_attempt_at = now + backoff


def run_webhook_delivery_tick(session_factory) -> dict:
    """One sweep of due deliveries. `session_factory` is a zero-arg callable
    returning a fresh DB session (database.SessionLocal in production; the
    test harness patches it). Returns counters for logging."""
    db = None
    counts = {"due": 0, "delivered": 0, "failed": 0, "rejected": 0, "errors": 0}
    try:
        db = session_factory()
        now = datetime.now(timezone.utc)
        due = _due_deliveries(db, now)
        counts["due"] = len(due)
        for delivery in due:
            try:
                attempt_delivery(db, delivery)
                db.flush()
            except Exception:
                db.rollback()
                counts["errors"] += 1
                logger.exception(
                    "run_webhooks: attempt failed for delivery id=%s", delivery.id
                )
                continue
            if delivery.status == DELIVERY_DELIVERED:
                counts["delivered"] += 1
            elif delivery.status == DELIVERY_FAILED_PERMANENT:
                counts["failed"] += 1
            elif delivery.status == DELIVERY_REJECTED:
                counts["rejected"] += 1
        db.commit()
    except Exception:
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        counts["errors"] += 1
        logger.exception("run_webhooks: delivery tick failed")
    finally:
        if db is not None:
            db.close()
    return counts

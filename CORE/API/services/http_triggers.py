"""GX.S7 (launch-completion-megaplan Group X) — scheduled HTTP triggers.

A tenant/app defines a trigger: cron expression OR interval seconds -> an
HMAC-SHA256-signed HTTP call (method + path) to a Persistent Stack service
or an App target, over the internal network.

Design invariants (see the migration for the data model):

* ONE APScheduler job (registered in services/scheduler.py start_scheduler)
  sweeps every 30s. It claims due rows with ``SELECT ... FOR UPDATE SKIP
  LOCKED`` so multiple API processes never double-fire.
* The outbound HTTP call is NEVER made inside an open DB transaction
  (pgbouncer pinning): the claim commits first, the call runs on plain
  (unattached) state, and a short second session writes the run-history row.
* Every fire writes an APPEND-ONLY ``HttpTriggerRun`` row (success OR
  failure). A BEFORE DELETE trigger on the table makes physical deletes
  raise (ISO 27001).
* On failure (non-2xx / timeout / N consecutive), an alert fans out through
  all three channels: MOD notification (services.app_notifications), email
  (services.email_send_api), and an outbound webhook (reusing the HMAC +
  SSRF pattern from services.run_completion_webhooks).
* Per-trigger HMAC secret: encrypted under the tenant key (the
  ApiKey.encrypted_keycloak_secret pattern), returned ONCE at creation.
* MOD -> stack/app caller identity (GX.S7): a fire AT a stack-service or app
  target additionally carries a short-TTL (<=300 s) RS256 "MOD trigger call"
  token (typ ``mod-trigger-call-v1``, utils.build_task_token) as
  ``Authorization: Bearer <token>`` IN ADDITION TO the HMAC headers (which
  are ALWAYS sent — existing receivers keep working). Gated by the platform
  setting ``http_triggers.identity_signing_enabled`` (default on). The
  receiver-side verification path is documented in
  docs/build-on-mod/GUIDE.md (HTTP triggers section).
* Tunables live in the platform-settings registry under ``triggers.*`` —
  never env.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlparse

from fastapi import HTTPException, status as http_status
from sqlalchemy import text
from sqlalchemy.orm import Session

import models
from services import platform_settings as _ps
from services import tenant_key_local
from utils.paid_import import optional_paid

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')

# Keep the name `key_provider` — tests monkeypatch it. In the Paid tree this
# IS services.key_provider (vault/pkcs11/kmip backends included); in the CE
# carve it falls back to the shipped local-only module, byte-compatible.
key_provider = optional_paid("services.key_provider") or tenant_key_local


# --- Platform-setting helpers (never env) ------------------------------------

def _timeout_seconds(db) -> int:
    return int(_ps.get_setting("triggers.timeout_seconds", db=db))


def _max_per_tenant(db) -> int:
    return int(_ps.get_setting("triggers.max_per_tenant", db=db))


def _failure_alert_threshold(db) -> int:
    return int(_ps.get_setting("triggers.failure_alert_threshold", db=db))


def _min_interval_seconds(db) -> int:
    return int(_ps.get_setting("triggers.min_interval_seconds", db=db))


# --- Cron / interval next-fire computation -----------------------------------

def _parse_cron_field(field: str, lo: int, hi: int) -> List[int]:
    """Parse one 5-field cron component (supports *, lists, ranges, steps)."""
    values = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty cron field component in {field!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"invalid cron step in {field!r}")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron value out of range in {field!r}")
        values.update(range(start, end + 1, step))
    return sorted(values)


def next_fire_for(
    *,
    schedule_kind: str,
    cron_expression: Optional[str] = None,
    interval_seconds: Optional[int] = None,
    after: datetime,
) -> datetime:
    """Compute the next fire time strictly after `after` (UTC).

    cron   -> the next wall-clock slot matching the 5-field expression.
    interval -> after + interval_seconds.
    """
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    after = after.astimezone(timezone.utc)

    if schedule_kind == "interval":
        if not interval_seconds or interval_seconds <= 0:
            raise ValueError("interval schedule requires a positive interval_seconds")
        return after + timedelta(seconds=interval_seconds)

    if schedule_kind == "cron":
        if not cron_expression:
            raise ValueError("cron schedule requires a cron_expression")
        fields = cron_expression.split()
        if len(fields) != 5:
            raise ValueError("cron_expression must have exactly 5 fields")
        mins = _parse_cron_field(fields[0], 0, 59)
        hours = _parse_cron_field(fields[1], 0, 23)
        days = _parse_cron_field(fields[2], 1, 31)
        months = _parse_cron_field(fields[3], 1, 12)
        dows = _parse_cron_field(fields[4], 0, 6)  # 0 = Sunday

        # Start at the first minute-boundary STRICTLY after `after` (so a
        # trigger due exactly at its own next-fire slot re-fires on the NEXT
        # matching slot, not the same one).
        candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        # Scan forward, minute by minute, up to ~4 years (bounded).
        for _ in range(4 * 366 * 24 * 60):
            if (candidate.month in months
                    and candidate.day in days
                    and candidate.hour in hours
                    and candidate.minute in mins
                    and candidate.isoweekday() % 7 in dows):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("no cron fire time found within 4 years")

    raise ValueError(f"unknown schedule_kind {schedule_kind!r}")


# --- SSRF guard (FX.151: the shared services/ssrf_guard.py) ------------------

from services import ssrf_guard as _ssrf_guard

# The guard itself lives in services/ssrf_guard.py — one implementation for
# every outbound-URL site (FX.151), so triggers can no longer drift from the
# webhooks. These names stay as re-exports/aliases because import sites and
# tests reference them on THIS module.
_BLOCKED_INTERNAL_HOSTNAMES = _ssrf_guard.BLOCKED_INTERNAL_HOSTNAMES
_ip_refused = _ssrf_guard._ip_refused

# Cloud-metadata addresses, refused even when a caller opts into private
# addressing (they are link-local, but the IPv6 form below is not).
_METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254"})


def _ssrf_optin_exempt(url: str) -> bool:
    """True when ``allow_private`` must NOT unlock this target.

    ``allow_private`` is an opt-in to private ranges for a validated stack/
    app service — it never unlocks link-local / cloud-metadata (a hosts-file
    trick pointing at the metadata endpoint is still metadata), and an
    unresolvable host is never a validated service. The shared guard keeps
    refusing loopback under that setting, so this only has to cover the
    link-local / metadata pair plus unresolvability, literal and resolved
    alike.
    """
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            candidates = [sockaddr[0]
                          for *_rest, sockaddr in socket.getaddrinfo(host, None)]
        except Exception:  # noqa: BLE001 — unresolvable -> never opt-in
            candidates = []
    if not candidates:
        return True
    for cand in candidates:
        if cand.lower().rstrip(".") in _METADATA_HOSTS:
            return True
        try:
            if ipaddress.ip_address(cand).is_link_local:
                return True
        except ValueError:
            continue
    return False


def ssrf_check(url: str, *, allow_private: bool = False) -> Optional[str]:
    """Return a refusal reason if `url` must NOT be called, else None.

    Thin wrapper over :func:`services.ssrf_guard.check_url` — no policy lives
    here. Triggers legitimately fire over the internal network, so http is
    accepted (``allow_http=True``); private ranges only when the caller has
    validated the target as a registered Persistent Stack service or App
    (``allow_private``). Loopback, link-local / cloud-metadata, reserved,
    multicast and the docker-internal hostnames are refused either way.
    """
    if allow_private and _ssrf_optin_exempt(url):
        allow_private = False
    try:
        _ssrf_guard.check_url(url, allow_http=True, allow_private=allow_private)
    except _ssrf_guard.SsrfBlocked as e:
        return str(e)
    return None


# --- HMAC signing (X-MOD-Signature / X-MOD-Timestamp, like GX.R3) -------------

SIG_HEADER = "X-MOD-Signature"
TIMESTAMP_HEADER = "X-MOD-Timestamp"
EVENT_HEADER = "X-MOD-Event"
DELIVERY_HEADER = "X-MOD-Delivery"


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """`X-MOD-Signature` value: `sha256=<hex>` over HMAC-SHA256 of
    `<timestamp>.<body>` keyed by the trigger's signing secret."""
    msg = timestamp.encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def sign_request(url: str, method: str, path: str, body: bytes,
                 timestamp: str, secret: str,
                 identity_token: Optional[str] = None) -> Dict[str, str]:
    """Headers for one signed trigger call.

    The per-trigger HMAC (X-MOD-Signature / X-MOD-Timestamp, secret = the
    per-trigger signing secret) is ALWAYS present — that is the contract
    existing receivers were built against. When ``identity_token`` is given
    (a minted ``mod-trigger-call-v1`` JWT, GX.S7), it is sent IN ADDITION as
    ``Authorization: Bearer <token>`` so the receiver can verify the call
    came from MOD itself (see docs/build-on-mod/GUIDE.md, HTTP triggers).
    The HMAC stays because (a) pre-GX.S7 receivers only know the HMAC and
    (b) the token is per-call (300 s) while the secret is per-trigger, so
    either check alone is not a complete replay/origin control.
    """
    headers = {
        "Content-Type": "application/json",
        SIG_HEADER: compute_signature(secret, timestamp, body),
        TIMESTAMP_HEADER: timestamp,
        EVENT_HEADER: "http_trigger.fire",
        DELIVERY_HEADER: str(uuid.uuid4()),
    }
    if identity_token:
        headers["Authorization"] = "Bearer " + identity_token
    return headers


# --- Secret encryption (tenant-key pattern from api_keys / run_webhooks) ------

def _ensure_tenant_encryption_key(db, tenant) -> str:
    if not tenant.encryption_key:
        _plaintext, tenant.encryption_key = key_provider.generate_and_wrap_tenant_key(
            tenant.id, db=db)
        db.flush()
    return tenant.encryption_key


def encrypt_secret(db, tenant, plaintext: str) -> str:
    from utils import encryption
    key_material = _ensure_tenant_encryption_key(db, tenant)
    try:
        decrypted_tenant_key = key_provider.unwrap_tenant_key(key_material, tenant.id, db=db)
    except tenant_key_local.PaidKeyBackendRequired:
        raise HTTPException(
            status_code=402,
            detail="This secret was encrypted by a key backend (Vault/HSM/KMS) "
                   "not available in MOD Community Edition.",
        )
    return encryption.encrypt_value(plaintext, decrypted_tenant_key)


def decrypt_secret(db, tenant, encrypted_value: str) -> Optional[str]:
    from utils import encryption
    if not encrypted_value or not tenant.encryption_key:
        return None
    try:
        decrypted_tenant_key = key_provider.unwrap_tenant_key(
            tenant.encryption_key, tenant.id, db=db)
        return encryption.decrypt_value(encrypted_value, decrypted_tenant_key)
    except tenant_key_local.PaidKeyBackendRequired:
        raise HTTPException(
            status_code=402,
            detail="This secret was encrypted by a key backend (Vault/HSM/KMS) "
                   "not available in MOD Community Edition.",
        )
    except Exception:  # noqa: BLE001 — corrupt/rotated key material
        logger.warning("http_triggers: failed to decrypt trigger secret (tenant=%s)",
                       tenant.id)
        return None


def generate_trigger_secret() -> str:
    return "mod_trg_" + secrets.token_urlsafe(32)


# --- MOD -> stack/app caller identity (GX.S7) --------------------------------

SETTING_IDENTITY_SIGNING = "http_triggers.identity_signing_enabled"


def _identity_signing_enabled(db) -> bool:
    """The platform gate for the Bearer trigger-call token (default on).
    A settings read failure degrades to HMAC-only (never breaks the fire)."""
    try:
        return bool(_ps.get_setting(SETTING_IDENTITY_SIGNING, db=db))
    except Exception:  # noqa: BLE001
        return False


def mint_call_token(*, tenant_id: int, target_kind: str, target_id: Optional[int],
                    trigger_id: int, body: bytes, fired_at: int) -> Optional[str]:
    """Mint the ``mod-trigger-call-v1`` Bearer token for ONE fire's outbound
    call, or None when signing must degrade to HMAC-only (no manager signing
    key provisioned, or mint failure). Minting is pure key+claims — safe to
    run with no DB session open (phase 2 of the fire)."""
    if target_kind not in ("stack", "app") or target_id is None:
        return None
    from utils import build_task_token as _btt
    if not _btt.is_configured():
        return None
    try:
        return _btt.mint_trigger_call_token(
            tenant_id=tenant_id, target_kind=target_kind,
            target_id=int(target_id), trigger_id=trigger_id,
            body=body, fired_at=int(fired_at),
        )
    except Exception as e:  # noqa: BLE001 — never fail a fire on identity
        logger.warning("http_triggers: trigger-call token mint failed "
                       "(trigger=%s): %s", trigger_id, e)
        return None


# --- Target URL resolution ----------------------------------------------------

def _base_with_port(lb_endpoint: Optional[str],
                    port: Optional[int]) -> Optional[str]:
    """Normalise a stack ``lb_endpoint`` to a ``scheme://host[:port]`` base.

    ``lb_endpoint`` may carry a scheme and/or a path (``http://h:8080/x``), or
    no scheme at all (``h:8000``). When ``port`` is set it REPLACES whatever
    port the endpoint already carries — the old code APPENDED it, which built
    ``http://h:8080:8080/path`` and made every stack-target fire fail (1644).
    IPv6 hosts keep their brackets. Returns None when there is no usable host.
    """
    if not lb_endpoint:
        return None
    if "://" in lb_endpoint:
        parts = urlsplit(lb_endpoint)
        scheme = parts.scheme
    else:
        # Schemeless row ("h:8000"): urlsplit would read "h" as the scheme, so
        # prefix a bare netloc and default the scheme to http.
        parts = urlsplit("//" + lb_endpoint)
        scheme = "http"
    netloc = parts.netloc
    if "]" in netloc:  # bracketed IPv6 host
        cut = netloc.rindex("]")
        host, existing = netloc[:cut + 1], netloc[cut + 1:]
        existing = existing[1:] if existing.startswith(":") else ""
    else:
        host, _, existing = netloc.partition(":")
        if not existing.isdigit():
            existing = ""
    if not host:
        return None
    base = f"{scheme}://{host}"
    if port:
        base += f":{int(port)}"
    elif existing:
        base += f":{existing}"
    return base


def resolve_target_url(db, trigger) -> Optional[str]:
    """Resolve the concrete URL a trigger calls, from its identity rows.

    Returns None when the referenced stack/app no longer exists (or is
    deleted) — the fire is then recorded as a 'skipped'/'failed' run and no
    HTTP call is made.
    """
    if trigger.target_kind == "stack-service":
        if not trigger.stack_id:
            return None
        stack = db.query(models.PersistentStack).filter(
            models.PersistentStack.id == trigger.stack_id,
            models.PersistentStack.tenant_id == trigger.tenant_id,
        ).first()
        if stack is None or stack.lb_endpoint is None:
            return None
        base = _base_with_port(stack.lb_endpoint, trigger.stack_port)
        if base is None:
            return None
        # NOTE(GX.S7): per-service mesh endpoints DO exist — the worker-
        # advertised MagicDNS name in `stack.mesh_endpoints`
        # (services.app_routing._magicdns_name) — but they resolve only on
        # the site/tailnet DNS plane, which the manager-side API process
        # cannot always reach (tailnet-egress sidecar is per-install). The
        # lb_endpoint host:port is the routable address for the outbound
        # call, so it stays the resolution source. No routing-layer change.
        path = (trigger.path or "")
        if not path.startswith("/"):
            path = "/" + path
        return base + path
    if trigger.target_kind == "app":
        if not trigger.app_id:
            return None
        app = db.query(models.App).filter(
            models.App.id == trigger.app_id,
            models.App.tenant_id == trigger.tenant_id,
            models.App.deleted_at.is_(None),
        ).first()
        if app is None:
            return None
        # NOTE(GX.S7): the app's own routing rows (app.target_kind +
        # stack_service/stack_port) and the services.app_routing.resolve_upstream
        # resolver exist, but that resolver returns the worker-advertised
        # MagicDNS name (stack.mesh_endpoints), which only resolves on the
        # site/tailnet DNS plane — the manager-side API process cannot always
        # reach it. external_url + stack lb_endpoint:stack_port remain the
        # routable resolution source for the outbound call. No routing-layer
        # change.
        base = None
        if app.external_url:
            base = app.external_url
        elif app.stack_id:
            stack = db.query(models.PersistentStack).filter(
                models.PersistentStack.id == app.stack_id).first()
            if stack and stack.lb_endpoint:
                base = _base_with_port(stack.lb_endpoint, app.stack_port)
        if not base:
            return None
        path = (trigger.path or "")
        if not path.startswith("/"):
            path = "/" + path
        return base + path
    return None


# --- CRUD (router-facing, no HTTP) -------------------------------------------

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
_TARGET_KINDS = {"stack-service", "app"}


def _count_live(db, tenant_id: int) -> int:
    return (
        db.query(models.HttpTrigger)
        .filter(models.HttpTrigger.tenant_id == tenant_id,
                models.HttpTrigger.deleted_at.is_(None))
        .count()
    )


def _validate_schedule(db, *, schedule_kind, cron_expression,
                       interval_seconds) -> None:
    min_interval = _min_interval_seconds(db)
    if schedule_kind == "interval":
        if not interval_seconds or interval_seconds < min_interval:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"interval_seconds must be at least {min_interval}",
            )
    elif schedule_kind == "cron":
        if not cron_expression:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="cron_expression is required for a cron schedule",
            )
        # Validate the expression parses + has a reachable next fire.
        try:
            next_fire_for(schedule_kind="cron",
                          cron_expression=cron_expression,
                          after=datetime.now(timezone.utc))
        except ValueError as e:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid cron_expression: {e}",
            ) from None
    else:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="schedule_kind must be 'cron' or 'interval'",
        )


def _verify_target_ownership(db, *, tenant_id: int,
                             app_id: Optional[int],
                             stack_id: Optional[int]) -> None:
    """GX.G2: reject foreign/nonexistent target ids at create time.

    Mirrors the tenant + soft-delete filters resolve_target_url applies at
    fire time (App.deleted_at IS NULL; PersistentStack has no soft-delete
    column). Foreign ids get the same generic 404 as a missing trigger so
    their existence is not revealed.
    """
    if app_id:
        app = db.query(models.App).filter(
            models.App.id == app_id,
            models.App.tenant_id == tenant_id,
            models.App.deleted_at.is_(None),
        ).first()
        if app is None:
            raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND,
                                detail="target not found")
    if stack_id:
        stack = db.query(models.PersistentStack).filter(
            models.PersistentStack.id == stack_id,
            models.PersistentStack.tenant_id == tenant_id,
        ).first()
        if stack is None:
            raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND,
                                detail="target not found")


def create_trigger(db, *, tenant_id: int, user_id: Optional[int],
                   app_id: Optional[int], name: str,
                   schedule_kind: str, cron_expression: Optional[str],
                   interval_seconds: Optional[int], method: str,
                   path: str, target_kind: str, stack_id: Optional[int],
                   stack_service: Optional[str], stack_port: Optional[int],
                   callback_url: Optional[str]) -> Dict[str, Any]:
    """Create a trigger. Returns a dict INCLUDING the plaintext signing
    secret — shown ONCE to the caller (like GX.R3 / api_keys)."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="name is required")
    method = (method or "POST").upper()
    if method not in _METHODS:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"method must be one of {sorted(_METHODS)}")
    if target_kind not in _TARGET_KINDS:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="target_kind must be 'stack-service' or 'app'")
    if target_kind == "stack-service" and not stack_id:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="stack_id is required for a stack-service target")
    if target_kind == "app" and not app_id:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="app_id is required for an app target")
    _verify_target_ownership(db, tenant_id=tenant_id,
                             app_id=app_id, stack_id=stack_id)

    # FIX-CE-UNLIMITED-A: the CE default for triggers.max_per_tenant is the
    # sentinel 0 = unlimited (CE has no quantity caps).
    # `limit > 0` is the smallest guard: a no-op for any finite limit, so
    # Paid (50) and any explicit operator row enforce exactly as before.
    _limit = _max_per_tenant(db)
    if _limit > 0 and _count_live(db, tenant_id) >= _limit:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="triggers.max_per_tenant limit reached",
        )

    _validate_schedule(db, schedule_kind=schedule_kind,
                       cron_expression=cron_expression,
                       interval_seconds=interval_seconds)

    if callback_url:
        refusal = ssrf_check(callback_url, allow_private=True)
        if refusal:
            raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"callback_url refused: {refusal}")

    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND,
                            detail="tenant not found")

    plain_secret = generate_trigger_secret()
    enc_secret = encrypt_secret(db, tenant, plain_secret)

    now = datetime.now(timezone.utc)
    next_fire = next_fire_for(
        schedule_kind=schedule_kind, cron_expression=cron_expression,
        interval_seconds=interval_seconds, after=now,
    )
    trig = models.HttpTrigger(
        tenant_id=tenant_id, app_id=app_id, name=name,
        schedule_kind=schedule_kind, cron_expression=cron_expression,
        interval_seconds=interval_seconds, method=method,
        path=(path or "").strip() or "", target_kind=target_kind,
        stack_id=stack_id, stack_service=stack_service, stack_port=stack_port,
        encrypted_secret=enc_secret, callback_url=callback_url,
        status="active", consecutive_failures=0,
        next_fire_at=next_fire, created_by_user_id=user_id,
    )
    db.add(trig)
    db.flush()
    _audit(db, tenant_id=tenant_id, user_id=user_id,
           action=models.AuditAction.HTTP_TRIGGER_CREATED,
           details={"trigger_id": trig.id, "name": name,
                    "schedule_kind": schedule_kind, "target_kind": target_kind})
    db.commit()
    db.refresh(trig)
    return {
        "id": trig.id, "name": trig.name, "secret": plain_secret,
        "next_fire_at": trig.next_fire_at.isoformat(),
    }


def _get_for_tenant(db, trigger_id: int, tenant_id: int):
    return (
        db.query(models.HttpTrigger)
        .filter(models.HttpTrigger.id == trigger_id,
                models.HttpTrigger.tenant_id == tenant_id,
                models.HttpTrigger.deleted_at.is_(None))
        .first()
    )


def get_trigger(db, trigger_id: int, tenant_id: int) -> Optional[models.HttpTrigger]:
    return _get_for_tenant(db, trigger_id, tenant_id)


def _public(row: models.HttpTrigger) -> Dict[str, Any]:
    return {
        "id": row.id,
        "app_id": row.app_id,
        "name": row.name,
        "schedule_kind": row.schedule_kind,
        "cron_expression": row.cron_expression,
        "interval_seconds": row.interval_seconds,
        "method": row.method,
        "path": row.path,
        "target_kind": row.target_kind,
        "stack_id": row.stack_id,
        "stack_service": row.stack_service,
        "stack_port": row.stack_port,
        "callback_url": row.callback_url,
        "status": row.status,
        "consecutive_failures": row.consecutive_failures,
        "last_fired_at": row.last_fired_at.isoformat() if row.last_fired_at else None,
        "next_fire_at": row.next_fire_at.isoformat() if row.next_fire_at else None,
        # NOTE: `encrypted_secret` is NEVER returned after creation.
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_triggers(db, tenant_id: int) -> List[Dict[str, Any]]:
    rows = (
        db.query(models.HttpTrigger)
        .filter(models.HttpTrigger.tenant_id == tenant_id,
                models.HttpTrigger.deleted_at.is_(None))
        .order_by(models.HttpTrigger.id)
        .all()
    )
    return [_public(r) for r in rows]


def _set_next_fire(db, trigger, now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    trigger.next_fire_at = next_fire_for(
        schedule_kind=trigger.schedule_kind,
        cron_expression=trigger.cron_expression,
        interval_seconds=trigger.interval_seconds,
        after=now,
    )


def pause_trigger(db, trigger_id: int, tenant_id: int,
                  user_id: Optional[int]) -> Optional[models.HttpTrigger]:
    t = _get_for_tenant(db, trigger_id, tenant_id)
    if t is None:
        return None
    t.status = "paused"
    t.paused_at = datetime.now(timezone.utc)
    t.paused_by_user_id = user_id
    db.commit()
    db.refresh(t)
    return t


def resume_trigger(db, trigger_id: int, tenant_id: int,
                   user_id: Optional[int]) -> Optional[models.HttpTrigger]:
    t = _get_for_tenant(db, trigger_id, tenant_id)
    if t is None:
        return None
    t.status = "active"
    t.paused_at = None
    _set_next_fire(db, t)
    db.commit()
    db.refresh(t)
    return t


def soft_delete_trigger(db, trigger_id: int, tenant_id: int,
                        user_id: Optional[int]) -> bool:
    t = _get_for_tenant(db, trigger_id, tenant_id)
    if t is None:
        return False
    t.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return True


def list_runs(db, trigger_id: int, tenant_id: int, *,
              skip: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
    t = _get_for_tenant(db, trigger_id, tenant_id)
    if t is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND,
                            detail="trigger not found")
    rows = (
        db.query(models.HttpTriggerRun)
        .filter(models.HttpTriggerRun.trigger_id == trigger_id,
                models.HttpTriggerRun.tenant_id == tenant_id)
        .order_by(models.HttpTriggerRun.fired_at.desc(), models.HttpTriggerRun.id.desc())
        .offset(skip).limit(limit)
        .all()
    )
    return [{
        "id": r.id, "status": r.status, "resolved_url": r.resolved_url,
        "http_status": r.http_status, "error": r.error,
        "duration_ms": r.duration_ms, "alert_sent": r.alert_sent,
        "fired_at": r.fired_at.isoformat() if r.fired_at else None,
    } for r in rows]


# --- The fire path (one call, never in an open transaction) -------------------

def _write_run(session_factory, *, trigger_id: int, tenant_id: int,
               status: str, resolved_url: Optional[str] = None,
               http_status: Optional[int] = None, error: Optional[str] = None,
               duration_ms: Optional[int] = None,
               alert_sent: bool = False) -> None:
    db = None
    try:
        db = session_factory()
        db.add(models.HttpTriggerRun(
            trigger_id=trigger_id, tenant_id=tenant_id, status=status,
            resolved_url=resolved_url, http_status=http_status,
            error=error, duration_ms=duration_ms, alert_sent=alert_sent,
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        logger.exception("http_triggers: failed to write run row (trigger=%s)",
                         trigger_id)
    finally:
        if db is not None:
            db.close()


def _claim_due(db, now: datetime, limit: int = 50) -> List[int]:
    """Pick due triggers with ``SELECT ... FOR UPDATE SKIP LOCKED`` so
    multiple API processes never double-fire the same due row. The caller
    then advances each claimed row's ``next_fire_at`` and commits, which
    both releases the row locks and makes the row non-due for a second
    process (belt + suspenders)."""
    rows = db.execute(
        text(
            """
            SELECT id FROM http_triggers
            WHERE deleted_at IS NULL AND status = 'active' AND next_fire_at <= :now
            ORDER BY next_fire_at
            LIMIT :lim
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"now": now, "lim": limit},
    ).fetchall()
    return [r[0] for r in rows]


def _mark_fired(db, trigger, now: datetime) -> None:
    """Advance next_fire_at and record last_fired_at for a claimed trigger."""
    trigger.last_fired_at = now
    _set_next_fire(db, trigger, now=now)
    db.commit()


def run_trigger_fire(session_factory, *, trigger_id: int) -> Dict[str, Any]:
    """Perform ONE fire for an already-claimed trigger.

    Called with a session_factory (zero-arg -> fresh Session). The pattern:
    open session -> load trigger + secret + tenant + resolve URL -> COMMIT
    (releasing the DB connection so the outbound HTTP call is NOT inside an
    open transaction) -> make the httpx call -> open a NEW session for the
    run-history row + any alert fan-out.
    """
    result: Dict[str, Any] = {"trigger_id": trigger_id, "status": "skipped"}

    # Phase 1: read everything, then commit before any network I/O.
    phase1 = None
    secret = None
    timeout = 10
    try:
        phase1 = session_factory()
        trigger = phase1.get(models.HttpTrigger, trigger_id)
        if trigger is None or trigger.deleted_at is not None \
                or trigger.status != "active":
            return {"trigger_id": trigger_id, "status": "skipped",
                    "reason": "trigger not active"}
        tenant = phase1.query(models.Tenant).filter(
            models.Tenant.id == trigger.tenant_id).first()
        if tenant is None:
            return {"trigger_id": trigger_id, "status": "skipped",
                    "reason": "tenant missing"}
        secret = decrypt_secret(phase1, tenant, trigger.encrypted_secret)
        url = resolve_target_url(phase1, trigger)
        # httpx timeout from the platform setting (read while the session is
        # open; the CALL itself happens after the commit below).
        timeout = _timeout_seconds(phase1)
        # Snapshot everything the fire needs BEFORE commit/close — after the
        # commit the ORM instance expires and the session detaches, so
        # attribute access would raise DetachedInstanceError.
        snap = {
            "method": trigger.method,
            "path": trigger.path,
            "tenant_id": trigger.tenant_id,
            # GX.S7 caller-identity inputs (snapshot before detach).
            "target_kind": trigger.target_kind,
            "target_id": (trigger.stack_id if trigger.target_kind == "stack-service"
                          else trigger.app_id),
            "identity_signing_enabled": _identity_signing_enabled(phase1),
        }
        allow_private = True  # identity-validated stack/app target
        # Commit the read session BEFORE the HTTP call (pgbouncer pinning).
        phase1.commit()
        snap_ref = snap
    except Exception as e:  # noqa: BLE001
        logger.exception("http_triggers: fire pre-read failed (trigger=%s)",
                         trigger_id)
        if phase1 is not None:
            try:
                phase1.rollback()
            except Exception:
                pass
        return {"trigger_id": trigger_id, "status": "failed", "error": str(e)}
    finally:
        if phase1 is not None:
            phase1.close()

    tenant_id = snap_ref["tenant_id"]
    method = snap_ref["method"]
    path = snap_ref["path"]
    now = datetime.now(timezone.utc)
    if not url:
        _write_run(session_factory, trigger_id=trigger_id,
                   tenant_id=tenant_id, status="failed",
                   error="could not resolve target url")
        result.update(status="failed", error="could not resolve target url")
        return result

    refusal = ssrf_check(url, allow_private=allow_private)
    if refusal:
        _write_run(session_factory, trigger_id=trigger_id,
                   tenant_id=tenant_id, status="failed",
                   resolved_url=url, error=f"ssrf refusal: {refusal}")
        result.update(status="failed", error=f"ssrf refusal: {refusal}")
        return result

    if secret is None:
        _write_run(session_factory, trigger_id=trigger_id,
                   tenant_id=tenant_id, status="failed",
                   resolved_url=url, error="signing secret unavailable")
        result.update(status="failed", error="signing secret unavailable")
        return result

    # GSEC FX.19 R-042 — webhook egress disabled: do not make the outbound
    # fire. Record a failed run row (so the history reflects the refusal) and
    # skip the httpx call. Never raises.
    from utils import egress_policy
    _r042 = egress_policy.refuse_reason(egress_policy.CATEGORY_WEBHOOKS)
    if _r042:
        _write_run(session_factory, trigger_id=trigger_id,
                   tenant_id=tenant_id, status="failed",
                   resolved_url=url, error=f"egress disabled: {_r042}")
        result.update(status="failed", error=f"egress disabled: {_r042}")
        logger.info("http_triggers: fire skipped (trigger=%s) — %s",
                    trigger_id, _r042)
        return result

    # Phase 2: the outbound call — NO DB session open.
    body = json.dumps({
        "event": "http_trigger.fire",
        "trigger_id": trigger_id,
        "fired_at": now.isoformat(),
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    timestamp = str(int(now.timestamp()))
    # GX.S7 — MOD caller identity: mint the short-TTL Bearer token for THIS
    # call (HMAC headers always stay). Degrades to None (HMAC-only) when the
    # setting is off or no manager signing key is provisioned.
    id_token = None
    if snap_ref.get("identity_signing_enabled"):
        id_token = mint_call_token(
            tenant_id=tenant_id,
            target_kind="stack" if snap_ref["target_kind"] == "stack-service" else "app",
            target_id=snap_ref["target_id"],
            trigger_id=trigger_id, body=body, fired_at=int(now.timestamp()),
        )
    headers = sign_request(url, method, path, body, timestamp, secret,
                           identity_token=id_token)
    started = time.monotonic()
    http_resp_status = None
    call_error = None
    try:
        # GSEC FX.151.p2a2 — the outbound fire goes through the shared guarded
        # client (:func:`utils.safe_http.request`): scheme vetting, per-attempt
        # DNS re-check, pinned address, manual redirect re-vetting and the size
        # cap. Because the target is an identity-validated stack or app
        # (``allow_private=True``), the guard's default "refuse private" rule
        # is unlocked for exactly this host via the explicit internal-allow mode,
        # which also writes the ``SSRF_ALLOWLIST_FETCH`` audit row.
        from urllib.parse import urlparse
        from utils import safe_http
        resp = safe_http.request(
            method, url, data=body, headers=headers or {},
            _ssrf_connect_timeout=min(5.0, float(timeout) or 5.0),
            _ssrf_read_timeout=float(timeout) or 10.0,
            _ssrf_allowlist=[urlparse(url).hostname]
                             if urlparse(url).hostname else None,
        )
        http_resp_status = resp.status_code
    except Exception as e:  # noqa: BLE001 — timeout / connection error
        call_error = f"{type(e).__name__}: {e}"

    duration_ms = int((time.monotonic() - started) * 1000)
    success = http_resp_status is not None and 200 <= http_resp_status < 300

    # Phase 3 (failure): advance the rolling failure counter and decide
    # WHETHER this failure trips the alert, BEFORE the run row is written —
    # so the history row can record `alert_sent` for the alerting fire. A
    # SUCCESS resets the rolling counter (regardless of fire source: tick or
    # run-now).
    alert_sent = False
    user_id = None
    if not success:
        alert_sent, user_id = _evaluate_failure(
            session_factory, trigger_id=trigger_id, tenant_id=tenant_id)
    else:
        _reset_failures(session_factory, trigger_id)

    _write_run(session_factory, trigger_id=trigger_id,
               tenant_id=tenant_id,
               status="success" if success else "failed",
               resolved_url=url, http_status=http_resp_status,
               error=None if success else (call_error or f"HTTP {http_resp_status}"),
               duration_ms=duration_ms, alert_sent=alert_sent)
    result.update(status="success" if success else "failed",
                  http_status=http_resp_status, error=call_error)

    # Phase 4 (alert): fan out through all three channels (off the run-row
    # write, on its own sessions so the fan-out never holds a txn open).
    if alert_sent:
        _fan_out_alert(session_factory, tenant_id=tenant_id, user_id=user_id,
                       trigger_id=trigger_id, url=url,
                       http_status=http_resp_status,
                       error=call_error or f"HTTP {http_resp_status}")
    return result


def _evaluate_failure(session_factory, *, trigger_id: int,
                      tenant_id: int) -> tuple:
    """Advance the consecutive-failure counter for a failed fire and return
    ``(alert, user_id)`` — `alert` is True when this failure crosses the
    `triggers.failure_alert_threshold` consecutive-failure count (and is not
    within the once-per-day re-alert window). Uses its own short session and
    commits before returning, so no transaction is held across the fan-out."""
    alert = False
    user_id = None
    db = None
    try:
        db = session_factory()
        trigger = db.get(models.HttpTrigger, trigger_id)
        if trigger is None:
            return False, None
        trigger.consecutive_failures = (trigger.consecutive_failures or 0) + 1
        threshold = _failure_alert_threshold(db)
        now = datetime.now(timezone.utc)
        alert = (threshold > 0
                 and trigger.consecutive_failures >= threshold
                 # Re-alert at most once per day (avoid a spam loop): require
                 # either the first crossing or a day of silence.
                 and (trigger.last_alerted_at is None
                      or (now - trigger.last_alerted_at) >= timedelta(days=1)))
        user_id = trigger.created_by_user_id
        db.commit()
    except Exception:  # noqa: BLE001
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        alert = False
    finally:
        if db is not None:
            db.close()
    return alert, user_id


def _fan_out_alert(session_factory, *, tenant_id: int, user_id,
                   trigger_id: int, url: str, http_status: Optional[int],
                   error: str) -> None:
    """Best-effort alert through all three channels. Each channel is
    wrapped so one failing channel never blocks the others; failures are
    logged, not raised (an alert outage must not take the trigger down).
    Opens its own session (and closes it) so the fan-out — which includes
    an outbound webhook HTTP call — never holds a transaction open."""
    title = f"HTTP trigger #{trigger_id} failing"
    body = (f"The scheduled HTTP trigger (id={trigger_id}) has failed "
            f"consecutively. Last error: {error}. Target: {url}. "
            f"HTTP status: {http_status}.")
    db = None
    try:
        db = session_factory()
    except Exception:  # noqa: BLE001
        logger.exception("http_triggers: alert session failed (trigger=%s)",
                         trigger_id)
        return
    try:
        _fan_out_alert_channels(db, tenant_id=tenant_id, user_id=user_id,
                                trigger_id=trigger_id, url=url,
                                http_status=http_status, error=error,
                                title=title, body=body)
    finally:
        try:
            trig2 = db.get(models.HttpTrigger, trigger_id)
            if trig2 is not None:
                trig2.last_alerted_at = datetime.now(timezone.utc)
            db.commit()
        except Exception:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()


def _fan_out_alert_channels(db, *, tenant_id: int, user_id,
                            trigger_id: int, url: str,
                            http_status: Optional[int], error: str,
                            title: str, body: str) -> None:
    """The three alert channels, on the caller's session."""

    # Channel 1 — MOD in-app/email notification (GX.S8).
    try:
        from services import app_notifications
        targets = {"user_ids": [user_id]} if user_id else {}
        app_notifications.fan_out(
            db, tenant_id=tenant_id, user_id=user_id,
            targets=targets, title=title, body=body,
            severity="error", link=None,
            channels=["in_app", "email"],
            dedupe_key=f"http_trigger:{trigger_id}:{tenant_id}",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("http_triggers: MOD notification alert failed: %s", e)

    # Channel 2 — direct email (GX.S4).
    try:
        from services import email_send_api
        addrs = []
        if user_id is not None:
            u = db.query(models.User).filter(models.User.id == user_id).first()
            if u and u.email:
                addrs = [u.email]
        if addrs:
            email_send_api.prepare_send(
                db, tenant_id=tenant_id, user_id=user_id, app_id=None,
                to=addrs, cc=[], bcc=[], subject=title,
                text=body, html=None, attachments=None,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("http_triggers: email alert failed: %s", e)

    # Channel 3 — outbound webhook (reuses the GX.R3 signing pattern).
    try:
        # GSEC FX.19 R-042 — webhook egress disabled: skip the alert send.
        from utils import egress_policy
        _r042 = egress_policy.refuse_reason(egress_policy.CATEGORY_WEBHOOKS)
        if _r042:
            logger.info("http_triggers: alert webhook skipped (trigger=%s) — %s",
                        trigger_id, _r042)
            return
        trigger = db.get(models.HttpTrigger, trigger_id)
        if trigger and trigger.callback_url:
            tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
            secret = decrypt_secret(db, tenant, trigger.encrypted_secret)
            if secret:
                wbody = json.dumps({
                    "event": "http_trigger.alert",
                    "trigger_id": trigger_id,
                    "error": error,
                    "http_status": http_status,
                    "target": url,
                }, separators=(",", ":"), sort_keys=True).encode("utf-8")
                ts = str(int(datetime.now(timezone.utc).timestamp()))
                wh = sign_request(trigger.callback_url, "POST", "", wbody, ts, secret)
                # GSEC FX.151.p2a2 — the alert webhook goes through the shared
                # guarded client (safe_http.post): scheme vetting, per-attempt
                # DNS re-check, pinned address, manual redirect re-vetting and
                # the size cap. The resolved callback host is passed to the
                # guard's explicit internal-allow mode so tenant-provided
                # callback URLs keep working (audited).
                from urllib.parse import urlparse
                from utils import safe_http
                callback_host = urlparse(trigger.callback_url).hostname
                try:
                    safe_http.post(
                        trigger.callback_url, data=wbody, headers=wh,
                        _ssrf_connect_timeout=5.0,
                        _ssrf_read_timeout=10.0,
                        _ssrf_allowlist=[callback_host]
                                         if callback_host else None,
                    )
                except Exception:
                    pass  # alert webhook failure is best-effort
    except Exception as e:  # noqa: BLE001
        logger.warning("http_triggers: webhook alert failed: %s", e)


def _audit(db, *, tenant_id: int, user_id: Optional[int], action,
           details: Dict[str, Any]) -> None:
    db.add(models.AuditEvent(
        tenant_id=tenant_id, user_id=user_id,
        event_name=action.value if hasattr(action, "value") else str(action),
        details=details,
    ))


# --- The 30s sweep (called by the APScheduler job) ----------------------------

def run_trigger_tick(session_factory) -> Dict[str, Any]:
    """One 30s sweep: claim due triggers (FOR UPDATE SKIP LOCKED), commit the
    claim, then fire each on its own session. `session_factory` is a zero-arg
    callable returning a fresh Session (database.SessionLocal in production;
    the test harness patches it). Returns counters for logging."""
    counts = {"due": 0, "fired": 0, "failed": 0, "skipped": 0, "errors": 0}
    claim = None
    due_ids: List[int] = []
    now = datetime.now(timezone.utc)

    # GT.C2 — drain mode: skip the tick WITHOUT claiming or advancing any
    # trigger's next_fire (advancement happens in _mark_fired, after the claim
    # below). A trigger that was due while paused stays due, so it fires ONCE
    # (not N times) the moment dispatch resumes.
    try:
        from services import platform_settings as _psr_ht
        if _psr_ht.get_setting("dispatch.paused"):
            return counts
    except Exception:  # noqa: BLE001 — a settings read failure means dispatch on
        pass

    try:
        claim = session_factory()
        due_ids = _claim_due(claim, now)
        counts["due"] = len(due_ids)
        if due_ids:
            # Commit the claim (advances nothing yet) so the locks release and
            # a second process won't pick the same rows. We advance each
            # trigger's next_fire_at in _mark_fired before its call.
            for tid in due_ids:
                trig = claim.get(models.HttpTrigger, tid)
                if trig is None or trig.deleted_at is not None:
                    continue
                _mark_fired(claim, trig, now)  # commits (advances next_fire_at)
        claim.commit()
    except Exception as e:  # noqa: BLE001
        if claim is not None:
            try:
                claim.rollback()
            except Exception:
                pass
        counts["errors"] += 1
        logger.exception("http_triggers: claim phase failed: %s", e)
    finally:
        if claim is not None:
            claim.close()

    for tid in due_ids:
        try:
            r = run_trigger_fire(session_factory, trigger_id=tid)
            # run_trigger_fire resets the rolling failure counter on a
            # success itself, so no extra pass is needed here.
            if r.get("status") == "success":
                counts["fired"] += 1
            elif r.get("status") == "failed":
                counts["failed"] += 1
            else:
                counts["skipped"] += 1
        except Exception:  # noqa: BLE001
            counts["errors"] += 1
            logger.exception("http_triggers: fire failed for trigger %s", tid)
    return counts


def _reset_failures(session_factory, trigger_id: int) -> None:
    db = None
    try:
        db = session_factory()
        trig = db.get(models.HttpTrigger, trigger_id)
        if trig is not None and (trig.consecutive_failures or 0) != 0:
            trig.consecutive_failures = 0
            db.commit()
    except Exception:  # noqa: BLE001
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
    finally:
        if db is not None:
            db.close()

# API/services/outbound_webhooks.py

"""Outbound webhook delivery (HMAC-signed).

Fans workflow lifecycle events out to tenant-configured `WebhookEndpoint`
subscriptions. Delivery is best-effort and strictly non-fatal: a failure to
reach a subscriber must never break the workflow completion path that invokes
it. Each request body is signed with the endpoint's `signing_secret` using
HMAC-SHA256 so subscribers can verify authenticity.

Prior to this module the `WebhookEndpoint` CRUD surface existed but nothing
ever fired it — the "wired-but-not-firing" gap.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import threading
from datetime import datetime, timezone
from typing import Iterable, List, Optional
from urllib.parse import urlparse

import models

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-MOD-Signature"
EVENT_HEADER = "X-MOD-Event"
_DELIVERY_TIMEOUT_SECONDS = 5

# Workflow lifecycle event names.
EVENT_WORKFLOW_COMPLETED = "workflow.completed"
_STATUS_TO_EVENT = {
    "success": "workflow.succeeded",
    "succeeded": "workflow.succeeded",
    "completed": "workflow.succeeded",
    "failed": "workflow.failed",
    "canceled": "workflow.canceled",
    "cancelled": "workflow.canceled",
}


def _delivery_url_refusal(url: str) -> Optional[str]:
    """Return a reason string if `url` must NOT be delivered to (SSRF guard),
    else None.

    Webhook endpoints are tenant-controlled and the API container is the
    client, so a tenant could otherwise point a webhook at internal services
    (vault, keycloak, the API itself). We therefore require https and refuse
    hosts that resolve to private/loopback/link-local/reserved/multicast
    addresses (logic mirrors services/triggers/streaming.py).

    On-prem deployments legitimately target internal hosts, so when
    `DEPLOYMENT_MODE=onprem` or `OUTBOUND_WEBHOOK_ALLOW_PRIVATE=true` the
    IP-resolve check is skipped. The https requirement is still enforced
    unless `OUTBOUND_WEBHOOK_ALLOW_PRIVATE=true` (an on-prem customer may
    target an internal http host).
    """
    onprem = os.getenv("DEPLOYMENT_MODE", "").lower() == "onprem"
    allow_flag = os.getenv("OUTBOUND_WEBHOOK_ALLOW_PRIVATE", "false").lower() == "true"
    allow_private = onprem or allow_flag

    try:
        parsed = urlparse(url)
    except Exception as e:  # noqa: BLE001 — malformed → refuse
        return f"unparseable url: {e}"

    # https is always required, except when the allow-flag is set (on-prem
    # customers may legitimately target internal http hosts too).
    if parsed.scheme != "https" and not allow_flag:
        return "must use https"
    if not parsed.hostname:
        return "no host"

    if allow_private:
        return None

    try:
        for _fam, _types, _proto, _canon, sockaddr in socket.getaddrinfo(
            parsed.hostname, None
        ):
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                return (
                    f"{parsed.hostname} resolves to a private/loopback address ({ip})"
                )
    except Exception as e:  # noqa: BLE001 — unresolvable → refuse
        return f"could not resolve host: {e}"
    return None


def sign_payload(secret: str, body: bytes) -> str:
    """Compute the `X-MOD-Signature` value for a raw request body.

    Format: ``sha256=<hex>`` — HMAC-SHA256 of the exact bytes sent, keyed by
    the endpoint's signing secret. Subscribers recompute this over the raw
    body they receive and compare in constant time.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _serialize(primary_event: str, payload: dict) -> bytes:
    envelope = {
        "event": primary_event,
        "data": payload,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # Deterministic bytes so the signature is reproducible/verifiable.
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")


def dispatch_event(
    db,
    tenant_id: int,
    candidate_events: Iterable[str],
    primary_event: str,
    payload: dict,
) -> int:
    """Deliver `primary_event` to every active endpoint in `tenant_id` that is
    subscribed to any of `candidate_events` (or the wildcard ``*``).

    Returns the number of endpoints an HTTP POST was attempted for. Fully
    non-fatal: all DB and network errors are swallowed and logged.
    """
    candidates = set(candidate_events)
    try:
        endpoints: List[models.WebhookEndpoint] = (
            db.query(models.WebhookEndpoint)
            .filter(
                models.WebhookEndpoint.tenant_id == tenant_id,
                models.WebhookEndpoint.is_active == True,  # noqa: E712
            )
            .all()
        )
    except Exception:
        logger.exception("outbound webhook: endpoint lookup failed (tenant=%s)", tenant_id)
        return 0

    from utils import safe_http

    body = _serialize(primary_event, payload)
    attempted = 0
    for ep in endpoints:
        subs = set(ep.subscribed_events or [])
        if "*" not in subs and not (subs & candidates):
            continue
        headers = {
            "Content-Type": "application/json",
            EVENT_HEADER: primary_event,
            SIGNATURE_HEADER: sign_payload(ep.signing_secret, body),
        }
        refusal = _delivery_url_refusal(ep.url)
        if refusal:
            # SSRF guard — skip this endpoint. Log the endpoint id only,
            # never the URL (it may carry tokens/query secrets).
            logger.warning(
                "outbound webhook: refusing delivery to endpoint id=%s: %s",
                ep.id,
                refusal,
            )
            continue
        # GSEC FX.161.p1b — a destination that drifted outside the tenant's
        # output allowlist since the endpoint was configured is skipped
        # (never raises into the completion path); check_destination
        # already wrote the audit row before raising.
        try:
            from fastapi import HTTPException as _HTTPException
            # FX.162.p5 — CE carve: no DLP output allowlist -> no restriction.
            from utils.paid_import import optional_paid
            _dlp = optional_paid("services.dlp_output_allowlist")
            if _dlp is not None:
                _dlp.check_destination(
                    db, tenant_id, ep.url, kind="outbound_webhook",
                )
        except _HTTPException:
            logger.warning(
                "outbound webhook: refusing delivery to endpoint id=%s: "
                "destination not in tenant output allowlist", ep.id,
            )
            continue
        attempted += 1
        try:
            # The _delivery_url_refusal pre-check above is the authoritative
            # SSRF gate for this site and honours on-prem parity: in
            # on-prem/allow-private mode a private-resolving host legitimately
            # delivers. Only in that mode do we hand safe_http the already-
            # vetted host (per-URL allowlist) so its own address check does not
            # double-refuse the on-prem target. In SaaS mode no allowlist is
            # passed, so safe_http still refuses any private target that slips
            # past the pre-check (e.g. DNS drift between the two lookups).
            _allow_private = (
                os.getenv("DEPLOYMENT_MODE", "").lower() == "onprem"
                or os.getenv("OUTBOUND_WEBHOOK_ALLOW_PRIVATE", "false").lower() == "true"
            )
            _host = urlparse(ep.url).hostname if _allow_private else None
            safe_http.post(
                ep.url,
                data=body,
                headers=headers,
                _ssrf_allowlist=[_host] if _host else None,
                _ssrf_connect_timeout=_DELIVERY_TIMEOUT_SECONDS,
                _ssrf_read_timeout=_DELIVERY_TIMEOUT_SECONDS,
                tenant_id=tenant_id,
            )
        except safe_http.SsrfBlocked:
            logger.warning(
                "outbound webhook: delivery to endpoint id=%s refused by SSRF guard",
                ep.id,
            )
        except Exception:
            logger.exception("outbound webhook: delivery to endpoint id=%s failed", ep.id)
    return attempted


def dispatch_workflow_completion(
    db, tenant_id: int, workflow_run_id: int, status_str: str
) -> int:
    """Fire the workflow-completion webhook fan-out for a terminal run.

    Endpoints subscribed to the generic ``workflow.completed`` event OR the
    status-specific event (``workflow.succeeded`` / ``workflow.failed`` /
    ``workflow.canceled``) each receive exactly one signed delivery.
    """
    specific = _STATUS_TO_EVENT.get((status_str or "").lower())
    candidate_events = [EVENT_WORKFLOW_COMPLETED] + ([specific] if specific else [])
    primary_event = specific or EVENT_WORKFLOW_COMPLETED
    payload = {"workflow_run_id": workflow_run_id, "status": status_str}
    return dispatch_event(db, tenant_id, candidate_events, primary_event, payload)


def _default_session_factory():
    """Create the DB session the background delivery thread uses.

    A plain module-level function (not inlined) so tests can monkeypatch it
    to bind onto the test harness's own connection — `database.SessionLocal`
    requires `database.initialize()`, which the test app never calls.
    """
    from database import SessionLocal

    return SessionLocal()


def dispatch_workflow_completion_background(
    tenant_id: int, workflow_run_id: int, status_str: str
) -> None:
    """Schedule `dispatch_workflow_completion` on a daemon thread with its own
    DB session, so the caller (typically a finalize path holding a DB session
    mid-request) never blocks on outbound network I/O and never pins its
    connection through an HTTP round-trip.

    Fire-and-forget: exceptions (session creation, lookup, delivery) are all
    swallowed and logged inside the thread — this must never surface to, or
    affect, the caller.
    """

    def _run() -> None:
        try:
            db = _default_session_factory()
        except Exception:
            logger.exception(
                "outbound webhook: background session creation failed (run=%s)",
                workflow_run_id,
            )
            return
        try:
            dispatch_workflow_completion(
                db=db,
                tenant_id=tenant_id,
                workflow_run_id=workflow_run_id,
                status_str=status_str,
            )
        except Exception:
            logger.exception(
                "outbound webhook: background dispatch failed (run=%s)",
                workflow_run_id,
            )
        finally:
            db.close()

    threading.Thread(
        target=_run,
        name=f"outbound-webhook-{workflow_run_id}",
        daemon=True,
    ).start()

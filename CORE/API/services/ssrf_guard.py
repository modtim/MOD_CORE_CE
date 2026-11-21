"""FX.151 (GSEC.12) — the single SSRF target guard shared by every outbound-URL site.

The run-completion webhooks module (services/run_completion_webhooks.py) grew
the canonical URL vetting check; HTTP-poll triggers (services/http_triggers.py)
and the rest of the egress sites mirror it. This module owns that logic so the
sites cannot drift apart again: vet a tenant-supplied URL BEFORE any request is
made, at every attempt (DNS is re-resolved at call time, so a record that drifts
to a private address is caught on the attempt that would have used it).

Public surface:

* :func:`check_url` — raise :class:`SsrfBlocked` when the target is refused.
* :func:`ssrf_check` — the legacy reason-string form (``None`` = allowed) that
  the webhook/trigger call sites and their tests already use; it is a thin
  wrapper over :func:`check_url`, so both forms answer identically.

Rules (unchanged from ``run_completion_webhooks.ssrf_check``):

* https-only unless the caller passes ``allow_http`` (platform setting
  ``run_webhooks.allow_http``); any other scheme is refused outright.
* the hostname must not be a docker-internal service name
  (:data:`BLOCKED_INTERNAL_HOSTNAMES`) — always refused, even on-prem, so a
  tenant can never point a callback at the API's own peers (vault, keycloak,
  the DB, the API itself).
* unless the caller passes ``allow_private`` (platform setting
  ``run_webhooks.allow_private``), the hostname must not resolve to a
  private / loopback / link-local / reserved / multicast / unspecified
  address — which covers 10/8, 127.x, 169.254.169.254 (cloud metadata,
  ``is_link_local``), ``::1``, and the IPv6 ULA range fc00::/7.
  ``allow_private`` still refuses loopback and unspecified.

No policy lives here: the settings are read by the callers and passed in as
arguments, so this module imports no platform services (no import cycles) and
stays testable without a DB.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse


class SsrfBlocked(ValueError):
    """Raised by :func:`check_url` when a URL must not be fetched.

    Subclasses :class:`ValueError` so a caller that only guards the transport
    still refuses; the message is the human-readable refusal reason.
    """


# Internal hostnames that must never be a delivery/fetch target even when a
# deployment opts into private addressing (the API container's own peers —
# vault, keycloak, the DB, the API itself).
BLOCKED_INTERNAL_HOSTNAMES = frozenset({
    "localhost",
    "my_fastapi_app",
    "fastapi-app",
    "api",
    "vault",
    "keycloak",
    "postgres",
    "postgres-app",
    "mod_pgbouncer",
    "pgbouncer",
    "redis",
    "my_redis",
    "valkey",
    "mod_qdrant",
    "qdrant",
    "my_versitygw",
    "versitygw",
    "my_minio",
    "minio",
    "harbor",
    "my_harbor",
    "nginx",
    "caddy",
    "db",
    "database",
    "my_vault",
    "my_valkey",
})

# Legacy alias — some sites grep for the underscored name.
_BLOCKED_INTERNAL_HOSTNAMES = BLOCKED_INTERNAL_HOSTNAMES


def _ip_refused(ip: "ipaddress._BaseAddress", allow_private: bool) -> bool:
    """True when `ip` must not be a fetch/delivery target."""
    if allow_private:
        # On-prem opt-in: private addressing is allowed, but the API container's
        # own address and any loopback peer are still never a valid target.
        return ip.is_loopback or ip.is_unspecified
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _refusal_reason(
    url: str, *, allow_http: bool = False, allow_private: bool = False
) -> Optional[str]:
    """The single source of truth: refusal reason for `url`, or None if allowed."""
    try:
        parsed = urlparse(url)
    except Exception as e:  # noqa: BLE001 — malformed -> refuse
        return f"unparseable url: {e}"
    if parsed.scheme not in ("https", "http") or not allow_http and parsed.scheme != "https":
        return "must use https (http is disabled by platform setting run_webhooks.allow_http)"
    host = parsed.hostname
    if not host:
        return "no host"
    if host.lower().rstrip(".") in BLOCKED_INTERNAL_HOSTNAMES:
        return f"internal hostname '{host}' is not allowed"
    # IPv6 literal or bare-IP hostname.
    try:
        ip = ipaddress.ip_address(host)
        if _ip_refused(ip, allow_private):
            return f"{host} is a private/loopback/link-local address"
        return None
    except ValueError:
        pass
    if allow_private:
        return None
    try:
        for _fam, _types, _proto, _canon, sockaddr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if _ip_refused(ip, allow_private):
                return f"{host} resolves to a private/loopback/link-local address ({ip})"
    except Exception as e:  # noqa: BLE001 — unresolvable -> refuse
        return f"could not resolve host: {e}"
    return None


def check_url(url: str, *, allow_http: bool = False, allow_private: bool = False) -> None:
    """Raise :class:`SsrfBlocked` if `url` must NOT be fetched/delivered to.

    Returns ``None`` when the target is allowed. See the module docstring for
    the rules; ``allow_http``/``allow_private`` come from the platform-settings
    registry (``run_webhooks.allow_http`` / ``run_webhooks.allow_private``) and
    are passed in by the caller so this module needs no DB.
    """
    reason = _refusal_reason(url, allow_http=allow_http, allow_private=allow_private)
    if reason is not None:
        raise SsrfBlocked(reason)


def ssrf_check(url: str, *, allow_http: bool = False, allow_private: bool = False) -> Optional[str]:
    """Legacy reason-string form of :func:`check_url` — refusal reason, or None.

    Kept so the existing webhook/trigger call sites (and their tests) keep
    working; it delegates to :func:`check_url` so the two forms cannot drift.
    """
    try:
        check_url(url, allow_http=allow_http, allow_private=allow_private)
    except SsrfBlocked as e:
        return str(e)
    return None

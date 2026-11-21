"""
GSEC R-011 — single source of truth for "which client IP is this request from".

Before R-011 every consumer read X-Forwarded-For blindly (left-most hop,
trusted for ANY peer), so any public client could spoof its identity:
audit/SCIM log rows, IP allowlists, run-surface caps and rate-limit keys
all keyed on attacker-chosen addresses.

Trust model
-----------
X-Forwarded-For / X-Real-IP are honoured ONLY when the direct TCP peer
(`request.client.host`) is a trusted proxy — i.e. it actually sits in front
of the API (nginx on the docker bridge, loopback for in-container calls,
or an explicitly configured upstream edge/CDN). Trusted proxy CIDRs come
from the `net.trusted_proxy_cidrs` platform setting (comma-separated;
default "127.0.0.1/32,::1/128,172.16.0.0/12"), never from env vars.

When the peer IS a trusted proxy, the client IP is the right-most address
in the XFF chain that is NOT itself a trusted proxy (the left-most entry
is attacker-controllable). When the peer is NOT trusted, the peer address
is the client IP and all forwarded headers are ignored.

The setting lookup is cached per process with a short TTL (5s) so a hot
request path (rate limiting, audit) does not hit Valkey/DB per request;
a setting change takes effect within that window.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

SETTING_KEY = "net.trusted_proxy_cidrs"
# GSEC R-011 built-in fallback trust. CE (Community Edition) ships NO
# reverse proxy — the API port is published directly — so CE trusts
# loopback only: an untrusted peer's X-Forwarded-For / X-Real-IP is
# ignored and the peer address is the client IP. Paid keeps the docker
# bridge range (nginx lives there). Fail-to-paid on any edition-probe
# error, so a broken probe can never silently widen the trust.
_PAID_DEFAULT_TRUSTED = "127.0.0.1/32,::1/128,172.16.0.0/12"
_CE_DEFAULT_TRUSTED = "127.0.0.1/32,::1/128"


def _default_trusted() -> str:
    """Edition-aware fallback trust string (CE-narrowed, fail-to-paid)."""
    try:
        from utils.paid_import import is_ce_edition
        if is_ce_edition():
            return _CE_DEFAULT_TRUSTED
    except Exception:  # noqa: BLE001 — fail to the PAID default
        pass
    return _PAID_DEFAULT_TRUSTED


DEFAULT_TRUSTED = _default_trusted()

_trust_cache: Optional[Tuple[float, list]] = None
_TRUST_CACHE_TTL = 5.0


def _coerce_network(value: str) -> Optional[ipaddress._BaseNetwork]:
    try:
        return ipaddress.ip_network(value.strip(), strict=False)
    except ValueError:
        return None


def _parse_cidrs(raw: str) -> list:
    nets = []
    for part in raw.split(","):
        net = _coerce_network(part)
        if net is None:
            logger.warning("client_ip: ignoring invalid CIDR %r in %s", part, SETTING_KEY)
        else:
            nets.append(net)
    return nets


def get_trusted_networks() -> list:
    """Trusted-proxy network list from the platform setting (TTL-cached).

    Falls back to the built-in default (loopback + docker bridge) when the
    registry or the backing store is unavailable, so a DB/Valkey outage
    degrades to "trust only the immediate proxy" rather than to "trust no
    XFF at all" (which would collapse audit attribution) or "trust all RFC1918"
    (the R-011 hole).
    """
    global _trust_cache
    now = time.monotonic()
    if _trust_cache is not None and now - _trust_cache[0] < _TRUST_CACHE_TTL:
        return _trust_cache[1]
    raw = DEFAULT_TRUSTED
    try:
        from services import platform_settings as _ps
        raw = _ps.get_setting(SETTING_KEY)
    except Exception as e:
        logger.warning("client_ip: %s lookup failed (%s); using default", SETTING_KEY, e)
    nets = _parse_cidrs(raw)
    if not nets:
        nets = _parse_cidrs(DEFAULT_TRUSTED)
    _trust_cache = (now, nets)
    return nets


def is_trusted_peer(ip: Optional[str], nets: Optional[list] = None) -> bool:
    """True if `ip` falls inside a trusted-proxy network."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    nets = nets if nets is not None else get_trusted_networks()
    return any(addr in net for net in nets)


def trusted_client_ip(request) -> Optional[str]:
    """Resolve the real client IP for `request` (see module docstring).

    - Peer not a trusted proxy  → peer address (headers ignored).
    - Peer trusted, no XFF       → X-Real-IP if set, else the peer itself
      (the proxy is the final hop).
    - Peer trusted, XFF present  → right-most untrusted hop (left-most is
      attacker-controllable, so it is never used).
    """
    peer = request.client.host if request.client else None
    nets = get_trusted_networks()
    if not is_trusted_peer(peer, nets):
        return peer

    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        chain = [h.strip() for h in xff.split(",") if h.strip()]
        for hop in reversed(chain):
            if not is_trusted_peer(hop, nets):
                return hop
        # every hop is itself a trusted proxy — closest one is the answer
        return chain[-1]

    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    return peer

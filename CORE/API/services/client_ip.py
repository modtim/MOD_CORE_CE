"""FX.106 p3a — real-client-IP resolution behind a scrubbing provider.

Single entry point every client-IP consumer should call:
:func:`resolve_client_ip`. It answers "which client address does this
request really come from" when the API sits behind a reverse proxy (our
nginx edge, a WAF, Cloudflare) that rewrites or forwards address headers.

Trust model (inherited from GSEC R-011 / R-084, unchanged)
----------------------------------------------------------
``X-Forwarded-For`` / ``X-Real-IP`` / ``CF-Connecting-IP`` are honoured
ONLY when the direct TCP peer (``request.client.host``) falls inside one
of the trusted-proxy ranges. Ranges come from the UNION of two platform
settings (never env):

  * ``net.trusted_proxy_cidrs`` (str, comma-separated) — the legacy
    consumer read, kept registered so existing call sites/tests keep
    working; and
  * ``network.trusted_proxy_cidrs`` (list) — the FX.106 p3a ranges
    setting, the one the nginx edge renders to ``set_real_ip_from``.

When the peer is NOT trusted, every forwarded header is ignored outright
(anti-spoof) and the peer address is the client IP. When it IS trusted,
the XFF chain is walked RIGHT-TO-LEFT and the first hop that is not
itself trusted wins: the left-most XFF entry is attacker-controllable
(nginx's ``$proxy_add_x_forwarded_for`` appends, so a public client can
forge it) and must never be the key.

CF-Connecting-IP wins when present and the peer is trusted: Cloudflare
sets it to the real client even when a second proxy hop sits in front,
where the XFF chain has already lost the original address.

The range list is TTL-cached (5 s) so a hot path (rate limiting, failed-login
counting, audit rows) does not read Valkey/DB per request; a setting change
takes effect within that window.

nginx side (the edge container cannot read the DB — the same
four-registration-point trap noted in CORE/startup/docker-compose.yml for
NGINX_TRUSTED_PROXY_CIDRS): the same ranges are rendered to
``set_real_ip_from`` lines by CORE/startup/scripts/nginx-entrypoint.sh.
A changed ``network.trusted_proxy_cidrs`` reaches the edge by rendering
this module's :func:`apply_real_ip_block` over the DB value to
``CORE/startup/rendered/real-ip-drop.conf`` (mounted ro over
``/etc/nginx/real-ip-drop.conf``) and recreating nginx; the drop file
takes precedence over the entrypoint's built-in default. nginx is
excluded from update-system.sh, so an explicit recreate is required.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import tempfile
import time
from typing import Any, Iterable, List, Optional

# FX.106 p3a circular-import fix (10-07): services/platform_settings.py imports
# audit, which imports THIS module (`from services.client_ip import resolve_client_ip
# as trusted_client_ip`, the R-145 AST-visible name). A module-level import here
# makes `python3 -c "import services.client_ip"` blow up on a partially
# initialized platform_settings. Keep it lazy: only the settings-reading helpers
# below need it, and they call :func:`_psr`.
def _psr():
    from services import platform_settings as psr  # lazy: breaks the audit cycle

    return psr

from utils.client_ip import (
    get_trusted_networks,
    is_trusted_peer,
)

logger = logging.getLogger(__name__)

# FX.106 p3a list-typed ranges setting (services/platform_settings.py)
RANGES_SETTING_KEY = "network.trusted_proxy_cidrs"
# legacy str twin already registered (GSEC R-011) — both are honoured
LEGACY_SETTING_KEY = "net.trusted_proxy_cidrs"

_TRUST_CACHE_TTL = 5.0
_trust_cache: "tuple | None" = None  # (monotonic, legacy-list identity, nets)


def _parse_networks(values: Iterable[str]) -> List[Any]:
    """Parse CIDR strings into networks, skipping unparseable entries."""
    nets: List[Any] = []
    for part in values:
        if not isinstance(part, str) or not part.strip():
            continue
        net = ipaddress.ip_network(part.strip(), strict=False)
        if net not in nets:
            nets.append(net)
    return nets


def _trusted_networks() -> List[Any]:
    """TTL-cached union of both trusted-proxy-range settings."""
    global _trust_cache
    now = time.monotonic()
    legacy = get_trusted_networks()
    # keyed on the identity of the legacy helper's cached list: within its
    # own TTL it returns the same object, so a swapped/expired legacy cache
    # (tests, settings change) forces a recompute here — no stale trust
    if (_trust_cache is not None and now - _trust_cache[0] < _TRUST_CACHE_TTL
            and _trust_cache[1] is legacy):
        return _trust_cache[2]
    raw: List[str] = []
    psr = _psr()
    try:
        value = psr.get_setting(RANGES_SETTING_KEY)
        if isinstance(value, (list, tuple)):
            raw.extend(value)
    except Exception as e:  # noqa: BLE001 — a settings outage must not 500
        logger.warning("client_ip: %s lookup failed (%s)", RANGES_SETTING_KEY, e)
    # the legacy str twin (utils/client_ip already caches + defaults it)
    raw.extend(str(n) for n in legacy)
    nets = _parse_networks(raw)
    if not nets:
        nets = _parse_networks(
            psr.DEFAULT_TRUSTED_PROXY_RANGES)  # type: ignore[attr-defined]
    _trust_cache = (now, legacy, nets)
    return nets


def trusted_proxy_networks() -> List[Any]:
    """Public accessor for the trusted-proxy network list (the union of
    both settings, TTL-cached) — consumers that test membership rather
    than resolve an IP (auto-block exemptions, allowlists) use this so
    they agree with :func:`resolve_client_ip`."""
    return _trusted_networks()


def _peer_address(request) -> Optional[str]:
    client = getattr(request, "client", None)
    return getattr(client, "host", None)


def _valid_ip(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return value


def resolve_client_ip(request) -> Optional[str]:
    """Real client IP for `request`, or None when it cannot be determined.

    - Peer outside every trusted range -> the peer itself; forwarded headers
      are ignored (a spoofed XFF from a public client can never change the
      rate-limit / failed-login / audit key).
    - Peer inside a trusted range -> CF-Connecting-IP if valid, else the
      right-most XFF hop that is not itself trusted, else X-Real-IP, else
      the peer (the proxy is the final hop).
    - Malformed forwarded values are never returned.
    """
    peer = _peer_address(request)
    nets = _trusted_networks()
    if not (peer and is_trusted_peer(peer, nets)):
        return peer

    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf and _valid_ip(cf):
        return cf

    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        chain = [h.strip() for h in xff.split(",") if h.strip()]
        for hop in reversed(chain):
            if not is_trusted_peer(hop, nets):
                return _valid_ip(hop) or peer
        if chain:
            # every hop is itself a trusted proxy — the closest one wins
            return _valid_ip(chain[-1]) or peer

    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return _valid_ip(real_ip) or peer
    return peer


# ---------------------------------------------------------------------------
# nginx side — set_real_ip_from rendering (restart-window apply)
# ---------------------------------------------------------------------------

SNIPPET_HEADER = (
    "# GSEC FX.106 p3a — trusted-proxy ranges for the edge's realip module.\n"
    "# Rendered from the platform setting network.trusted_proxy_cidrs.\n"
)


def _nginx_network(net) -> str:
    """nginx realip form of a network: bare address when fully masked, else
    ``<address>/<prefixlen>`` — byte-identical to what the nginx entrypoint
    renders from its shell-side default (127.0.0.1, ::1, 172.16.0.0/12 …)."""
    if net.prefixlen == net.max_prefixlen:
        return str(net.network_address)
    return f"{net.network_address}/{net.prefixlen}"


def render_real_ip_block(cidrs: Iterable[str]) -> str:
    """Render ``set_real_ip_from`` lines (nginx realip directive shape)."""
    lines = [SNIPPET_HEADER.rstrip("\n")]
    for item in cidrs:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"trusted-proxy range must be a non-empty string: {item!r}")
        net = ipaddress.ip_network(item.strip(), strict=False)
        if net.prefixlen == 0:
            raise ValueError(
                f"trusted-proxy range must not be any-address: {item.strip()!r}")
        lines.append(f"    set_real_ip_from {_nginx_network(net)};")
    return "\n".join(lines) + "\n"


def real_ip_block_from_settings(db: Optional[Any] = None) -> str:
    """Render the block from the live ``network.trusted_proxy_cidrs`` value."""
    return render_real_ip_block(_psr().get_setting(RANGES_SETTING_KEY, db=db))


def apply_real_ip_block(db: Optional[Any] = None,
                        path: Optional[str] = None) -> str:
    """Render the DB-sourced nginx block and write it atomically to ``path``.

    ``path=None`` is a pure render (no write). The restart-window apply runs
    it with the drop-file path (``CORE/startup/rendered/real-ip-drop.conf``)
    and then recreates nginx. Returns the rendered text.
    """
    block = real_ip_block_from_settings(db=db)
    if path is None:
        return block
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # docker bind-mounts a host path that does not exist yet as an empty
    # DIRECTORY at both ends (same hazard noted for the mesh drop in
    # docker-compose.yml). Clear that placeholder so the real file can land.
    if os.path.isdir(path):
        try:
            os.rmdir(path)
        except OSError:
            pass  # non-empty dir: refuse to clobber, let os.replace raise
    fd, tmp_path = tempfile.mkstemp(prefix=".real-ip-drop.", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(block)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return block

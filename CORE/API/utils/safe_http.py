"""GSEC FX.151 part 1b — SSRF-guarded HTTP client (utils/safe_http.py).

A single guarded entry point for server-side outbound fetches
(``request`` / ``get`` / ``post``), replacing the raw ``requests.*`` calls
at the 13 sites inventoried in
docs/security-audit/ssrf-fetch-site-inventory.md (call-site migration is
part 1c).

Locked guard design (do not change):

* ``http``/``https`` schemes only.
* The URL's host is resolved through this module's own ``socket.getaddrinfo``
  reference and EVERY resolved address is vetted: private (10/8, 172.16/12,
  192.168/16, IPv6 ULA fc00::/7), loopback (127/8, ::1), link-local
  (169.254/32 — including the 169.254.169.254 cloud metadata endpoint — and
  IPv6 fe80::/10), CGNAT/shared space (100.64/10), multicast, reserved and
  unspecified addresses are all refused, UNLESS the host matches the
  ``security.ssrf_allowlist`` platform setting (list of hostnames/CIDRs,
  default empty), plus — when the caller passes ``tenant_id`` — that
  tenant's own allowlist (FX.151 p3, owner decision Q138=A: exceptions
  are granted per tenant by a tenant admin, never platform-wide). The
  effective list is platform ∪ tenant; the tenant list lives in the
  existing per-tenant settings mechanism (``models.TenantSetting``, key
  ``security.ssrf_tenant_allowlist``) and is read through
  :func:`get_tenant_allowlist`. A settings/read outage degrades to an
  EMPTY tenant list (fail closed).
* An allowlisted fetch the address check would otherwise have refused
  writes ONE audit row: ``SSRF_ALLOWLIST_FETCH`` via
  ``audit.log_security_event`` — metadata carries the host only, NEVER the
  full URL (which may carry tokens).
* The vetted IP is pinned for the send by temporarily wrapping
  ``socket.getaddrinfo`` around the call, so TLS SNI + certificate
  verification still use the hostname (no cert/SNI mismatch, no TOCTOU
  re-resolution to a different IP).
* Redirects are followed manually (``allow_redirects=False``): at most 5
  hops, and EVERY hop is re-vetted (a 302 to a private IP is refused just
  like a direct hit).
* Unresolvable hosts are NOT an error here: the fetch passes through and
  the transport fails naturally with its own DNS error.
* Size guard: the ``Content-Length`` header is pre-checked on the final
  response, and with ``stream=True`` the streamed byte count is capped and
  the iteration aborts past the cap.
* Defaults: connect timeout 5 s, read timeout 30 s, body cap 50 MB.
* Sends go through the GLOBAL ``requests.request`` (never a private
  ``Session``), so existing tests that monkeypatch the module-level
  ``requests`` functions still intercept the traffic.

Refusals and size-cap violations raise :class:`SsrfBlocked`
(a ``ValueError``).
"""
from __future__ import annotations

import ipaddress
import socket
from socket import getaddrinfo as _getaddrinfo
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_REDIRECT_HOPS = 5
_CGNAT_NETWORKS = [ipaddress.ip_network("100.64.0.0/10")]


class SsrfBlocked(ValueError):
    """An outbound fetch was refused by the SSRF guard (or its size cap)."""


#: FX.151 p3 (owner decision Q138=A) — exceptions are granted per tenant by a
#: tenant admin, so the effective allowlist is the platform list ∪ THIS
#: tenant's list. The tenant list rides the existing per-tenant settings
#: mechanism (the registered key ``security.ssrf_allowlist`` read with
#: tenant scope, i.e. its tenant-scoped row), so entry format and validation
#: are shared with the platform list.
PLATFORM_ALLOWLIST_KEY = "security.ssrf_allowlist"


def _load_allowlist(override: Optional[List[str]] = None) -> List[str]:
    """Resolve the allowlist: explicit override, else the platform setting.

    A settings outage degrades to an EMPTY allowlist (fail closed).
    """
    if override is not None:
        return [str(x) for x in override]
    try:
        from services import platform_settings as _psr
        raw = _psr.get_setting("security.ssrf_allowlist")
    except Exception:  # noqa: BLE001 — settings outage = empty (safe)
        return []
    return [str(x) for x in raw] if isinstance(raw, (list, tuple)) else []


def get_tenant_allowlist(tenant_id: Optional[int], db=None) -> List[str]:
    """FX.151 p3 — THIS tenant's allowlist (the per-tenant settings row for
    ``security.ssrf_allowlist``, tenant scope).

    Returns [] for no tenant, no row, or a non-list value (fail closed); a
    settings outage also degrades to []. Entries follow the platform list's
    format rules (validated on write by the registered key's validator); a
    malformed stored entry is skipped here rather than matching anything.
    """
    if tenant_id is None:
        return []
    try:
        from services import platform_settings as _psr
        raw = _psr.get_setting(PLATFORM_ALLOWLIST_KEY, db=db,
                               tenant_id=int(tenant_id))
    except Exception:  # noqa: BLE001 — settings outage = empty (safe)
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x) for x in raw]


def _host_in_allowlist(host: str, allowlist: List[str]) -> bool:
    """Exact hostname (subdomains match) or an IP inside a CIDR entry."""
    for entry in allowlist:
        e = str(entry).strip().lower()
        if not e:
            continue
        if "/" in e:
            try:
                net = ipaddress.ip_network(e, strict=False)
            except ValueError:
                continue  # malformed entry: the setting validator rejects it
            try:
                if ipaddress.ip_address(host) in net:
                    return True
            except ValueError:
                continue
        if host == e or host.endswith("." + e):
            return True
    return False


def _address_blocked(ip) -> bool:
    """True for anything the SSRF guard refuses by default."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return _address_blocked(ip.ipv4_mapped)  # e.g. ::ffff:127.0.0.1
    return bool(
        ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or any(ip in net for net in _CGNAT_NETWORKS)
    )


def _private_exempt_ok(ip) -> bool:
    """Addresses the tenant-owned-host exemption may reach: private only."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if str(ip) in ("169.254.169.254", "fd00:ec2::254"):
        return False
    return bool(ip.is_private and not (
        ip.is_loopback or ip.is_unspecified or ip.is_link_local
        or ip.is_multicast or ip.is_reserved))


def _resolve(host: str, port: int) -> Optional[List[Tuple]]:
    """Resolve via this module's own getaddrinfo reference.

    Returns the raw getaddrinfo list, or None when the host does not
    resolve — unresolvable is NOT an error (the transport fails naturally
    downstream); it just means nothing can be pinned or vetted.
    """
    try:
        infos = _getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, socket.herror, OSError, UnicodeError):
        return None
    return infos or None


def _audit_allowlisted_fetch(
    host: str,
    tenant_id: Optional[int] = None,
    matched_by: str = "platform",
) -> None:
    """One audit row per allowlisted fetch (host only — never the URL).

    FX.151 p3: metadata also names the tenant and which list granted the
    exception (``platform`` / ``tenant``). BUG.B80 shape — the audit helper
    commits inside the caller's session, so the write runs on its OWN
    savepoint: an FK miss / IntegrityError / dead connection rolls back only
    the audit row and never propagates out of the fetch path.
    """
    try:
        import audit
        import database
        db = database.SessionLocal()
        try:
            # A session that cannot open a savepoint (a test double) is used
            # as-is; the write is still never allowed to escape the fetch path.
            try:
                savepoint = db.begin_nested()
            except Exception:  # noqa: BLE001 — no savepoint available
                savepoint = None
            try:
                audit.log_security_event(
                    db,
                    audit.models.AuditAction.SSRF_ALLOWLIST_FETCH,
                    tenant_id=tenant_id,
                    metadata={
                        "host": host,
                        "source": "utils.safe_http",
                        "matched_by": matched_by,
                        "tenant_id": tenant_id,
                    },
                )
            except Exception:  # noqa: BLE001 — roll back only the audit row
                if savepoint is not None:
                    try:
                        savepoint.rollback()
                    except Exception:  # already released by the helper's commit
                        pass
                raise
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — audit failure must not break the fetch
        pass


def _pin_getaddrinfo(pinned: Optional[List[Tuple]], host: str) -> Optional[Any]:
    """Wrap socket.getaddrinfo so the send connects to the vetted addresses
    (SNI + cert verification still use the hostname). Returns an undo.

    The wrapper answers for the vetted host (or a bare-IP spelling of one of
    the pinned addresses); every other lookup falls through to the real
    resolver so unrelated traffic in this process is untouched.
    """
    if not pinned or not host:
        return None
    real = socket.getaddrinfo
    pinned_hosts = {str(host).lower()}
    pinned_hosts.update(a[4][0].lower() for a in pinned)

    def _pinned(h, port, *args, **kwargs):
        if str(h).lower() in pinned_hosts:
            return pinned
        return real(h, port, *args, **kwargs)

    socket.getaddrinfo = _pinned
    return real


def _enforce_size_cap(resp: "requests.Response", max_bytes: int) -> None:
    if max_bytes <= 0:
        return
    length = resp.headers.get("Content-Length")
    if length is not None:
        try:
            length = int(length)
        except ValueError:
            length = None  # malformed header — the streamed count still guards
        if length is not None and length > max_bytes:
            raise SsrfBlocked(
                f"response body {length} bytes exceeds the "
                f"{max_bytes} byte SSRF size cap"
            )


def _cap_stream(resp: "requests.Response", max_bytes: int) -> None:
    """Abort the iterator once the streamed byte count passes the cap."""
    if max_bytes <= 0:
        return
    chunks = resp.iter_content(chunk_size=None)

    def _capped():
        seen = 0
        for chunk in chunks:
            seen += len(chunk)
            if seen > max_bytes:
                raise SsrfBlocked(
                    f"streamed {seen} bytes, exceeds the {max_bytes} byte "
                    f"SSRF size cap"
                )
            yield chunk

    resp.iter_content = lambda chunk_size=None: _capped()


def _vet_and_send(
    url: str,
    method: str,
    kw: Dict[str, Any],
    *,
    allowlist: List[str],
    tenant_allowlist: Optional[List[str]] = None,
    tenant_id: Optional[int] = None,
    private_ok_host: Optional[str] = None,
    connect_timeout: float,
    read_timeout: float,
) -> "requests.Response":
    """Vet the target, pin the vetted IP for the send, return the response."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    # FX.151 p3: the effective allowlist is platform ∪ tenant. Which half
    # granted the exception is recorded, so an audit row naming the tenant
    # only appears when the TENANT entry is what let the fetch through.
    matched_by = "platform"
    allowlisted = _host_in_allowlist(host, allowlist) if host else False
    if not allowlisted and tenant_allowlist and host:
        if _host_in_allowlist(host, tenant_allowlist):
            allowlisted = True
            matched_by = "tenant"
    infos = _resolve(host, port) if host else None
    if infos is not None:
        bad = [
            ipaddress.ip_address(a[4][0])
            for a in infos
            if _address_blocked(ipaddress.ip_address(a[4][0]))
        ]
        if bad and private_ok_host and host.lower().rstrip(".") == private_ok_host:
            # GX n9: exact-host private exemption (caller proved tenant
            # ownership). Private ranges only; loopback/link-local/metadata/
            # unspecified/multicast/reserved/CGNAT stay blocked.
            bad = [b for b in bad if not _private_exempt_ok(b)] or []
            if not bad:
                allowlisted = True
                matched_by = "tenant_owned_host"
        if bad and not allowlisted:
            raise SsrfBlocked(
                f"SSRF guard: refusing fetch to {host!r} "
                f"(resolves to blocked address(es): {', '.join(str(b) for b in bad)})"
            )
        if bad:
            _audit_allowlisted_fetch(host, tenant_id, matched_by)
    pinned_undo = _pin_getaddrinfo(infos, host)
    try:
        resp = requests.request(
            method,
            url,
            allow_redirects=False,
            timeout=(connect_timeout, read_timeout),
            **kw,
        )
    finally:
        if pinned_undo is not None:
            socket.getaddrinfo = pinned_undo
    return resp


def request(
    method: str,
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    tenant_id: Optional[int] = None,
    **kw: Any,
) -> "requests.Response":
    """SSRF-guarded outbound fetch (manual redirect following, max 5 hops).

    ``stream=True`` responses are byte-capped; the allowlist is read from
    the ``security.ssrf_allowlist`` platform setting. When ``tenant_id`` is
    given (FX.151 p3, Q138=A) the effective allowlist is the platform list
    ∪ that tenant's own allowlist, and a fetch allowed only by the tenant
    entry is audited with ``matched_by="tenant"``.
    """
    scheme = (urlsplit(url).scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SsrfBlocked(
            f"SSRF guard: only http/https URLs are allowed (got {scheme!r})"
        )
    max_bytes = int(max_bytes or 0)
    allowlist = _load_allowlist(kw.pop("_ssrf_allowlist", None))
    tenant_allowlist = get_tenant_allowlist(tenant_id)
    connect_timeout = kw.pop("_ssrf_connect_timeout", DEFAULT_CONNECT_TIMEOUT)
    read_timeout = kw.pop("_ssrf_read_timeout", DEFAULT_READ_TIMEOUT)
    private_ok_host = kw.pop("_ssrf_private_ok_host", None)
    if private_ok_host:
        private_ok_host = str(private_ok_host).lower()
    last_url = url
    for _ in range(MAX_REDIRECT_HOPS + 1):
        # Re-check the scheme every hop: a 302 to file:///ftp:// is refused.
        hop_scheme = (urlsplit(last_url).scheme or "").lower()
        if hop_scheme not in ALLOWED_SCHEMES:
            raise SsrfBlocked(
                f"SSRF guard: only http/https URLs are allowed "
                f"(got {hop_scheme!r})"
            )
        resp = _vet_and_send(
            last_url, method, kw,
            allowlist=allowlist,
            tenant_allowlist=tenant_allowlist,
            tenant_id=tenant_id,
            private_ok_host=private_ok_host,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
        if private_ok_host and (resp.is_redirect or resp.is_permanent_redirect):
            resp.close()
            raise SsrfBlocked(
                "SSRF guard: redirects are never followed for a "
                "tenant-owned private host"
            )
        if resp.is_redirect or resp.is_permanent_redirect:
            location = resp.headers.get("Location")
            if not location:
                return resp
            resp.close()
            last_url = urljoin(last_url, location)
            continue
        _enforce_size_cap(resp, max_bytes)
        if kw.get("stream"):
            _cap_stream(resp, max_bytes)
        return resp
    raise SsrfBlocked(
        f"SSRF guard: too many redirects (>{MAX_REDIRECT_HOPS} hops)"
    )


def get(url: str, **kw: Any) -> "requests.Response":
    """SSRF-guarded GET (see :func:`request`)."""
    return request("GET", url, **kw)


def post(url: str, **kw: Any) -> "requests.Response":
    """SSRF-guarded POST (see :func:`request`)."""
    return request("POST", url, **kw)

"""Declared-egress allowlist compiler (declared-egress-and-sealed-nodes 2.1).

Pure functions, no DB. Normalises the two egress declaration shapes the
platform carries today into one canonical list of ``EgressEntry``:

* node shape  -- ``allowed_egress: [{host, ports, protocol}]`` (schemas.py)
* stack shape -- ``x-mod.egress.hosts: [str]`` (persistent stacks)

The entry grammar mirrors the Worker's ``internal/egress`` package exactly so
anything accepted here is accepted (and enforced identically) by the worker's
Docker egress proxy and the K8s CiliumNetworkPolicy generator. A single entry
is one of:

    example.com / example.com:8443         exact host, default or that port
    *.example.com / *.example.com:8443     any subdomain (NOT the apex)
    10.1.2.3 / 10.1.2.3:9000               IPv4 / IPv6 literal (prefix)
    [2001:db8::1]:9000                     bracketed IPv6 with a port
    10.0.0.0/8 / fd00::/8                  CIDR (default ports)

Rejected (fail closed -- one bad entry fails the whole list): empty entries,
bare ``*``, ``*`` anywhere but a leading ``*.``, single-label wildcard
suffixes (``*.com``), ports outside 1-65535, IPv6 zones, malformed hostnames,
a port on a CIDR, and a node ``host:port`` whose ``ports`` disagree. ``kind`` is ``tailnet`` for mesh
peers (100.64.0.0/10 addresses / CIDRs inside it, and names under the mesh DNS
suffix) and ``internet`` otherwise.

The tailnet predicate mirrors
``routers.persistent_stacks._shared._is_tailnet_host``; a parity test guards
against drift (services must not import routers).
"""

from __future__ import annotations

import ipaddress
import logging

import models
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

DEFAULT_PORTS: Tuple[int, ...] = (80, 443)

KIND_INTERNET = "internet"
KIND_TAILNET = "tailnet"

_TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_DNS_SUFFIXES = (".example.com",)

_TCP_PROTOCOLS = frozenset({"", "tcp", "http", "https"})


class EgressAllowlistError(ValueError):
    """An egress declaration failed validation (the whole list is rejected)."""


# Breadth thresholds for classify_breadth
MAX_NODE_ENTRIES = 16
MULTI_LABEL_SUFFIXES = (
    "co.uk", "com.au", "co.jp", "org.uk", "gov.uk",
    "ac.uk", "com.br", "co.nz", "co.in",
)


@dataclass(frozen=True)
class EgressEntry:
    host: str
    ports: Tuple[int, ...]
    kind: str


def _parse_port(s: Any) -> int:
    t = str(s).strip() if s is not None else ""
    if t == "":
        raise EgressAllowlistError("empty port")
    if not t.isascii() or not t.isdigit():
        raise EgressAllowlistError(f"invalid port {t!r}")
    p = int(t)
    if p < 1 or p > 65535:
        raise EgressAllowlistError(f"port {t!r} out of range 1-65535")
    return p


def _validate_hostname(h: str) -> None:
    if not h:
        raise EgressAllowlistError("empty hostname")
    if len(h) > 253:
        raise EgressAllowlistError("hostname too long")
    labels = h.split(".")
    for label in labels:
        if not label or len(label) > 63:
            raise EgressAllowlistError(f"invalid label {label!r}")
        if label[0] == "-" or label[-1] == "-":
            raise EgressAllowlistError(f"label {label!r} starts or ends with '-'")
        for c in label:
            if not (("a" <= c <= "z") or ("0" <= c <= "9") or c in "-_"):
                raise EgressAllowlistError(f"invalid character {c!r} in hostname")
    if labels[-1].strip("0123456789") == "":
        raise EgressAllowlistError(
            "numeric final label (not a valid hostname or IPv4 address)"
        )


def _canonical_ip(s: str) -> str:
    """Canonical lowercase form of an IP string (Go ``str(netip.Addr)``)."""
    ip = ipaddress.ip_address(s)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _is_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class _Rule:
    """One parsed entry: ``(host, ports, is_cidr)``.

    ``host`` is canonical (lowercase, trailing dot stripped; wildcards reduce to
    their suffix; IPv6/IP literals are canonicalised). ``ports`` is the effective
    port set for this entry.
    """

    __slots__ = ("host", "ports", "is_cidr")

    def __init__(self, host: str, ports: Tuple[int, ...], is_cidr: bool) -> None:
        self.host = host
        self.ports = ports
        self.is_cidr = is_cidr

    @property
    def is_ip(self) -> bool:
        return _is_literal(self.host) and "/" not in self.host


def _parse_cidr(e: str) -> _Rule:
    try:
        net = ipaddress.ip_network(e, strict=False)
    except ValueError as exc:
        raise EgressAllowlistError(f"invalid CIDR: {exc}") from None
    if isinstance(net, ipaddress.IPv6Network) and net.network_address.ipv4_mapped is not None:
        net = ipaddress.ip_network(
            f"{net.network_address.ipv4_mapped}/{net.prefixlen - 96}"
        )
    return _Rule(str(net), DEFAULT_PORTS, is_cidr=True)


def _parse_ipv6_literal(e: str) -> _Rule:
    if e.startswith("["):
        end = e.find("]")
        if end < 0:
            raise EgressAllowlistError("unterminated '['")
        addr, rest = e[1:end], e[end + 1 :]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise EgressAllowlistError(f"invalid IP address: {exc}") from None
        if not isinstance(ip, ipaddress.IPv6Address):
            raise EgressAllowlistError("brackets are only valid around IPv6 addresses")
        port = None
        if rest:
            if not rest.startswith(":"):
                raise EgressAllowlistError("unexpected text after ']'")
            port = _parse_port(rest[1:])
        return _Rule(_canonical_ip(addr), (port,) if port else DEFAULT_PORTS, False)

    # Unbracketed IPv6 (two+ colons) -- no port allowed.
    try:
        ip = ipaddress.ip_address(e)
    except ValueError as exc:
        raise EgressAllowlistError(f"invalid IP address: {exc}") from None
    if not isinstance(ip, ipaddress.IPv6Address):
        raise EgressAllowlistError("IPv6 literal has a port")
    return _Rule(_canonical_ip(e), DEFAULT_PORTS, False)


def _parse_hostname_or_ipv4(e: str) -> _Rule:
    host, port = e, None
    if ":" in e:
        host, _, port_str = e.rpartition(":")
        port = _parse_port(port_str)

    # IPv4 literal (Go keeps the parsed port on the prefix rule).
    if "." in host and _is_literal(host):
        if not isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address):
            raise EgressAllowlistError("invalid address")
        return _Rule(_canonical_ip(host), (port,) if port else DEFAULT_PORTS, False)

    host = host.lower().rstrip(".")
    if host.startswith("*."):
        suffix = host[2:]
        try:
            _validate_hostname(suffix)
        except EgressAllowlistError as exc:
            raise EgressAllowlistError(f"invalid wildcard suffix: {exc}") from None
        if "." not in suffix:
            raise EgressAllowlistError(
                f'wildcard suffix must have at least two labels ("*.{suffix}" is too broad)'
            )
        return _Rule(suffix, (port,) if port else DEFAULT_PORTS, False)
    if "*" in host:
        raise EgressAllowlistError('"*" is only allowed as a leading "*."')
    _validate_hostname(host)
    return _Rule(host, (port,) if port else DEFAULT_PORTS, False)


def _parse_entry(e: str) -> _Rule:
    """Parse one grammar string -> a canonical ``_Rule``."""
    if e == "*":
        raise EgressAllowlistError('bare "*" is not allowed')
    if "%" in e:
        raise EgressAllowlistError("IPv6 zones are not allowed")
    if "/" in e:
        return _parse_cidr(e)
    if e.count(":") >= 2:
        return _parse_ipv6_literal(e)
    return _parse_hostname_or_ipv4(e)


def classify_host(host: str) -> str:
    """``tailnet`` for mesh peers, else ``internet``. ``host`` is canonical."""
    h = str(host or "").strip().lower().rstrip(".")
    if h.endswith(_TAILNET_DNS_SUFFIXES):
        return KIND_TAILNET
    try:
        if "/" in h:
            net = ipaddress.ip_network(h, strict=False)
            if net.version == 4 and net.subnet_of(_TAILNET_NET):
                return KIND_TAILNET
        elif ipaddress.ip_address(h) in _TAILNET_NET:
            return KIND_TAILNET
    except ValueError:
        pass
    return KIND_INTERNET


def _node_item(item: dict) -> _Rule:
    proto = item.get("protocol")
    proto = "" if proto is None else str(proto).strip().lower()
    if proto not in _TCP_PROTOCOLS:
        raise EgressAllowlistError(f"unsupported protocol {proto!r} (TCP only)")
    raw_host = item.get("host")
    host_str = "" if raw_host is None else str(raw_host).strip()
    if not host_str:
        raise EgressAllowlistError("empty host")
    parsed = _parse_entry(host_str)

    raw_ports = item.get("ports")
    if raw_ports is None:
        explicit: set[int] = set()
    elif isinstance(raw_ports, (int, str)):
        explicit = {_parse_port(raw_ports)}
    elif isinstance(raw_ports, (list, tuple)):
        explicit = {_parse_port(p) for p in raw_ports}
    else:
        raise EgressAllowlistError(
            f"ports must be a list, got {type(raw_ports).__name__}"
        )

    # The Worker grammar has no port syntax on a CIDR.
    if parsed.is_cidr and explicit and explicit - set(DEFAULT_PORTS):
        raise EgressAllowlistError(f"CIDR {host_str!r} cannot carry explicit ports")
    # host:port plus a disagreeing ports list is ambiguous -- fail closed.
    if explicit and parsed.ports != DEFAULT_PORTS and explicit != set(parsed.ports):
        raise EgressAllowlistError(
            f"host {host_str!r} names port {parsed.ports[0]} but ports={sorted(explicit)}"
        )

    if explicit:
        return _Rule(parsed.host, tuple(sorted(explicit)), parsed.is_cidr)
    return parsed


def _string_item(item: str) -> _Rule:
    e = item.strip()
    if not e:
        raise EgressAllowlistError("empty entry")
    return _parse_entry(e)


def compile_allowlist(entries: Optional[Iterable[Any]]) -> List[EgressEntry]:
    """Compile node- or stack-shaped egress declarations to canonical entries.

    Accepts a list whose items are grammar strings (stack ``x-mod.egress.hosts``)
    and/or ``{host, ports, protocol}`` dicts (node ``allowed_egress``). Entries
    naming the same host are merged (ports unioned) in first-seen order. Any
    invalid entry raises ``EgressAllowlistError`` (fail closed). ``None`` or an
    empty list compiles to ``[]`` (default-deny).
    """
    if entries is None:
        return []
    if isinstance(entries, (str, bytes, dict)):
        raise EgressAllowlistError("egress entries must be a list")
    order: List[str] = []
    merged: dict = {}
    for idx, item in enumerate(entries):
        try:
            if isinstance(item, str):
                parsed = _string_item(item)
            elif isinstance(item, dict):
                parsed = _node_item(item)
            else:
                raise EgressAllowlistError(f"unsupported entry type {type(item).__name__}")
        except EgressAllowlistError as exc:
            raise EgressAllowlistError(f"egress entry {idx} ({item!r}): {exc}") from None
        host = parsed.host
        if host not in merged:
            order.append(host)
            merged[host] = set()
        merged[host].update(parsed.ports)
    return [
        EgressEntry(host=h, ports=tuple(sorted(merged[h])), kind=classify_host(h))
        for h in order
    ]


def classify_breadth(entries: Optional[Iterable[Any]]) -> List[str]:
    """Return human-readable reasons why a compiled allowlist is broad.

    An empty list means the allowlist is narrow (acceptable).  Each non-empty
    element is a short sentence that can be shown to an operator in a warning
    banner.

    Broadness conditions (at most one entry per condition):

    * Public-suffix wildcard  -- ``*.com`` (any single-label wildcard or a
      wildcard whose suffix is a known multi-label public-suffix like
      ``*.co.uk``).  A plain bare ``*`` also triggers this.
    * Overwide CIDR           -- IPv4 prefix < /24, IPv6 prefix < /56.
      0.0.0.0/0 and ::/0 are always flagged.
    * Entry overflow          -- more than ``MAX_NODE_ENTRIES`` hosts.
    * Port 0                  -- allows all ports.
    * Port range              -- any single host listing more than the two
      default ports (80+443).

    Malformed input may cause ``compile_allowlist`` to raise; breadth
    pre-checks (wildcards, port 0) are collected before compilation, so their
    reasons still surface.  ``None`` and ``[]`` return ``[]``.
    """
    if entries is None:
        return []

    reasons: List[str] = []

    # 1. Public-suffix wildcards (check raw input before compilation)
    _detect_public_suffix_wildcards(entries, reasons)

    # 2. Port 0 from raw input (before compilation, since _parse_port rejects 0)
    for item in entries:
        ports = _extract_port_list(item)
        if 0 in ports:
            reasons.append("port 0 allows all ports")
            break

    # 3. Compile and validate — catch malformed input so pre-check reasons
    #    still surface (C3 handles the 422s for the remainder).
    compiled: List[EgressEntry] = []
    try:
        compiled = compile_allowlist(entries)
    except EgressAllowlistError:
        pass

    # 4. Entry count
    if len(compiled) > MAX_NODE_ENTRIES:
        reasons.append(
            f"egress allowlist has {len(compiled)} entries "
            f"(max {MAX_NODE_ENTRIES})"
        )

    # 5. CIDR width — only flag if STRICTLY wider (prefixlen < threshold)
    for entry in compiled:
        host = entry.host
        if "/" not in host:
            continue
        try:
            net = ipaddress.ip_network(host, strict=False)
        except ValueError:
            continue
        if net.version == 4 and net.prefixlen < 24:
            if net.prefixlen == 0 and str(net.network_address) == "0.0.0.0":
                reasons.append("0.0.0.0/0 allows all IPv4 traffic")
            else:
                reasons.append(
                    f"CIDR {host} is wider than /24"
                )
        elif net.version == 6 and net.prefixlen < 56:
            if net.prefixlen == 0 and str(net.network_address) == "::":
                reasons.append("::/0 allows all IPv6 traffic")
            else:
                reasons.append(
                    f"CIDR {host} is wider than /56"
                )

    # 6. Port range (more than default ports) — from raw input
    for item in entries:
        ports = _extract_port_list(item)
        non_default = {p for p in ports if p not in DEFAULT_PORTS}
        if len(non_default) > 0:
            reasons.append(
                f"egress allows non-default ports: {sorted(non_default)}"
            )
            break

    return reasons


def _detect_public_suffix_wildcards(
    entries: Iterable[Any], reasons: List[str]
) -> None:
    """Check raw entries for broad public-suffix wildcards."""
    seen: set[str] = set()
    for item in entries:
        host = ""
        if isinstance(item, str):
            host = item.strip()
        elif isinstance(item, dict):
            h = item.get("host")
            if h is not None:
                host = str(h).strip()

        # Bare * covers everything
        if host == "*":
            if "*" not in seen:
                seen.add("*")
                reasons.append(
                    f'wildcard "{host}" covers entire public suffix'
                )
            continue

        if host.startswith("*."):
            suffix = host[2:].lower().rstrip(".")
            if "." not in suffix:
                # Single-label wildcard: *.com, *.io
                if suffix not in seen:
                    seen.add(suffix)
                    reasons.append(
                        f'wildcard "{host}" covers entire public suffix'
                    )
            else:
                # Multi-label check: *.co.uk → suffix ends with a known suffix
                for mls in MULTI_LABEL_SUFFIXES:
                    if suffix == mls or suffix.endswith("." + mls):
                        if suffix not in seen:
                            seen.add(suffix)
                            reasons.append(
                                f'wildcard "{host}" covers entire public suffix'
                            )
                        break


def _extract_port_list(item: Any) -> set:
    """Extract ports from a dict entry, falling back to DEFAULT_PORTS for strings."""
    if isinstance(item, dict):
        raw = item.get("ports")
        if raw is None:
            return set(DEFAULT_PORTS)
        if isinstance(raw, (int, str)):
            try:
                return {int(raw)}
            except (ValueError, TypeError):
                return set(DEFAULT_PORTS)
        if isinstance(raw, (list, tuple)):
            ports: set = set()
            for p in raw:
                try:
                    ports.add(int(p))
                except (ValueError, TypeError):
                    pass
            return ports if ports else set(DEFAULT_PORTS)
        return set(DEFAULT_PORTS)
    return set(DEFAULT_PORTS)


def _with_port(host: str, port: int) -> str:
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def to_worker_list(entries: Iterable[EgressEntry]) -> List[str]:
    """Render entries as the ``payload['allowed_egress']`` strings the worker parses.

    Default ports (80+443) render as the bare host; any other port renders as
    ``host:port`` (``[v6]:port`` for IPv6). CIDRs always render bare.
    """
    out: List[str] = []
    seen = set()

    def add(s: str) -> None:
        if s not in seen:
            seen.add(s)
            out.append(s)

    for entry in entries:
        ports = set(entry.ports) or set(DEFAULT_PORTS)
        if "/" in entry.host:
            add(entry.host)
            continue
        if set(DEFAULT_PORTS) <= ports:
            add(entry.host)
            ports -= set(DEFAULT_PORTS)
        for p in sorted(ports):
            add(_with_port(entry.host, p))
    return out


# ---------------------------------------------------------------------------
# C5a — declare egress + audit trail
# ---------------------------------------------------------------------------

def _diff_hosts(new_hosts: frozenset[str], old_hosts: frozenset[str]):
    """Return ``(added, removed)`` host name sets."""
    return new_hosts - old_hosts, old_hosts - new_hosts


def record_egress_declaration(
    db,
    *,
    tenant_id: int,
    actor_user_id: int,
    subject_type: str,
    subject_id,
    entries,
    previous_entries=None,
):
    """Record one egress-declaration audit event and the diff vs previous.

    ``subject_type`` is ``"node"`` or ``"stack"``; ``subject_id`` is the
    resource primary-key.  ``entries`` / ``previous_entries`` are the same
    iterable-of-dicts-or-strings accepted by :func:`compile_allowlist`.

    Best-effort: wrapped in a nested savepoint so a failed INSERT can never
    abort the caller's transaction.  A warning is logged on failure.
    """
    log = logging.getLogger("egress_declare")

    savepoint = db.begin_nested()
    try:
        new = compile_allowlist(entries)
        old = compile_allowlist(previous_entries)

        new_hosts = frozenset(e.host for e in new)
        old_hosts = frozenset(e.host for e in old)
        added, removed = _diff_hosts(new_hosts, old_hosts)

        # Classify hosts by kind (tailnet vs internet)
        new_kinds = sorted(set(e.kind for e in new))
        old_kinds = sorted(set(e.kind for e in old))

        details = {
            "workload_kind": subject_type,
            "workload_id": subject_id,
            "hosts": sorted(new_hosts),
            "kinds": new_kinds,
            "breadth": len(new_hosts),
            "added": sorted(added),
            "removed": sorted(removed),
            "total_old_hosts": len(old_hosts),
            "total_new_hosts": len(new_hosts),
        }

        audit_event = models.AuditEvent(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            event_name="EGRESS_DECLARED",
            details=details,
        )
        db.add(audit_event)
        db.commit()
        db.refresh(audit_event)
        log.info(
            "EGRESS_DECLARED tenant=%s actor=%s %s=%s hosts=%s added=%s removed=%s",
            tenant_id, actor_user_id, subject_type, subject_id,
            len(new_hosts), len(added), len(removed),
        )

    except Exception:  # noqa: BLE001 — audit must never break the caller
        try:
            savepoint.rollback()
        except Exception:
            pass  # savepoint may already be released by the commit above
        log.warning(
            "egress declaration audit FAILED tenant=%s %s=%s",
            tenant_id, subject_type, subject_id, exc_info=True,
        )

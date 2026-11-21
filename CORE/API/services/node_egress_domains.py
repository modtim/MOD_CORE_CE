"""FX.91 p2a [B] — the node-manifest `egress_domains` field (GSEC Q77=A).

FX.91 makes node containers default-deny outbound (the worker's
`mod-user-internal` posture, CORE/Worker/internal/executor/netpol.go) and
grants egress only through the per-task proxy bridge. The manifest half of
that contract is a node declaring WHICH external hostnames it needs, so a
tenant admin can review the list once (p2b palette UI, p2c approval)
instead of the platform guessing:

    egress_domains: ["huggingface.co", "*.cdn.jsdelivr.net"]

This module owns the grammar + normalisation for that field. It is a
separate, stricter shape from the existing `allowed_egress`
(`services/egress_allowlist.py`, `{host, ports, protocol}` entries that
also accept IP/CIDR literals): `egress_domains` is a plain list of
hostnames — no ports, no schemes, no paths, no IPs, no CIDRs — because the
approval UI shows it to a human and the enforcement half (p3) matches FQDNs
only. Both fields compile through the same allowlist at dispatch; neither
replaces the other.

Grammar (fail closed — one bad entry fails the whole list):

    example.com            exact hostname
    *.example.com          any subdomain of the apex (wildcard ONLY as the
                           leading `*.` label; `*.com` is rejected)

Rules: lowercase ASCII only (no case folding — an uppercase entry is a
manifest slip and is rejected, not silently fixed), 1-253 chars, labels
1-63 chars of `[a-z0-9_-]` not starting/ending with `-`, a wildcard entry
needs at least two labels behind it, a non-numeric final label (IP
literals rejected), no scheme / path / port / whitespace / IDN, max 50
entries. Valid entries are deduped and sorted; the stored value is always
that canonical list.

`get_node_egress_domains(node_def)` is the read helper every later part
(p2b palette, p2c approval, p3 gov-profile blocking) reads through.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

MAX_ENTRIES = 50
MAX_HOSTNAME_LEN = 253
MAX_LABEL_LEN = 63

_LABEL_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789-_"
)
_DIGITS = "0123456789"


class EgressDomainError(ValueError):
    """Raised when a manifest `egress_domains` list violates the grammar.

    `code` is a stable machine-readable identifier the manifest-parsing
    seam maps to its own field error (same shape as the other manifest
    field failures there):

      * 'not_a_list'   — the value is not a list of strings
      * 'too_many'     — more than MAX_ENTRIES entries
      * 'bad_entry'    — scheme / port / path / IP / IDN / bad label / bad
                         wildcard / too long (an empty entry is reported
                         here too)
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_hostname(host: str) -> None:
    """One hostname, no wildcard. Raises EgressDomainError('bad_entry')."""
    if not host:
        raise EgressDomainError("bad_entry", "empty hostname")
    if len(host) > MAX_HOSTNAME_LEN:
        raise EgressDomainError(
            "bad_entry",
            f"hostname {host!r} exceeds {MAX_HOSTNAME_LEN} characters",
        )
    labels = host.split(".")
    for label in labels:
        if not label:
            raise EgressDomainError(
                "bad_entry", f"hostname {host!r} has an empty label",
            )
        if len(label) > MAX_LABEL_LEN:
            raise EgressDomainError(
                "bad_entry",
                f"label {label!r} exceeds {MAX_LABEL_LEN} characters",
            )
        if label[0] == "-" or label[-1] == "-":
            raise EgressDomainError(
                "bad_entry", f"label {label!r} starts or ends with '-'"
            )
        for c in label:
            if c not in _LABEL_OK:
                raise EgressDomainError(
                    "bad_entry",
                    f"invalid character {c!r} in hostname {host!r} "
                    "(lowercase a-z, 0-9, '-' and '_' only)",
                )
    if labels[-1].strip(_DIGITS) == "":
        raise EgressDomainError(
            "bad_entry",
            f"hostname {host!r} has an all-numeric final label "
            "(an IP address is not a hostname)",
        )


def _normalise_entry(entry: Any) -> str:
    """One manifest entry -> the canonical hostname string, or raise.

    Accepts only a plain string. A dict is rejected outright: the richer
    `{host, ports, protocol}` shape belongs to `allowed_egress`, and
    silently accepting it here would let one manifest carry two
    contradictory egress declarations.
    """
    if not isinstance(entry, str):
        raise EgressDomainError(
            "not_a_list",
            f"entry {entry!r} is not a string "
            "(use allowed_egress for {host,ports,protocol} entries)",
        )
    e = entry.strip()
    if not e:
        raise EgressDomainError("bad_entry", "empty entry")
    if e == "*" or e.startswith("*.") and len(e) == 2:
        raise EgressDomainError(
            "bad_entry", "bare '*' wildcard matches everything — forbidden"
        )
    if e.startswith("*."):
        rest = e[2:]
        _validate_hostname(rest)
        labels = rest.split(".")
        if len(labels) < 2:
            raise EgressDomainError(
                "bad_entry",
                f"wildcard {e!r} needs a real domain behind it "
                "(*.com-style single-label suffixes are forbidden)",
            )
        return e
    if e.startswith("*"):
        raise EgressDomainError(
            "bad_entry",
            f"wildcard {e!r} is only allowed as a leading '*.' label",
        )
    _validate_hostname(e)
    return e


def normalise_egress_domains(raw: Any) -> List[str]:
    """Validate + normalise a manifest `egress_domains` value.

    None / absent -> [] (default-deny: the node declares no external host).
    A valid list is deduped and sorted so the stored value and the palette
    render are stable across re-scans. Any violation raises
    EgressDomainError — the caller decides whether that is a 400 or a
    logged-and-skipped manifest (the node scanner's per-node isolation).
    """
    if raw is None:
        return []
    if isinstance(raw, (str, bytes, dict)):
        raise EgressDomainError(
            "not_a_list",
            "egress_domains must be a list of hostnames",
        )
    if not isinstance(raw, list):
        raise EgressDomainError(
            "not_a_list",
            f"egress_domains must be a list (got {type(raw).__name__})",
        )
    if len(raw) > MAX_ENTRIES:
        raise EgressDomainError(
            "too_many",
            f"egress_domains declares {len(raw)} entries, "
            f"max {MAX_ENTRIES}",
        )
    out = set()
    for idx, item in enumerate(raw):
        try:
            out.add(_normalise_entry(item))
        except EgressDomainError as exc:
            raise EgressDomainError(
                exc.code,
                f"egress_domains entry {idx} ({item!r}): {exc.message}",
            ) from None
    return sorted(out)


def get_node_egress_domains(node_def) -> List[str]:
    """Stored `egress_domains` for a NodeDefinition row, normalised.

    Read-only and defensive by design: a pre-migration row (column not
    applied yet), a NULL row, or a row whose JSON got hand-edited into a
    bad shape all yield [] — default-deny — rather than raising, so the
    palette list never 500s because one node declared egress badly. The
    WRITE path (normalise_egress_domains) is the strict one.
    """
    if node_def is None:
        return []
    raw = getattr(node_def, "egress_domains", None)
    if raw is None:
        return []
    try:
        return normalise_egress_domains(raw)
    except EgressDomainError:
        return []


def describe_egress_domains(raw: Any) -> Dict[str, Any]:
    """Small diagnostic shape for the manifest-parsing seam's error text.

    Returns {'ok': bool, 'domains': [...], 'error': {'code','message'}|None}
    so a caller can report the failure without catching the exception
    twice. Never raises.
    """
    try:
        return {
            "ok": True,
            "domains": normalise_egress_domains(raw),
            "error": None,
        }
    except EgressDomainError as exc:
        return {
            "ok": False,
            "domains": [],
            "error": {"code": exc.code, "message": exc.message},
        }

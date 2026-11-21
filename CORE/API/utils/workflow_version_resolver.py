"""workflow-expressiveness-primitives 2.2 — semver-style version resolver (PURE).

Sub-workflow nodes carry a ``version_constraint`` (spec
workflow-sub-workflow-composition: "Sub-workflow versions resolve via semver-style
constraints"): ``latest``, an exact version, ``^M.m.p``, ``~M.m.p`` or ``M.x``.
The orchestrator SHALL resolve to the highest matching available version of the
referenced workflow.

This module is deliberately pure — no DB, no I/O, no settings reads. It takes the
available version *labels* (the strings as published, e.g. ``"v1.4.0"``) and
returns the ORIGINAL string of the winner, so the caller can use it to look the
row up without re-formatting.

Public API:
    parse_version(s)                      -> (major, minor, patch) | None
    matches(spec, version)                -> bool
    resolve(spec, available)              -> original string | None
    parse_spec(spec)                      -> Spec tuple (raises ValueError)

Caret ranges follow npm semantics, including the ``^0.x`` special cases
(``^0.2.3`` -> >=0.2.3 <0.3.0, ``^0.0.3`` -> >=0.0.3 <0.1.0).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple

Version = Tuple[int, int, int]
Wildcard = None

# A version label: optional leading "v", M[.m[.p]], optional -pre/+build suffix.
_VERSION_RE = re.compile(
    r"""^\s*v?
        (?P<major>\d+)
        (?:\.(?P<minor>\d+))?
        (?:\.(?P<patch>\d+))?
        (?:[-+](?P<suffix>[0-9A-Za-z][0-9A-Za-z.-]*))?
        \s*$""",
    re.VERBOSE,
)

# A spec body: three segments, each digits or a wildcard (x / X / *).
_SPEC_SEG_RE = re.compile(r"^(?:\d+|[xX*])$")


def parse_version(s: object) -> Optional[Version]:
    """Parse a version label into a (major, minor, patch) tuple, or None.

    Accepts ``"1.2.3"``, ``"v1.2.3"``, ``"1.2"`` -> ``(1, 2, 0)`` and ``"1"``
    -> ``(1, 0, 0)``. Pre-release / build suffixes (``1.2.3-beta.1``,
    ``1.2.3+build7``) are tolerated and dropped, matching the ordering rule the
    fleet already uses in ``utils/worker_version_gate.py``. Anything else
    (empty, prose, wildcards, ``1..3``) is not a version -> None.
    """
    if not isinstance(s, str):
        return None
    m = _VERSION_RE.match(s)
    if not m:
        return None
    return (
        int(m.group("major")),
        int(m.group("minor") or 0),
        int(m.group("patch") or 0),
    )


def _parse_spec_body(body: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Split a constraint body into three slots; ``None`` slot = wildcard.

    Raises ValueError on a malformed body (bad segment, or a concrete segment
    after a wildcard such as ``1.x.3``).
    """
    if not body:
        raise ValueError("empty version constraint")
    segs = body.split(".")
    if len(segs) > 3:
        raise ValueError(f"version constraint has too many segments: {body!r}")
    slots: list[Optional[int]] = []
    seen_wildcard = False
    for seg in segs:
        if not _SPEC_SEG_RE.match(seg):
            raise ValueError(f"bad version-constraint segment: {seg!r}")
        if seg[-1] in "xX*":
            seen_wildcard = True
            slots.append(Wildcard)
        else:
            if seen_wildcard:
                raise ValueError(
                    f"concrete segment after a wildcard: {body!r}"
                )
            slots.append(int(seg))
    while len(slots) < 3:
        slots.append(Wildcard)
    return (slots[0], slots[1], slots[2])


def parse_spec(spec: object) -> Tuple[str, Optional[Version]]:
    """Compile a constraint into ``("latest"|"range"|"exact", floor)``.

    ``"range"`` carries the caret/tilde/wildcard slots; ``matches`` derives the
    upper bound from them. Raises ValueError for an unusable constraint.
    """
    if not isinstance(spec, str):
        raise ValueError(f"version constraint must be a string: {spec!r}")
    text = spec.strip()
    if not text:
        raise ValueError("empty version constraint")
    if text.lower() == "latest":
        return ("latest", None)
    if text[0] == "^":
        return ("caret", _parse_spec_body(text[1:]))
    if text[0] == "~":
        return ("tilde", _parse_spec_body(text[1:]))
    slots = _parse_spec_body(text)
    if any(slot is Wildcard for slot in slots):
        return ("wildcard", slots)
    return ("exact", (slots[0], slots[1], slots[2]))


def _upper_bound(kind: str, slots: Tuple[Optional[int], ...]) -> Optional[Version]:
    """Exclusive upper bound for a caret/tilde/wildcard range (None = open)."""
    major, minor, patch = slots
    if major is Wildcard:
        return None  # ^x / ~x / x — everything
    if kind == "wildcard":
        if minor is Wildcard:
            return (major + 1, 0, 0)
        if patch is Wildcard:
            return (major, minor + 1, 0)
        return (major, minor, patch + 1)
    if kind == "tilde":
        if minor is Wildcard:
            return (major + 1, 0, 0)
        return (major, minor + 1, 0)
    # caret: pinned at the left-most non-zero specified component (npm)
    if minor is Wildcard:
        return (major + 1, 0, 0)
    if major > 0:
        return (major + 1, 0, 0)
    if minor > 0:
        return (0, minor + 1, 0)
    return (0, 1, 0)


def _floor(slots: Tuple[Optional[int], ...]) -> Version:
    """Inclusive lower bound: wildcard slots read as 0."""
    return tuple(0 if slot is Wildcard else slot for slot in slots)  # type: ignore


def matches(spec: object, version: object) -> bool:
    """True iff ``version`` (a label string) satisfies ``spec``.

    Unparseable versions never match. Invalid specs raise ValueError.
    """
    kind, slots = parse_spec(spec)
    got = parse_version(version)
    if got is None:
        return False
    if kind == "latest":
        return True
    if kind == "exact":
        return got == slots  # type: ignore
    if got < _floor(slots):
        return False
    upper = _upper_bound(kind, slots)
    return upper is None or got < upper


def resolve(spec: str, available: Iterable[str]) -> Optional[str]:
    """Highest available version satisfying ``spec``, as its ORIGINAL string.

    Unparseable / non-matching entries are ignored; no match -> None. Invalid
    spec -> ValueError. Ties (same numeric version, e.g. ``"1.2.0"`` and
    ``"v1.2.0"``) keep the first one seen.
    """
    kind, slots = parse_spec(spec)
    best: Optional[Version] = None
    best_label: Optional[str] = None
    for label in available or ():
        got = parse_version(label)
        if got is None:
            continue
        if kind != "latest":
            if got < _floor(slots):
                continue
            upper = _upper_bound(kind, slots)
            if upper is not None and got >= upper:
                continue
            if kind == "exact" and got != slots:
                continue
        if best is None or got > best:
            best, best_label = got, label
    return best_label

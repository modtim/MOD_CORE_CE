"""Semver 2.0 range resolver — pure stdlib, no DB, no router/service imports.

API:
    parse_version(s: str) -> Version
    parse_range(spec: str) -> Range
    resolve(spec: str, available: Iterable[str]) -> Optional[str]
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple


# ── limits ──────────────────────────────────────────────────────────────────
_MAX_SPEC = 64
_MAX_VER = 128


# ── Version ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple  # tuple of (int | str, ...)
    build: str

    @property
    def is_prerelease(self) -> bool:
        return len(self.prerelease) > 0

    # Comparison (SemVer 2.0 §11 — build metadata IGNORED)
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return (self.major, self.minor, self.patch, self.prerelease) == (
            other.major, other.minor, other.patch, other.prerelease,
        )

    def __lt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        a = (self.major, self.minor, self.patch, self.prerelease)
        b = (other.major, other.minor, other.patch, other.prerelease)
        return _cmp_prerelease(a[:4], b[:4]) < 0

    def __le__(self, other: "Version") -> bool:
        return self == other or self < other

    def __gt__(self, other: "Version") -> bool:
        return not self <= other

    def __ge__(self, other: "Version") -> bool:
        return not self < other


def _cmp_prerelease(a: tuple, b: tuple) -> int:
    """SemVer 2.0 §11 precedence comparison for prerelease arrays."""
    a_pre = a[-1] if len(a) > 3 else ()
    b_pre = b[-1] if len(b) > 3 else ()
    # core version always compared first via the tuple
    if a[:3] != b[:3]:
        return (a[:3] > b[:3]) - (a[:3] < b[:3])
    # both have no prerelease → equal
    if not a_pre and not b_pre:
        return 0
    # no-prerelease > any-prerelease
    if not a_pre and b_pre:
        return 1
    if a_pre and not b_pre:
        return -1
    # both have prerelease tokens — compare element by element
    for x, y in zip(a_pre, b_pre):
        if x == y:
            continue
        xi, yi = isinstance(x, int), isinstance(y, int)
        if xi != yi:
            # numeric < alnum
            return -1 if xi else 1
        if x < y:
            return -1
        if x > y:
            return 1
    # shorter set < longer set
    return len(a_pre) - len(b_pre)


# ── parse_version ───────────────────────────────────────────────────────────
_SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def _tokenize_pre(s: str) -> tuple:
    """Split prerelease string into (int | str, ...) tokens."""
    tokens: list[int | str] = []
    for t in s.split("."):
        if t.isdigit():
            tokens.append(int(t))
        else:
            tokens.append(t)
    return tuple(tokens)


def parse_version(s: str) -> Version:
    """Parse a SemVer 2.0 string → Version.  Raises ValueError on bad input."""
    if not isinstance(s, str) or len(s) > _MAX_VER:
        raise ValueError(f"version string exceeds {_MAX_VER} chars")
    m = _SEMVER_RE.match(s)
    if not m:
        raise ValueError(f"not a valid semver: {s!r}")
    pre_raw = m.group("pre")
    pre = _tokenize_pre(pre_raw) if pre_raw else ()
    build = m.group("build") or ""
    return Version(
        major=int(m.group("major")),
        minor=int(m.group("minor")),
        patch=int(m.group("patch")),
        prerelease=pre,
        build=build,
    )


# ── Range ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Range:
    """A parsed semver range.  ``matches(v)`` returns True if ``v`` is in range."""

    spec: str  # original spec string

    # For caret/tilde/wildcard: lower bound (inclusive)
    _low: Optional[Version] = None
    # Upper bound (exclusive)
    _high: Optional[Version] = None
    # Exact match version (if spec is "1.2.3")
    _exact: Optional[Version] = None
    # "latest"/"*" sentinel
    _latest: bool = False
    # Prerelease opt-in: True when spec explicitly contains a prerelease token
    _prerelease_spec: bool = False

    def matches(self, v: Version) -> bool:
        if self._exact is not None:
            return v == self._exact
        if self._latest:
            # latest only matches actual releases (no prerelease)
            return not v.is_prerelease
        lo = self._low
        hi = self._high
        # Prerelease filtering (npm rule): a prerelease version only matches a
        # range if the range's lower-bound comparator ALSO carries a prerelease
        # tag AND shares the same [major, minor, patch] tuple as the candidate.
        if v.is_prerelease:
            if not self._prerelease_spec or lo is None or not lo.is_prerelease:
                return False
            if (v.major, v.minor, v.patch) != (lo.major, lo.minor, lo.patch):
                return False
        if lo is not None and v < lo:
            return False
        if hi is not None and v >= hi:
            return False
        return True

    def __repr__(self) -> str:
        return f"Range({self.spec!r})"


# ── parse_range ─────────────────────────────────────────────────────────────
# Precompiled regexes for range spec parsing
_RE_WILDCARD = re.compile(
    r"^\s*(?P<v>0|[1-9]\d*)(?:\.(?P<w>x|X|\*))?$"
)


def _is_prerelease_spec(spec: str) -> bool:
    """Return True if the spec itself contains a prerelease hyphen segment."""
    # Strip leading/trailing whitespace
    s = spec.strip()
    # Find the version portion (before any space-separated operator)
    vpart = s.split()[0] if s.split() else s
    return "-" in vpart.split("+")[0]


def parse_range(spec: str) -> Range:
    """Parse a semver range specification string → Range.

    Supported forms:
        "latest" | "*"  → highest release
        "1.2.3"         → exact match
        "^1.2.3"        → caret: >=1.2.3, <2.0.0  (same rules for ^0.x)
        "~1.2.3"        :  >=1.2.3, <1.3.0
        "1.x" / "1.2.x" / "1" / "1.2" → wildcard ranges
    """
    if not isinstance(spec, str) or len(spec) > _MAX_SPEC:
        raise ValueError(f"range spec exceeds {_MAX_SPEC} chars")
    s = spec.strip()
    prerelease_spec = _is_prerelease_spec(s)

    # "latest" or "*"
    if s.lower() in ("latest", "*"):
        return Range(spec=spec, _latest=True)

    # Wildcard: "1.x", "1.2.x", "1", "1.2"
    # "1" → >=1.0.0 <2.0.0  /  "1.2" → >=1.2.0 <1.3.0  /  "1.x" same / "1.2.x" same
    m = _RE_WILDCARD.match(s)
    if m:
        major = int(m.group("v"))
        minor_w = m.group("w")
        if minor_w is not None:
            # "1.x" or "1*" → >=1.0.0 <2.0.0
            return Range(
                spec=spec,
                _low=Version(major, 0, 0, (), ""),
                _high=Version(major + 1, 0, 0, (), ""),
                _prerelease_spec=prerelease_spec,
            )
        else:
            # Plain number "1" → same as "1.x"
            return Range(
                spec=spec,
                _low=Version(major, 0, 0, (), ""),
                _high=Version(major + 1, 0, 0, (), ""),
                _prerelease_spec=prerelease_spec,
            )

    # Check for wildcard patterns like "1.x", "1.2.x" with x/X/* in minor/patch
    m2 = re.match(
        r"^\s*(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)(?:\.(?P<patch>x|X|\*))?\s*$", s
    )
    if m2:
        major = int(m2.group("major"))
        minor = int(m2.group("minor"))
        patch_wild = m2.group("patch") is not None
        if patch_wild:
            # "1.2.x" → >=1.2.0 <1.3.0
            return Range(
                spec=spec,
                _low=Version(major, minor, 0, (), ""),
                _high=Version(major, minor + 1, 0, (), ""),
                _prerelease_spec=prerelease_spec,
            )
        else:
            # "1.2" without patch wildcard → >=1.2.0 <1.3.0 (npm compat)
            return Range(
                spec=spec,
                _low=Version(major, minor, 0, (), ""),
                _high=Version(major, minor + 1, 0, (), ""),
                _prerelease_spec=prerelease_spec,
            )

    # Caret: ^1.2.3
    if s.startswith("^"):
        inner = s[1:].strip()
        v = parse_version(inner)
        if v.major != 0:
            # ^1.2.3 → >=1.2.3 <2.0.0
            return Range(
                spec=spec,
                _low=v,
                _high=Version(v.major + 1, 0, 0, (), ""),
                _prerelease_spec=prerelease_spec,
            )
        elif v.minor != 0:
            # ^0.2.3 → >=0.2.3 <0.3.0
            return Range(
                spec=spec,
                _low=v,
                _high=Version(0, v.minor + 1, 0, (), ""),
                _prerelease_spec=prerelease_spec,
            )
        else:
            # ^0.0.3 → >=0.0.3 <0.0.4
            return Range(
                spec=spec,
                _low=v,
                _high=Version(0, 0, v.patch + 1, (), ""),
                _prerelease_spec=prerelease_spec,
            )

    # Tilde: ~1.2.3
    if s.startswith("~"):
        inner = s[1:].strip()
        v = parse_version(inner)
        # ~1.2.3 → >=1.2.3 <1.3.0
        return Range(
            spec=spec,
            _low=v,
            _high=Version(v.major, v.minor + 1, 0, (), ""),
            _prerelease_spec=prerelease_spec,
        )

    # Exact match: "1.2.3"
    v = parse_version(s)
    return Range(
        spec=spec,
        _exact=v,
        _prerelease_spec=prerelease_spec,
    )


# ── resolve ─────────────────────────────────────────────────────────────────
def resolve(spec: str, available: Iterable[str]) -> Optional[str]:
    """Return the highest matching ORIGINAL string from *available*, or None.

    Invalid version strings in *available* are silently skipped.
    The returned string is the exact original input string (not re-formatted).
    """
    rng = parse_range(spec)
    best: Optional[str] = None
    best_ver: Optional[Version] = None
    for raw in available:
        try:
            v = parse_version(raw)
        except ValueError:
            continue
        if not rng.matches(v):
            continue
        if best_ver is None or v > best_ver:
            best = raw
            best_ver = v
    return best

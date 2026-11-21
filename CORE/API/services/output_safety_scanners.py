"""compliance-fortress 3.1 (+ the regex-PII half of 3.2) — output-safety scanners.

MOD ships nodes that PRODUCE output (images, video, audio, text, JSON) which is
then downloaded, embedded in an app, or handed to an external client.
compliance-fortress 3 says: before output leaves the platform, scan it for
safety problems — and 3.2 specifically for PII a user (or a model) accidentally
baked into the output: an email, a US SSN, a credit card, a phone number.

This module is the REGISTRY only. It owns:

  * the `Finding` shape every scanner reports (scanner, category, severity,
    detail, span) — and the rule that a Finding NEVER carries the matched value
    itself, only a MASK of it, so a finding can be logged / audited / shown in
    the UI without the PII ever being copied into a log line;
  * registration: `register_scanner()` (idempotent by name; a duplicate name
    with a different object is a bug and raises ValueError), `scanners_for(mime)`
    (mime -> coarse kind -> the scanners that declared that kind), and
    `scan_output()` (run the applicable scanners; one failing scanner can never
    stop the others — it reports a `scanner_error` finding instead);
  * the built-in platform defaults: `RegexPIIScanner` (3.2 regex PII),
    `NSFWScanner` (3.2 CLIP-style, image/video) and `PresidioScanner` (3.2
    Presidio-style PII, text/json). The last two sit behind a swappable
    BACKEND interface: the real third-party backend (open_clip /
    presidio-analyzer) is used only when it is importable AND already vendored
    on disk — MOD never downloads a model at scan time — and otherwise a
    clearly-marked local heuristic backend runs. A scanner whose backend cannot
    run reports a `not_scanned` Finding, never a silent "clean".

NOT here (separate tasks): the DB table + audit trail (3.4), the worker/output
hook that actually calls scan_output on produced artefacts (3.5). This module
writes nothing to the DB and blocks nothing by itself — it only reports
findings; the caller decides what to do with severity="block".

FEATURE POSTURE: `compliance.output_safety_scan_enabled` defaults to False
(scanning is opt-in until the compliance vertical turns it on). A call site that
passes an explicit `enabled={...}` set opts in for that call regardless.
"""
from __future__ import annotations

import logging
import os
import re
import time
from utils import fortress_metrics
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Protocol, Tuple

from services import platform_settings as psr  # leaf module: no audit cycle

logger = logging.getLogger(__name__)

ENABLED_SETTING = "compliance.output_safety_scan_enabled"
MAX_BYTES_SETTING = "compliance.output_safety_scan_max_bytes"
ENABLED_DEFAULT = False
MAX_BYTES_DEFAULT = 5_242_880
MAX_BYTES_FLOOR = 1024
MAX_BYTES_CEIL = 104_857_600

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_BLOCK = "block"
SEVERITIES = frozenset({SEVERITY_INFO, SEVERITY_WARN, SEVERITY_BLOCK})

CATEGORY_SCANNER_ERROR = "scanner_error"
CATEGORY_TRUNCATED = "truncated"

KIND_TEXT = "text"
KIND_JSON = "json"
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_BINARY = "binary"
KINDS = frozenset({KIND_TEXT, KIND_JSON, KIND_IMAGE, KIND_VIDEO, KIND_AUDIO,
                   KIND_BINARY})

# application/* minors that are really text (so a text scanner still runs).
TEXT_APPLICATION_MINORS = frozenset({
    "x-sh", "x-shellscript", "x-python", "python", "javascript", "x-javascript",
    "xml", "xhtml+xml", "yaml", "x-yaml", "toml", "x-toml", "csv",
    "x-http-message", "pgp", "pgp-signature", "graphql", "graphql+json",
})
JSON_APPLICATION_MINORS = frozenset({"json", "jsonl", "geo+json", "ld+json",
                                     "hal+json", "problem+json", "scim+json"})


@dataclass(frozen=True)
class Finding:
    """One problem a scanner found. NEVER put the matched value in `detail`."""
    scanner: str
    category: str
    severity: str
    detail: str
    span: Optional[Tuple[int, int]] = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"Finding.severity must be one of info|warn|block, got {self.severity!r}"
            )


class Scanner(Protocol):
    """What MOD needs from any output-safety scanner.

    `name` is the registry key (unique), `kinds` is the set of coarse output
    kinds the scanner understands, and `scan(data, mime=...)` returns a list of
    Findings (possibly empty). A scanner may raise — `scan_output` turns that
    into a `scanner_error` Finding and keeps the others running.
    """
    name: str
    kinds: FrozenSet[str]

    def scan(self, data: bytes, *, mime: str) -> List[Finding]: ...


# ---------------------------------------------------------------------------
# mime -> coarse kind
# ---------------------------------------------------------------------------


def kind_for(mime: Optional[str]) -> str:
    """Coarse output kind for a MIME string ("text/plain; charset=utf-8",
    "application/json", "image/png") or for a bare kind name ("json").

    Unknown/absent MIME is `binary`: the conservative answer, so a text/JSON
    scanner never silently skips something whose type we do not know (a caller
    that means "plain text" should pass "text")."""
    if not mime:
        return KIND_BINARY
    head = mime.split(";", 1)[0].strip().lower()
    if not head:
        return KIND_BINARY
    if head in KINDS:  # a coarse kind passed straight through
        return head
    major, _, minor = head.partition("/")
    if major in KINDS:
        return major
    if major == "application":
        if minor in JSON_APPLICATION_MINORS or minor.endswith("+json"):
            return KIND_JSON
        if minor in TEXT_APPLICATION_MINORS:
            return KIND_TEXT
    return KIND_BINARY


def scanners_for(mime: Optional[str], kind: Optional[str] = None) -> List[Any]:
    """The registered scanners that declared the kind `mime` resolves to.

    Registration order is preserved so `scan_output` results are stable.
    `kind` overrides the mime lookup for callers that already know it."""
    k = kind if kind in KINDS else kind_for(mime)
    return [s for s in _REGISTRY.values() if k in s.kinds]


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

# name -> scanner object, in registration order (one process-wide dict: the
# API's uvicorn workers each import this module once, exactly like
# services/egress_denial_metrics.py keeps its own module state).
_REGISTRY: Dict[str, Any] = {}


def register_scanner(scanner: Any) -> bool:
    """Add a scanner to the platform registry. Idempotent by name.

    Re-registering the SAME object is a no-op (hot reload / double import).
    A different object claiming an existing name is a real bug — it would make
    which scanner runs depend on import order — so it raises ValueError.
    Returns True when the scanner is in the registry afterwards."""
    name = getattr(scanner, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError("scanner.name must be a non-empty str")
    kinds = getattr(scanner, "kinds", None)
    if not isinstance(kinds, (frozenset, set, tuple, list)) or not kinds:
        raise ValueError(f"scanner {name!r} must declare a non-empty kinds set")
    unknown = {k for k in kinds if k not in KINDS}
    if unknown:
        raise ValueError(f"scanner {name!r} declares unknown kinds {sorted(unknown)}")
    if not callable(getattr(scanner, "scan", None)):
        raise ValueError(f"scanner {name!r} must define scan(data, *, mime)")

    existing = _REGISTRY.get(name)
    if existing is scanner:
        return True
    if existing is not None:
        raise ValueError(
            f"scanner name {name!r} already registered by a different object "
            f"({getattr(existing, '__module__', '?')}); rename one"
        )
    _REGISTRY[name] = scanner
    return True


def unregister_scanner(name: str) -> bool:
    """Remove a scanner by name (tests / a tenant opting a default out)."""
    return _REGISTRY.pop(name, None) is not None


# ---------------------------------------------------------------------------
# platform settings (registry only — no env vars, no DB writes)
# ---------------------------------------------------------------------------


def _validate_max_bytes(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) \
            or not MAX_BYTES_FLOOR <= value <= MAX_BYTES_CEIL:
        raise ValueError(
            f"{MAX_BYTES_SETTING} must be an int in "
            f"{MAX_BYTES_FLOOR}..{MAX_BYTES_CEIL}"
        )


psr.register(
    ENABLED_SETTING,
    bool,
    ENABLED_DEFAULT,
    group="compliance",
    description="3.1 — run the output-safety scanner registry on produced "
                "output before it is served. True turns on every registered "
                "scanner whose kind matches the output's MIME type; False "
                "scans nothing (call sites that pass an explicit `enabled` "
                "name-set still scan). Findings are reported to the caller; "
                "this module never blocks or rewrites output itself.",
)
psr.register(
    MAX_BYTES_SETTING,
    int,
    MAX_BYTES_DEFAULT,
    validator=_validate_max_bytes,
    group="compliance",
    description="3.1 — most bytes of a single output artefact the scanners "
                "will look at (default 5 MiB). Longer artefacts are scanned "
                "up to the cap and additionally report a "
                f"`{CATEGORY_TRUNCATED}` finding so the gap is visible, never "
                f"silently clean. {MAX_BYTES_FLOOR}..{MAX_BYTES_CEIL}.",
)


def enabled(db: Optional[Any] = None) -> bool:
    """Feature on? A settings outage keeps the default posture (off)."""
    try:
        return bool(psr.get_setting(ENABLED_SETTING, db=db))
    except Exception as e:  # noqa: BLE001 — settings/DB outage
        logger.debug("output_safety_scanners: enabled read failed: %s", e)
        return ENABLED_DEFAULT


def _feature_on() -> bool:
    """`enabled()` without the settings read shadowed by a call-site param."""
    return enabled()


def max_bytes(db: Optional[Any] = None) -> int:
    """Byte cap for one artefact; an outage falls back to the default."""
    try:
        v = int(psr.get_setting(MAX_BYTES_SETTING, db=db))
    except Exception:  # noqa: BLE001 — settings/DB outage
        return MAX_BYTES_DEFAULT
    return v if MAX_BYTES_FLOOR <= v <= MAX_BYTES_CEIL else MAX_BYTES_DEFAULT


# ---------------------------------------------------------------------------
# built-in platform default: the regex PII scanner (3.2)
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# 13..19 digits, separators (space/dash/dot) allowed anywhere between digits.
CARD_RE = re.compile(r"(?<![\d.-])(?:\d[ .\-]?){12,18}\d(?![\d.-])")
# E.164-ish: optional +CC, then 7..15 digits in groups, separators allowed.
PHONE_RE = re.compile(
    r"(?<![\d.+-])"
    r"(?:\+\d{1,3}[ .\-]?)?"
    r"(?:\(?\d{2,4}\)?[ .\-]?){1,3}\d{2,4}(?![\d.+-])"
)
KEEP_LAST = 4  # digits of a match kept visible in the mask


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum (right-to-left doubling every second digit)."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mask_digits(text: str, keep_last: int = KEEP_LAST) -> str:
    """Structure-preserving mask: every digit -> '*', the last `keep_last`
    digits stay. The value itself never appears in a Finding."""
    digits = [c for c in text if c.isdigit()]
    visible_from = max(0, len(digits) - keep_last)
    seen: Dict[str, int] = {}
    out = []
    for c in text:
        if not c.isdigit():
            out.append(c)
            continue
        seen[c] = seen.get(c, 0) + 1
        out.append(c if seen[c] > visible_from else "*")
    return "".join(out)


def _mask_email(text: str) -> str:
    at = text.find("@")
    return "***" + text[at:] if at > 0 else "***"


class RegexPIIScanner:
    """Platform default scanner (3.2): regex PII sweep over text/JSON output.

    Categories: email, ssn, credit_card (Luhn-checked), phone. Severity is
    `warn` by default — the caller (3.4/3.5) decides whether a warning blocks
    delivery; a scanner that only reports can never be the enforcement point.
    """
    name = "regex_pii"
    kinds = frozenset({KIND_TEXT, KIND_JSON})

    def scan(self, data: bytes, *, mime: str) -> List[Finding]:
        text = data.decode("utf-8", errors="replace")
        findings: List[Finding] = []
        taken: List[Tuple[int, int]] = []

        def claim(start: int, end: int) -> bool:
            for s, e in taken:
                if start < e and s < end:
                    return False
            taken.append((start, end))
            return True

        for m in EMAIL_RE.finditer(text):
            if claim(m.start(), m.end()):
                findings.append(Finding(
                    scanner=self.name, category="email", severity=SEVERITY_WARN,
                    detail=f"email {_mask_email(m.group(0))}", span=m.span(),
                ))
        for m in SSN_RE.finditer(text):
            if claim(m.start(), m.end()):
                findings.append(Finding(
                    scanner=self.name, category="ssn", severity=SEVERITY_WARN,
                    detail=f"ssn {_mask_digits(m.group(0))}", span=m.span(),
                ))
        for m in CARD_RE.finditer(text):
            digits = "".join(c for c in m.group(0) if c.isdigit())
            if not (13 <= len(digits) <= 19) or not _luhn_ok(digits):
                continue  # a bad checksum is not a card — do not flag it
            if claim(m.start(), m.end()):
                findings.append(Finding(
                    scanner=self.name, category="credit_card",
                    severity=SEVERITY_WARN,
                    detail=f"credit_card {_mask_digits(m.group(0))} "
                           f"({len(digits)} digits, luhn ok)",
                    span=m.span(),
                ))
        for m in PHONE_RE.finditer(text):
            digits = "".join(c for c in m.group(0) if c.isdigit())
            if not (7 <= len(digits) <= 15):
                continue
            if claim(m.start(), m.end()):
                findings.append(Finding(
                    scanner=self.name, category="phone", severity=SEVERITY_WARN,
                    detail=f"phone {_mask_digits(m.group(0))}", span=m.span(),
                ))
        findings.sort(key=lambda f: (f.span or (0, 0), f.category))
        return findings


# ---------------------------------------------------------------------------
# built-in platform default: secret-leak scanner (3.2)
# ---------------------------------------------------------------------------

SECRET_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)
SECRET_AWS_KEY_ID_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
SECRET_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")
SECRET_SLACK_TOKEN_RE = re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")
SECRET_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
SECRET_PREFIX_CHARS = 4  # leading chars of a match kept visible in `detail`


class SecretLeakScanner:
    """Platform default scanner (3.2): regex sweep for leaked credentials/secrets.

    Categories: private_key, aws_access_key_id, github_token, slack_token
    (severity `block`) and jwt (severity `warn`). Reporting only — the `block`
    severity is enforced by 3.6 only when `fortress.output_block_enforced` is
    ON; this scanner never blocks anything itself. `detail` NEVER carries the
    matched secret, only its category, length and a few leading characters.
    """
    name = "secret_leak"
    kinds = frozenset({KIND_TEXT, KIND_JSON})

    def scan(self, data: bytes, *, mime: str) -> List[Finding]:
        text = data.decode("utf-8", errors="replace")
        findings: List[Finding] = []
        taken: List[Tuple[int, int]] = []

        def claim(start: int, end: int) -> bool:
            for s, e in taken:
                if start < e and s < end:
                    return False
            taken.append((start, end))
            return True

        def brief(match: str) -> str:
            head = match[:SECRET_PREFIX_CHARS]
            return f"{head}…" if len(match) > SECRET_PREFIX_CHARS else head

        for m in SECRET_PRIVATE_KEY_RE.finditer(text):
            if claim(m.start(), m.end()):
                findings.append(Finding(
                    scanner=self.name, category="private_key",
                    severity=SEVERITY_BLOCK,
                    detail="private key block", span=m.span(),
                ))
        for m in SECRET_AWS_KEY_ID_RE.finditer(text):
            if claim(m.start(), m.end()):
                findings.append(Finding(
                    scanner=self.name, category="aws_access_key_id",
                    severity=SEVERITY_BLOCK,
                    detail=f"aws_access_key_id (len={len(m.group(0))}, "
                           f"{brief(m.group(0))})", span=m.span(),
                ))
        for m in SECRET_GITHUB_TOKEN_RE.finditer(text):
            if claim(m.start(), m.end()):
                findings.append(Finding(
                    scanner=self.name, category="github_token",
                    severity=SEVERITY_BLOCK,
                    detail=f"github_token (len={len(m.group(0))}, "
                           f"{brief(m.group(0))})", span=m.span(),
                ))
        for m in SECRET_SLACK_TOKEN_RE.finditer(text):
            if claim(m.start(), m.end()):
                findings.append(Finding(
                    scanner=self.name, category="slack_token",
                    severity=SEVERITY_BLOCK,
                    detail=f"slack_token (len={len(m.group(0))}, "
                           f"{brief(m.group(0))})", span=m.span(),
                ))
        for m in SECRET_JWT_RE.finditer(text):
            if claim(m.start(), m.end()):
                findings.append(Finding(
                    scanner=self.name, category="jwt", severity=SEVERITY_WARN,
                    detail=f"jwt (len={len(m.group(0))}, "
                           f"{brief(m.group(0))})", span=m.span(),
                ))
        findings.sort(key=lambda f: (f.span or (0, 0), f.category))
        return findings


# ---------------------------------------------------------------------------
# swappable backends (3.2 continued: NSFW + Presidio-style PII)
# ---------------------------------------------------------------------------
# A scanner class never imports a heavy ML library at module import time and
# never downloads weights at scan time. Each class holds a BACKEND: an object
# with `prepare()` (lazy import + load, returns False when unavailable) and
# `detect(data, mime=...) -> list[Hit]`. If no backend can run, the scanner
# reports a `not_scanned` Finding — an unavailable scanner is loud, never a
# silent "clean".

CATEGORY_NOT_SCANNED = "not_scanned"
CATEGORY_SCAN_BUDGET_EXHAUSTED = "scan_budget_exhausted"

REAL_BACKENDS_SETTING = "compliance.output_safety_real_backends_enabled"
REAL_BACKENDS_DEFAULT = False
CLIP_BUNDLE_SETTING = "compliance.nsfw_clip_bundle_vendored"
CLIP_BUNDLE_DEFAULT = False
SKIN_PCT_SETTING = "compliance.nsfw_skin_tone_ratio_pct"
SKIN_PCT_DEFAULT = 35
SKIN_PCT_FLOOR = 5
SKIN_PCT_CEIL = 95
NSFW_CLIP_BUNDLE_DIR = "/app/data/nsfw_clip"
HEURISTIC_MARKER = "heuristic"
PRESDIO_LANGUAGES = ("en",)


@dataclass(frozen=True)
class Hit:
    """One backend hit, scanner-agnostic (the scanner adds its own name)."""
    category: str
    severity: str
    detail: str
    span: Optional[Tuple[int, int]] = None


def _validate_skin_pct(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) \
            or not SKIN_PCT_FLOOR <= value <= SKIN_PCT_CEIL:
        raise ValueError(
            f"{SKIN_PCT_SETTING} must be an int in {SKIN_PCT_FLOOR}..{SKIN_PCT_CEIL}"
        )


psr.register(
    REAL_BACKENDS_SETTING,
    bool,
    REAL_BACKENDS_DEFAULT,
    group="compliance",
    description="3.2 — attempt the REAL third-party backends (open_clip / "
                "openai-clip for NSFW, presidio-analyzer for PII) instead of "
                "the bundled local heuristics. Off by default: the real "
                "backends need vendored weights/packages, and MOD never "
                "downloads a model at scan time. When a real backend is "
                "requested but unavailable, the scanner reports "
                f"`{CATEGORY_NOT_SCANNED}`, never a clean result.",
)
psr.register(
    CLIP_BUNDLE_SETTING,
    bool,
    CLIP_BUNDLE_DEFAULT,
    group="compliance",
    description="3.2 — True only when the CLIP encoder weights are already "
                f"vendored on disk at {NSFW_CLIP_BUNDLE_DIR} (image + text "
                "tower). False keeps the NSFW scanner on its local heuristic; "
                "MOD never fetches weights at runtime.",
)
psr.register(
    SKIN_PCT_SETTING,
    int,
    SKIN_PCT_DEFAULT,
    validator=_validate_skin_pct,
    group="compliance",
    description="3.2 — percent of a downscaled image that must fall in the "
                "skin-tone band before the local NSFW heuristic warns "
                f"({SKIN_PCT_FLOOR}..{SKIN_PCT_CEIL}, default "
                f"{SKIN_PCT_DEFAULT}). Advisory only: the heuristic is a "
                "cheap pre-filter, the CLIP backend is the classifier.",
)


def real_backends_enabled() -> bool:
    try:
        return bool(psr.get_setting(REAL_BACKENDS_SETTING))
    except Exception:  # noqa: BLE001 — settings outage keeps the default
        return REAL_BACKENDS_DEFAULT


def clip_bundle_vendored() -> bool:
    try:
        return bool(psr.get_setting(CLIP_BUNDLE_SETTING))
    except Exception:  # noqa: BLE001 — settings outage keeps the default
        return CLIP_BUNDLE_DEFAULT


def skin_tone_pct(db: Optional[Any] = None) -> int:
    try:
        v = int(psr.get_setting(SKIN_PCT_SETTING, db=db))
    except Exception:  # noqa: BLE001 — settings outage
        return SKIN_PCT_DEFAULT
    return v if SKIN_PCT_FLOOR <= v <= SKIN_PCT_CEIL else SKIN_PCT_DEFAULT


def _mask_span(text: str, start: int, end: int) -> str:
    """Structure-preserving mask of an entity span: first char + last two stay,
    everything between becomes '*'. The value itself never appears."""
    n = end - start
    if n <= 0:
        return "***"
    if n <= 3:
        return "*" * n
    return text[start] + "*" * (n - 3) + text[end - 2:end]


IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
DATE_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\b")
PERSON_RE = re.compile(r"\b[A-Z][a-z]{1,20}(?:[ \-][A-Z][a-z]{1,20}){1,2}\b")
PERSON_STOP = frozenset({
    "the", "and", "mr", "Mrs", "Mr", "Dr", "Dr.", "Saint", "Mount", "Fort",
    "New", "Los", "Las", "San", "Studio", "The", "Chapter", "Section",
    "Image", "Node", "Workflow", "MOD", "Hello", "World",
})


class PresidioBackend:
    """Real presidio-analyzer backend (lazy import, no model download)."""
    name = "presidio"

    def __init__(self) -> None:
        self.ready = False
        self._analyzer = None

    def prepare(self) -> bool:
        if self.ready:
            return True
        try:
            from presidio_analyzer import Analyzer  # type: ignore
        except ImportError:
            self.ready = False
            return False
        try:
            self._analyzer = Analyzer()
        except Exception:  # noqa: BLE001 — a presidio that cannot init is off
            self.ready = False
            return False
        self.ready = True
        return True

    def detect(self, data: bytes, *, mime: str) -> List[Hit]:
        text = data.decode("utf-8", errors="replace")
        hits: List[Hit] = []
        for r in self._analyzer.analyze(text=text, languages=PRESDIO_LANGUAGES):
            hits.append(Hit(
                category=str(r.entity_type).lower().replace(" ", "_"),
                severity=SEVERITY_WARN,
                detail=f"presidio {str(r.entity_type).lower().replace(' ', '_')} "
                       f"{_mask_span(text, r.start, r.end)} (len={r.end - r.start})",
                span=(r.start, r.end),
            ))
        return hits


class HeuristicTextBackend:
    """Bundled local backend behind PresidioScanner: pure-python entity sweep
    (email / ssn / phone / card / ip / date / person-name). CLEARLY MARKED
    `heuristic` in every detail — it is a cheap pre-filter, not presidio."""
    name = "heuristic_text"

    def prepare(self) -> bool:
        return True

    def detect(self, data: bytes, *, mime: str) -> List[Hit]:
        text = data.decode("utf-8", errors="replace")
        hits: List[Hit] = []
        taken: List[Tuple[int, int]] = []

        def claim(start: int, end: int) -> bool:
            for s, e in taken:
                if start < e and s < end:
                    return False
            taken.append((start, end))
            return True

        for rx, cat in ((EMAIL_RE, "email"), (SSN_RE, "ssn"), (IP_RE, "ip"),
                        (DATE_RE, "date")):
            for m2 in rx.finditer(text):
                if claim(m2.start(), m2.end()):
                    hits.append(Hit(
                        category=cat, severity=SEVERITY_WARN,
                        detail=f"{HEURISTIC_MARKER} {cat} "
                               f"{_mask_span(text, m2.start(), m2.end())}",
                        span=m2.span(),
                    ))
        for m2 in PHONE_RE.finditer(text):
            digits = "".join(c for c in m2.group(0) if c.isdigit())
            if not (7 <= len(digits) <= 15):
                continue
            if claim(m2.start(), m2.end()):
                hits.append(Hit(
                    category="phone", severity=SEVERITY_WARN,
                    detail=f"{HEURISTIC_MARKER} phone {_mask_digits(m2.group(0))}",
                    span=m2.span(),
                ))
        for m2 in PERSON_RE.finditer(text):
            if m2.group(0).title() in {p.title() for p in PERSON_STOP}:
                continue
            if claim(m2.start(), m2.end()):
                hits.append(Hit(
                    category="person", severity=SEVERITY_INFO,
                    detail=f"{HEURISTIC_MARKER} person {_mask_span(text, m2.start(), m2.end())}",
                    span=m2.span(),
                ))
        return hits


class ClipBackend:
    """Real CLIP zero-shot NSFW classifier. Needs BOTH the library and a
    vendored weight bundle; MOD never downloads either at scan time."""
    name = "clip"

    def __init__(self) -> None:
        self.ready = False
        self._model = None
        self._preprocess = None
        self._tokens = None

    def prepare(self) -> bool:
        if self.ready:
            return True
        if not real_backends_enabled() or not clip_bundle_vendored():
            self.ready = False
            return False
        try:
            import open_clip  # type: ignore
            from PIL import Image  # type: ignore
            import numpy  # type: ignore
            import torch  # type: ignore
        except ImportError:
            self.ready = False
            return False
        try:
            names = [n for n in os.listdir(NSFW_CLIP_BUNDLE_DIR)
                     if n.endswith((".bin", ".safetensors", ".pt"))]
            if not names:
                self.ready = False
                return False
            self._model = open_clip.create_model_from_pretrained(
                "ViT-B-32", pretrained=NSFW_CLIP_BUNDLE_DIR)
            self._preprocess = open_clip.transform.Compose(
                [open_clip.transform.Normalize()])
            self._tokens = open_clip.tokenize([
                "a safe image", "an unsafe image", "nudity", "a normal image"])
        except Exception:  # noqa: BLE001 — a bundle we cannot load is off
            self.ready = False
            return False
        self.ready = True
        return True

    def detect(self, data: bytes, *, mime: str) -> List[Hit]:
        from PIL import Image
        import numpy  # noqa: F401
        import torch  # noqa: F401
        with BytesIO(data) as fh:
            img = Image.open(fh).convert("RGB")
        feats = self._model.encode_image(self._preprocess(img))
        feats = feats / feats.norm(dim=-1, keepdim=True)
        with torch.no_grad():
            txt = self._model.encode_text(self._tokens)
            txt = txt / txt.norm(dim=-1, keepdim=True)
            probs = (100.0 * feats @ txt.T).softmax(dim=-1)[0]
        unsafe = float(probs[1] + probs[2])
        if unsafe >= 0.5:
            return [Hit(category="nsfw", severity=SEVERITY_BLOCK,
                        detail=f"clip nsfw probability {unsafe:.2f}", span=None)]
        return []


class HeuristicImageBackend:
    """Bundled local backend behind NSFWScanner: PIL + numpy skin-tone ratio
    on a 64x64 downscale. CLEARLY MARKED heuristic — an advisory pre-filter
    that never claims to be a CLIP classifier."""
    name = "heuristic_image"

    def __init__(self) -> None:
        self.ready = False

    def prepare(self) -> bool:
        if self.ready:
            return True
        try:
            from PIL import Image  # type: ignore
            import numpy  # type: ignore
        except ImportError:
            self.ready = False
            return False
        self.ready = True
        return True

    def detect(self, data: bytes, *, mime: str) -> List[Hit]:
        from PIL import Image
        import numpy
        with BytesIO(data) as fh:
            img = Image.open(fh)
            img.load()
        a = numpy.asarray(img.convert("RGB").resize((64, 64)), dtype=numpy.int16)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        hi = numpy.maximum(numpy.maximum(r, g), b)
        lo = numpy.minimum(numpy.minimum(r, g), b)
        band = (r > 95) & (g > 40) & (b > 20) & (r > g) & (r >= b) \
             & ((hi - lo) > 15) & (numpy.abs(r - g) > 15)
        ratio = float(band.mean())
        threshold = skin_tone_pct() / 100.0
        if ratio < threshold:
            return []
        return [Hit(
            category="skin_tone_ratio", severity=SEVERITY_WARN,
            detail=f"{HEURISTIC_MARKER} skin-tone pixels {ratio * 100:.0f}% of "
                   f"a 64x64 downscale (threshold {skin_tone_pct()}%) — advisory, "
                   "not a CLIP verdict",
            span=None,
        )]


def _pick_backend(image: bool) -> Optional[Any]:
    """The backend a scanner should try, or None when nothing can run.

    Real backend first when the platform asked for it (and it is importable +
    vendored, checked in `prepare()`); otherwise the bundled heuristic."""
    if image:
        if real_backends_enabled():
            return ClipBackend()
        return HeuristicImageBackend()
    if real_backends_enabled():
        return PresidioBackend()
    return HeuristicTextBackend()


def _not_scanned(scanner_name: str, why: str) -> Finding:
    """An unavailable scanner is loud: it reports a gap, never a clean result."""
    return Finding(
        scanner=scanner_name, category=CATEGORY_NOT_SCANNED, severity=SEVERITY_WARN,
        detail=f"backend unavailable ({why}); this output was NOT scanned by {scanner_name}",
    )


class NSFWScanner:
    """Platform default 3.2 — NSFW/violence scan of image + video output.

    Backend is swappable: `ClipBackend` (real open_clip zero-shot classifier,
    only when the library imports AND the weights are vendored at
    NSFW_CLIP_BUNDLE_DIR — never downloaded here) or `HeuristicImageBackend`
    (PIL + numpy skin-tone ratio, every detail marked `heuristic`). When
    neither can run — or when the artefact is a video container the offline
    backend cannot frame-extract — the scanner reports `not_scanned`."""
    name = "nsfw_clip"
    kinds = frozenset({KIND_IMAGE, KIND_VIDEO})

    def __init__(self) -> None:
        self._backend: Optional[Any] = None

    def scan(self, data: bytes, *, mime: str) -> List[Finding]:
        if not data:
            return []
        backend = self._backend or _pick_backend(image=True)
        if backend is None:
            return [_not_scanned(self.name, "no backend configured")]
        if not backend.prepare():
            return [_not_scanned(self.name, f"{backend.name} backend not loadable/vendored")]
        if kind_for(mime) == KIND_VIDEO:
            return [_not_scanned(
                self.name,
                "video needs frame extraction + the CLIP backend; the offline "
                "heuristic only inspects single images",
            )]
        try:
            hits = backend.detect(data, mime=mime)
        except Exception:  # noqa: BLE001 — undecodable/corrupt media is a gap
            return [_not_scanned(self.name, f"{backend.name} could not decode the artefact")]
        self._backend = backend
        return [Finding(scanner=self.name, category=h.category, severity=h.severity,
                        detail=h.detail, span=h.span) for h in hits]


class PresidioScanner:
    """Platform default 3.2 — Presidio-style PII recognition on text/JSON.

    `PresidioBackend` (presidio-analyzer, lazy import, no model download) or
    the bundled `HeuristicTextBackend` (pure-python entity sweep, details marked
    `heuristic`). Complements — does not replace — `RegexPIIScanner`: presidio
    adds person/date/ip/entity classes regexes do not, and the two overlap is
    deduped by span in the caller if a tenant wants one report per span."""
    name = "presidio_pii"
    kinds = frozenset({KIND_TEXT, KIND_JSON})

    def __init__(self) -> None:
        self._backend: Optional[Any] = None

    def scan(self, data: bytes, *, mime: str) -> List[Finding]:
        if not data:
            return []
        backend = self._backend or _pick_backend(image=False)
        if backend is None:
            return [_not_scanned(self.name, "no backend configured")]
        if not backend.prepare():
            return [_not_scanned(self.name, f"{backend.name} backend not importable")]
        try:
            hits = backend.detect(data, mime=mime)
        except Exception:  # noqa: BLE001 — a broken backend reports a gap
            return [_not_scanned(self.name, f"{backend.name} raised")]
        self._backend = backend
        return [Finding(scanner=self.name, category=h.category, severity=h.severity,
                        detail=h.detail, span=h.span) for h in hits]


# ---------------------------------------------------------------------------
# scan_output — the one entry point a caller uses
# ---------------------------------------------------------------------------


def scan_output(data: Optional[bytes], mime: Optional[str], *,
                enabled: Optional[set] = None,
                max_bytes_override: Optional[int] = None) -> List[Finding]:
    """Timing wrapper: observe duration, then delegate (see _scan_impl)."""
    _t0 = time.perf_counter()
    try:
        _res = _scan_impl(data, mime, enabled=enabled,
                          max_bytes_override=max_bytes_override)
    except Exception:  # noqa: BLE001 — observed, then re-raised unchanged
        fortress_metrics.observe_scan(time.perf_counter() - _t0, "error")
        fortress_metrics.inc_scan_result("other", "error")
        raise
    fortress_metrics.observe_scan(
        time.perf_counter() - _t0, "findings" if _res else "clean")
    return _res


def _scan_impl(data: Optional[bytes], mime: Optional[str], *,
               enabled: Optional[set] = None,
               max_bytes_override: Optional[int] = None) -> List[Finding]:
    """Scan one output artefact; return its findings (empty = clean).

    `enabled` (a set of scanner names) restricts the run to those scanners and
    opts in regardless of the platform toggle; `None` means "use the platform
    posture": nothing runs when `compliance.output_safety_scan_enabled` is off.
    The artefact is truncated to `compliance.output_safety_scan_max_bytes`
    (a longer artefact reports a `truncated` finding so the unscanned tail is
    visible). A scanner that raises yields one `scanner_error` finding — with
    the exception TYPE only, never the exception text, which can echo the
    scanned data — and the remaining scanners still run.
    """
    if data is None:
        data = b""
    cap = max_bytes_override if max_bytes_override else max_bytes()
    findings: List[Finding] = []
    if len(data) > cap:
        findings.append(Finding(
            scanner="output_safety_scanners", category=CATEGORY_TRUNCATED,
            severity=SEVERITY_WARN,
            detail=f"artefact is {len(data)} bytes; only the first {cap} were scanned",
            span=(cap, len(data)),
        ))
        data = data[:cap]

    if enabled is not None:
        cands = [s for name, s in _REGISTRY.items() if name in enabled]
    elif not _feature_on():
        return findings
    else:
        cands = scanners_for(mime)

    for scanner in cands:
        try:
            _hits = scanner.scan(data, mime=mime or "")
            findings.extend(_hits)
            fortress_metrics.inc_scan_result(
                scanner.name, "findings" if _hits else "clean")
        except Exception:  # noqa: BLE001 — one bad scanner must not stop the rest
            logger.debug("output_safety_scanners: %s failed for mime %s",
                         scanner.name, mime)
            fortress_metrics.inc_scan_result(scanner.name, "error")
            findings.append(Finding(
                scanner=scanner.name, category=CATEGORY_SCANNER_ERROR,
                severity=SEVERITY_WARN,
                detail="scanner raised and was skipped (exception text omitted: "
                       "it can echo the scanned output)",
            ))
    return findings


# ---------------------------------------------------------------------------
# platform defaults: registered on import (idempotent, like psr.register)
# ---------------------------------------------------------------------------

PLATFORM_DEFAULT_SCANNERS: Tuple[str, ...] = ()


def _install_platform_defaults() -> None:
    global PLATFORM_DEFAULT_SCANNERS
    names = []
    for scanner in (RegexPIIScanner(), SecretLeakScanner(),
                    NSFWScanner(), PresidioScanner()):
        try:
            if register_scanner(scanner):
                names.append(scanner.name)
        except ValueError as e:  # noqa: BLE001 — a name clash from another module
            logger.warning("output_safety_scanners: default %s not registered: %s",
                           scanner.name, e)
    PLATFORM_DEFAULT_SCANNERS = tuple(names)


_install_platform_defaults()


def summary() -> Dict[str, Any]:
    """Registry snapshot for the admin/settings UI (names + kinds only)."""
    return {
        "registered": {name: sorted(s.kinds) for name, s in _REGISTRY.items()},
        "platform_defaults": list(PLATFORM_DEFAULT_SCANNERS),
        "enabled_default": ENABLED_DEFAULT,
        "max_bytes_default": MAX_BYTES_DEFAULT,
    }

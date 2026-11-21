"""GY.V4a — PII/PHI redaction profiles for logs and LLM-bound text.

`utils.secret_redaction` covers SECRETS (passwords, tokens, JWTs). This module
covers PERSONAL DATA: the identifiers a HIPAA minimum-necessary / GLBA / ABA
1.6(c) posture expects to be masked before text leaves the process — into a log
line, or into a prompt sent to an LLM provider.

Profiles (platform setting ``privacy.redaction_profile``, default ``off`` so
today's behaviour is unchanged):

    off      — no PII redaction at all (secret-only, as shipped)
    standard — email, phone, SSN, payment card, IBAN
    hipaa    — standard + labelled MRN + labelled DOB (PHI identifiers)
    strict   — hipaa + IPv4 (network identifiers treated as identifying)

Every match is replaced with a typed token, e.g. ``[REDACTED:EMAIL]``, so an
operator can still see WHAT was masked without seeing the value.

Performance: all patterns are precompiled at import and are linear (no nested
quantifiers, so no catastrophic backtracking). The profile is read from the
platform-settings registry through a short-TTL in-process cache — redaction
never hits the DB per log line. Any failure in the setting read degrades to
``off`` (secret-only behaviour) rather than raising to the caller.

Luhn and IBAN matches are CHECKED, not just shape-matched, so arbitrary digit
runs (order numbers, run ids, timestamps) do not redact.
"""
from __future__ import annotations

import re
import time
from typing import Dict, Optional

PROFILES = ("off", "standard", "hipaa", "strict")

# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

# Order matters: cards are checked before phones so a Luhn-valid card run is
# tokenised as CARD rather than half-matched as a NANP number.
DETECTORS = {
    "CARD": re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    "EMAIL": re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "PHONE": re.compile(
        r"(?<!\d)(?:\+?1[-. ]?)?\(?[2-9]\d{2}\)?[-. ]?[2-9]\d{2}[-. ]?\d{4}(?!\d)"
    ),
    "E164": re.compile(r"\+[1-9]\d{6,14}\b"),
    "SSN": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    "MRN": re.compile(r"\bMRN\b\s*[#:=]?\s*\d{4,12}", re.IGNORECASE),
    "DOB": re.compile(
        r"\bDOB\b\s*[:=]\s*(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})",
        re.IGNORECASE),
    "IPV4": re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
}

# Detector names active per profile. "off" is intentionally empty.
PROFILE_DETS: Dict[str, frozenset] = {
    "off": frozenset(),
    "standard": frozenset({"EMAIL", "PHONE", "E164", "SSN", "CARD", "IBAN"}),
    "hipaa": frozenset({"EMAIL", "PHONE", "E164", "SSN", "CARD", "IBAN",
                         "MRN", "DOB"}),
    "strict": frozenset({"EMAIL", "PHONE", "E164", "SSN", "CARD", "IBAN",
                         "MRN", "DOB", "IPV4"}),
}

# GY.C0 needs-map helper: declared compliance environment -> the profile this
# task recommends for it. Advisory only — nothing here writes the setting.
ENVIRONMENT_PROFILE_MAP: Dict[str, str] = {
    "hipaa": "hipaa",
    "ferpa_coppa": "hipaa",
    "pci_dss": "strict",
    "cjis": "strict",
    "legal_privilege": "strict",
    "privacy": "standard",
    "iso27001": "standard",
    "soc2": "standard",
    "tpn": "standard",
    "cmmc": "standard",
    "fedramp_moderate": "standard",
    "fedramp_high_il5": "strict",
    "iec62443_nerc": "strict",
    "itar_ear": "standard",
    "eu_ai_act": "standard",
    "none": "off",
}


def recommended_profile_for_environment(env: str) -> str:
    """Profile this task recommends for a declared environment (advisory)."""
    return ENVIRONMENT_PROFILE_MAP.get(env, "off")


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 12 <= len(digits) <= 19:
        return False
    total = 0
    alternate = False
    for d in reversed(digits):
        if alternate:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alternate = not alternate
    return total % 10 == 0


def _iban_ok(candidate: str) -> bool:
    s = candidate.replace(" ", "").upper()
    if len(s) < 15 or len(s) > 34:
        return False
    rearranged = s[4:] + s[:4]
    expanded = "".join(str(int(c)) if c.isdigit() else str(ord(c) - 55)
                       for c in rearranged)
    try:
        return int(expanded) % 97 == 1
    except ValueError:
        return False


# Detectors whose match must be validated before it is trusted.
_VALIDATORS = {"CARD": _luhn_ok, "IBAN": _iban_ok}


def token(name: str) -> str:
    return f"[REDACTED:{name}]"


def redact(text: str, profile: Optional[str] = None) -> str:
    """Redact PII from text under the given profile (default: the live setting).

    Returns ``text`` unchanged when the profile is ``off`` or empty, and on any
    internal error — redaction must never fail the caller.
    """
    if not text:
        return text
    active = profile if profile is not None else current_profile()
    dets = PROFILE_DETS.get(active)
    if not dets:
        return text
    result = text
    try:
        for name in sorted(dets):
            pattern = DETECTORS[name]
            validator = _VALIDATORS.get(name)
            if validator is None:
                result = pattern.sub(token(name), result)
            else:
                result = pattern.sub(
                    lambda m, v=validator, t=token(name):
                        t if v(m.group(0)) else m.group(0),
                    result)
    except Exception:  # noqa: BLE001 — redaction must never fail the caller
        return text
    return result


# ---------------------------------------------------------------------------
# Profile resolution (short-TTL cache, never per log line)
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 30.0
_cached_profile: Optional[str] = None
_cache_expires_at: float = 0.0


def current_profile() -> str:
    """Active profile, cached for ``_CACHE_TTL_SECONDS``.

    A setting-read failure degrades to ``off`` (secret-only behaviour) rather
    than raising — a broken settings read must not take down logging.
    """
    global _cached_profile, _cache_expires_at
    now = time.monotonic()
    if _cached_profile is not None and now < _cache_expires_at:
        return _cached_profile
    try:
        from services import platform_settings as psr
        raw = psr.get_setting("privacy.redaction_profile")
        value = str(raw or "off").strip().lower()
        if value not in PROFILES:
            value = "off"
    except Exception:  # noqa: BLE001 — fall back to secret-only behaviour
        value = "off"
    _cached_profile = value
    _cache_expires_at = now + _CACHE_TTL_SECONDS
    return value


def profile_active() -> bool:
    return current_profile() != "off"


def clear_cache() -> None:
    """Drop the cached profile (tests, and settings-change invalidation)."""
    global _cached_profile, _cache_expires_at
    _cached_profile = None
    _cache_expires_at = 0.0


# ---------------------------------------------------------------------------
# Log-line masking (GSEC R-086 — PII in logs)
# ---------------------------------------------------------------------------
# `redact()` above is profile-gated: with `privacy.redaction_profile` off it is
# a no-op, which is why plaintext emails still reach the log today. Call sites
# that interpolate an email into a log line must not depend on that setting,
# so `mask_email` is unconditional: it keeps just enough of the value to stay
# greppable ("who did this action") and drops the rest. It is a lossy display
# form, NOT a token — the record of truth (audit_events, the users row, the
# Keycloak admin) still holds the full email; only the log line changes.
#
# `j***@x.com` keeps the first local-part character and the domain. The domain
# of a personal email is itself identifying, so callers who need less should
# use the profile-gated `redact()` (which becomes `[REDACTED:EMAIL]`) instead.
def mask_email(value: Optional[str]) -> str:
    """Mask an email for a log line: ``jane.doe@x.com`` -> ``j***@x.com``.

    Non-email strings are returned masked to their first character (a bare
    name/identifier must not be logged in full either), and empty/None become
    ``""`` so a missing value never prints as ``None`` in a PII slot.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if "@" not in text:
        return text[0] + "***" if len(text) > 1 else text
    local, _, domain = text.partition("@")
    local = local.strip()
    if not local:
        return "***@" + domain.strip()
    return local[0] + "***@" + domain.strip()

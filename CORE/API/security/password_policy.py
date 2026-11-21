"""Phase 1 — secure password policy (NIST 800-63B + CMMC 2.0 + ISO 27001).

Design: pick the strictest superset of all three standards as a single
policy and enforce it at every entry point that touches a password.
The realm policy in Keycloak is the floor; this module is the API
ceiling. Both must agree on accept/reject decisions.

Rules (enforced):
  - Min length 15, max length 128 (CMMC privileged baseline 14, NIST 8;
    15 clears both privileged and standard tiers without two policies).
  - Allow ALL printable ASCII + Unicode + spaces. NIST removed
    composition rules (no "must have uppercase + digit + symbol").
  - HIBP k-anonymity breach check (api.pwnedpasswords.com/range/<5>).
    Reject any breach hit. Local 10k common-password blocklist serves
    as offline fallback when HIBP is unreachable.
  - Reject if password contains the username, email local-part, or
    tenant slug (case-insensitive substring).
  - zxcvbn score must be ≥ 3 / 4.
  - No mandatory rotation. Force reset only on compromise / policy
    rollout (handled by the migration that bulk-adds Keycloak's
    UPDATE_PASSWORD required action).

This module does NOT touch Keycloak. Callers invoke validate_password()
BEFORE any Keycloak admin call that sets a password (Keycloak's own
realm policy is the second gate).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)


MIN_LENGTH = 15
MAX_LENGTH = 128
MIN_ZXCVBN_SCORE = 3
HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix5}"
HIBP_TIMEOUT_SECONDS = 3.0

# Path to the bundled 10k common-password blocklist (SecLists top-10k).
_BLOCKLIST_PATH = Path(__file__).parent / "passwords" / "common-blocklist.txt"


class PasswordPolicyError(ValueError):
    """Raised when a password violates the policy. The `reasons` list
    is human-readable and may be shown to the user verbatim."""

    def __init__(self, reasons: List[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass
class PasswordStrength:
    """Result of a non-blocking strength probe (live UI feedback)."""
    score: int                 # 0..4 zxcvbn
    length_ok: bool
    crack_time_seconds: float  # online_throttling_100_per_hour estimate
    suggestions: List[str]
    warning: str


# ---------------------------------------------------------------------
# Blocklist loader (cached at first use).
# ---------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_blocklist() -> frozenset[str]:
    """Read the bundled common-password blocklist into a frozenset for
    O(1) membership checks. ~10k entries, ~73 KB on disk."""
    if not _BLOCKLIST_PATH.exists():
        logger.warning(
            "password_policy: blocklist file missing at %s — "
            "common-password rejection disabled", _BLOCKLIST_PATH,
        )
        return frozenset()
    try:
        with open(_BLOCKLIST_PATH, "r", encoding="utf-8", errors="replace") as f:
            return frozenset(
                line.strip().lower() for line in f if line.strip()
            )
    except Exception:
        logger.exception("password_policy: blocklist load failed")
        return frozenset()


# ---------------------------------------------------------------------
# HIBP k-anonymity check.
# ---------------------------------------------------------------------

def _hibp_breach_count(password: str, *, timeout: float = HIBP_TIMEOUT_SECONDS) -> Optional[int]:
    """Look up `password` against HIBP's k-anonymity range API. Returns
    the breach count when found, 0 when not in any breach corpus, or
    None when the API is unreachable (caller falls back to local
    blocklist)."""
    # GSEC FX.19 R-042 / GY.C8 — sealed mode: zero outbound calls, so the
    # HIBP range lookup (api.pwnedpasswords.com) is skipped; the caller
    # falls back to the local blocklist policy.
    from utils import egress_policy
    if egress_policy.sealed_mode():
        return None
    sha1_full = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = sha1_full[:5], sha1_full[5:]
    url = HIBP_RANGE_URL.format(prefix5=prefix)
    try:
        resp = httpx.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "MOD-Core-PasswordPolicy/1.0"},
        )
        if resp.status_code != 200:
            return None
    except httpx.HTTPError:
        return None
    for line in resp.text.splitlines():
        # Each line is "<35-char-suffix>:<count>".
        if ":" not in line:
            continue
        suffix_part, count_part = line.split(":", 1)
        if suffix_part.strip().upper() == suffix:
            try:
                return int(count_part.strip().split()[0])
            except (ValueError, IndexError):
                return None
    return 0


# ---------------------------------------------------------------------
# Identity-similarity check.
# ---------------------------------------------------------------------

def _password_contains_identity(
    password: str,
    *,
    username: Optional[str],
    email: Optional[str],
    tenant_slug: Optional[str],
) -> Optional[str]:
    """Return the offending field name when `password` (case-insensitive)
    contains the username, email local-part, or tenant slug; else None.
    The minimum length we'll match on is 3 chars to avoid false positives
    on short user identifiers."""
    pw_lower = password.lower()
    candidates: list[tuple[str, str]] = []
    if username and len(username) >= 3:
        candidates.append(("username", username.lower()))
    if email and "@" in email:
        local = email.split("@", 1)[0].strip()
        if len(local) >= 3:
            candidates.append(("email", local.lower()))
    if tenant_slug and len(tenant_slug) >= 3:
        candidates.append(("tenant", tenant_slug.lower()))
    for label, needle in candidates:
        if needle and needle in pw_lower:
            return label
    return None


# ---------------------------------------------------------------------
# zxcvbn strength score.
# ---------------------------------------------------------------------

def _zxcvbn_score(
    password: str,
    *,
    user_inputs: Optional[List[str]] = None,
) -> tuple[int, float, List[str], str]:
    """Return (score, online-throttled crack time in seconds,
    suggestions, warning). Score is 0..4."""
    try:
        from zxcvbn import zxcvbn  # type: ignore
    except ImportError:
        # zxcvbn not installed → don't reject on score, but log loudly.
        logger.error("password_policy: zxcvbn not installed; skipping score check")
        return 4, 1.0, [], ""
    inputs = [s for s in (user_inputs or []) if s]
    result = zxcvbn(password, user_inputs=inputs)
    score = int(result.get("score", 0))
    crack_seconds = float(
        result.get("crack_times_seconds", {}).get(
            "online_throttling_100_per_hour", 0.0,
        )
    )
    feedback = result.get("feedback", {}) or {}
    return score, crack_seconds, list(feedback.get("suggestions") or []), str(feedback.get("warning") or "")


# ---------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------

def validate_password(
    password: str,
    *,
    username: Optional[str] = None,
    email: Optional[str] = None,
    tenant_slug: Optional[str] = None,
    check_breach: bool = True,
) -> None:
    """Raise PasswordPolicyError(reasons) if the password fails any rule.
    Returns None on success.

    `check_breach` can be set to False in test environments where the
    HIBP API is unreachable; the local blocklist still applies.
    """
    if not isinstance(password, str):
        raise PasswordPolicyError(["Password must be a string"])

    reasons: List[str] = []

    if len(password) < MIN_LENGTH:
        reasons.append(
            f"Password must be at least {MIN_LENGTH} characters long "
            f"(was {len(password)})."
        )
    if len(password) > MAX_LENGTH:
        reasons.append(
            f"Password must be at most {MAX_LENGTH} characters long."
        )

    # Reject obvious null bytes / control chars (keep it printable +
    # whitespace).
    if any(ord(c) < 0x20 and c not in ("\t",) for c in password):
        reasons.append("Password contains unsupported control characters.")

    # Identity-similarity.
    identity_hit = _password_contains_identity(
        password, username=username, email=email, tenant_slug=tenant_slug,
    )
    if identity_hit:
        reasons.append(
            f"Password must not contain your {identity_hit}."
        )

    # Local common-password blocklist (always cheap; fast-fail before
    # HIBP).
    blocklist = _load_blocklist()
    if password.lower() in blocklist:
        reasons.append(
            "Password is on the common-password blocklist. Pick something "
            "less guessable."
        )

    # zxcvbn score (informs the user about low entropy).
    user_inputs: List[str] = []
    if username:
        user_inputs.append(username)
    if email:
        user_inputs.append(email)
        if "@" in email:
            user_inputs.append(email.split("@", 1)[0])
    if tenant_slug:
        user_inputs.append(tenant_slug)
    score, _, suggestions, warning = _zxcvbn_score(password, user_inputs=user_inputs)
    if score < MIN_ZXCVBN_SCORE:
        msg = "Password is too weak."
        if warning:
            msg += f" {warning}"
        if suggestions:
            msg += " " + " ".join(suggestions)
        reasons.append(msg)

    # HIBP — last because it's the slowest gate.
    if check_breach and not reasons:
        # Skip HIBP if other rules already failed; the user has work
        # to do anyway and we don't want to leak partial passwords on
        # the wire.
        breach_count = _hibp_breach_count(password)
        if breach_count is not None and breach_count > 0:
            reasons.append(
                f"This password has appeared in {breach_count:,} known "
                "data breaches. Pick a unique password."
            )

    if reasons:
        raise PasswordPolicyError(reasons)


def password_policy_summary() -> dict:
    """Returns the rules in a shape suitable for the frontend hint UI."""
    return {
        "min_length": MIN_LENGTH,
        "max_length": MAX_LENGTH,
        "min_zxcvbn_score": MIN_ZXCVBN_SCORE,
        "rules": [
            f"Must be at least {MIN_LENGTH} characters long.",
            f"Must be at most {MAX_LENGTH} characters long.",
            "Must NOT contain your username, email, or tenant name.",
            "Must NOT be a common / breached password "
            "(checked against HaveIBeenPwned + a 10,000-entry blocklist).",
            "Must score at least 3/4 on the zxcvbn strength estimator "
            "(no specific character classes required — long passphrases "
            "outperform short complex ones).",
        ],
        "rotation": "No periodic rotation required. Reset only on suspected compromise.",
        "compliance": ["NIST SP 800-63B", "CMMC 2.0 IA.L2-3.5.7", "ISO 27001 Annex A.5.17"],
    }


def password_strength(
    password: str,
    *,
    username: Optional[str] = None,
    email: Optional[str] = None,
    tenant_slug: Optional[str] = None,
) -> PasswordStrength:
    """Non-blocking strength probe for live UI feedback. Does NOT hit
    HIBP and does NOT raise on failure — it just returns the score so
    the client can render a meter."""
    user_inputs: List[str] = []
    if username:
        user_inputs.append(username)
    if email:
        user_inputs.append(email)
        if "@" in email:
            user_inputs.append(email.split("@", 1)[0])
    if tenant_slug:
        user_inputs.append(tenant_slug)
    score, crack_seconds, suggestions, warning = _zxcvbn_score(
        password, user_inputs=user_inputs,
    )
    return PasswordStrength(
        score=score,
        length_ok=MIN_LENGTH <= len(password) <= MAX_LENGTH,
        crack_time_seconds=crack_seconds,
        suggestions=suggestions,
        warning=warning,
    )


# ---------------------------------------------------------------------
# Keycloak realm policy string (mirrors the API rules).
# ---------------------------------------------------------------------

# Keycloak realm-level passwordPolicy is a single string. This constant
# mirrors the DEFAULT (non-gov) realm policy built by CORE/startup/keycloak-
# init.sh EXACTLY (FX.55 follow-up): length(12) + passwordHistory(5), plus
# the passwordBlacklist(file=...) clause — the repo ships
# CORE/startup/keycloak/password-blacklists/mod-breached-passwords.txt
# bind-mounted into both keycloak containers at
# /opt/keycloak/data/password-blacklists/, so on a standard install the
# file is present and the clause is part of the default string. The init
# script omits the clause only when the operator removed the file. The
# gov profile (fedramp_high_il5) swaps length(12) for length(15) in the
# script; the API gate keeps its own stricter MIN_LENGTH=15 floor and the
# zxcvbn/HIBP checks — realm-level notUsername/notEmail/hash clauses are
# NOT part of the init script's policy, so they are not in this mirror
# either (the realm converges to ONE string, whichever writer ran last).
KEYCLOAK_PASSWORD_POLICY = (
    "length(12) and passwordHistory(5)"
    " and passwordBlacklist"
    "(file=/opt/keycloak/data/password-blacklists/mod-breached-passwords.txt)"
)
# WARNING: do NOT add `forceExpiredPasswordChange(N)` to this policy
# unless you actually want passwords to expire every N days. The plan
# spec called for "no expiration" → the correct way is to OMIT the rule
# entirely. `forceExpiredPasswordChange(0)` does NOT mean "never expire"
# — it means "expire on every login," which results in the
# password-change prompt firing every single time the user authenticates.
# Filed in the bulk-rotation context where everyone gets an
# UPDATE_PASSWORD required-action via keycloak-init.sh; that one-shot
# rotation is the right way to force-rotate, not a 0-day expiration.


__all__ = [
    "MIN_LENGTH",
    "MAX_LENGTH",
    "MIN_ZXCVBN_SCORE",
    "KEYCLOAK_PASSWORD_POLICY",
    "PasswordPolicyError",
    "PasswordStrength",
    "validate_password",
    "password_policy_summary",
    "password_strength",
]

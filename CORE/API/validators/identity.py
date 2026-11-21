"""LDF-3 §10 — Server-side identity validators.

The single source of truth for the username / email / company-name
rules. Wired into `users.create`, `users.update`, `auth.signup`,
`tenants.create`, `tenants.update`. The first-run flow (§9) routes
its admin-email + admin-username fields through these as well.

Each validator raises a typed *ValidationError on rejection so callers
can surface field-level error messages.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Final, FrozenSet, Optional


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------

class IdentityValidationError(ValueError):
    """Base — every validator raises a subclass."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class UsernameValidationError(IdentityValidationError):
    pass


class EmailValidationError(IdentityValidationError):
    pass


class CompanyNameValidationError(IdentityValidationError):
    pass


# ---------------------------------------------------------------------
# Shared rules
# ---------------------------------------------------------------------

# SQL-injection-shape chars rejected across every validator. NOT a full
# anti-injection layer (parameterised queries are) — this is defence-
# in-depth so an injection probe shows up as a 422 audit event before
# anything reaches the DB.
SQL_INJECTION_SHAPE_CHARS: Final[FrozenSet[str]] = frozenset({
    "'", '"', ";", "\\", "\x00",
})

# Multi-char shapes (denied by substring scan).
SQL_INJECTION_SHAPES: Final[tuple[str, ...]] = ("--", "/*", "*/")


def _has_control_chars(s: str) -> bool:
    """True if any codepoint is < 0x20 or == 0x7F (DEL)."""
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in s)


def _has_injection_shape(s: str) -> bool:
    if any(c in SQL_INJECTION_SHAPE_CHARS for c in s):
        return True
    return any(shape in s for shape in SQL_INJECTION_SHAPES)


# ---------------------------------------------------------------------
# Username
# ---------------------------------------------------------------------

USERNAME_DENYLIST: Final[FrozenSet[str]] = frozenset({
    "root", "admin", "administrator", "system", "operator",
    "superuser", "support", "null", "undefined", "anonymous",
    "guest", "postgres", "keycloak", "mod", "modcore",
})

USERNAME_LENGTH_MIN: Final[int] = 3
USERNAME_LENGTH_MAX: Final[int] = 32
USERNAME_CHARSET_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_username(value: str | None) -> str:
    """Normalise + validate a username.

    Returns the (already-stripped) username on success. Raises
    UsernameValidationError on any rule failure.
    """
    if value is None:
        raise UsernameValidationError("username_empty", "Username is required")
    if not isinstance(value, str):
        raise UsernameValidationError(
            "username_type", "Username must be a string"
        )
    if value != value.strip():
        raise UsernameValidationError(
            "username_whitespace",
            "Username must not have leading or trailing whitespace",
        )
    if value == "":
        raise UsernameValidationError("username_empty", "Username is required")
    if _has_control_chars(value):
        raise UsernameValidationError(
            "username_control_chars", "Username must not contain control characters"
        )
    if _has_injection_shape(value):
        raise UsernameValidationError(
            "username_injection_shape",
            "Username contains forbidden punctuation",
        )
    if len(value) < USERNAME_LENGTH_MIN:
        raise UsernameValidationError(
            "username_too_short",
            f"Username must be at least {USERNAME_LENGTH_MIN} characters",
        )
    if len(value) > USERNAME_LENGTH_MAX:
        raise UsernameValidationError(
            "username_too_long",
            f"Username must be no more than {USERNAME_LENGTH_MAX} characters",
        )
    if not USERNAME_CHARSET_RE.fullmatch(value):
        raise UsernameValidationError(
            "username_charset",
            "Username may only contain letters, digits, '-', and '_'",
        )
    if value.startswith(".") or value.startswith("-") or value.startswith("_"):
        raise UsernameValidationError(
            "username_leading_punct",
            "Username may not start with '.', '-', or '_'",
        )
    if value.lower() in USERNAME_DENYLIST:
        raise UsernameValidationError(
            "username_reserved",
            f"Username '{value}' is reserved",
        )
    return value


# ---------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------

EMAIL_LENGTH_MAX: Final[int] = 254
# Local-host / IP-host rejection — production deployments need a
# routable email host. Mirrors what email-validator does optionally.
_EMAIL_FORBIDDEN_HOSTS: Final[FrozenSet[str]] = frozenset({
    "localhost", "127.0.0.1", "::1",
})


def validate_email(value: str | None) -> str:
    """Validate using `email-validator` (already in deps via
    `pydantic[email]`) plus length cap + non-routable-host rejection.
    """
    if value is None:
        raise EmailValidationError("email_empty", "Email is required")
    if not isinstance(value, str):
        raise EmailValidationError("email_type", "Email must be a string")
    value = value.strip()
    if value == "":
        raise EmailValidationError("email_empty", "Email is required")
    if _has_control_chars(value):
        raise EmailValidationError(
            "email_control_chars", "Email must not contain control characters"
        )
    if len(value) > EMAIL_LENGTH_MAX:
        raise EmailValidationError(
            "email_too_long",
            f"Email must be no more than {EMAIL_LENGTH_MAX} characters",
        )
    try:
        from email_validator import validate_email as _ev_validate, EmailNotValidError
    except ImportError:
        # email-validator not available — fall back to a permissive
        # regex check rather than skipping validation entirely.
        if "@" not in value or value.count("@") != 1:
            raise EmailValidationError(
                "email_format", "Email must be of the form local@domain"
            )
    else:
        try:
            ev = _ev_validate(value, check_deliverability=False)
            value = ev.normalized
        except EmailNotValidError as e:
            raise EmailValidationError("email_format", str(e)) from None
    domain = value.split("@", 1)[1].lower() if "@" in value else ""
    if domain in _EMAIL_FORBIDDEN_HOSTS:
        raise EmailValidationError(
            "email_unroutable_host",
            f"Email host '{domain}' is not routable",
        )
    return value


# ---------------------------------------------------------------------
# Company / Tenant name
# ---------------------------------------------------------------------

COMPANY_NAME_DENYLIST: Final[FrozenSet[str]] = frozenset({
    # Identity / role names
    "root", "admin", "administrator", "system", "operator",
    "superuser", "support", "null", "undefined", "anonymous",
    "guest", "postgres", "keycloak", "mod", "modcore",
    "platform", "default", "tenant", "everyone",
    # LDF-4 — Windows-reserved device names. Block reservation as a
    # tenant name to avoid downstream surprises in any path that ever
    # uses the name as a filesystem component.
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5",
    "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5",
    "lpt6", "lpt7", "lpt8", "lpt9",
})

COMPANY_NAME_LENGTH_MIN: Final[int] = 2
COMPANY_NAME_LENGTH_MAX: Final[int] = 64
# Letters (incl. accented), digits, space, and the punctuation set
# `[-_.,'&()]`. Matches `Acme Corp.`, `Tim's Studio`, `R & D Labs`.
_COMPANY_NAME_ALLOWED_PUNCT: Final[FrozenSet[str]] = frozenset(" -_.,&()")


def _has_zero_width_or_format_chars(s: str) -> bool:
    """LDF-4 — invisible / format-control codepoints used in
    homoglyph + impersonation attacks. Even when each char is in a
    "letter-like" Unicode category, these don't render — so two
    tenant names visually identical but containing a hidden ZWSP
    can collide in display while being distinct in storage.

    Categories rejected:
      Cf  — format characters (U+200B-U+200F, U+202A-U+202E,
            U+2060-U+206F bidi controls, ZWNJ, ZWJ, RTL/LTR override)
      Cc  — control (already caught by `_has_control_chars`, but
            redundant here for safety)
      Cn  — unassigned (defensive)
      Zl  — line separator
      Zp  — paragraph separator

    Note: ASCII space (U+0020) is category Zs (Separator, Space) and
    is allowed via the explicit punctuation whitelist; other Zs
    separators (NBSP, IDEOGRAPHIC SPACE, etc.) are rejected by the
    main charset check below.
    """
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in ("Cf", "Cc", "Cn", "Zl", "Zp"):
            return True
    return False


def _detect_mixed_script(s: str) -> Optional[str]:
    """LDF-4 — homoglyph defence. A tenant name like 'Аcme' (Cyrillic
    'А' U+0410 vs Latin 'A' U+0041) renders identically to 'Acme' but
    is a distinct string. An attacker can register the homoglyph to
    impersonate the legit tenant.

    Returns the offending script-name pair if the string mixes scripts
    within letters; returns None if all letter codepoints come from a
    single script (or are unscripted ASCII like digits / punct).

    Common-script characters (digits, punct, whitespace) are excluded
    from the mix check so 'Acme 2 Corp' (Latin + Common digits) is
    fine; 'Acme Аcme' (Latin + Cyrillic on letters) is rejected.
    """
    scripts: set[str] = set()
    for ch in s:
        if unicodedata.category(ch)[0] != "L":
            continue  # non-letter (digits, punct, space) — common
        # Coarse script detection via Unicode block name. Good enough
        # for the homoglyph cases we care about (Latin / Cyrillic /
        # Greek / Hebrew / Arabic / CJK confusable detection).
        try:
            name = unicodedata.name(ch, "")
        except ValueError:
            continue
        if " " in name:
            head = name.split(" ", 1)[0]
            scripts.add(head)
    if len(scripts) > 1:
        return ", ".join(sorted(scripts))
    return None


def validate_company_name(value: str | None) -> str:
    if value is None:
        raise CompanyNameValidationError(
            "company_empty", "Tenant name is required"
        )
    if not isinstance(value, str):
        raise CompanyNameValidationError(
            "company_type", "Tenant name must be a string"
        )
    if value != value.strip():
        raise CompanyNameValidationError(
            "company_whitespace",
            "Tenant name must not have leading or trailing whitespace",
        )
    if value == "":
        raise CompanyNameValidationError(
            "company_empty", "Tenant name is required"
        )
    # LDF-4 — NFKC normalise FIRST so subsequent checks operate on
    # the canonical form. NFKC also folds compatibility variants
    # (full-width Latin, ligatures) so two visually-equivalent inputs
    # collapse to the same string before storage / uniqueness check.
    value = unicodedata.normalize("NFKC", value)
    if value != value.strip():
        # Normalisation may surface trimmable whitespace (e.g.
        # full-width space U+3000 → ASCII space).
        raise CompanyNameValidationError(
            "company_whitespace",
            "Tenant name must not have leading or trailing whitespace",
        )
    if _has_control_chars(value):
        raise CompanyNameValidationError(
            "company_control_chars",
            "Tenant name must not contain control characters",
        )
    if _has_zero_width_or_format_chars(value):
        raise CompanyNameValidationError(
            "company_invisible_chars",
            "Tenant name contains invisible / format characters",
        )
    if _has_injection_shape(value):
        raise CompanyNameValidationError(
            "company_injection_shape",
            "Tenant name contains forbidden punctuation",
        )
    mixed = _detect_mixed_script(value)
    if mixed:
        raise CompanyNameValidationError(
            "company_mixed_script",
            f"Tenant name mixes scripts ({mixed}); pick one writing system",
        )
    if len(value) < COMPANY_NAME_LENGTH_MIN:
        raise CompanyNameValidationError(
            "company_too_short",
            f"Tenant name must be at least {COMPANY_NAME_LENGTH_MIN} characters",
        )
    if len(value) > COMPANY_NAME_LENGTH_MAX:
        raise CompanyNameValidationError(
            "company_too_long",
            f"Tenant name must be no more than {COMPANY_NAME_LENGTH_MAX} characters",
        )
    for ch in value:
        # Letters (any unicode), digits, space, allowed punctuation,
        # apostrophe (the SQL-shape check already rejects single-quote
        # so apostrophe falls through into the injection-shape branch
        # — explicitly disallow it here).
        cat = unicodedata.category(ch)
        if cat[0] in {"L", "N"}:
            continue
        if ch in _COMPANY_NAME_ALLOWED_PUNCT:
            continue
        raise CompanyNameValidationError(
            "company_charset",
            f"Tenant name has an unsupported character: {ch!r}",
        )
    if value.lower() in COMPANY_NAME_DENYLIST:
        raise CompanyNameValidationError(
            "company_reserved",
            f"Tenant name '{value}' is reserved",
        )
    return value

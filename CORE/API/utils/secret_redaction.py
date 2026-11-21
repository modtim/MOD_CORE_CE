"""Secret redaction filter for logging.

This module provides functionality to mask sensitive data in logs to prevent
accidental exposure of passwords, API keys, tokens, and other secrets.

ISO 27001 A.12.4.1 Compliance - Event Logging with Sensitive Data Protection
"""

import re
import logging
import hashlib
from typing import Optional, List, Dict, Any


# Patterns that indicate sensitive data
SENSITIVE_PATTERNS = [
    # Password patterns
    (r'(?i)(password|passwd|pwd)["\']?\s*[=:]["\']?([^"\',\)}\s\]]{4,})', 'PASSWORD'),

    # API key patterns
    (r'(?i)(api[_-]?key|apikey)["\']?\s*[=:]["\']?([^"\',\)}\s\]]{8,})', 'API_KEY'),

    # Token patterns
    (r'(?i)(token|auth[_-]?token|access[_-]?token)["\']?\s*[=:]["\']?([^"\',\)}\s\]]{8,})', 'TOKEN'),

    # Secret patterns
    (r'(?i)(secret|secret[_-]?key)["\']?\s*[=:]["\']?([^"\',\)}\s\]]{8,})', 'SECRET'),

    # AWS credentials
    (r'AKIA[0-9A-Z]{16}', 'AWS_ACCESS_KEY'),

    # Private keys
    (r'-----BEGIN (RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----', 'PRIVATE_KEY'),

    # Bearer tokens
    (r'(?i)Bearer\s+[a-zA-Z0-9\-._~+/_=]+', 'BEARER_TOKEN'),

    # Connection strings with passwords
    (r'(?i)(postgresql|mysql|mongodb|redis)://[^:]+:([^@]+)@', 'CONNECTION_STRING'),

    # JWT tokens
    (r'eyJ[a-zA-Z0-9\-_]*\.eyJ[a-zA-Z0-9\-_]*\.[a-zA-Z0-9\-_]*', 'JWT_TOKEN'),
]

# Fields that should always be redacted in JSON/dict output
SENSITIVE_FIELDS = [
    'password', 'passwd', 'pwd', 'secret', 'api_key', 'apikey', 'token',
    'auth_token', 'access_token', 'refresh_token', 'private_key', 'ssh_key',
    'certificate', 'client_secret', 'bearer_token', 'authorization',
    'api_secret', 'encryption_key', 'signing_key'
]

# ---------------------------------------------------------------------------
# CE-1643b: support-bundle redaction must not depend on paid evidence_bundle.
# Public API: is_sensitive_setting_key() — standalone, no try/except import.
# ---------------------------------------------------------------------------

def is_sensitive_setting_key(key: str) -> bool:
    """A setting key whose name matches the platform's sensitive-field
    vocabulary never ships its value in a bundle (non-secret values only).
    """
    fields = {str(f).lower() for f in SENSITIVE_FIELDS}
    parts = {p.lower() for p in key.split(".")}
    low = key.lower()
    return bool(parts & fields) or any(
        f in low for f in fields if len(f) >= 6)


# ---------------------------------------------------------------------------
# GSEC FX.96 (owner decision Q82=A) — customer content must never reach MOD
# internal system logs. Emails in log lines become a stable, non-reversible
# digest ("email:" + first 10 hex of sha256), and file names/paths logged by
# system loggers become "h:" + the same digest (hash_for_log). The digest keeps
# "which email/which file did this" greppable without storing the value.
# ---------------------------------------------------------------------------

# Same shape as utils.pii_redaction's EMAIL detector so both modules agree on
# what an email is.
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Default for the `logging.hash_emails` platform setting (registered in
# services/platform_settings.py, group "privacy", default True). The registered
# setting wins; this constant is the fallback for the unregistered-key path and
# for tests that do not touch the settings subsystem.
HASH_EMAILS_DEFAULT = True


def _resolve_hash_emails() -> bool:
    """Read the `logging.hash_emails` platform setting once (at filter setup).

    Unregistered key (KeyError/SettingNotRegisteredError) -> HASH_EMAILS_DEFAULT.
    A broken settings subsystem -> False, i.e. today's secret-only behaviour, so
    a settings failure never changes log output (GSEC R-086 contract).
    """
    try:
        from services import platform_settings
        raw = platform_settings.get_setting("logging.hash_emails")
    except KeyError:
        return HASH_EMAILS_DEFAULT
    except Exception:  # noqa: BLE001 — logging must never fail on settings
        return False
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return HASH_EMAILS_DEFAULT


def hash_for_log(value: Any) -> str:
    """Digest of a file name/path for a system log line: "h:" + sha256[:10].

    Call sites use this instead of logging the name itself (Q82=A: file names
    hashed in system logs). Stable per value, so two lines about the same file
    correlate without the name ever being stored."""
    return "h:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


def _email_digest(match: "re.Match") -> str:
    return "email:" + hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()[:10]


class SecretRedactionFilter(logging.Filter):
    """Logging filter that redacts sensitive data from log messages.

    Usage:
        logger = logging.getLogger(__name__)
        logger.addFilter(SecretRedactionFilter())
    """

    def __init__(self, name: str = '', hash_emails: Optional[bool] = None):
        super().__init__(name)
        self._compiled_patterns = [(re.compile(pattern), replacement)
                                    for pattern, replacement in SENSITIVE_PATTERNS]
        # FX.96: emails hashed by default (logging.hash_emails, default true).
        # Resolved lazily and cached per filter so importing this module never
        # touches the settings DB (an explicit argument, e.g. tests, wins).
        self._hash_emails = hash_emails

    def _hash_emails_active(self) -> bool:
        if self._hash_emails is None:
            self._hash_emails = _resolve_hash_emails()
        return self._hash_emails

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact sensitive data from log record."""
        # Redact the message
        record.msg = self.redact(str(record.msg))

        # Redact any string arguments
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self.redact_str(v) if isinstance(v, str) else v
                              for k, v in record.args.items()}
            else:
                record.args = tuple(self.redact_str(arg) if isinstance(arg, str) else arg
                                   for arg in record.args)

        # G-4 (FX.96 part 3, Q82=A): a traceback is rendered by the Formatter
        # at format time, AFTER filters run, so `logger.exception(...)` text
        # used to bypass everything above. Pre-render it here, redacted: the
        # stdlib Formatter only builds exc_text when it is still empty, so
        # writing it first means the appended traceback is this redacted one.
        # exc_info itself is left intact for handlers that read it.
        self._scrub_exception(record)

        return True

    def _scrub_exception(self, record: "logging.LogRecord") -> None:
        """Redact the traceback text on a record (G-4)."""
        exc_text = getattr(record, "exc_text", None)
        if exc_text:
            record.exc_text = self.redact_str(str(exc_text))
            return
        info = getattr(record, "exc_info", None)
        if not info or info is True:
            return
        try:
            import traceback
            raw = "".join(traceback.format_exception(*info))
        except Exception:  # noqa: BLE001 — redaction must never fail logging
            return
        if raw:
            record.exc_text = self.redact_str(raw)

    def redact(self, text: str) -> str:
        """Redact all sensitive patterns from text.

        Secrets are always redacted. PII/PHI is redacted on top when the
        ``privacy.redaction_profile`` platform setting (GY.V4a, GSEC R-086) is
        not 'off' — see utils.pii_redaction. The profile read is cached and
        never raises, so a settings failure leaves secret-only behaviour.
        """
        if not text:
            return text

        result = text
        for pattern, replacement in self._compiled_patterns:
            result = pattern.sub(f'<{replacement}>', result)

        try:
            from utils import pii_redaction
            if pii_redaction.profile_active():
                result = pii_redaction.redact(result)
        except Exception:  # noqa: BLE001 — redaction must never fail logging
            pass

        # FX.96 (Q82=A): emails in system logs become a stable digest, never
        # the value. Runs AFTER the profile-gated PII pass so a profile that
        # already tokenised the email keeps its [REDACTED:EMAIL] form; with the
        # profile off (the shipped default) this is what closes the gap.
        if self._hash_emails_active():
            result = EMAIL_RE.sub(_email_digest, result)

        return result

    def redact_str(self, value: str) -> str:
        """Redact sensitive data from a string value."""
        if not isinstance(value, str):
            return value
        return self.redact(value)


# FX.117 (Q104=A) — minimum exact-value length for masking. Values shorter
# than this are NOT masked: common short tokens ("abc123", "dev", ...) appear
# in far too many unrelated log lines, so masking them would flood node
# logs/outputs with false positives. Run-time secret values are expected to
# be longer; the pattern-based redaction above still covers short secrets
# that match a known shape (password=, token: Bearer ..., etc.).
MIN_EXACT_MASK_LEN = 6


def _mask_exact_in_str(text: str, values: list) -> str:
    for v in values:
        text = text.replace(v, "***")
    return text


def mask_exact_values(data: Any, values) -> Any:
    """FX.117 (Q104=A) — replace exact known secret VALUES with ``***`` in
    structured data (dicts/lists/strings) the API stores and serves: node
    logs, outputs, and error messages.

    ``values`` is an iterable of plaintext secret values (the run's
    referenced secrets). Unlike the pattern-based ``redact()`` — which only
    catches values that look like a known secret shape — this masks any
    occurrence of the exact value regardless of shape, closing the case
    where a node echoes a bare credential (no ``password=`` prefix, no
    ``Bearer`` prefix) into its stdout/output.

    Rules:
      * Values shorter than ``MIN_EXACT_MASK_LEN`` (6) are skipped to avoid
        false-positive floods (documented above).
      * Longer values are replaced before shorter ones so that one value
        being a substring of another still masks the longer match first.
      * Returns the input untouched when there is nothing to mask; dicts and
        lists are rebuilt lazily only when a mutation actually happens.

    Never raises: a masking failure must never break task completion/failure
    reporting — the value is still absent from the PATTERN pass, and a
    malformed input simply passes through.
    """
    try:
        vals = sorted(
            {v for v in (values or []) if isinstance(v, str) and len(v) >= MIN_EXACT_MASK_LEN},
            key=len,
            reverse=True,
        )
        if not vals:
            return data

        def _walk(node):
            if isinstance(node, str):
                out = node
                for v in vals:
                    out = out.replace(v, "***")
                return out
            if isinstance(node, list):
                return [_walk(x) for x in node]
            if isinstance(node, dict):
                return {k: _walk(x) for k, x in node.items()}
            return node

        return _walk(data)
    except Exception:  # noqa: BLE001 — redaction must never break the report path
        return data


def redact_sensitive_data(data: Any) -> Any:
    """Recursively redact sensitive data from dictionaries, lists, and strings.

    This is useful for sanitizing request bodies, response data, and other
    structured data before logging.

    Args:
        data: The data to redact (dict, list, or string)

    Returns:
        The data with sensitive values replaced with '<REDACTED>'
    """
    if isinstance(data, dict):
        return {k: redact_sensitive_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    elif isinstance(data, str):
        # Check if entire string looks like a sensitive value
        for field_name in SENSITIVE_FIELDS:
            if field_name in data.lower() and len(data) > 10:
                return '<REDACTED>'
        return data
    else:
        return data


def redact_dict_fields(data: Dict[str, Any], field_names: List[str] = None) -> Dict[str, Any]:
    """Redact specific fields in a dictionary.

    Args:
        data: Dictionary to redact
        field_names: List of field names to redact. If None, uses SENSITIVE_FIELDS.

    Returns:
        Dictionary with specified fields redacted
    """
    if not data or not isinstance(data, dict):
        return data

    fields_to_redact = field_names or SENSITIVE_FIELDS
    result = dict(data)

    for field in fields_to_redact:
        if field in result:
            result[field] = '<REDACTED>'

    return result


def get_safe_log_data(data: Dict[str, Any], exclude_fields: List[str] = None) -> Dict[str, Any]:
    """Get a sanitized version of data safe for logging.

    Removes or masks sensitive fields.

    Args:
        data: Original data
        exclude_fields: Additional fields to exclude entirely

    Returns:
        Sanitized data safe for logging
    """
    if not data or not isinstance(data, dict):
        return data

    # Fields to exclude entirely (not even show as redacted)
    fields_to_exclude = set(exclude_fields or [])
    fields_to_exclude.update(['ssh_key_secret_name', 'sudo_password_secret_name'])

    result = {}

    for key, value in data.items():
        # Skip excluded fields entirely
        if key.lower() in fields_to_exclude:
            continue

        # Redact sensitive fields
        if key.lower() in [f.lower() for f in SENSITIVE_FIELDS]:
            result[key] = '<REDACTED>'
        elif isinstance(value, dict):
            result[key] = get_safe_log_data(value, exclude_fields)
        else:
            result[key] = value

    return result


# Create a default redaction filter instance
_default_filter = SecretRedactionFilter()


def redact(message: str) -> str:
    """Convenience function to redact a log message."""
    return _default_filter.redact(message)


def install_secret_redaction_for_logger(logger_name: str = "uvicorn.access") -> bool:
    """Attach a SecretRedactionFilter to a named logger so secrets in its
    records (msg or args) are masked before any handler formats/emits them.

    Needed for uvicorn's ``uvicorn.access`` logger: it gets its own
    ``AccessFormatter`` handler via ``dictConfig`` and does NOT propagate to the
    root logger, so it never sees the app-level redaction filter that
    ``configure_logging`` installs on the root console handler. Its default log
    line includes the full request line (query string included), and the SSE
    stream endpoints authenticate via ``?access_token=<Keycloak JWT>`` — so
    without this the JWT would be written verbatim to ``docker logs``.

    Idempotent: returns False and changes nothing if a SecretRedactionFilter is
    already attached. Must be called AFTER uvicorn's ``dictConfig`` runs (in
    this app, during lifespan startup) — an earlier attach would be wiped by
    ``dictConfig(incremental=False)``.
    """
    lg = logging.getLogger(logger_name)
    for existing in lg.filters:
        if isinstance(existing, SecretRedactionFilter):
            return False
    lg.addFilter(SecretRedactionFilter())
    return True

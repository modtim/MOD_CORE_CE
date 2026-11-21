"""Logging configuration based on LOG_AGGREGATION_LEVEL

This module configures secure logging with automatic secret redaction to prevent
accidental exposure of sensitive data in logs.

ISO 27001 A.12.4.1 Compliance - Event Logging with Sensitive Data Protection
"""

import os
import sys
import logging
from utils.secret_redaction import SecretRedactionFilter

def configure_logging():
    """
    Configure secure logging based on LOG_AGGREGATION_LEVEL.

    Levels:
    - 0: ERROR only (minimal logs)
    - 1: WARNING and above
    - 2: INFO and above (default)
    - 3: DEBUG (verbose) - NOT RECOMMENDED for production
    - Higher values: Same as 3

    All logs are automatically redacted to remove sensitive data.
    """
    try:
        level = int(os.getenv("LOG_AGGREGATION_LEVEL", "2"))
    except ValueError:
        # Non-numeric value, default to INFO
        level = 2

    # Map aggregation levels to Python logging levels
    if level == 0:
        log_level = logging.ERROR
    elif level == 1:
        log_level = logging.WARNING
    elif level == 2:
        log_level = logging.INFO
    else:
        # 3 or higher = DEBUG (warn about security implications)
        log_level = logging.DEBUG

    # Configure root logger with secret redaction filter
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Clear existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Create console handler with redaction
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.addFilter(SecretRedactionFilter())
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    root_logger.addHandler(console_handler)

    # Warn if DEBUG level is enabled in production
    if level >= 3 and os.getenv("ENVIRONMENT", "production") == "production":
        logging.warning("DEBUG logging enabled in production - sensitive data may be exposed")

    # Silence noisy third-party HTTP / async libraries even when the
    # app-wide level is DEBUG. The API makes 100s of internal
    # MinIO/Keycloak/Vault calls per minute; urllib3/httpcore/httpx
    # emit 3-5 records per call. Formatting all those records is
    # measurable CPU at launch scale (50+ users) and the records add
    # nothing operators can act on. App-level DEBUG remains intact so
    # request-flow tracing still works.
    for noisy in ("urllib3", "urllib3.connectionpool",
                  "httpcore", "httpcore.http11", "httpcore.connection",
                  "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_level


def ensure_root_console_handler():
    """Re-attach the console StreamHandler to the root logger if missing.

    uvicorn's ``dictConfig`` may wipe the handlers set by
    ``configure_logging()`` (which runs before ASGI startup).  This
    idempotent helper restores the single redacted console sink so that
    all log records — including those from ``logger = logging.getLogger(
    "main")`` in the unhandled-exception handler — propagate to stdout.

    Guards against duplicates: only adds one StreamHandler whose stream
    is stdout or stderr and that carries the SecretRedactionFilter.
    """
    root = logging.getLogger()

    # Check if we already have a suitable handler
    for h in root.handlers:
        if (
            isinstance(h, logging.StreamHandler)
            and h.stream in (sys.stdout, sys.stderr)
            and any(
                isinstance(f, SecretRedactionFilter)
                for f in h.filters
            )
        ):
            return  # already configured

    # Re-use the configured level from the root logger
    log_level = root.level
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.addFilter(SecretRedactionFilter())
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    root.addHandler(console_handler)


def _has_effective_handler(lg):
    """True if ``lg`` or a logger it propagates to carries a handler.

    ``uvicorn.access`` / ``uvicorn.error`` set ``propagate = False`` in uvicorn's
    own ``dictConfig``, so for them only their OWN handlers count — a record
    emitted there never reaches the root sink.
    """
    while lg:
        if lg.handlers:
            return True
        if not lg.propagate:
            return False
        lg = lg.parent


def ensure_uvicorn_access_logging():
    """Re-attach the access-log sink to uvicorn's own loggers if a config wipe dropped it.

    ``logging.config.dictConfig`` applied with ``disable_existing_loggers`` left
    at its ``True`` default, or with a ``handlers`` block that replaces the
    existing ones, drops the ``uvicorn.access`` / ``uvicorn.error`` handlers that
    uvicorn installed for its request lines. Those loggers do NOT propagate to
    the root, so once their handler is gone every request line is silently
    discarded: the API keeps serving but ``docker logs`` shows no access lines
    after a relaunch (``ensure_root_console_handler()`` restores the ROOT sink
    only, which does not help a non-propagating child).

    Idempotent: only adds one redacted StreamHandler when the logger has no
    effective handler of its own or inherited. Log levels and formats are left
    exactly as uvicorn/app configured them; the SecretRedactionFilter is the
    only addition, matching every other sink in this module.
    """
    for name in ("uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.disabled = False
        if _has_effective_handler(lg):
            continue
        access_handler = logging.StreamHandler()
        access_handler.setLevel(logging.INFO)
        access_handler.addFilter(SecretRedactionFilter())
        access_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        lg.addHandler(access_handler)


def get_log_level():
    """Get current log level name"""
    try:
        level = int(os.getenv("LOG_AGGREGATION_LEVEL", "2"))
    except ValueError:
        level = 2

    level_map = {
        0: "ERROR",
        1: "WARNING",
        2: "INFO",
    }

    return level_map.get(level, "DEBUG")

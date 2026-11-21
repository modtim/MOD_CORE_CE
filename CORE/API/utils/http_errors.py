"""Sanitized error responses (GSEC R-019).

Router/crud/service sites used to raise ``HTTPException`` with the raw
exception interpolated into ``detail`` (``f"...: {e}"`` / ``str(e)``), which
let internal hostnames, Redis/Stripe/DB error text and request-derived
content reach clients. ``internal_http_error`` is the one helper that
replaces every such site:

* it generates an ``error_id`` (``uuid4().hex`` — the same key name and
  value shape as ``main.py``'s generic 500 handler, so clients see one
  error shape: ``{"message": ..., "error_id": ...}``);
* it logs ONE ``logger.error`` with the exception (``exc_info``) carrying
  the same ``error_id`` for server-side correlation;
* it returns ``HTTPException(status_code, detail={"message": public_message,
  "error_id": error_id})`` — the public message is a short, generic
  description of the operation, never the exception text.

Use as::

    except VaultError as e:
        raise internal_http_error(503, "Email password could not be saved", e) from e
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import HTTPException
from starlette.exceptions import HTTPException as _StarletteHTTPException

CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class ApiError(HTTPException):
    """A developer-controlled error with a stable `code` for API consumers.

    The `code` is validated against CODE_RE before being emitted in the
    response body; failures cause the code key to be dropped (never a 500).
    """

    status_code: int
    code: str
    detail: str
    retry_after: Optional[int]
    headers: Dict[str, str]

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        retry_after: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        # Subclass of HTTPException so callers' `except HTTPException` and handler-less
        # apps keep pre-c14 behaviour; Starlette resolves the ApiError handler first via MRO.
        super().__init__(status_code=status_code, detail=detail, headers=headers or None)
        self.code = code
        self.retry_after = retry_after
        self.headers = headers or {}


class _HTTPExceptionDetail:
    """Value wrapper to distinguish str/detail from dict detail in the
    HTTPException exception handler, avoiding isinstance checks on
    starlette's HTTPException.data (which can be anything).
    """

    def __init__(self, value: Any) -> None:
        self.value = value

logger = logging.getLogger(__name__)


def internal_http_error(
    status_code: int,
    public_message: str,
    exc: Optional[BaseException],
    *,
    log: logging.Logger = logger,
) -> HTTPException:
    """Build a sanitized HTTPException for an internal failure.

    ``public_message`` is the client-facing text (short + generic, e.g.
    "Stripe checkout failed"); ``exc`` is the caught exception that carries
    the real reason — it goes to the log (with ``error_id``), never to the
    response body.
    """
    error_id = uuid4().hex
    log.error(
        "internal error_id=%s status=%s op=%s: %s: %s",
        error_id,
        status_code,
        public_message,
        type(exc).__name__ if exc is not None else "N/A",
        exc if exc is not None else "(no exception)",
        exc_info=exc,
    )
    return HTTPException(
        status_code=status_code,
        detail={"message": public_message, "error_id": error_id},
    )

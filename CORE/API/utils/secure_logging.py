"""Secure logging middleware for FastAPI.

This middleware ensures sensitive data is never exposed in request/response logs.

ISO 27001 A.12.4.1 Compliance - Event Logging with Sensitive Data Protection
"""

import json
import logging
from typing import Callable, List, Dict, Any, Optional
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from fastapi import Request as FastAPIRequest

from utils.secret_redaction import redact, redact_dict_fields, SENSITIVE_FIELDS

logger = logging.getLogger(__name__)


# Paths that should never have request bodies logged
SKIP_BODY_LOGGING_PATHS = [
    '/login', '/auth', '/token', '/oauth', '/register', '/invite',
    '/webhook', '/stripe', '/payment', '/billing', '/reset-password',
    # Worker bootstrap / credential issuance endpoints (avoid even query-string logging)
    '/workers/enroll',
]


class SecureLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware that adds secure logging with automatic secret redaction.

    Usage:
        app.add_middleware(SecureLoggingMiddleware)
    """

    def __init__(
        self,
        app,
        log_requests: bool = True,
        log_responses: bool = False,
        skip_paths: List[str] = None,
        redact_fields: List[str] = None
    ):
        super().__init__(app)
        self.log_requests = log_requests
        self.log_responses = log_responses
        self.skip_paths = skip_paths or SKIP_BODY_LOGGING_PATHS
        self.redact_fields = redact_fields or SENSITIVE_FIELDS

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Log request without sensitive data
        if self.log_requests:
            self._log_request_safely(request)

        try:
            response = await call_next(request)

            # Log response without sensitive data
            if self.log_responses:
                self._log_response_safely(request, response)

            return response

        except Exception as e:
            # Log exceptions safely
            logger.warning(f"Request to {request.url.path} failed: {type(e).__name__}")
            raise

    def _log_request_safely(self, request: Request) -> None:
        """Log request details safely, redacting sensitive data."""
        try:
            # Don't log full paths for auth-related endpoints
            path = request.url.path
            is_sensitive_path = any(sp in path for sp in self.skip_paths)

            if is_sensitive_path:
                logger.info(f"Request: {request.method} {path}")
                return

            # Log basic request info
            log_msg = f"Request: {request.method} {path}"

            # Add query params if present (redacted)
            if request.url.query:
                # Parse and redact query params
                from urllib.parse import parse_qs
                params = parse_qs(request.url.query)
                safe_params = redact_dict_fields(params, self.redact_fields)
                log_msg += f" params={safe_params}"

            logger.info(log_msg)

        except Exception as e:
            # Never fail request processing due to logging errors
            logger.debug(f"Could not log request safely: {e}")

    def _log_response_safely(self, request: Request, response: Response) -> None:
        """Log response details safely, redacting sensitive data."""
        try:
            logger.info(f"Response: {request.method} {request.url.path} - {response.status_code}")
        except Exception as e:
            logger.debug(f"Could not log response safely: {e}")


async def log_api_request_safely(method: str, path: str, body: Optional[Dict] = None) -> None:
    """Safely log an API request with redaction.

    Use this function instead of direct logging when you need to log
    request details that may contain sensitive data.

    Usage:
        await log_api_request_safely("POST", "/api/workers", {
            "worker_name": "worker-1",
            "password": "secret123"
        })
        # Logs: Request: POST /api/workers body={'worker_name': 'worker-1', 'password': '<REDACTED>'}
    """
    if body:
        safe_body = redact_dict_fields(body)
        logger.info(f"Request: {method} {path} body={safe_body}")
    else:
        logger.info(f"Request: {method} {path}")


async def log_api_response_safely(path: str, status_code: int, response_data: Optional[Dict] = None) -> None:
    """Safely log an API response with redaction.

    Use this function instead of direct logging when you need to log
    response details that may contain sensitive data.
    """
    if response_data:
        safe_data = redact_dict_fields(response_data)
        logger.info(f"Response: {path} - {status_code} data={safe_data}")
    else:
        logger.info(f"Response: {path} - {status_code}")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Middleware that adds request context to logs for better traceability.

    Adds request ID and other context to all logs for a given request.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Generate or get request ID
        request_id = request.headers.get('x-request-id') or self._generate_request_id()
        request.state.request_id = request_id

        # Add request ID to response for tracing
        response = await call_next(request)
        response.headers['x-request-id'] = request_id

        return response

    def _generate_request_id(self) -> str:
        """Generate a unique request ID."""
        import uuid
        return str(uuid.uuid4())[:8]


# Utility functions for safe logging in route handlers

def safe_log_info(message: str, **kwargs) -> None:
    """Log an info message with automatic secret redaction."""
    safe_message = redact(message)
    safe_kwargs = {k: redact(str(v)) if isinstance(v, str) else v for k, v in kwargs.items()}
    logger.info(safe_message, **safe_kwargs)


def safe_log_warning(message: str, **kwargs) -> None:
    """Log a warning message with automatic secret redaction."""
    safe_message = redact(message)
    safe_kwargs = {k: redact(str(v)) if isinstance(v, str) else v for k, v in kwargs.items()}
    logger.warning(safe_message, **safe_kwargs)


def safe_log_error(message: str, **kwargs) -> None:
    """Log an error message with automatic secret redaction."""
    safe_message = redact(message)
    safe_kwargs = {k: redact(str(v)) if isinstance(v, str) else v for k, v in kwargs.items()}
    logger.error(safe_message, **safe_kwargs)

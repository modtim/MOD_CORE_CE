"""sz-4.7.2 (sz-productivity-floor decomp §4.7.2) — the OUTPUT DELIVERY
LOGGER service: the API-side writer for `output_delivery_log`
(models.OutputDeliveryLog, migration 20261011d).

One row per delivery ATTEMPT by an output node (Slack / Discord / email /
SMS / push / webhook / file). The wiring point is the task-complete path in
`worker_tasks.py` / the output-node routers — a later item; this module is
the writer only, so it can be called from any request or worker callback.

APPEND-ONLY: there is no update or delete helper here. Rows are never
mutated; retention is a soft policy (archive/export), per ISO 27001
no-hard-delete.

TRANSACTION: `record_delivery` ADDs and FLUSHes but never commits — the
caller owns the transaction, so a delivery row can be rolled back with the
rest of the request (and never commits half a request out from under it).

NOTHING SECRET IS STORED:
* `destination_hint` — the redacted destination only: URL → scheme://host
  (path/query/userinfo dropped, because Slack/Discord webhook tokens live
  IN the path), email → first char + domain, E.164 phone → last 2 digits,
  anything else → first 4 chars. Max 255.
* `payload_summary` — SHAPE only: type, byte size, sorted top-level key
  names (secret-named keys are listed, their values are not). No values.
* `error` — free text scrubbed of URLs / emails / phones / bearer-ish
  tokens, truncated to 1024.

`redact_destination` and `summarize_payload` are pure functions and are
exposed so the compliance view can reuse exactly the same redaction.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import models
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MASK = "***"

# Exactly the model's CHECK sets (ck_output_delivery_log_channel / _status).
CHANNELS = {"slack", "discord", "email", "sms", "push", "webhook", "file"}
STATUSES = {"delivered", "failed", "retrying", "skipped"}

HINT_MAX = 255
ERROR_MAX = 1024
KEYS_MAX = 50

_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d{7,}")
# "Bearer abc", "Authorization: Basic abc", "token=abc" — the value goes, the
# label stays. The optional non-capturing (?:bearer|basic)\s+ absorbs the
# auth SCHEME word so "Authorization: Bearer <jwt>" redacts the whole value
# instead of only matching the scheme word itself (which is why "basic"
# also has to be a label on its own: a bare "Basic <creds>" with no
# "Authorization:" prefix still has to be caught).
_TOKEN_RE = re.compile(
    r"(?i)\b(bearer|basic|authorization|token|api[_-]?key|secret)\b"
    r"[=:\s]+(?:(?:bearer|basic)\s+)?[A-Za-z0-9_./+\-]{6,}")
# A bare JWT (header.payload.signature, base64url segments) with no
# recognizable label in front of it at all.
_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _looks_like_host(value: str) -> bool:
    """A bare host (no scheme), e.g. hooks.slack.com — safe to keep as-is."""
    if " " in value or "@" in value or "/" in value:
        return False
    parts = value.split(":")[0].split(".")
    return len(parts) >= 2 and all(parts)


def redact_destination(destination: Optional[str]) -> Optional[str]:
    """Redact a destination to the recognizable-but-useless part.

    Never a full webhook URL (a webhook URL is a secret — anyone holding it
    can post as MOD), never an email, never a phone number.
    """
    if destination is None:
        return None
    raw = str(destination).strip()
    if not raw:
        return None

    split = urlsplit(raw)
    if split.scheme and split.hostname:
        # host only: userinfo (user:pass) and the path/query are dropped —
        # Slack + Discord webhook tokens live IN the path.
        return _clip(f"{split.scheme}://{split.hostname}/{MASK}", HINT_MAX)
    if _looks_like_host(raw):
        return _clip(raw, HINT_MAX)
    if _EMAIL_RE.fullmatch(raw):
        domain = raw.split("@", 1)[1]
        return _clip(f"{raw[0]}{MASK}@{domain}", HINT_MAX)
    if _PHONE_RE.fullmatch(raw):
        return _clip(f"+{MASK}{raw[-2:]}", HINT_MAX)
    return _clip(f"{raw[:4]}{MASK}", HINT_MAX)


def scrub_text(text: Optional[str]) -> Optional[str]:
    """Strip URLs / emails / phones / bearer-ish tokens from free text."""
    if text is None:
        return None
    out = str(text)
    out = _URL_RE.sub(lambda m: redact_destination(m.group(0)) or MASK, out)
    out = _EMAIL_RE.sub(lambda m: redact_destination(m.group(0)) or MASK, out)
    out = _PHONE_RE.sub(lambda m: redact_destination(m.group(0)) or MASK, out)
    out = _TOKEN_RE.sub(lambda m: f"{m.group(1)} {MASK}", out)
    out = _JWT_RE.sub(MASK, out)
    return _clip(out, ERROR_MAX)


def summarize_payload(payload: Any) -> Dict[str, Any]:
    """Shape only — type, size, top-level key names. NEVER any values."""
    kind = "none"
    if isinstance(payload, dict):
        kind = "dict"
    elif isinstance(payload, list):
        kind = "list"
    elif isinstance(payload, str):
        kind = "str"
    elif isinstance(payload, (bytes, bytearray)):
        kind = "bytes"

    size = 0
    keys: List[str] = []
    if kind == "dict":
        size = len(json.dumps(payload, default=str).encode("utf-8"))
        keys = sorted(str(k) for k in payload.keys())[:KEYS_MAX]
    elif kind == "list":
        size = len(json.dumps(payload, default=str).encode("utf-8"))
    elif kind == "str":
        size = len(payload.encode("utf-8"))
    elif kind == "bytes":
        size = len(payload)

    return {"type": kind, "bytes": size, "keys": keys}


def record_delivery(
    db: Session,
    *,
    tenant_id: int,
    channel: str,
    status: str,
    workflow_run_id: Optional[int] = None,
    api_task_id: Optional[int] = None,
    node_id: Optional[str] = None,
    destination: Optional[str] = None,
    http_status: Optional[int] = None,
    attempt: int = 1,
    payload: Any = None,
    error: Optional[str] = None,
) -> models.OutputDeliveryLog:
    """Append one delivery-attempt row. Validates BEFORE touching the DB.

    Adds + flushes only — the caller owns the transaction (no commit here).
    """
    if channel not in CHANNELS:
        raise ValueError(f"unknown delivery channel: {channel!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown delivery status: {status!r}")

    row = models.OutputDeliveryLog(
        tenant_id=tenant_id,
        workflow_run_id=workflow_run_id,
        api_task_id=api_task_id,
        node_id=node_id,
        channel=channel,
        destination_hint=redact_destination(destination),
        status=status,
        http_status=http_status,
        attempt=int(attempt or 1),
        payload_summary=summarize_payload(payload),
        error=scrub_text(error),
    )
    db.add(row)
    db.flush()
    return row

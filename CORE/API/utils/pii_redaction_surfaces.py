"""GY.V4b — the read/display + LLM-egress surfaces wired onto GY.V4a.

`utils.pii_redaction` owns the detectors and the profile. This module is the
thin wrapper those surfaces call, and it exists so the wiring in routers and
services stays one line long at every site:

    from utils.pii_redaction_surfaces import safe_redact
    text = safe_redact(text)

Contract every wired surface relies on:

- profile ``off`` -> the value is returned as the SAME object, so behaviour is
  byte-identical to today (no copy, no re-encode, no token substitution).
- redaction is applied on READ only. Nothing here mutates stored rows, stored
  log tails, file contents or S3 keys — callers pass a value out, not back in.
- any failure (profile read, detector, unexpected type) returns the input
  unchanged and logs exactly one warning. Redaction must never fail the caller.
- text fields only: non-string values (ints, dicts of hashes, None) pass
  through untouched; containers are redacted element-wise without mutating the
  original mapping/list.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def safe_redact(value: Any) -> Any:
    """Redact one text field under the live profile; never raises.

    Returns ``value`` unchanged when it is not text, when the profile is off,
    and on any error (one warning logged in the error case).
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        from utils import pii_redaction
        if not pii_redaction.profile_active():
            return value
        out = pii_redaction.redact(value)
        if out == value:
            return value
        logger.debug(
            "GY.V4b: PII redaction applied to a %d-char field", len(value))
        return out
    except Exception as e:  # noqa: BLE001 — redaction must never fail the caller
        logger.warning("GY.V4b: redaction failed, passing text through: %s", e)
        return value


def redact_mapping(mapping: Any) -> Any:
    """Redact the string VALUES of a dict (keys are never rewritten).

    Returns the mapping unchanged when it is not a dict, when the profile is
    off, or on error. A new dict is built — the caller's stored mapping is not
    mutated.
    """
    if not isinstance(mapping, dict):
        return mapping
    try:
        from utils import pii_redaction
        if not pii_redaction.profile_active():
            return mapping
        out = {}
        changed = False
        for k, v in mapping.items():
            rv = safe_redact(v)
            if rv is not v:
                changed = True
            out[k] = rv
        if not changed:
            return mapping
        return out
    except Exception as e:  # noqa: BLE001 — redaction must never fail the caller
        logger.warning("GY.V4b: mapping redaction failed, passing through: %s", e)
        return mapping


def redact_list(items: Any) -> Any:
    """Redact string items (and string values of dict items) of a list.

    Same guarantees as ``redact_mapping``: off / non-list / error -> the
    original list, and a new list is built rather than mutating the stored one.
    """
    if not isinstance(items, list):
        return items
    try:
        from utils import pii_redaction
        if not pii_redaction.profile_active():
            return items
        out = []
        changed = False
        for item in items:
            if isinstance(item, dict):
                rv = redact_mapping(item)
            elif isinstance(item, str):
                rv = safe_redact(item)
            else:
                rv = item
            if rv is not item:
                changed = True
            out.append(rv)
        if not changed:
            return items
        return out
    except Exception as e:  # noqa: BLE001 — redaction must never fail the caller
        logger.warning("GY.V4b: list redaction failed, passing through: %s", e)
        return items


def redact_node_run_view(row: Any) -> Any:
    """Redact the text fields of one NodeRun for a run-detail API response.

    Only ``inputs`` values, ``outputs`` entries and ``error_message`` are
    touched; nothing else on the row. Stored data is never mutated — a plain
    dict copy is built, so the ORM row the caller queried stays as-is.

    Off profile (or a non-NodeRun input, or any error) returns the row
    unchanged, so the response is byte-identical to today. File references
    inside ``outputs`` (content_hash / file_name / file_path) are only redacted
    as text values; no file content or S3 key is ever rewritten.
    """
    try:
        from utils import pii_redaction
        if not pii_redaction.profile_active():
            return row
        inputs = getattr(row, "inputs", None)
        outputs = getattr(row, "outputs", None)
        error = getattr(row, "error_message", None)
        r_inputs = redact_mapping(inputs)
        r_outputs = redact_list(outputs)
        r_error = safe_redact(error)
        if (r_inputs is inputs and r_outputs is outputs
                and r_error is error):
            # Nothing matched: hand back the ORM row itself, so the response is
            # byte-identical to today even with a profile on.
            return row
        out = {
            "id": getattr(row, "id", None),
            "workflow_node_id": getattr(row, "workflow_node_id", None),
            "status": getattr(row, "status", None),
            "started_at": getattr(row, "started_at", None),
            "completed_at": getattr(row, "completed_at", None),
            "inputs": r_inputs,
            "outputs": r_outputs,
            "error_message": r_error,
            "workflow_node": getattr(row, "workflow_node", None),
        }
        return out
    except Exception as e:  # noqa: BLE001 — redaction must never fail the caller
        logger.warning("GY.V4b: node-run redaction failed, passing through: %s", e)
        return row


def redact_messages(messages: Any) -> Any:
    """Redact the ``content`` of chat messages before a provider call.

    Used by the support/assistant chat LLM paths. Non-list input and off
    profile return the caller's list unchanged; a new list of new dicts is
    built so the caller's message objects are never mutated.
    """
    if not isinstance(messages, list):
        return messages
    try:
        from utils import pii_redaction
        if not pii_redaction.profile_active():
            return messages
        out = []
        changed = False
        for m in messages:
            if not isinstance(m, dict):
                out.append(m)
                continue
            content = m.get("content")
            rc = safe_redact(content)
            if rc is content:
                out.append(m)
            else:
                changed = True
                new = dict(m)
                new["content"] = rc
                out.append(new)
        return out if changed else messages
    except Exception as e:  # noqa: BLE001 — redaction must never fail the caller
        logger.warning("GY.V4b: message redaction failed, passing through: %s", e)
        return messages

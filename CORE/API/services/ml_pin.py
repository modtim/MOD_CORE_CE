"""Operator pin for the scheduler ONNX model.

Resolution chain:
- env `SCHEDULER_MODEL_VERSION_PIN` unset/empty → follow whatever the
  hot-reload thread would normally load (the latest version in MinIO).
- env set to a positive integer N → load `scheduler-vN.onnx`.
- env set to anything else → log + audit; behave as if unset.

The actual hot-reload thread lives in `main.py:_hot_reload_loop`. This
module only owns the parse + audit emission; the thread integrates by
calling `resolve_target_version()` each poll cycle.

Audit events:
- `MODEL_PIN_INVALID` — env value couldn't be parsed (rate-limited).
- `MODEL_PIN_MISSING` — env points at a version that isn't in MinIO.
  Hot-reload falls back to the active model.
- `MODEL_PIN_ACTIVE` — pin is set AND latest (active) is newer than
  the pinned version, so the pin is silently keeping the operator on
  a stale model. Rate-limited to once per hour per process to avoid
  flooding the audit log.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Mapping, Optional, Tuple

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

PIN_ENV_VAR = "SCHEDULER_MODEL_VERSION_PIN"

MODEL_PIN_INVALID_EVENT = "MODEL_PIN_INVALID"
MODEL_PIN_MISSING_EVENT = "MODEL_PIN_MISSING"
MODEL_PIN_ACTIVE_EVENT = "MODEL_PIN_ACTIVE"

# Outcomes returned by resolve_target_version.
OUTCOME_UNPINNED = "unpinned"
OUTCOME_PINNED = "pinned"
OUTCOME_PIN_INVALID = "pin_invalid"


# Rate-limiter state for stale-pin and pin-invalid audits. Keyed by
# event_name → last emission timestamp (monotonic seconds).
_RATE_LIMIT_WINDOW_S = 60 * 60  # 1 hour per design Decision 4.
_last_emit_ts: dict[str, float] = {}
_emit_lock = threading.Lock()


def _should_emit(event_name: str, *, window_s: float = _RATE_LIMIT_WINDOW_S) -> bool:
    """True if the rate-limit window has elapsed since the last emission."""
    now = time.monotonic()
    with _emit_lock:
        last = _last_emit_ts.get(event_name)
        if last is not None and (now - last) < window_s:
            return False
        _last_emit_ts[event_name] = now
        return True


def reset_rate_limit_for_tests() -> None:
    """Clear the rate-limit state. Test-only helper."""
    with _emit_lock:
        _last_emit_ts.clear()


def resolve_target_version(
    env: Mapping[str, str], active_version: Optional[int],
) -> Tuple[Optional[int], str]:
    """Compute the target model version to load.

    Returns (version_to_load, outcome). When the pin env var is unset
    or malformed, version_to_load is `active_version` and the caller
    behaves as if no pin existed.
    """
    raw = env.get(PIN_ENV_VAR, "").strip()
    if not raw:
        return (active_version, OUTCOME_UNPINNED)

    try:
        parsed = int(raw)
    except ValueError:
        return (active_version, OUTCOME_PIN_INVALID)

    if parsed < 1:
        return (active_version, OUTCOME_PIN_INVALID)

    return (parsed, OUTCOME_PINNED)


def _emit_audit(db: Optional[Session], event_name: str, details: dict) -> None:
    if db is None:
        logger.debug("ml_pin: no db session — skipping audit %s", event_name)
        return
    try:
        db.add(models.AuditEvent(
            tenant_id=None, user_id=None,
            event_name=event_name, details=details,
        ))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("ml_pin: audit emit failed for %s: %s", event_name, e)


def emit_pin_invalid(db: Optional[Session], raw_value: str) -> None:
    """Emit MODEL_PIN_INVALID, rate-limited to once per hour."""
    if not _should_emit(MODEL_PIN_INVALID_EVENT):
        return
    truncated = raw_value[:64]
    logger.warning(
        "ml_pin: %s set to %r — cannot parse as positive integer, falling back to active",
        PIN_ENV_VAR, truncated,
    )
    _emit_audit(db, MODEL_PIN_INVALID_EVENT, {"raw_value": truncated})


def emit_pin_missing(
    db: Optional[Session], requested: int, fallback: Optional[int],
) -> None:
    """Emit MODEL_PIN_MISSING — pin points at a version not in MinIO.

    Rate-limited per (requested,fallback) tuple so a chronically-broken
    pin doesn't spam the audit log on every 60s poll cycle.
    """
    key = f"{MODEL_PIN_MISSING_EVENT}:{requested}:{fallback}"
    if not _should_emit(key):
        return
    logger.warning(
        "ml_pin: pinned version v%d not found in MinIO; loading active v%s instead",
        requested, fallback,
    )
    _emit_audit(
        db, MODEL_PIN_MISSING_EVENT,
        {"requested_version": requested, "fallback_version": fallback},
    )


def emit_pin_active(
    db: Optional[Session], pinned: int, active: int,
) -> None:
    """Emit MODEL_PIN_ACTIVE — pin is set AND latest is newer.

    Rate-limited to once per hour per (pinned, active) tuple. Surfaces
    a stale pin so periodic ops review can catch it.
    """
    if pinned >= active:
        return
    key = f"{MODEL_PIN_ACTIVE_EVENT}:{pinned}:{active}"
    if not _should_emit(key):
        return
    logger.info(
        "ml_pin: still pinned to v%d while latest is v%d (gap=%d). Operator action may be required.",
        pinned, active, active - pinned,
    )
    _emit_audit(
        db, MODEL_PIN_ACTIVE_EVENT,
        {"pinned_version": pinned, "active_version": active, "gap": active - pinned},
    )

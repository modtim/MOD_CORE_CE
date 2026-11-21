"""sz-4.7c — capture output-node delivery results from task completion.

Wires the `output_delivery_log` writer (services/output_delivery_logger) into
the task-complete path (routers/worker_tasks.py / report_task_complete).

CALLER OWNS THE TRANSACTION: this module only flushes, never commits.
A SAVEPOINT guards every DB write so a capture failure can never poison the
task-completion transaction.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from services import output_delivery_logger as odl

logger = logging.getLogger(__name__)

# node_kind (from task.task_payload["node_kind"]) → delivery channel
# Each entry is an output node that fires a notification.
NODE_KIND_TO_CHANNEL: dict[str, str] = {
    "slack_webhook": "slack",
    "discord_webhook": "discord",
    "smtp_send": "email",
    "twilio_sms": "sms",
    "pushover_push": "push",
    "webhook_post": "webhook",
}


def capture_from_task_result(
    db: Session,
    task: Any,
    *,
    succeeded: bool,
    result: dict | None = None,
    error: str | None = None,
    attempt: int | None = None,
) -> int:
    """Record output-node delivery result at task completion.

    Returns rows written (0 for non-output tasks — cheap no-op).

    Behaviour:
    * If the node_kind is not in NODE_KIND_TO_CHANNEL → return 0 immediately
      (zero DB touch).
    * Otherwise record one delivery row via `odl.record_delivery`.
    * status = "delivered" when succeeded, "failed" otherwise.
    * *attempt* (1-based) distinguishes retried failures from the terminal
      one; defaults to 1 when the caller doesn't track attempts.
    * http_status / destination are pulled from *result* only if present;
      the writer redacts destinations so no secrets leak.
    * All DB writes run inside `db.begin_nested()` so a capture failure can
      never poison the caller's transaction.
    """
    nk = (task.task_payload or {}).get("node_kind")
    channel = NODE_KIND_TO_CHANNEL.get(nk)
    if channel is None:
        return 0

    try:
        with db.begin_nested():
            rows = 0
            status = "delivered" if succeeded else "failed"
            http_status = None
            destination = None
            payload_data = None

            if result is not None and not isinstance(result, dict):
                # Malformed result — e.g. a string or number instead of a dict.
                # Don't record a row; the meta is unreliable.
                return 0

            http_status = None
            destination = None
            payload_data = None
            if isinstance(result, dict):
                http_status = result.get("http_status") or result.get("status_code")
                destination = result.get("destination") or result.get("dest")
                payload_data = result.get("payload") or result.get("receipt")

            # Multi-recipient: if destination is a list, write one row per item.
            # If a single destination is provided, write one row.
            # If no destination is available, write one row with destination=None
            # so the delivery attempt is still recorded.
            destinations: list[Any] | None
            if isinstance(destination, list):
                destinations = destination if destination else [None]
            elif destination is not None:
                destinations = [destination]
            else:
                destinations = [None]

            for dest_item in destinations:
                odl.record_delivery(
                    db,
                    tenant_id=task.tenant_id,
                    channel=channel,
                    status=status,
                    workflow_run_id=getattr(task, "workflow_run_id", None),
                    api_task_id=task.id,
                    node_id=task.node_id,
                    destination=dest_item,
                    http_status=http_status,
                    attempt=attempt if attempt is not None else 1,
                    error=error,
                    payload=payload_data,
                )
                rows += 1

            return rows
    except Exception:  # noqa: BLE001 — capture must never fail completion
        logger.warning(
            "output_delivery_capture failed for task %d: %s",
            task.id,
            type(Exception).__name__,
            exc_info=True,
        )
        return 0

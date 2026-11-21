"""Layer-4 freshness — "approved" doesn't mean "approved forever".

Three surfaces:

1. **Staleness predicate** (`is_scan_stale`) — a node whose `scanned_at`
   is older than STALE_SCAN_DAYS is considered stale. Used by the
   scheduler's pre-dispatch check and the nightly rescan loop.

2. **Nightly rescan dispatcher** (`enqueue_stale_rescans`) — queries
   every `scan_status=approved` node, finds the stale ones, enqueues a
   `trivy_scan` task for each via the existing `_dispatch_trivy_scan`
   path. Intended to be called by a background-thread loop in main.py
   at a nightly cadence.

3. **Pre-dispatch staleness skip** — the scheduler can call
   `pre_dispatch_stale_check` on a task about to run. If the node's
   scan is stale, the scheduler enqueues a fresh scan and defers the
   task instead of dispatching against possibly-stale security state.

SaaS-only: on-prem deployments don't run the scanner at all
(`_is_saas()` gates `_dispatch_trivy_scan`), so staleness is moot there.
The nightly dispatcher short-circuits when not in SaaS; the staleness
predicate itself is pure and callable anywhere.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover
    import models

logger = logging.getLogger(__name__)


# Days after which an approved scan is considered stale. 7 is aggressive
# enough to catch most newly-published CVE DB updates but loose enough
# that tenants aren't flooded with rescan notifications weekly.
STALE_SCAN_DAYS = int(os.getenv("FRESHNESS_STALE_DAYS", "7"))


def _is_saas() -> bool:
    return os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas"


def is_scan_stale(scanned_at: Optional[datetime], *, now: Optional[datetime] = None) -> bool:
    """True when scanned_at is None or older than STALE_SCAN_DAYS.

    `now` is injectable for tests; defaults to `datetime.now(timezone.utc)`.
    """
    if scanned_at is None:
        return True
    current = now or datetime.now(timezone.utc)
    # Normalize to aware UTC so naive-vs-aware doesn't explode.
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=timezone.utc)
    age = current - scanned_at
    return age > timedelta(days=STALE_SCAN_DAYS)


def find_stale_approved_nodes(db: Session, *, limit: int = 200) -> list:
    """Return approved nodes whose last scan is older than STALE_SCAN_DAYS.

    Limited to 200 per call — prevents a single nightly tick from
    enqueuing thousands of rescans on a long-idle deployment. Run it
    repeatedly; subsequent passes pick up anything left over.
    """
    import models
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_SCAN_DAYS)
    return (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.scan_status == "approved")
        .filter(models.NodeDefinition.tenant_id.isnot(None))
        .filter(
            (models.NodeDefinition.scanned_at.is_(None))
            | (models.NodeDefinition.scanned_at < cutoff)
        )
        .order_by(models.NodeDefinition.scanned_at.asc().nullsfirst())
        .limit(limit)
        .all()
    )


def enqueue_stale_rescans(db: Session, *, limit: int = 200) -> list[int]:
    """Enqueue a fresh Trivy scan task for every stale approved node.

    Returns the list of node-definition IDs scheduled. Safe to call on
    a background thread; idempotent within a single stale window
    (repeated calls re-enqueue, which is harmless — the scanner path
    updates scan_status→scanning before running and won't double-run
    because the next _find_stale call filters by scanned_at).

    Short-circuits to empty list when not in SaaS mode — on-prem
    deployments don't scan at all.
    """
    if not _is_saas():
        return []

    # Import lazily to avoid circular with routers.
    from routers.node_definitions import _dispatch_trivy_scan

    ids: list[int] = []
    for nd in find_stale_approved_nodes(db, limit=limit):
        if not nd.executable_path:
            continue
        try:
            _dispatch_trivy_scan(db, nd.id, nd.executable_path, nd.tenant_id)
            ids.append(nd.id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Failed to enqueue stale rescan for node %s: %s", nd.id, exc
            )
    if ids:
        logger.info("Enqueued stale rescans for %d node(s): %s", len(ids), ids)
    return ids


def pre_dispatch_stale_check(db: Session, task) -> bool:
    """Called by the scheduler before dispatching a task. If the task's
    node_definition has a stale scan, enqueue a rescan first and return
    True to tell the scheduler to skip this task for now. Returns False
    when the task is safe to dispatch.

    `task` is an ApiTaskQueue row. We resolve its NodeDefinition by the
    task_payload's node_kind — matching how the scheduler resolves node
    context elsewhere.
    """
    if not _is_saas():
        return False
    if task is None:
        return False

    import models

    node_kind = None
    payload = getattr(task, "task_payload", None) or {}
    if isinstance(payload, dict):
        node_kind = payload.get("node_kind")
    if not node_kind:
        return False

    nd = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.node_kind == node_kind)
        .order_by(models.NodeDefinition.id.desc())
        .first()
    )
    if nd is None:
        return False

    if not is_scan_stale(nd.scanned_at):
        return False

    try:
        from routers.node_definitions import _dispatch_trivy_scan
        if nd.executable_path and nd.tenant_id is not None:
            _dispatch_trivy_scan(db, nd.id, nd.executable_path, nd.tenant_id)
            logger.info(
                "Pre-dispatch staleness: enqueued rescan for node %s "
                "(last scanned %s); task %s deferred.",
                nd.id, nd.scanned_at, task.id,
            )
            return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Pre-dispatch staleness check failed: %s", exc)
    return False

"""ml_retrain — submit the TFT scheduler retrain pipeline as a worker task.

Replaces the in-process `subprocess.run(["torchrun", ...])` path that the
manual endpoint and the daily auto-retrain APScheduler tick used to share.
That path could never have worked in deployed stacks: the API container
has no `torch`/`torchrun`, no host venv with the ML deps exists, and no
dedicated trainer service runs in compose. See
`the internal design notes`
Decision 8 for the runtime-gap finding.

Submission model: enqueue an api_task_queue row pointing at the
`ml_retrain` system NodeDefinition with priority_class='low'. mod-worker
pulls the row and runs the ml-retrain container, which authenticates to
Vault, runs the trainer pipeline, and uploads the ONNX + metadata to the
`platform-models` MinIO bucket. Shadow promotion via the existing daily
4:30 UTC tick picks it up from there — no change to that path.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

import models
from utils.registries import ce_mirror_ref, platform_image, platform_registry

logger = logging.getLogger(__name__)

ML_RETRAIN_NODE_KIND = "ml_retrain"
ML_RETRAIN_DEFAULT_IMAGE = platform_image("ml-retrain:latest")
RETRAIN_SUBMITTED_EVENT = "RETRAIN_SUBMITTED"

# Env keys propagated from the API process to the retrain container so
# the entrypoint can authenticate to Vault and connect to Postgres.
# AppRole creds are bootstrap auth, exchanged for a bounded-TTL token
# inside the container; storing them in task_payload is bounded blast
# radius vs. a long-lived service credential. POSTGRES_HOST/PORT/DB
# are infra topology, not secrets.
_VAULT_BOOTSTRAP_KEYS = (
    "VAULT_ADDR",
    "VAULT_APPROLE_ROLE_ID",
    "VAULT_APPROLE_SECRET_ID",
    "VAULT_DB_ROLE",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
)


def _build_container_env(trigger_source: str, force: bool) -> dict:
    """Build the container_env dict the worker injects into the retrain
    container. Includes the Vault bootstrap + Postgres topology copied
    from the API process's own env, plus the runtime knobs the
    entrypoint reads."""
    env = {
        # Marks this as a system-trust task. mod-worker's executor
        # already conditions Harbor cred passthrough on this key; the
        # ml-retrain entrypoint also reads it for sanity.
        "PLATFORM_TASK": "1",
        "RETRAIN_TRIGGER_SOURCE": trigger_source,
        "RETRAIN_FORCE": "1" if force else "0",
    }
    for key in _VAULT_BOOTSTRAP_KEYS:
        v = os.environ.get(key)
        if v:
            env[key] = v
    return env


def _resolve_tenant_id(db: Session, requested: Optional[int]) -> Optional[int]:
    """Return the requested tenant if set, otherwise the first active tenant.

    Retrain is a platform-wide system task, but `api_task_queue.tenant_id`
    is NOT NULL. The convention used elsewhere (see
    `routers.node_definitions._auto_calibrate_new_node_hook`) is
    requested → user.tenant_id → first-active fallback. Callers pass in
    `current_user.tenant_id` for the manual endpoint and rely on the
    fallback for the scheduler tick.
    """
    if requested is not None:
        return requested
    # Tenant uses soft-delete (`deleted_at IS NULL` = active), not an
    # `is_active` flag — same convention as `Tenant.deleted_at` in
    # models.py:338.
    fallback = (
        db.query(models.Tenant.id)
        .filter(models.Tenant.deleted_at.is_(None))
        .order_by(models.Tenant.id.asc())
        .first()
    )
    return fallback[0] if fallback else None


def _find_pending_retrain(db: Session) -> Optional[models.ApiTaskQueue]:
    """Return an in-flight retrain task if one exists, else None.

    "In-flight" = PENDING or CLAIMED or RUNNING. Used for idempotency: a
    second submission while one is already pending shouldn't queue a
    duplicate that would just sit behind the first.
    """
    # task_payload uses the project's custom JSONB TypeDecorator
    # (impl=JSON), so `op("->>")` binds the right side as JSON-typed and
    # breaks the lookup. Same gotcha caught in CORE/ML/data_exporter.py;
    # text() is the working form.
    in_flight = (models.ApiTaskStatus.PENDING, models.ApiTaskStatus.CLAIMED, models.ApiTaskStatus.RUNNING)
    return (
        db.query(models.ApiTaskQueue)
        .filter(models.ApiTaskQueue.status.in_(in_flight))
        .filter(text("task_payload ->> 'node_kind' = :node_kind").bindparams(node_kind=ML_RETRAIN_NODE_KIND))
        .order_by(models.ApiTaskQueue.id.desc())
        .first()
    )


def _resolve_retrain_node(db: Session) -> Optional[models.NodeDefinition]:
    """Look up the seeded ml_retrain NodeDefinition (system row).

    Returns the system catalog row (`user_id IS NULL`). Per-tenant copies
    that the scanner provisions are also OK as a fallback — they share
    the same executable_path."""
    return (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.node_kind == ML_RETRAIN_NODE_KIND)
        .order_by(
            models.NodeDefinition.user_id.is_(None).desc(),  # prefer system row
            models.NodeDefinition.id.asc(),
        )
        .first()
    )


def submit_retrain_run(
    db: Session,
    *,
    force: bool = False,
    trigger_source: str = "manual",
    tenant_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Tuple[Optional[models.ApiTaskQueue], str]:
    """Enqueue an ml_retrain worker task. Returns (task, outcome).

    `outcome` is one of:
      - "submitted"         — new task enqueued, audit emitted
      - "already_in_flight" — another retrain task is PENDING/CLAIMED/RUNNING;
                              returned task is the existing one
      - "no_node"           — ml_retrain NodeDefinition is not seeded;
                              task is None
      - "no_tenant"         — no tenant resolvable (system has no active
                              tenants); task is None

    Callers (`POST /calibration/retrain`, the auto_retrain_eval scheduler
    tick) decide how to surface each outcome to the user. Locking
    semantics (Redis `mod:retrain:lock`) stay with the callers; this
    helper provides a DB-level idempotency check as an additional guard.
    """
    node_def = _resolve_retrain_node(db)
    if node_def is None:
        logger.warning(
            "submit_retrain_run: no NodeDefinition with node_kind=%s — seed it before retrying",
            ML_RETRAIN_NODE_KIND,
        )
        return None, "no_node"

    existing = _find_pending_retrain(db)
    if existing is not None:
        logger.info(
            "submit_retrain_run: retrain task %d already %s — returning existing",
            existing.id, existing.status.value if hasattr(existing.status, "value") else existing.status,
        )
        return existing, "already_in_flight"

    effective_tenant = _resolve_tenant_id(db, tenant_id)
    if effective_tenant is None:
        logger.warning("submit_retrain_run: no active tenant available, cannot enqueue")
        return None, "no_tenant"

    image = node_def.executable_path or ML_RETRAIN_DEFAULT_IMAGE
    if "/" not in image:
        # Bare short name → qualify against the configured registry, same
        # rule as `_qualify_image_ref` in routers.node_definitions for
        # system catalog rows.
        registry = platform_registry()
        image = f"{registry}/modtim/{image}"
    image = ce_mirror_ref(image)

    # Queue routing follows the platform-wide rule: derive from
    # NodeDefinition.gpu_variants via select_queue_for_node. For
    # ml_retrain (1 variant: 'cuda') this produces queue_name='CUDA',
    # which the GPU-capable mod-worker subscribes to. Falling back to
    # 'CPU' for the (defensive) case where the seed forgot to set
    # gpu_variants would just mean retrain runs on CPU — slow but not
    # broken.
    try:
        from utils.queue_selector import select_queue_for_node
        queue_name = select_queue_for_node(node_def)
    except Exception:
        queue_name = "CUDA"

    task = models.ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"ml-retrain-{trigger_source}",
        tenant_id=effective_tenant,
        queue_name=queue_name,
        priority=1,
        priority_class="low",
        task_payload={
            "node_kind": ML_RETRAIN_NODE_KIND,
            "processing_image": image,
            "force": bool(force),
            "trigger_source": trigger_source,
            "node_definition_id": node_def.id,
            "gpu_required": True,
            "container_env": _build_container_env(trigger_source, force),
        },
        status=models.ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace=f"ml_retrain_{trigger_source}",
    )
    db.add(task)
    db.flush()

    try:
        db.add(models.AuditEvent(
            tenant_id=effective_tenant,
            user_id=user_id,
            event_name=RETRAIN_SUBMITTED_EVENT,
            details={
                "trigger_source": trigger_source,
                "force": bool(force),
                "task_id": task.id,
                "node_definition_id": node_def.id,
                "image": image,
            },
        ))
    except Exception as audit_err:
        # Audit emission shouldn't block retrain — log and proceed. The
        # task is already in the queue, which is the load-bearing
        # observability signal.
        logger.debug("AuditEvent emit failed for retrain submission %d: %s", task.id, audit_err)

    db.commit()

    # Best-effort worker wake-up, AFTER the commit so the Valkey call never
    # runs with an open transaction (same pattern as weights_mirror / cogs_shared).
    # Without it the mod-worker only finds this task on its next 30s poll.
    try:
        import redis

        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish(f"task:available:{task.queue_name}", "1")
    except Exception:  # noqa: BLE001 — wake-up must never fail the enqueue
        pass

    logger.info(
        "submit_retrain_run: enqueued task=%d trigger_source=%s force=%s",
        task.id, trigger_source, force,
    )
    return task, "submitted"

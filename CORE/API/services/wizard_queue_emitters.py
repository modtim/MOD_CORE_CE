"""Per-wizard emitters + native bridge for the Wizard Queue.

Each long-running wizard keeps its own execution path (design D12); this
module is the thin surfacing layer that writes a ``WizardRun`` for it,
reconciles the native status onto the canonical lifecycle (D13), mirrors
the native log tail into the persisted (redacted) log store (D19), and
registers the HARD-cancel / retry hooks (D14/D21).

Native substrates (from the codebase map):
  * node / stack / container-adapter / build-repair -> a build
    ``WorkflowRun`` (+ child ``ApiTaskQueue`` rows drained by the Go
    worker). native_source = ``workflow_run``, native_ref = the
    workflow_run_id.
  * agent / project -> a ``SupportChatSession`` tree whose child builds
    are ``WorkflowRun``s stamped with ``project_session_id``. The parent
    agent-project run links its child node-build runs via
    ``WizardRun.parent_run_id`` so a HARD cancel cascades (D21).

Every call site MUST wrap these emitters in try/except so surfacing can
never break a real build. The reconcile bridge is idempotent.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

import models
from models import (
    WizardRun,
    WizardRunType,
    WizardRunStatus,
)
from services import wizard_queue as svc

logger = logging.getLogger(__name__)

NATIVE_WORKFLOW_RUN = "workflow_run"
NATIVE_PROJECT_SESSION = "project_session"

# target_ref_type used to bridge an ApiTaskQueue-backed wizard run (today:
# container-adapter) back to its WizardRun. These dispatches have no
# workflow_run_id to key on via native_source/native_ref, so they're
# linked through target_ref_type/target_ref_id instead (GE.WQR.5).
TARGET_REF_TASK = "task"

# target_ref_type used to bridge a WorkflowRun-backed NODE build back to the
# NodeDefinition it built (emit_build_run stamps it); `_retry_build` reads it
# back to resolve the rebuild context.
TARGET_REF_NODE = "node"

# Rebuild scopes (services/node_build_dispatch.SCOPE_*). A build with no
# recorded scope rebuilds the whole fan-out — the no-variant default.
SCOPE_SINGLE_VARIANT = "single_variant"
SCOPE_FULL_FANOUT = "full_fanout"


# ---------------------------------------------------------------------
# Native status -> canonical lifecycle
# ---------------------------------------------------------------------
# The node-build poll endpoint already collapses WorkflowRun/ApiTaskQueue
# status into this FE vocabulary; we map that onto the canonical set.
_NODE_BUILD_STATUS_MAP = {
    "pending": WizardRunStatus.QUEUED,
    "queued": WizardRunStatus.QUEUED,
    "waiting-for-worker": WizardRunStatus.WAITING_FOR_WORKER,
    "running": WizardRunStatus.RUNNING,
    "verifying": WizardRunStatus.VERIFYING,
    "completed": WizardRunStatus.SUCCEEDED,
    "success": WizardRunStatus.SUCCEEDED,
    "succeeded": WizardRunStatus.SUCCEEDED,
    "failed": WizardRunStatus.FAILED,
    "failure": WizardRunStatus.FAILED,
    "canceled": WizardRunStatus.CANCELED,
    "cancelled": WizardRunStatus.CANCELED,
}

_PROGRESS_FOR_STATUS = {
    WizardRunStatus.QUEUED: 5,
    WizardRunStatus.WAITING_FOR_WORKER: 10,
    WizardRunStatus.RUNNING: 50,
    WizardRunStatus.VERIFYING: 85,
    WizardRunStatus.SUCCEEDED: 100,
}


def map_native_status(native: str) -> Optional[WizardRunStatus]:
    return _NODE_BUILD_STATUS_MAP.get((native or "").strip().lower())


# ---------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------
def emit_build_run(
    db: Session,
    *,
    wizard_type: WizardRunType,
    workflow_run_id,
    target_name: Optional[str] = None,
    target_ref_type: Optional[str] = None,
    target_ref_id: Optional[str] = None,
    creator: Optional[models.User] = None,
    system: bool = False,
    tenant_id: Optional[int] = None,
    owner_user_id: Optional[int] = None,
    parent_run_id: Optional[str] = None,
    log_source: str = "container_build",
    commit: bool = True,
) -> Optional[WizardRun]:
    """Surface a WorkflowRun-backed wizard build (node / stack /
    container-adapter / build-repair). Idempotent per workflow_run_id."""
    native_ref = str(workflow_run_id)
    existing = (
        db.query(WizardRun)
        .filter(
            WizardRun.native_source == NATIVE_WORKFLOW_RUN,
            WizardRun.native_ref == native_ref,
            WizardRun.deleted_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        return existing
    run = svc.create_run(
        db,
        wizard_type=wizard_type,
        target_name=target_name,
        target_ref_type=target_ref_type,
        target_ref_id=target_ref_id,
        creator=creator,
        system=system,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        parent_run_id=parent_run_id,
        log_source=log_source,
        native_source=NATIVE_WORKFLOW_RUN,
        native_ref=native_ref,
        status=WizardRunStatus.QUEUED,
        commit=commit,
    )
    return run


def emit_agent_project_run(
    db: Session,
    *,
    project_session_id: str,
    target_name: Optional[str] = None,
    creator: Optional[models.User] = None,
    system: bool = False,
    commit: bool = True,
) -> Optional[WizardRun]:
    """Surface the parent Autonomous Agent / Project run. Its child node
    builds are emitted via emit_build_run(parent_run_id=<this>.id) so a
    HARD cancel cascades to them (D21)."""
    native_ref = str(project_session_id)
    existing = (
        db.query(WizardRun)
        .filter(
            WizardRun.native_source == NATIVE_PROJECT_SESSION,
            WizardRun.native_ref == native_ref,
            WizardRun.deleted_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        return existing
    return svc.create_run(
        db,
        wizard_type=WizardRunType.AGENT_PROJECT,
        target_name=target_name,
        target_ref_type="project",
        target_ref_id=native_ref,
        creator=creator,
        system=system,
        log_source="llm_step",
        native_source=NATIVE_PROJECT_SESSION,
        native_ref=native_ref,
        status=WizardRunStatus.RUNNING,
        commit=commit,
    )


def emit_container_task_run(
    db: Session,
    *,
    wizard_type: WizardRunType,
    task_id,
    target_name: Optional[str] = None,
    creator: Optional[models.User] = None,
    system: bool = False,
    tenant_id: Optional[int] = None,
    owner_user_id: Optional[int] = None,
    log_source: str = "container_build",
    commit: bool = True,
) -> Optional[WizardRun]:
    """Surface an ApiTaskQueue-backed wizard run (container-adapter
    today). Linked via target_ref_type="task"/target_ref_id=<task_id>
    rather than the native_source/native_ref bridge emit_build_run uses,
    since these dispatches have no workflow_run_id. Idempotent per
    task_id. Status starts QUEUED; reconcile_task_status() below
    advances it as the task itself progresses."""
    ref = str(task_id)
    existing = (
        db.query(WizardRun)
        .filter(
            WizardRun.target_ref_type == TARGET_REF_TASK,
            WizardRun.target_ref_id == ref,
            WizardRun.deleted_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        return existing
    return svc.create_run(
        db,
        wizard_type=wizard_type,
        target_name=target_name,
        target_ref_type=TARGET_REF_TASK,
        target_ref_id=ref,
        creator=creator,
        system=system,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        log_source=log_source,
        status=WizardRunStatus.QUEUED,
        commit=commit,
    )


# ---------------------------------------------------------------------
# Native status / log reconcile (poll bridge)
# ---------------------------------------------------------------------
def reconcile_workflow_run_status(
    db: Session,
    workflow_run_id,
    native_status: str,
    log_tail: Optional[str] = None,
    *,
    commit: bool = True,
) -> Optional[WizardRun]:
    """Map a build WorkflowRun's current native status + log tail onto
    its WizardRun. Safe to call on every progress poll: it only writes
    when something actually changed. Returns None when no WizardRun is
    bridged to this workflow run (e.g. legacy runs — no backfill, D16)."""
    native_ref = str(workflow_run_id)
    run = (
        db.query(WizardRun)
        .filter(
            WizardRun.native_source == NATIVE_WORKFLOW_RUN,
            WizardRun.native_ref == native_ref,
            WizardRun.deleted_at.is_(None),
        )
        .first()
    )
    if run is None:
        return None

    # Never resurrect a run the queue already terminated (e.g. a HARD
    # cancel) just because the native poll still says "running".
    from models import WIZARD_TERMINAL_STATUSES
    if run.status in WIZARD_TERMINAL_STATUSES:
        return run

    canonical = map_native_status(native_status)

    # Mirror the NEW portion of the native log tail as redacted lines.
    if log_tail:
        extra = dict(run.extra or {})
        synced = int(extra.get("log_tail_len", 0) or 0)
        if len(log_tail) > synced:
            delta = log_tail[synced:]
            svc.append_log(db, run, delta, level="info", commit=False)
            extra["log_tail_len"] = len(log_tail)
            run.extra = extra
            db.flush()

    if canonical is not None and canonical != run.status:
        progress = _PROGRESS_FOR_STATUS.get(canonical)
        svc.set_status(db, run, canonical, progress=progress, commit=False)

    if commit:
        db.commit()
    return run


def reconcile_task_status(
    db: Session,
    task_id,
    native_status: str,
    *,
    commit: bool = True,
) -> Optional[WizardRun]:
    """Map an ApiTaskQueue task's current native status onto its
    WizardRun (bridged via target_ref_type="task"/target_ref_id, see
    emit_container_task_run). Safe to call from the task-completion /
    failure paths: a no-op when no WizardRun is linked to this task
    (the overwhelming majority of tasks aren't wizard-surfaced)."""
    ref = str(task_id)
    run = (
        db.query(WizardRun)
        .filter(
            WizardRun.target_ref_type == TARGET_REF_TASK,
            WizardRun.target_ref_id == ref,
            WizardRun.deleted_at.is_(None),
        )
        .first()
    )
    if run is None:
        return None

    # Never resurrect a run the queue already terminated (e.g. a HARD
    # cancel) just because a late/duplicate report still comes in.
    if run.status in models.WIZARD_TERMINAL_STATUSES:
        return run

    canonical = map_native_status(native_status)
    if canonical is not None and canonical != run.status:
        progress = _PROGRESS_FOR_STATUS.get(canonical)
        svc.set_status(db, run, canonical, progress=progress, commit=False)

    if commit:
        db.commit()
    return run


# ---------------------------------------------------------------------
# HARD cancel hooks
# ---------------------------------------------------------------------
def _kill_workflow_run(db: Session, run: WizardRun) -> None:
    """Cooperative HARD cancel of a build WorkflowRun: mark the run
    failed/terminal and flip all active child ApiTaskQueue rows to
    CANCELLED so unclaimed work never starts. (There is no live
    container-kill channel in this stack; a container already executing
    runs to completion and its result is discarded.)"""
    if not run.native_ref:
        return
    try:
        wf_run_id = int(run.native_ref)
    except (TypeError, ValueError):
        return

    wf = (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.id == wf_run_id)
        .first()
    )
    if wf is not None:
        try:
            if wf.status in (models.TaskStatus.QUEUED, models.TaskStatus.RUNNING):
                wf.status = models.TaskStatus.FAILED
                if hasattr(wf, "error_message"):
                    wf.error_message = "Canceled via Wizard Queue (hard)"
        except Exception:  # noqa: BLE001
            pass

    active = (
        models.ApiTaskStatus.PENDING,
        models.ApiTaskStatus.ASSIGNED,
        models.ApiTaskStatus.CLAIMED,
        models.ApiTaskStatus.RUNNING,
    )
    (
        db.query(models.ApiTaskQueue)
        .filter(
            models.ApiTaskQueue.workflow_run_id == wf_run_id,
            models.ApiTaskQueue.status.in_(active),
        )
        .update({models.ApiTaskQueue.status: models.ApiTaskStatus.CANCELLED},
                synchronize_session=False)
    )
    db.flush()


def _kill_project_session(db: Session, run: WizardRun) -> None:
    """The parent agent/project run has no task of its own to kill — its
    work lives in child node-build runs, which the queue cascades to via
    WizardRun.parent_run_id (each child's own workflow_run hook does the
    real kill). Nothing extra to do here."""
    return


# ---------------------------------------------------------------------
# Retry hooks (fresh re-dispatch)  — GE.WQR.10
# ---------------------------------------------------------------------
# STACK, CONTAINER_ADAPTER and the WorkflowRun-backed builds (NODE /
# BUILD_REPAIR / AGENT_PROJECT) ALL get a REAL re-dispatch below (owner
# decision C5=A): `_retry_build` resolves the original build's
# NodeDefinition + NodeSourceRevision (target_ref_type="node"/target_ref_id
# + services.node_source_revisions.latest) and calls
# services.node_build_dispatch.dispatch_rebuild with the SAME scope/variant
# the original build used, so clicking Retry actually starts a new build
# rather than leaving a QUEUED row nobody will ever run.
# Every hook here is called from services.wizard_queue.retry_run with the
# WizardRun write already open (commit=False) — it must never commit and
# must never let an exception escape uncaught, since retry must always be
# able to leave the new run in SOME terminal-or-queued state rather than
# hang the caller or bubble into a 500.
def _retry_build(db: Session, original: WizardRun, new: WizardRun) -> None:
    """Fresh re-dispatch for a WorkflowRun-backed build (NODE / BUILD_REPAIR
    / AGENT_PROJECT). The retry-as-a-new-linked-run mechanics (new WizardRun,
    retry_of, original keeps its terminal state) are done by the service;
    this hook triggers the native re-dispatch itself: it resolves the
    ORIGINAL build's NodeDefinition + NodeSourceRevision and calls
    ``services.node_build_dispatch.dispatch_rebuild`` with the SAME scope
    (single_variant / full_fanout) and variant the original build used, so
    Retry really starts a new build.

    Context resolution (owner decision C5=A):
      * node  — ``original.target_ref_type == "node"`` + ``target_ref_id``
                (what node_definitions_node_wizard_service.emit_build_run
                stamps), looked up live so a soft-deleted node is rejected;
      * revision — ``services.node_source_revisions.latest(db, node.id)``,
                the same latest-non-deleted revision the /{node_id}/rebuild
                endpoint rebuilds from;
      * scope/variant — stashed on ``extra["rebuild_scope"]`` /
                ``extra["rebuild_variant"]`` when the build is emitted, else
                the original run's own ``extra`` (retry_run copies it onto
                ``new``), else full_fanout (the no-variant default).

    Never leaves the new run hanging QUEUED: a vanished context (node
    deleted, no revision, unresolvable ids) or a raising dispatch marks the
    new run FAILED with a plain-English reason. Nothing here commits (the
    caller holds the write open) and no exception ever escapes.
    """
    node = _resolve_build_node(db, original)
    if node is None:
        _retry_build_failed(
            db, new,
            "original build context no longer exists (node deleted or source "
            "revision missing) — start a new build",
        )
        return

    revision = None
    try:
        from services import node_source_revisions as nsr
        revision = nsr.latest(db, node.id)
    except Exception as e:  # noqa: BLE001
        logger.warning("wizard-queue retry: revision lookup failed for node %s: %s",
                       node.id, e)

    if revision is None or getattr(revision, "hero", None) in ({}, None):
        _retry_build_failed(
            db, new,
            "original build context no longer exists (node deleted or source "
            "revision missing) — start a new build",
        )
        return

    extra = dict(original.extra or {}) if isinstance(original.extra, dict) else {}
    scope = extra.get("rebuild_scope") or SCOPE_FULL_FANOUT
    variant = extra.get("rebuild_variant")

    try:
        import services.node_build_dispatch as node_build_dispatch
        result = node_build_dispatch.dispatch_rebuild(
            db,
            node=node,
            revision=revision,
            scope=scope,
            variant=variant,
            user_id=(original.owner_user_id
                     if original.owner_user_id is not None else new.owner_user_id),
        )
    except Exception as e:  # noqa: BLE001 — retry must never bubble a 500
        logger.warning(
            "wizard-queue retry: dispatch_rebuild failed for run %s (node %s): %s",
            new.id, node.id, e,
        )
        _retry_build_failed(
            db, new,
            f"{type(e).__name__}: {e}",
        )
        return

    run_id = None
    if isinstance(result, dict):
        run_id = result.get("workflow_run_id")
    # Re-point the new run's native bridge at the FRESH build run so
    # reconcile/kill follow it going forward (retry_run copied native_source
    # but left native_ref empty). target_ref_type/target_ref_id stay on the
    # NODE so retrying a retry still resolves the rebuild context.
    if run_id is not None:
        new.native_ref = str(run_id)
    svc.append_log(
        db, new,
        f"retry re-dispatched as build run {run_id} (scope={scope}" \
        f"{', variant=' + str(variant) if variant else ''})",
        level="info", commit=False)


def _retry_build_failed(db: Session, new: WizardRun, reason: str) -> None:
    """Shared FAILED terminal marking for `_retry_build` — a clear plain-
    English reason, never a hanging QUEUED row, never a commit."""
    svc.append_log(db, new, f"retry failed: {reason}", level="error", commit=False)
    svc.set_status(db, new, WizardRunStatus.FAILED, error_message=reason, commit=False)


def _resolve_build_node(db: Session, original: WizardRun) -> Optional[models.NodeDefinition]:
    """The live (non-soft-deleted) NodeDefinition the original build ran on,
    or None when the context is gone. NODE builds are linked through
    ``target_ref_type="node"`` / ``target_ref_id`` (the id
    emit_build_run stamps); a run with no such link has no rebuild context."""
    if original.target_ref_type != TARGET_REF_NODE or not original.target_ref_id:
        return None
    try:
        node_id = int(original.target_ref_id)
    except (TypeError, ValueError):
        return None
    node = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.id == node_id,
                models.NodeDefinition.deleted_at.is_(None))
        .first()
    )
    return node


def _retry_stack(db: Session, original: WizardRun, new: WizardRun) -> None:
    """STACK retry: re-run the wizard LLM pre-fill with the ORIGINAL
    prompt.

    The prompt is stashed in `original.extra["prompt"]` at emit time
    (routers/persistent_stacks/persistent_stacks.py:create_from_wizard).
    The WizardRun schema has no literal `log_source_descriptor` column —
    that name is the original design doc's (wizard-queue-redesign
    tasks.md 1.2) label for what became the `log_source` field, a short
    native-log-kind tag ("llm_step"/"container_build"), which is far too
    narrow (String(50)) to hold a free-text prompt. `extra` (JSONB) is
    the field actually meant for arbitrary per-run payload, so that's
    where the prompt lives and where retry reads it back from.

    The LLM stack can be OFF: `call_wizard_llm` may raise or time out.
    This hook NEVER lets that escape — on any failure the new run is
    marked FAILED with a message so the queue shows a clean, terminal
    result instead of hanging in QUEUED forever or bubbling a 500 out of
    the retry endpoint.
    """
    prompt = None
    try:
        prompt = (original.extra or {}).get("prompt")
    except Exception:  # noqa: BLE001 — extra malformed on an old row
        prompt = None
    if not prompt:
        svc.append_log(db, new,
                       "retry failed: no original prompt stored on this run",
                       level="error", commit=False)
        svc.set_status(db, new, WizardRunStatus.FAILED,
                       error_message="original prompt not available for retry",
                       commit=False)
        return

    try:
        from utils.llm_client import call_wizard_llm
        from routers.persistent_stacks._shared import _WIZARD_SYSTEM
    except Exception as e:  # noqa: BLE001
        svc.append_log(db, new, f"retry setup error: {e}",
                       level="error", commit=False)
        svc.set_status(db, new, WizardRunStatus.FAILED,
                       error_message=str(e), commit=False)
        return

    # Commit the new QUEUED run before the (up to 120 s) LLM call so no
    # transaction — and no PgBouncer backend — is held open across it.
    db.commit()

    try:
        result = call_wizard_llm(
            _WIZARD_SYSTEM,
            f"Create a persistent stack for: {prompt}",
            response_format="json", timeout_s=120,
            _label="persistent-stack-wizard-retry",
        )
    except Exception as e:  # noqa: BLE001 — e.g. the LLM stack is OFF
        svc.append_log(db, new, f"Wizard LLM error: {e}",
                       level="error", commit=False)
        svc.set_status(db, new, WizardRunStatus.FAILED,
                       error_message=str(e), commit=False)
        return

    # GA.ASW.3 — the wizard prompt is now catalog-grounded: `result` may carry
    # a `template_id` (+ `params`) instead of a raw `compose`. Resolve it
    # through the SAME catalog `/from-template` and the first-time dispatch
    # use, falling back to the raw `compose` (the original behaviour) when
    # the template_id is absent, "custom", or not a recognized catalog id.
    template_id = result.get("template_id") if isinstance(result, dict) else None
    compose = result.get("compose") if isinstance(result, dict) else None
    if isinstance(template_id, str) and template_id not in ("", "custom"):
        try:
            from services.service_templates import STACK_TEMPLATES, render_template
            if template_id in STACK_TEMPLATES:
                params = result.get("params") if isinstance(result.get("params"), dict) else {}
                compose, _storage_class = render_template(template_id, params)
        except Exception:  # noqa: BLE001 — fall back to raw compose (if any) below
            pass

    if not isinstance(compose, dict) or "services" not in compose:
        svc.append_log(db, new,
                       "retry failed: wizard LLM did not produce a valid "
                       "compose project",
                       level="error", commit=False)
        svc.set_status(db, new, WizardRunStatus.FAILED,
                       error_message="Wizard did not produce a valid compose "
                                     "project",
                       commit=False)
        return

    extra = dict(new.extra or {})
    extra["prompt"] = prompt
    extra["compose"] = compose
    new.extra = extra
    svc.append_log(db, new, "retry produced a fresh compose pre-fill",
                   level="info", commit=False)
    svc.set_status(db, new, WizardRunStatus.SUCCEEDED, commit=False)


def _retry_container_adapter(db: Session, original: WizardRun, new: WizardRun) -> None:
    """CONTAINER_ADAPTER retry: re-enqueue a FRESH ApiTaskQueue task via
    `_enqueue_container_task`, using the env stored on the ORIGINAL
    task's `task_payload["container_env"]` (looked up through
    `original.target_ref_id`, the task-id bridge `emit_container_task_run`
    sets up — see TARGET_REF_TASK above). `new` is then re-pointed at the
    freshly created task id so `reconcile_task_status()` picks up its
    progress going forward, same as a first-time dispatch.

    Never lets a lookup/dispatch failure escape: an unlinked run, a
    vanished original task, or an `_enqueue_container_task` error all
    mark the new run FAILED with a message rather than leaving it queued
    with nothing actually re-dispatched.
    """
    if original.target_ref_type != TARGET_REF_TASK or not original.target_ref_id:
        svc.append_log(db, new,
                       "retry failed: original run has no linked task",
                       level="error", commit=False)
        svc.set_status(db, new, WizardRunStatus.FAILED,
                       error_message="no linked task to retry", commit=False)
        return

    try:
        orig_task_id = int(original.target_ref_id)
    except (TypeError, ValueError):
        svc.append_log(db, new,
                       f"retry failed: bad linked task id "
                       f"{original.target_ref_id!r}",
                       level="error", commit=False)
        svc.set_status(db, new, WizardRunStatus.FAILED,
                       error_message="bad linked task id", commit=False)
        return

    orig_task = (
        db.query(models.ApiTaskQueue)
        .filter(models.ApiTaskQueue.id == orig_task_id)
        .first()
    )
    if orig_task is None:
        svc.append_log(db, new,
                       f"retry failed: original task {orig_task_id} not found",
                       level="error", commit=False)
        svc.set_status(db, new, WizardRunStatus.FAILED,
                       error_message="original task not found", commit=False)
        return

    payload = orig_task.task_payload or {}
    env = dict(payload.get("container_env") or {})
    node_kind_label = payload.get("node_kind") or "container_adapter"
    gpu_required = bool(payload.get("gpu_required", False))

    try:
        # Local import — routers.node_definitions_domains._shared is a
        # router-layer module; importing it lazily here (only when a
        # CONTAINER_ADAPTER retry actually fires) avoids a module-load-time
        # dependency from the service layer on the router layer.
        from routers.node_definitions_domains._shared import _enqueue_container_task
        new_task_id = _enqueue_container_task(
            db, original.tenant_id,
            # The only CONTAINER_ADAPTER dispatch site
            # (node_authoring.py:generate_dockerfile) always uses this
            # image; the stored payload only has the resolved/canonical
            # ref, not the raw one _enqueue_container_task expects.
            "containeradapter:latest",
            env,
            node_kind_label,
            queue_name=orig_task.queue_name or "CPU",
            gpu_required=gpu_required,
            max_attempts=orig_task.max_attempts or 3,
        )
    except Exception as e:  # noqa: BLE001
        svc.append_log(db, new, f"retry dispatch error: {e}",
                       level="error", commit=False)
        svc.set_status(db, new, WizardRunStatus.FAILED,
                       error_message=str(e), commit=False)
        return

    new.target_ref_type = TARGET_REF_TASK
    new.target_ref_id = str(new_task_id)
    svc.append_log(db, new, f"retry re-enqueued as task {new_task_id}",
                   level="info", commit=False)


# ---------------------------------------------------------------------
# Registration — imported for side effects at app startup (main.py) and
# in tests.
# ---------------------------------------------------------------------
def register_hooks() -> None:
    svc.register_cancel_hook(NATIVE_WORKFLOW_RUN, _kill_workflow_run)
    svc.register_cancel_hook(NATIVE_PROJECT_SESSION, _kill_project_session)
    for wt in (
        WizardRunType.NODE,
        WizardRunType.AGENT_PROJECT,
        WizardRunType.BUILD_REPAIR,
    ):
        svc.register_retry_hook(wt, _retry_build)
    svc.register_retry_hook(WizardRunType.STACK, _retry_stack)
    svc.register_retry_hook(WizardRunType.CONTAINER_ADAPTER,
                            _retry_container_adapter)


# Register on import so any code path that surfaces a run also gets the
# cancel/retry behaviour.
register_hooks()

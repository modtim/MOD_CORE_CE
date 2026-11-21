"""Stack-backend dispatch seam (k8s-reduction-engine W15).

The product persistent-stack lifecycle (deploy / start=wake / stop=sleep /
teardown / migrate) funnels through `routers.persistent_stacks._shared.
_dispatch_stack_task`, which enqueues a PLATFORM_TASK a docker worker claims
and runs as `docker compose ...`. This module is the one place that decides
whether an op instead goes to `K8sStackBackend` (design.md Seam 2).

Contract for callers:

    r = stack_backend_dispatch.dispatch_stack_op(db, tenant_id=..., stack_op=...,
                                                 project=..., compose_yaml=...,
                                                 stack_id=...)
    if r is NotHandled:
        ...existing docker path, unchanged...
    else:
        ...r is a K8sStackOpResult; the op already ran against the cluster...

`NotHandled` is returned whenever the effective backend is docker:
MOD_STACK_BACKEND unset/docker/unknown (env-only check, no DB access), or
MOD_STACK_BACKEND=k8s but the tenant lacks Cap.K8S_BACKEND
(`k8s_stack_backend_for` returns None). In docker mode this module touches
nothing, so the docker path is byte-for-byte unchanged.

In k8s mode (W18) the request-path hook uses `schedule_stack_op`: it records an
in-flight task row inside the caller's transaction and runs the K8s op AFTER
that transaction commits, off the request thread, with no DB session held
while the K8s API blocks. Because no worker completion handler will ever see
the task, the row transitions the docker completion handler
(`routers/worker_tasks.py` stack lifecycle update) would have applied are
mirrored by the deferred runner for deploy/migrate/teardown.
`dispatch_stack_op` remains as the synchronous form (scripts/tests only).

ISO 27001: teardown NEVER deletes the namespace and NEVER deletes PVCs
(`delete_volumes=False`); `K8sStackBackend.teardown` only removes the stack's
Deployments/Services. Data survives; nothing is hard-deleted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from product_names import themed_logger

logger = themed_logger(__name__, 'stacks')

BACKEND_DOCKER = "docker"
BACKEND_K8S = "k8s"


class _NotHandledType:
    """Sentinel: the effective backend is docker — caller proceeds as today."""

    _instance: Optional["_NotHandledType"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NotHandled"

    def __bool__(self) -> bool:
        return False


NotHandled = _NotHandledType()


@dataclass
class K8sStackOpResult:
    """Outcome of a lifecycle op executed against `K8sStackBackend`."""
    stack_op: str
    project: str
    namespace: Optional[str]
    ok: bool
    detail: Any = None
    error: Optional[str] = None
    row_updates: Dict[str, Any] = field(default_factory=dict)


# Ops the docker worker understands (see _dispatch_stack_task callers).
_DEPLOY_OPS = ("deploy", "migrate")
_WAKE_OPS = ("start",)
_SLEEP_OPS = ("stop",)
_TEARDOWN_OPS = ("teardown",)


def stack_backend_mode() -> str:
    """"docker" | "k8s" from MOD_STACK_BACKEND (default docker; unknown ->
    docker + warning, via `utils.k8s_backend_config._normalize_backend`).
    Env-only: the per-tenant Cap.K8S_BACKEND gate is applied later by
    `k8s_stack_backend_for`.

    k8s_backend_config is a PAID module (k8s reduction): on CE it is absent, so
    the only possible backend is docker (the docker path is byte-for-byte the
    default there)."""
    from utils.paid_import import optional_paid
    _cfg = optional_paid("utils.k8s_backend_config")
    if _cfg is None:
        return BACKEND_DOCKER
    return BACKEND_K8S if _cfg.get_stack_backend() == BACKEND_K8S else BACKEND_DOCKER


def stack_op_dispatchable(db, row) -> bool:
    """W30: should a lifecycle op (stop/start from sleep-back, eviction, wake)
    be dispatched for `row`? The docker path needs a host to pin the op to, so
    it dispatches only `if row.target_worker`. A k8s-backed stack has no docker
    host (target_worker may be None) but `_dispatch_stack_task`'s W15/W18 hook
    routes the op to `K8sStackBackend` before `worker_name` is ever used — so in
    k8s mode the op is dispatchable without one.

    Docker mode (MOD_STACK_BACKEND unset/docker): exactly `bool(row.target_worker)`,
    env-only, no DB access — byte-identical to the old gate. k8s env but the
    tenant lacks Cap.K8S_BACKEND: same as docker (the hook would fall through
    to the docker path, where an unpinned stop must not be enqueued). Never
    raises: a gate error keeps the docker answer."""
    if getattr(row, "target_worker", None):
        return True
    if stack_backend_mode() != BACKEND_K8S:
        return False
    try:
        from services.k8s_backend_gate import effective_stack_backend
        return effective_stack_backend(db, getattr(row, "tenant_id", None)) == BACKEND_K8S
    except Exception:  # noqa: BLE001
        logger.warning("stack_backend_dispatch: k8s gate for stack %s failed; "
                       "treating as hostless docker (no dispatch)",
                       getattr(row, "id", None), exc_info=True)
        return False


_K8S_PLACEMENT_LOGGED = False


def k8s_owns_placement(db, tenant_id) -> bool:
    """W33: does the kube scheduler (not MOD's docker-host VRAM view) own GPU
    placement for `tenant_id`'s stacks? True only when MOD_STACK_BACKEND=k8s AND
    the tenant gate resolves to k8s. Then callers MUST NOT pick a docker host
    (`nim_ephemeral._pick_host`) nor evict "competitors" pinned to one
    (`nim_ephemeral._stop_competitors`, `stack_wake.plan_placement`/`_evict`):
    via the `_dispatch_stack_task` k8s hook those stops are real Deployment
    scale-downs of other stacks, decided from a docker host's inventory. On k8s
    GPU fit is the scheduler's + device plugin's job (no fit = pod Pending).

    Docker mode: False, env-only, no DB access (docker path byte-identical).
    Never raises: any error answers False (keep today's docker behaviour)."""
    global _K8S_PLACEMENT_LOGGED
    try:
        if stack_backend_mode() != BACKEND_K8S:
            return False
        from services.k8s_backend_gate import effective_stack_backend
        owns = effective_stack_backend(db, tenant_id) == BACKEND_K8S
    except Exception:  # noqa: BLE001
        logger.warning("stack_backend_dispatch: k8s placement gate for tenant %s "
                       "failed; keeping docker host placement", tenant_id,
                       exc_info=True)
        return False
    if owns and not _K8S_PLACEMENT_LOGGED:
        _K8S_PLACEMENT_LOGGED = True
        logger.info("stack_backend_dispatch: k8s stack backend owns GPU placement "
                    "— skipping docker host pick + competitor eviction "
                    "(kube scheduler/device plugin place NIM/stack pods)")
    return owns


def _default_factory(db, tenant_id):
    from utils.paid_import import optional_paid
    # k8s_stack_backend is a PAID module (k8s reduction): on CE there is no k8s
    # backend, so the factory yields None (the caller treats None as "not
    # handled" and proceeds down the docker path).
    _k8s_backend = optional_paid("services.k8s_stack_backend")
    if _k8s_backend is None:
        return None
    return _k8s_backend.k8s_stack_backend_for(db, tenant_id)


def _parse_compose(compose_yaml: Any) -> Dict[str, Any]:
    if isinstance(compose_yaml, dict):
        return compose_yaml
    if not compose_yaml:
        raise ValueError("deploy requires a compose spec")
    import yaml
    spec = yaml.safe_load(compose_yaml)
    if not isinstance(spec, dict):
        raise ValueError("compose spec did not parse to a mapping")
    return spec


def _load_row(db, stack, stack_id):
    if stack is not None or stack_id is None or db is None:
        return stack
    try:
        import models
        return (db.query(models.PersistentStack)
                .filter(models.PersistentStack.id == stack_id).first())
    except Exception:  # noqa: BLE001 — row mirroring is best-effort
        logger.debug("stack_backend_dispatch: row %s lookup failed", stack_id, exc_info=True)
        return None


def _deploy_to_running(row) -> bool:
    """W28: does this deploy carry to-running intent? A re-deploy of a stack
    that is meant to be DOWN (desired_state="stopped" — slept by stack_wake /
    NIM sleep-back / eviction, or stopped by the operator) is a spec update
    only: the backend keeps its live replicas so it is not silently woken.
    Every operator deploy sets desired_state="running" before commit, and the
    reconciler only redeploys running-intent stacks, so those still come up.
    No row (scripts) or a legacy NULL desired_state keeps the old behavior."""
    return row is None or getattr(row, "desired_state", None) != "stopped"


def _replicas_preserved(detail: Any) -> bool:
    return bool(isinstance(detail, dict) and detail.get("replicas_preserved"))


def _mirror_completion(row, stack_op: str, ok: bool, error: Optional[str],
                       *, preserved: bool = False) -> Dict[str, Any]:
    """Apply the same transitions the docker completion handler applies.
    `preserved` (W28): the deploy patched existing Deployments without
    touching their scale, so it must not claim "running" — the stack's run
    state is unchanged; only a stale failed/deploying status settles to the
    stopped state it was meant to be in."""
    updates: Dict[str, Any] = {}
    if stack_op in _DEPLOY_OPS and ok and preserved:
        if getattr(row, "status", None) in ("failed", "deploying"):
            updates = {"status": "stopped",
                       "status_reason": "redeployed while stopped (replicas preserved)"}
    elif stack_op in _DEPLOY_OPS:
        updates = ({"status": "running", "status_reason": None} if ok
                   else {"status": "failed", "status_reason": error or f"{stack_op} failed"})
    elif stack_op in _TEARDOWN_OPS:
        updates = ({"cleanup_pending": False, "status_reason": None} if ok
                   else {"cleanup_pending": True, "status_reason": error or "teardown failed"})
    if row is not None:
        for k, v in updates.items():
            setattr(row, k, v)
    return updates


def _execute_op(backend, stack_op: str, project: str, compose_yaml: Any,
                *, to_running: bool = True):
    """Run one op against the backend. Returns (ok, detail, error); backend
    exceptions are captured, never raised. `to_running` (W28) is the deploy's
    scale intent: False keeps an existing stack's live replicas."""
    namespace = getattr(backend, "namespace", None)
    ok, detail, error = True, None, None
    try:
        if stack_op in _DEPLOY_OPS:
            detail = backend.deploy(project, _parse_compose(compose_yaml),
                                    apply_replicas=to_running)
        elif stack_op in _WAKE_OPS:
            detail = backend.wake(project)
        elif stack_op in _SLEEP_OPS:
            detail = backend.sleep(project)
        elif stack_op in _TEARDOWN_OPS:
            # ISO 27001: keep PVCs + namespace; never hard-delete data.
            detail = backend.teardown(project, delete_volumes=False)
        else:
            ok, error = False, f"stack op {stack_op!r} is not supported on the k8s stack backend"
    except Exception as exc:  # noqa: BLE001
        ok, error = False, f"{type(exc).__name__}: {exc}"
        logger.error("stack_backend_dispatch: k8s %s %s/%s failed: %s",
                     stack_op, namespace, project, error)
    if ok:
        logger.info("stack_backend_dispatch: k8s %s %s/%s ok", stack_op, namespace, project)
    return ok, detail, error


def _resolve_backend(db, tenant_id, backend_factory):
    """Gate: the backend when the effective backend is k8s, else NotHandled.
    DB (capability) reads + local kubeconfig/SA loading only — no K8s API call."""
    if stack_backend_mode() != BACKEND_K8S:
        return NotHandled
    backend = (backend_factory or _default_factory)(db, tenant_id)
    return NotHandled if backend is None else backend


def dispatch_stack_op(db, *, tenant_id: Optional[int], stack_op: str, project: str,
                      compose_yaml: Any = None, stack_id: Optional[int] = None,
                      stack: Any = None,
                      backend_factory: Optional[Callable[[Any, Optional[int]], Any]] = None,
                      ):
    """SYNCHRONOUS form: route one lifecycle op and run it now. Returns
    `NotHandled` (docker path) or a `K8sStackOpResult`. Backend exceptions are
    captured into the result (ok=False) rather than raised.

    Do NOT call this while holding an open DB transaction you care about — it
    blocks on the K8s API. The request-path hook in `_dispatch_stack_task` uses
    `schedule_stack_op` (W18), which defers the K8s call until after commit."""
    backend = _resolve_backend(db, tenant_id, backend_factory)
    if backend is NotHandled:
        return NotHandled
    row = _load_row(db, stack, stack_id)
    to_running = _deploy_to_running(row)
    ok, detail, error = _execute_op(backend, stack_op, project, compose_yaml,
                                    to_running=to_running)
    updates = _mirror_completion(row, stack_op, ok, error,
                                 preserved=not to_running and _replicas_preserved(detail))
    return K8sStackOpResult(stack_op=stack_op, project=project,
                            namespace=getattr(backend, "namespace", None),
                            ok=ok, detail=detail, error=error, row_updates=updates)


# ---------------------------------------------------------------------------
# W18: deferred (after-commit, off-request-thread) execution
# ---------------------------------------------------------------------------
#
# Project lesson: a blocking external call inside an open DB transaction pins a
# PgBouncer backend for its whole duration (platform-wide outages). So on the
# request path the hook only RECORDS an in-flight task row inside the caller's
# transaction; the K8s API calls run after that transaction commits, on a
# daemon thread, with NO DB session open while they block. A fresh short
# session then mirrors the row transitions and finalizes the task.

_DEFERRED_INFO_KEY = "k8s_stack_deferred_jobs"
_LISTENER_INFO_KEY = "k8s_stack_deferred_listening"
DEFAULT_OP_TIMEOUT_S = 300.0


def _op_timeout_s() -> float:
    import os
    try:
        v = float(os.getenv("MOD_K8S_STACK_OP_TIMEOUT_S", "") or DEFAULT_OP_TIMEOUT_S)
        return v if v > 0 else DEFAULT_OP_TIMEOUT_S
    except ValueError:
        return DEFAULT_OP_TIMEOUT_S


def _default_session_factory():
    from database import SessionLocal
    return SessionLocal()


def _default_spawn(fn: Callable[[], None]) -> None:
    import threading
    threading.Thread(target=fn, name="k8s-stack-op", daemon=True).start()


# Injectable for tests (run synchronously / bind to the test session).
_session_factory: Callable[[], Any] = _default_session_factory
_spawn: Callable[[Callable[[], None]], None] = _default_spawn


@dataclass
class _DeferredJob:
    task_id: int
    backend: Any
    stack_op: str
    project: str
    compose_yaml: Any
    stack_id: Optional[int]
    timeout_s: float


def _call_with_timeout(fn: Callable[[], Any], timeout_s: float):
    """Run `fn` on a helper thread, waiting at most `timeout_s`. Raises
    TimeoutError on overrun (the helper is a daemon and is abandoned — the
    kubernetes client has no cooperative cancel)."""
    import threading
    box: Dict[str, Any] = {}

    def _target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=_target, name="k8s-stack-op-call", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"k8s stack op exceeded {timeout_s:.0f}s")
    if "exc" in box:
        raise box["exc"]
    return box.get("value")


def run_deferred_job(job: _DeferredJob) -> None:
    """Execute one deferred op end-to-end. Never raises: every outcome is
    logged and lands as a COMPLETED/FAILED task row (ISO: updated in place)."""
    import models
    from datetime import datetime, timezone

    # 1. Short session: confirm the row committed and is still in flight
    #    (a rolled-back SAVEPOINT or a second delivery must not run the op).
    try:
        db = _session_factory()
        try:
            task = db.query(models.ApiTaskQueue).filter(
                models.ApiTaskQueue.id == job.task_id).first()
            state = ("missing" if task is None else
                     "run" if task.status == models.ApiTaskStatus.RUNNING else "done")
            # W28: read the scale intent AFTER the caller committed (operator
            # deploy sets desired_state="running" after scheduling the op).
            to_running = (_deploy_to_running(_load_row(db, None, job.stack_id))
                          if job.stack_op in _DEPLOY_OPS else True)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("stack_backend_dispatch: deferred %s task %s pre-check failed",
                         job.stack_op, job.task_id)
        state, to_running = "error", True
    if state in ("missing", "done"):
        logger.warning("stack_backend_dispatch: deferred %s task %s %s — k8s op skipped",
                       job.stack_op, job.task_id,
                       "not found (transaction rolled back?)" if state == "missing"
                       else "no longer in flight")
        return

    # 2. The K8s API calls — NO DB session open while they block.
    if state == "run":
        try:
            ok, detail, error = _call_with_timeout(
                lambda: _execute_op(job.backend, job.stack_op, job.project, job.compose_yaml,
                                    to_running=to_running),
                job.timeout_s)
        except Exception as exc:  # noqa: BLE001 — timeout
            ok, detail, error = False, None, f"{type(exc).__name__}: {exc}"
            logger.error("stack_backend_dispatch: deferred k8s %s %s failed: %s",
                         job.stack_op, job.project, error)
    else:
        ok, detail, error = False, None, "pre-check failed; k8s op not run"

    # 3. Fresh short session: mirror the stack row + finalize the task row.
    try:
        db = _session_factory()
        try:
            task = db.query(models.ApiTaskQueue).filter(
                models.ApiTaskQueue.id == job.task_id).first()
            row = _load_row(db, None, job.stack_id)
            _mirror_completion(row, job.stack_op, ok, error,
                               preserved=not to_running and _replicas_preserved(detail))
            if task is not None:
                payload = dict(task.task_payload or {})
                payload["k8s_detail"] = repr(detail)[:2000]
                task.task_payload = payload
                task.status = (models.ApiTaskStatus.COMPLETED if ok
                               else models.ApiTaskStatus.FAILED)
                task.error_message = error
                task.completed_at = datetime.now(timezone.utc)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("stack_backend_dispatch: deferred %s task %s finalize failed "
                         "(k8s ok=%s) — row left RUNNING; the SYSTEM-queue orphan "
                         "reaper fails it", job.stack_op, job.task_id, ok)


def _on_after_commit(session) -> None:
    jobs = session.info.pop(_DEFERRED_INFO_KEY, None) or []
    for job in jobs:
        try:
            _spawn(lambda j=job: run_deferred_job(j))
        except Exception:  # noqa: BLE001
            logger.exception("stack_backend_dispatch: could not start deferred %s task %s",
                             job.stack_op, job.task_id)


def _on_after_rollback(session) -> None:
    # Outermost rollback: the in-flight rows are gone — drop their jobs.
    jobs = session.info.pop(_DEFERRED_INFO_KEY, None)
    if jobs:
        logger.info("stack_backend_dispatch: transaction rolled back — %d deferred "
                    "k8s op(s) discarded", len(jobs))


def _schedule_after_commit(db, job: _DeferredJob) -> None:
    """One-shot per job: queued on `db.info` and drained by the session's
    `after_commit` (fires only for the outermost commit). A rollback drops it.
    If the job's row sat in a SAVEPOINT that rolled back while the outer txn
    committed, the runner's pre-check finds no row and skips the op."""
    from sqlalchemy import event
    if not db.info.get(_LISTENER_INFO_KEY):
        event.listen(db, "after_commit", _on_after_commit)
        event.listen(db, "after_rollback", _on_after_rollback)
        db.info[_LISTENER_INFO_KEY] = True
    db.info.setdefault(_DEFERRED_INFO_KEY, []).append(job)


def schedule_stack_op(db, *, tenant_id: Optional[int], stack_op: str, project: str,
                      compose_yaml: Any = None, stack_id: Optional[int] = None,
                      backend_factory: Optional[Callable[[Any, Optional[int]], Any]] = None,
                      ):
    """Request-path form (W18). Returns `NotHandled` (docker path) or the
    in-flight `ApiTaskQueue` row; the K8s op runs after `db` commits.

    The row is born status=RUNNING, queue_name="SYSTEM", assigned_to_worker
    and claimed_by_worker NULL. Why that can never be worker-claimed:
    `routers.worker_tasks._try_claim_task` only selects
    (status=ASSIGNED AND assigned_to_worker=<me>) or
    (status=PENDING AND assigned_to_worker IS NULL); the stale-claim reaper
    only touches CLAIMED; the scheduler only assigns PENDING; the SYSTEM-queue
    drain (`services.system_tasks`) only runs PENDING. RUNNING+SYSTEM is exactly
    "executing inside an API process", so if the API dies mid-op the existing
    SYSTEM orphan reaper fails the row (grace 30 min > op timeout)."""
    backend = _resolve_backend(db, tenant_id, backend_factory)
    if backend is NotHandled:
        return NotHandled
    import models
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    task = models.ApiTaskQueue(
        node_id=f"stack-{stack_op}-{project}",
        tenant_id=tenant_id, queue_name="SYSTEM", priority=9, task_type="system",
        priority_class="normal",
        task_payload={"stack_op": stack_op, "stack_project": project,
                      "stack_id": stack_id, "node_kind": f"persistent-stack-{stack_op}",
                      "k8s_backend": True, "k8s_deferred": True,
                      "k8s_namespace": getattr(backend, "namespace", None)},
        status=models.ApiTaskStatus.RUNNING,
        assigned_to_worker=None, claimed_by_worker=None,
        claimed_at=now, started_at=now,
        max_attempts=1, output_namespace="attempt_0",
    )
    db.add(task)
    db.flush()
    _schedule_after_commit(db, _DeferredJob(
        task_id=task.id, backend=backend, stack_op=stack_op, project=project,
        compose_yaml=compose_yaml, stack_id=stack_id, timeout_s=_op_timeout_s()))
    return task


# ---------------------------------------------------------------------------
# W26: reconcile a stack whose deferred k8s op was orphaned by an API restart
# ---------------------------------------------------------------------------
#
# If the API dies between `schedule_stack_op`'s commit and the deferred
# runner's finalize, the SYSTEM-queue orphan reaper
# (`services.system_tasks.reap_orphaned_system_tasks`) fails the task row, but
# nothing mirrors the stack row, which would sit at "deploying" until the HA
# reconciler re-places it. The reaper hands its k8s-deferred orphans to
# `reconcile_reaped_k8s_stacks`, which reads the ACTUAL cluster state and
# applies the transition the runner would have: ready -> running, else failed
# (with a status_reason). Same discipline as `run_deferred_job`: fresh short
# sessions, and NO session open while the K8s API call blocks.

# Only deploy-class ops flip the row to an in-flight status ("deploying");
# wake/sleep/teardown leave nothing to reconcile.
_RECONCILE_OPS = _DEPLOY_OPS
_INFLIGHT_STACK_STATUS = "deploying"


def k8s_reap_targets(tasks: Any) -> list:
    """Pure: the reconcile targets among reaped task rows (k8s-deferred,
    deploy-class, with a stack id). Read these BEFORE the reaper commits —
    they are plain dicts, safe to use after the session moves on."""
    out = []
    for task in tasks or ():
        payload = getattr(task, "task_payload", None)
        if not (isinstance(payload, dict) and payload.get("k8s_backend") is True
                and payload.get("k8s_deferred") is True):
            continue
        if payload.get("stack_op") not in _RECONCILE_OPS or payload.get("stack_id") is None:
            continue
        out.append({"task_id": getattr(task, "id", None),
                    "tenant_id": getattr(task, "tenant_id", None),
                    "stack_id": payload.get("stack_id"),
                    "stack_op": payload.get("stack_op"),
                    "project": payload.get("stack_project")})
    return out


def reconcile_stack_from_k8s(target: Dict[str, Any], *,
                             backend_factory: Optional[Callable[[Any, Optional[int]], Any]] = None,
                             timeout_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Reconcile one reaped target's stack row from `K8sStackBackend.status()`.
    Returns the applied row updates, or None when nothing was changed (row
    gone / no longer in flight). Never raises."""
    import models
    stack_id, task_id = target.get("stack_id"), target.get("task_id")
    factory = backend_factory or _default_factory
    project = target.get("project")

    # 1. Short session: row still in flight? + build the backend (DB capability
    #    reads / kubeconfig load only — no K8s API call).
    backend, err = None, None
    try:
        db = _session_factory()
        try:
            row = _load_row(db, None, stack_id)
            if row is None or getattr(row, "status", None) != _INFLIGHT_STACK_STATUS:
                return None
            if not project:
                project = _row_kwargs(row)["project"]
            try:
                backend = factory(db, target.get("tenant_id"))
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("stack_backend_dispatch: reaped task %s stack %s pre-check failed",
                         task_id, stack_id)
        return None

    # 2. The K8s status read — NO DB session open.
    st: Dict[str, Any] = {}
    if backend is None:
        err = err or "k8s stack backend no longer available for this tenant"
    else:
        try:
            st = _call_with_timeout(lambda: backend.status(project),
                                    timeout_s or _op_timeout_s()) or {}
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"

    if st.get("ready") and err is None:
        updates: Dict[str, Any] = {"status": "running", "status_reason": None}
    else:
        detail = err or (f"cluster reports {st.get('ready_replicas', 0)}/"
                         f"{st.get('desired', 0)} ready replicas")
        updates = {"status": "failed",
                   "status_reason": (f"k8s {target.get('stack_op')} orphaned by an API "
                                     f"restart (task {task_id}); {detail}")[:1000]}

    # 3. Fresh short session: apply only if still in flight (the reconciler or
    #    a new deploy may have moved it meanwhile).
    try:
        db = _session_factory()
        try:
            row = _load_row(db, None, stack_id)
            if row is None or getattr(row, "status", None) != _INFLIGHT_STACK_STATUS:
                return None
            for k, v in updates.items():
                setattr(row, k, v)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("stack_backend_dispatch: reaped task %s stack %s reconcile "
                         "write failed", task_id, stack_id)
        return None
    logger.warning("stack_backend_dispatch: stack %s (%s) reconciled from k8s after "
                   "orphaned task %s -> %s", stack_id, project, task_id, updates["status"])
    return updates


def reconcile_reaped_k8s_stacks(targets: Any, *, backend_factory=None) -> int:
    """Reaper hook: reconcile each target OFF the caller's thread/session (the
    reaper runs on startup maintenance / the scheduler with a session in hand).
    Returns the number of targets handed off. Never raises."""
    targets = list(targets or ())
    if not targets:
        return 0

    def _run():
        for t in targets:
            reconcile_stack_from_k8s(t, backend_factory=backend_factory)
    try:
        _spawn(_run)
    except Exception:  # noqa: BLE001
        logger.exception("stack_backend_dispatch: could not start reaped-stack reconcile")
        return 0
    return len(targets)


def _row_kwargs(stack) -> Dict[str, Any]:
    refs = getattr(stack, "storage_refs", None) or {}
    project = refs.get("project") or f"modstack{stack.id}g0"  # == _project_name
    return {"tenant_id": getattr(stack, "tenant_id", None), "project": project,
            "stack_id": getattr(stack, "id", None), "stack": stack}


def deploy_stack(db, stack, compose_yaml: Any, *, backend_factory=None):
    return dispatch_stack_op(db, stack_op="deploy", compose_yaml=compose_yaml,
                             backend_factory=backend_factory, **_row_kwargs(stack))


def wake_stack(db, stack, *, backend_factory=None):
    return dispatch_stack_op(db, stack_op="start", backend_factory=backend_factory,
                             **_row_kwargs(stack))


def sleep_stack(db, stack, *, backend_factory=None):
    return dispatch_stack_op(db, stack_op="stop", backend_factory=backend_factory,
                             **_row_kwargs(stack))


def teardown_stack(db, stack, *, backend_factory=None):
    return dispatch_stack_op(db, stack_op="teardown", backend_factory=backend_factory,
                             **_row_kwargs(stack))


__all__ = [
    "NotHandled", "K8sStackOpResult", "stack_backend_mode", "stack_op_dispatchable",
    "dispatch_stack_op", "is_k8s_task",
    "is_k8s_backend_task", "schedule_stack_op", "run_deferred_job",
    "deploy_stack", "wake_stack", "sleep_stack", "teardown_stack",
    "k8s_reap_targets", "reconcile_stack_from_k8s", "reconcile_reaped_k8s_stacks",
]


def is_k8s_backend_task(task: Any) -> bool:
    """True for any row the k8s stack backend recorded (in flight or done)."""
    payload = getattr(task, "task_payload", None)
    return isinstance(payload, dict) and payload.get("k8s_backend") is True


def is_k8s_task(task: Any) -> bool:
    """True when `task` is a k8s-backend row whose op has ALREADY finished, so
    the deploy-site guards skip the docker "deploying" flip (it would clobber
    the mirrored running/failed). W18: request-path rows are deferred
    (`k8s_deferred`, status RUNNING) — this returns False for them, so deploy
    sites set status="deploying" as in docker mode, and the deferred runner
    flips it to running/failed after commit (the docker completion-handler
    equivalent)."""
    if not is_k8s_backend_task(task):
        return False
    if task.task_payload.get("k8s_deferred") is not True:
        return True
    status = getattr(task, "status", None)
    status = getattr(status, "value", status)
    return status in ("completed", "failed", "expired", "cancelled")

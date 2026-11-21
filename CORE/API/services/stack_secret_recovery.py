"""Recover stacks whose secret files were lost with the host's tmpfs (reboot).

A containerized worker materialises a stack's `secret_file` variables under
/run/mod-secrets, which is tmpfs: every host or WSL reboot wipes it. The
containers come back (restart: unless-stopped) but docker cannot start a
container whose bind source file is gone, so the stack stays down.

The worker already notices (it flags `secrets_missing` for the project in its
heartbeat, see Worker/cmd/worker/main.go). The re-dispatch of the stack's own
deploy, which carries fresh secrets in its payload, used to live only in
`stack_observation.record_observations`, a module that is not part of the
Community Edition. This small module is the edition-neutral copy of that one
step so a CE install recovers by itself with no manual action. When the fuller
observation module is present it does the same job and this is not called.

Never reads, logs or stores a secret value: the deploy payload is built by the
same helpers an operator-triggered deploy uses.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

# A new recovery deploy for the same stack is not queued sooner than this after
# the previous deploy task was created (heartbeats arrive every ~15 s and a
# failed deploy must not turn into a hot loop).
RECOVERY_BACKOFF_SECONDS = 120

_PROJECT_RE = re.compile(r"^modstack(\d+)")

# Stack states in which an automatic redeploy must NOT fire (an operator stop,
# a teardown, or a deploy/migration already underway).
_SKIP_STATUS = ("stopped", "torn_down", "migrating", "deploying")


def _stack_for_project(db: Session, project: str) -> Optional["models.PersistentStack"]:
    from routers.persistent_stacks import _project_name

    m = _PROJECT_RE.match(project or "")
    if not m:
        return None
    stack = db.query(models.PersistentStack).get(int(m.group(1)))
    if stack is None or _project_name(stack) != project:
        return None
    return stack


def _recently_dispatched(db: Session, stack_id: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=RECOVERY_BACKOFF_SECONDS)
    q = (db.query(models.ApiTaskQueue)
         .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                 models.ApiTaskQueue.task_payload["stack_op"].as_string() == "deploy",
                 models.ApiTaskQueue.created_at >= cutoff))
    return q.first() is not None


def redeploy_for_lost_secrets(db: Session, worker_name: str,
                              stack: "models.PersistentStack") -> bool:
    """Queue the stack's own deploy, pinned to `worker_name`. True if queued.

    Guards: the operator wants it running, it is assigned to this worker,
    no deploy is in flight, and the backoff has elapsed.
    """
    from crud import persistent_stacks_domain as psd_crud
    from routers.persistent_stacks import (
        _aggregate_accelerator_demand, _compose_yaml_with_vars,
        _dispatch_stack_task, _project_name, _provision_mesh_identity,
        _resolve_stack_secrets)
    from services import stack_variables as _sv

    if (stack.desired_state or "running") != "running":
        return False
    if stack.status in _SKIP_STATUS:
        return False
    if not stack.target_worker or stack.target_worker != worker_name:
        # Review H1 (11-20): only the stack's OWN assigned worker may pull its
        # deploy (and its secret files). An unassigned stack, or any other
        # (possibly freshly auto-registered) worker name, never triggers one.
        return False
    if psd_crud.list_nonterminal_deploy_tasks(db, stack.id):
        return False
    if _recently_dispatched(db, stack.id):
        return False

    demand = _aggregate_accelerator_demand(stack.accelerator_requirements or {})
    try:
        _mesh = _provision_mesh_identity(db, stack)
    except Exception:  # noqa: BLE001 - mesh is a no-op unless configured
        _mesh = (None, None)
    compose_yaml, var_bundle = _compose_yaml_with_vars(
        stack, *_mesh, secret_env=_resolve_stack_secrets(stack, db=db), db=db)
    task = _dispatch_stack_task(
        db, worker_name, stack.tenant_id, "deploy", _project_name(stack),
        compose_yaml, stack_id=stack.id, accel_demand=demand,
        extra_payload=_sv.payload_files(var_bundle))
    payload = dict(task.task_payload or {})
    payload["resume_in_place"] = True
    payload["reconciler"] = {"reason": "stack secrets lost with tmpfs after host restart"}
    # Review2 M3: this automatic deploy's secret/storage file bytes are dropped
    # once it is terminal (worker_tasks terminal handler + the periodic sweep).
    payload["scrub_files_when_done"] = True
    task.task_payload = payload

    from services.stack_backend_dispatch import is_k8s_task
    if not is_k8s_task(task):
        stack.status = "deploying"
        stack.status_reason = (
            f"secret files were lost with the host's tmpfs on {worker_name}; "
            "redeploying to restore them")
    stack.desired_state = "running"
    db.add(models.AuditEvent(
        tenant_id=stack.tenant_id, user_id=None,
        event_name="STACK_RESUMED_AFTER_FENCE",
        details={"stack": stack.name, "stack_id": stack.id, "worker": worker_name,
                 "reason": "stack secrets lost with tmpfs after host restart; redeploying"}))
    db.flush()
    return True


def recover_lost_secrets(db: Session, worker_name: str,
                         hosted: Optional[List[Dict[str, Any]]]) -> int:
    """Handle every `secrets_missing` project in one heartbeat. Returns how many
    redeploys were queued. Never raises: a failure for one project is logged
    (class name only) and the rest still run."""
    queued = 0
    for entry in hosted or []:
        if not isinstance(entry, dict) or not entry.get("secrets_missing"):
            continue
        if "down" in entry:
            continue  # worker-controlled start: restore_down_stacks rewrites the files
        project = str(entry.get("project") or "")[:255]
        try:
            stack = _stack_for_project(db, project)
            if stack is not None and redeploy_for_lost_secrets(db, worker_name, stack):
                queued += 1
                logger.warning("stack %s: secret files lost on %s; redeploy queued",
                               stack.id, worker_name)
        except Exception as exc:  # noqa: BLE001 - heartbeat path must never fail
            logger.error("secret-loss redeploy failed for project %r: %s",
                         project[:64], type(exc).__name__)  # %r: no log injection (L4)
    return queued


# ---------------------------------------------------------------------------
# Worker-controlled stack start (11-20, MOD_STACK_WORKER_START=1, CE default).
#
# A worker that runs the loop (it sends `stack_loop: true` on every heartbeat,
# heartbeat.SetStackLoop; review2 L6) has switched its stack containers to
# restart policy `no`. It reports per project
# how many containers SHOULD run but do not (`down`), and this loop answers with
# ONE `boot_restore` task at a time per worker: the worker writes the secret/
# variable files and starts the down services on the existing containers
# (never down / -v / force-recreate). The API stays the authority: only stacks
# the operator wants running and that are assigned to the reporting worker are
# acted on (an unassigned stack is never restored by a worker).
#
# Backoff state lives in the DB, on the boot_restore task rows themselves
# (`restore_attempt`, `restore_backoff_s` in task_payload), so API restarts and
# several API processes share one backoff (review M1) and no migration is needed.
# ---------------------------------------------------------------------------

import random as _random

# Gap between two stack restores on the same worker (controlled start after a
# reboot: one project at a time, lowest id first, accelerator stacks last).
RESTORE_GAP_SECONDS = 10
# Crash restart backoff: 15 s, 30 s, 60 s ... capped at 5 min, never gives up,
# each step +-20 % jitter.
RESTORE_BACKOFF_BASE_SECONDS = 15
RESTORE_BACKOFF_CAP_SECONDS = 300
RESTORE_BACKOFF_JITTER = 0.2
# Settle window (review H3, review2 M2): a service's attempt count resets to 0
# only after heartbeats OBSERVED it up (not in `down_services`) for this long.
# Time alone never resets it: a service that crashes again 40 s after each
# restore keeps climbing the backoff, a delayed turn keeps its backoff, and an
# on-failure:N service that used its N tries stays given up (as Docker does)
# until it was seen up for the window or the next deploy/start/migrate.
RESTORE_SETTLE_SECONDS = 120
# Periodic secret-payload sweep (review2 M3): every terminal boot_restore row
# and every automatic fallback deploy row, of any worker and any age.
SCRUB_INTERVAL_SECONDS = 600
SCRUB_BATCH = 500
_last_scrub_monotonic: Optional[float] = None
# States the loop leaves alone (operator stop, teardown, deploy/migration).
_RESTORE_SKIP_STATUS = ("stopped", "torn_down", "migrating", "deploying", "stopping",
                        "tearing_down", "failed")
_DEGRADED_PREFIX = "worker-controlled start:"
# Stack ops that, while not terminal, rule out a restore of the same stack (M5).
# Review2 N1: the volume/data ops too; an in-place restore and a snapshot's
# stop fallback stop the stack ON PURPOSE, and a restore started next to them
# would bring the database up on a half-written data dir.
_STACK_LIFECYCLE_OPS = ("deploy", "migrate", "start", "stop", "teardown", "boot_restore",
                        "restore", "snapshot", "rehearse", "logical_restore",
                        "logical_backup")
# Ops after which the per-service restart state starts again (Docker resets
# its restart count on create/start too).
_STATE_RESET_OPS = ("deploy", "migrate", "start")
# Payload keys that carry secret / storage file bytes (removed once a
# boot_restore is terminal, review M2).
_FILE_PAYLOAD_KEYS = ("secret_files", "storage_files")


def _open_states():
    return [models.ApiTaskStatus.PENDING, models.ApiTaskStatus.ASSIGNED,
            models.ApiTaskStatus.CLAIMED, models.ApiTaskStatus.RUNNING]


def restore_backoff_seconds(attempts: int, jitter: Optional[float] = None) -> int:
    base = min(RESTORE_BACKOFF_BASE_SECONDS * (2 ** max(attempts, 0)),
               RESTORE_BACKOFF_CAP_SECONDS)
    if jitter is None:
        return base
    return max(1, int(round(base * (1.0 + jitter))))


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _last_boot_restore(db: Session, stack_id: int):
    return (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                    models.ApiTaskQueue.task_payload["stack_op"].as_string() == "boot_restore")
            .order_by(models.ApiTaskQueue.created_at.desc(), models.ApiTaskQueue.id.desc())
            .first())


def _on_failure_max(intent: str) -> int:
    """`on-failure:N` -> N (0 = unlimited), like Docker's MaximumRetryCount."""
    if not intent.startswith("on-failure:"):
        return 0
    try:
        return max(int(intent.split(":", 1)[1]), 0)
    except ValueError:
        return 0


def _state_row(db: Session, stack_id: int):
    """(row, state): the newest boot_restore row and its restart state; the
    state is empty when a deploy/start/migrate was queued after it (review2
    M2: like Docker, a new start begins a fresh restart count)."""
    last = _last_boot_restore(db, stack_id)
    if last is None:
        return None, {}
    st = (last.task_payload or {}).get("restart_state")
    if not isinstance(st, dict) or not st:
        return last, {}
    newer = (db.query(models.ApiTaskQueue.id)
             .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                     models.ApiTaskQueue.task_payload["stack_op"].as_string().in_(
                         list(_STATE_RESET_OPS)),
                     models.ApiTaskQueue.created_at > last.created_at)
             .first())
    if newer is not None:
        return last, {}
    return last, {k: dict(v) for k, v in st.items() if isinstance(v, dict)}


def restore_state(db: Session, stack_id: int) -> Dict[str, Dict[str, Any]]:
    """Per-container (service) restart state carried forward on the newest
    boot_restore row: {service: {"n": attempts, "at": iso, "b": backoff_s,
    "up": iso when heartbeats first saw it up again}}."""
    return _state_row(db, stack_id)[1]


def _parse_iso(v) -> Optional[datetime]:
    try:
        return _aware(datetime.fromisoformat(str(v)))
    except (TypeError, ValueError):
        return None


def observe_services(db: Session, stack_id: int, down: List[str], now: datetime) -> None:
    """Review2 M2: record what this heartbeat OBSERVED. A service in the state
    that is not down gets `up` (first time seen up); a down service that had
    been up for RESTORE_SETTLE_SECONDS starts a fresh count, one that crashed
    sooner keeps its count and backoff. Writes only on a transition."""
    row, state = _state_row(db, stack_id)
    if row is None or not state:
        return
    all_down = "*" in down
    changed = False
    for svc, st in state.items():
        up = _parse_iso(st.get("up"))
        if all_down or svc in down:
            if up is None:
                continue
            if now - up >= timedelta(seconds=RESTORE_SETTLE_SECONDS):
                st["n"] = 0  # it stayed up for the window: fresh count
            st.pop("up", None)
            changed = True
        elif up is None:
            st["up"] = now.isoformat()
            changed = True
    if changed:
        payload = dict(row.task_payload or {})
        payload["restart_state"] = state
        row.task_payload = payload


def service_due(state: Dict[str, Any], now: datetime):
    """(attempts, due) for one DOWN service. Never reset by time alone (review2
    M2): only observe_services resets the count, after the service was seen up
    for RESTORE_SETTLE_SECONDS."""
    if not state:
        return 0, True
    n = int(state.get("n") or 0)
    b = int(state.get("b") or restore_backoff_seconds(max(n - 1, 0)))
    at = _parse_iso(state.get("at"))
    if at is None:
        return 0, True
    return n, now >= at + timedelta(seconds=b)


def _stack_task_in_flight(db: Session, stack_id: int) -> bool:
    """Any non-terminal lifecycle task (incl. an operator start/stop) for the
    stack: a restore must not run next to it (review M5)."""
    return (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                    models.ApiTaskQueue.task_payload["stack_op"].as_string().in_(
                        list(_STACK_LIFECYCLE_OPS)),
                    models.ApiTaskQueue.status.in_(_open_states()))
            .first()) is not None


def _strip_file_payload(task) -> bool:
    p = dict(task.task_payload or {})
    if not any(k in p for k in _FILE_PAYLOAD_KEYS):
        return False
    for k in _FILE_PAYLOAD_KEYS:
        p.pop(k, None)
    p["files_scrubbed"] = True
    task.task_payload = p
    return True


def cancel_pending_boot_restores(db: Session, stack_id: int, reason: str) -> int:
    """Review H4: an operator stop/teardown/migrate (or a new deploy) cancels
    every non-terminal boot_restore of the stack and drops its file bytes. A
    restore already executing on the worker is caught there (per-project lock +
    stop generation, stackloop.go) and by on_boot_restore_result."""
    rows = (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                    models.ApiTaskQueue.task_payload["stack_op"].as_string() == "boot_restore",
                    models.ApiTaskQueue.status.in_(_open_states()))
            .all())
    for t in rows:
        t.status = models.ApiTaskStatus.CANCELLED
        t.error_message = reason[:200]
        _strip_file_payload(t)
    if rows:
        db.flush()
    return len(rows)


def payload_scrub_wanted(payload: Optional[Dict[str, Any]]) -> bool:
    """Rows whose file bytes go once terminal: every boot_restore, and the
    loop's automatic fallback deploys (marked scrub_files_when_done)."""
    p = payload or {}
    return p.get("stack_op") == "boot_restore" or p.get("scrub_files_when_done") is True


def scrub_terminal_stack_payloads(db: Session, limit: int = SCRUB_BATCH) -> int:
    """Review2 M3: drop secret/storage file bytes from EVERY terminal
    (completed/failed/cancelled/expired) boot_restore or fallback-deploy row,
    of any worker (also one that stopped heartbeating) and any age. Only rows
    that still carry bytes match, so repeated sweeps always make progress."""
    from sqlalchemy import or_
    q = models.ApiTaskQueue.task_payload
    rows = (db.query(models.ApiTaskQueue)
            .filter(~models.ApiTaskQueue.status.in_(_open_states()),
                    or_(q["stack_op"].as_string() == "boot_restore",
                        q["scrub_files_when_done"].as_boolean().is_(True)),
                    or_(q["secret_files"].isnot(None), q["storage_files"].isnot(None)))
            .order_by(models.ApiTaskQueue.id.asc()).limit(limit).all())
    n = sum(1 for t in rows if payload_scrub_wanted(t.task_payload) and _strip_file_payload(t))
    if n:
        db.flush()
    return n


def maybe_scrub_terminal_stack_payloads(db: Session, now_monotonic: Optional[float] = None) -> int:
    """Run the sweep at most once per SCRUB_INTERVAL_SECONDS per API process
    (review L8: not on every heartbeat). Never raises."""
    global _last_scrub_monotonic
    import time as _time
    now_m = _time.monotonic() if now_monotonic is None else now_monotonic
    if _last_scrub_monotonic is not None and now_m - _last_scrub_monotonic < SCRUB_INTERVAL_SECONDS:
        return 0
    _last_scrub_monotonic = now_m
    try:
        return scrub_terminal_stack_payloads(db)
    except Exception as exc:  # noqa: BLE001 - heartbeat path must never fail
        logger.error("stack payload scrub failed: %s", type(exc).__name__)
        return 0


def on_boot_restore_result(db: Session, task, stack, is_success: bool) -> None:
    """Completion of a boot_restore: drop its file bytes; if the operator
    stopped the stack meanwhile, stop it again (review H4 compensation)."""
    _strip_file_payload(task)
    if stack is None or (stack.desired_state or "running") == "running":
        return
    if not is_success or not task.assigned_to_worker:
        return
    from routers.persistent_stacks import _dispatch_stack_task, _project_name
    _dispatch_stack_task(db, task.assigned_to_worker, stack.tenant_id, "stop",
                         _project_name(stack), stack_id=stack.id)
    logger.warning("stack %s: boot_restore finished after an operator stop; "
                   "stop re-dispatched", stack.id)


def _worker_restore_busy(db: Session, worker_name: str) -> bool:
    """A boot_restore for this worker is still in flight, or one was queued
    less than RESTORE_GAP_SECONDS ago (one project at a time)."""
    open_states = [models.ApiTaskStatus.PENDING, models.ApiTaskStatus.ASSIGNED,
                   models.ApiTaskStatus.CLAIMED, models.ApiTaskStatus.RUNNING]
    base = (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_op"].as_string() == "boot_restore",
                    models.ApiTaskQueue.assigned_to_worker == worker_name))
    if base.filter(models.ApiTaskQueue.status.in_(open_states)).first() is not None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=RESTORE_GAP_SECONDS)
    return base.filter(models.ApiTaskQueue.created_at >= cutoff).first() is not None


def _wants_accelerator(stack: "models.PersistentStack") -> bool:
    req = stack.accelerator_requirements or {}
    return bool(req) and any(bool(v) for v in (req.values() if isinstance(req, dict) else req))


def restore_order(stacks: List["models.PersistentStack"]) -> List["models.PersistentStack"]:
    """Non-accelerator stacks first, then lowest id first."""
    return sorted(stacks, key=lambda s: (_wants_accelerator(s), s.id))


def _queue_boot_restore(db: Session, worker_name: str, stack: "models.PersistentStack"):
    from routers.persistent_stacks import (
        _compose_yaml_with_vars, _dispatch_stack_task, _project_name,
        _provision_mesh_identity, _resolve_stack_secrets)
    from services import stack_variables as _sv

    try:
        _mesh = _provision_mesh_identity(db, stack)
    except Exception:  # noqa: BLE001 - mesh is a no-op unless configured
        _mesh = (None, None)
    # Same helpers as a deploy payload; only the files travel, no compose.
    _yaml, var_bundle = _compose_yaml_with_vars(
        stack, *_mesh, secret_env=_resolve_stack_secrets(stack, db=db), db=db)
    return _dispatch_stack_task(
        db, worker_name, stack.tenant_id, "boot_restore", _project_name(stack),
        None, stack_id=stack.id, extra_payload=_sv.payload_files(var_bundle))


def restore_down_stacks(db: Session, worker_name: str,
                        hosted: Optional[List[Dict[str, Any]]],
                        now: Optional[datetime] = None) -> int:
    """One heartbeat of the worker-controlled start loop. Returns 1 when a
    boot_restore was queued, else 0. Never raises."""
    now = datetime.now(timezone.utc) if now is None else _aware(now)
    candidates: List[tuple] = []
    for entry in hosted or []:
        if not isinstance(entry, dict) or "down" not in entry:
            continue
        try:
            stack = _stack_for_project(db, str(entry.get("project") or "")[:255])
            if stack is None:
                continue
            if not stack.target_worker or stack.target_worker != worker_name:
                # M3: only the stack's own assigned worker may change its
                # status or trigger anything (no orphan copy, no forged name).
                continue
            down = int(entry.get("down") or 0)
            if (stack.desired_state or "running") == "running" and stack.status in ("running", "degraded"):
                raw = entry.get("down_services") if down > 0 else []
                names = (["*"] if not isinstance(raw, list) else
                         [str(d.get("service") or "")[:128] for d in raw[:64] if isinstance(d, dict)])
                observe_services(db, stack.id, names, now)  # review2 M2
            if down <= 0:
                # Status truth only; the backoff is NOT reset here (H3): it
                # resets after the settle window (restore_state).
                if stack.status == "degraded" and (stack.status_reason or "").startswith(_DEGRADED_PREFIX):
                    stack.status = "running"
                    stack.status_reason = None
                continue
            if (stack.desired_state or "running") != "running":
                continue
            if stack.status in _RESTORE_SKIP_STATUS or stack.status not in ("running", "degraded"):
                continue  # M5: never touch pending/created/... rows
            # Per container, Docker's own semantics (owner 11-20): the worker
            # lists only containers whose ORIGINAL policy says Docker would
            # restart them (`down_services`); on-failure:N stops after N tries.
            svcs = entry.get("down_services")
            if not isinstance(svcs, list):  # older worker: whole project
                svcs = [{"service": "*", "intent": ""}]
            state = restore_state(db, stack.id)
            want, due, max_n = [], [], 0
            for d in svcs[:64]:
                if not isinstance(d, dict):
                    continue
                svc = str(d.get("service") or "")[:128]
                if not svc:
                    continue
                n, is_due = service_due(state.get(svc) or {}, now)
                limit = _on_failure_max(str(d.get("intent") or ""))
                if limit and n >= limit:
                    continue  # Docker would have given up too: not "down"
                want.append(svc)
                max_n = max(max_n, n)
                if is_due:
                    due.append(svc)
            if not want:
                if stack.status == "degraded" and (stack.status_reason or "").startswith(_DEGRADED_PREFIX):
                    stack.status = "running"
                    stack.status_reason = None
                continue
            stack.status = "degraded"
            stack.status_reason = (
                f"{_DEGRADED_PREFIX} {len(want)} container(s) down on {worker_name}; "
                f"restore attempts {max_n}")
            if due:
                candidates.append((stack, (due, state)))
        except Exception as exc:  # noqa: BLE001 - heartbeat path must never fail
            logger.error("stack restore check failed: %s", type(exc).__name__)
    maybe_scrub_terminal_stack_payloads(db)  # review2 M3: global, on a timer
    db.flush()  # status truth (degraded / back to running) even when nothing is queued
    if not candidates or _worker_restore_busy(db, worker_name):
        return 0
    order = restore_order([s for s, _a in candidates])
    attempts_by_id = {s.id: a for s, a in candidates}
    for stack in order:
        due, state = attempts_by_id[stack.id]
        try:
            if _stack_task_in_flight(db, stack.id):
                continue  # M5: an operator start/stop/deploy is in flight
            task = _queue_boot_restore(db, worker_name, stack)
        except Exception as exc:  # noqa: BLE001
            logger.error("boot_restore dispatch failed for stack %s: %s", stack.id,
                         type(exc).__name__)
            return 0
        new_state = dict(state)
        for svc in due:
            n, _d = service_due(state.get(svc) or {}, now)
            new_state[svc] = {"n": n + 1, "at": now.isoformat(),
                              "b": restore_backoff_seconds(n, _random.uniform(
                                  -RESTORE_BACKOFF_JITTER, RESTORE_BACKOFF_JITTER))}
        payload = dict(task.task_payload or {})
        payload["restore_services"] = [] if "*" in due else sorted(due)
        payload["restart_state"] = new_state
        task.task_payload = payload
        logger.warning("stack %s: %d container(s) down; boot_restore queued on %s",
                       stack.id, len(due), worker_name)
        db.flush()
        return 1
    return 0


def stack_state_for_worker(db: Session, task, worker_name: str) -> Dict[str, Any]:
    """Answer for POST /workers/tasks/{id}/stack-state (review H4). Raises 404
    when the task has no stack, 403 when the caller is not the stack's
    assigned worker. `start` is the only thing the worker acts on."""
    from fastapi import HTTPException
    p = task.task_payload or {}
    sid = p.get("stack_id")
    stack = db.query(models.PersistentStack).get(int(sid)) if sid is not None else None
    if stack is None:
        raise HTTPException(status_code=404, detail="Stack not found")
    if not stack.target_worker or stack.target_worker != worker_name:
        raise HTTPException(status_code=403, detail="Not the stack's assigned worker")
    desired = stack.desired_state or "running"
    status = stack.status or ""
    task_status = getattr(task.status, "value", task.status)
    start = (desired == "running" and status not in _RESTORE_SKIP_STATUS
             and str(task_status) not in ("cancelled", "failed", "expired"))
    updated = getattr(stack, "updated_at", None)
    return {"stack_id": stack.id, "desired_state": desired, "status": status,
            "task_status": str(task_status),
            "updated_at": updated.isoformat() if updated is not None else None,
            "start": bool(start)}


def psd_nonterminal(db: Session, stack_id: int) -> bool:
    from crud import persistent_stacks_domain as psd_crud
    return bool(psd_crud.list_nonterminal_deploy_tasks(db, stack_id))


def redeploy_missing_stacks(db: Session, worker_name: str,
                            hosted: Optional[List[Dict[str, Any]]],
                            stack_loop: bool = False) -> int:
    """Fallback: a stack assigned to this worker that should run but has NO
    containers here (boot_restore cannot start what does not exist) gets the
    proven full redeploy, with the same guards/backoff as the secret path.
    redeploy_for_lost_secrets only ever queues it for the stack's OWN assigned
    worker (target_worker == worker_name)."""
    from routers.persistent_stacks import _project_name
    if hosted is None or not stack_loop:
        # Review2 L6: only a worker that ADVERTISES the loop (stack_loop: true
        # on its heartbeat) is trusted to mean "I looked and it is not here";
        # an empty list from a flag-off worker must not redeploy everything.
        return 0
    present = {str(e.get("project")) for e in hosted or [] if isinstance(e, dict)}
    queued = 0
    rows = (db.query(models.PersistentStack)
            .filter(models.PersistentStack.target_worker == worker_name,
                    models.PersistentStack.status.in_(("running", "degraded"))).all())
    for stack in rows:
        try:
            if _project_name(stack) in present:
                continue
            if redeploy_for_lost_secrets(db, worker_name, stack):
                stack.status_reason = (f"{_DEGRADED_PREFIX} no containers on "
                                       f"{worker_name}; full redeploy")
                queued += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("fallback redeploy failed for stack %s: %s", stack.id,
                         type(exc).__name__)
    return queued

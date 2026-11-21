"""gw-stack-networking GW.3 — placement treats fixed host ports as a resource.

A stack that requests a FIXED host port (`x-mod.host_ports` with an integer
`host`) needs that exact port (+ protocol) free on whichever single worker it
lands on — a compose project is one project on one host, so the port cannot be
split. Before this, host ports passed through verbatim and a second stack (or a
second copy) publishing the same host port failed at `compose up` with
"port is already allocated", discoverable only after the deploy had already been
placed and started.

Two sources of "the port is taken on worker W":

  * OBSERVED in use — a listener/port binding the worker itself is holding,
    whatever is running there. This comes from the worker's heartbeat
    `host_bound_ports` field (GW.2c, sanitized onto
    hardware_specs["host_bound_ports"]); `observed_bound_ports` returns empty
    only for a worker with no reading stored yet. A redeploying stack's OWN
    running containers are excluded from this source — via the worker's
    per-project report when it has one, and via a DB-side fallback (the
    stack's last successfully-deployed revision + a fresh stack observation
    on this worker) for older workers that only report the flat list.
  * RESERVED by another stack — a different stack already ASSIGNED to W
    (`target_worker = W`) whose stored compose also requests that fixed port.
    This is fully answerable from the DB today and is the check that makes two
    copies of the same stack collide cleanly instead of at `compose up`.

`host: "any"` never blocks: the worker allocates a free port from a range and
reports the mapping back (GW.2), so "any" is a request for *some* free port,
not a collision on a specific one.

`any` is deliberately excluded from every check here.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

import yaml
from sqlalchemy.orm import Session

import models
from utils.accelerator_fit import ACTIVE_STACK_STATUSES

# GW.2 fix (stale reading) — the worker's 30s-cadence heartbeat carries the
# last host-bound-ports reading forward when a beat omits the field, so the
# reading can outlive the worker that wrote it (older binary, provider gone).
# A reading older than this many heartbeat intervals is treated as ABSENT.
# The platform setting `stack.host_bound_ports_stale_intervals` is the
# operator-facing tunable; this is the fallback if the registry is
# unreachable (setting reads must never break placement).
_DEFAULT_HOST_BOUND_PORTS_STALE_INTERVALS = 3
_HOST_BOUND_PORTS_HEARTBEAT_INTERVAL_S = 30.0


def _host_bound_ports_stale_intervals(db: Session) -> int:
    """The operator-facing staleness (in heartbeat intervals). Falls back to
    the built-in default if the registry read fails — placement must never
    break on a settings read."""
    try:
        from services import platform_settings as _ps
        return int(_ps.get_setting(
            "stack.host_bound_ports_stale_intervals", db=db) or 0) \
            or _DEFAULT_HOST_BOUND_PORTS_STALE_INTERVALS
    except Exception:  # noqa: BLE001 — a settings read must not break placement
        return _DEFAULT_HOST_BOUND_PORTS_STALE_INTERVALS


def _host_bound_ports_at(specs: Dict[str, Any]) -> Optional[datetime]:
    """The capture time (UTC, naive-normalised) of the stored
    host_bound_ports reading, or None when the row predates the stamp."""
    raw = specs.get("host_bound_ports_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        ts = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def _host_bound_ports_is_stale(specs: Dict[str, Any], now: datetime,
                               intervals: int) -> bool:
    """True when the stored reading is older than `intervals` heartbeat
    intervals. A row with no capture stamp (stored before the field existed)
    is grandfathered: NOT stale — we have no better information and the
    carry-forward contract still applies."""
    ts = _host_bound_ports_at(specs)
    if ts is None:
        return False
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return (now - ts) > timedelta(
        seconds=_HOST_BOUND_PORTS_HEARTBEAT_INTERVAL_S * intervals)

# A (host_port, protocol) pair — the unit of collision. Protocol is normalised
# to lowercase ("tcp"/"udp"); the same integer port over tcp and udp is two
# distinct resources.
PortKey = Tuple[int, str]

# `x-mod.host_ports` entry whose host is an integer = a FIXED host-port
# request; `host == "any"` = allocate at deploy. Only the fixed kind blocks
# placement; this is the single source of that distinction.
_ANY_HOST = "any"


# =============================================================================
# spec parsing
# =============================================================================

def _load_spec(spec: Union[Dict[str, Any], str, None]) -> Dict[str, Any]:
    """Normalise a compose spec to a dict. Task payloads carry the rendered
    YAML as a STRING (`compose_spec`), while the stack row carries the parsed
    dict — both must flow through the same checks."""
    if isinstance(spec, dict):
        return spec
    if isinstance(spec, str) and spec.strip():
        try:
            loaded = yaml.safe_load(spec)
        except Exception:  # noqa: BLE001 — malformed spec is not a port request
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def requested_host_ports(spec: Union[Dict[str, Any], str, None]) -> List[Dict[str, Any]]:
    """The FIXED host-port requests in a compose spec's `x-mod.host_ports`.

    Returns one entry per fixed request: `{"service", "target", "host",
    "protocol", "bind"}`. Entries with `host: "any"` are EXCLUDED — those are
    allocated at deploy by the worker (GW.2) and never block placement. A
    spec with no `x-mod.host_ports` (legacy, or a mesh-only stack) returns [].
    """
    spec = _load_spec(spec)
    xmod = spec.get("x-mod")
    if not isinstance(xmod, dict):
        return []
    out: List[Dict[str, Any]] = []
    for entry in (xmod.get("host_ports") or []):
        if not isinstance(entry, dict):
            continue
        host = entry.get("host")
        # `host: "any"` (string) is a deploy-time allocation, never a block.
        if isinstance(host, str) and host.strip().lower() == _ANY_HOST:
            continue
        if isinstance(host, bool) or not isinstance(host, int):
            continue
        out.append({
            "service": entry.get("service"),
            "target": entry.get("target"),
            "host": host,
            "protocol": str(entry.get("protocol") or "tcp").strip().lower() or "tcp",
            "bind": entry.get("bind"),
        })
    return out


def _id_set(ids: Optional[Iterable[Any]]) -> Set[int]:
    if ids is None:
        return set()
    if isinstance(ids, int):
        return {ids}
    out: Set[int] = set()
    for x in ids:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


# =============================================================================
# the two sources of "the port is taken on worker W"
# =============================================================================

def observed_bound_ports(db: Session, worker: models.WorkerLicense,
                         now: Optional[datetime] = None) -> Set[PortKey]:
    """The (host_port, protocol) pairs this worker is ACTUALLY holding open, as
    it reports them in its heartbeat.

    `now` is injectable for tests (the staleness window is judged against it);
    production callers omit it and the process clock is used.

    Source (gw-stack-networking GW.2c): the worker's heartbeat
    `host_bound_ports` ({"tcp": [8080], "udp": [53]}) is sanitized at ingest
    and stored on `worker.hardware_specs["host_bound_ports"]`. The worker
    OMITS the field when it cannot observe (older binary / failed daemon
    query); the API carries the previous reading forward in that case
    (worker_management.py ingest), so an empty return here means "no reading
    of any port in use", not "the worker is silent".

    GW.2 fix (stale reading): the carry-forward above is right for a one-off
    daemon hiccup but WRONG when the worker stops answering the field at all
    (older binary / provider gone) — the last reading would then block ports
    forever. So a reading whose capture time
    (`hardware_specs["host_bound_ports_at"]`, stamped at ingest) is older than
    `stack.host_bound_ports_stale_intervals` heartbeat intervals is treated as
    ABSENT (returns empty). Rows predating the stamp are grandfathered —
    never stale. `now` is injectable for tests.
    """
    specs = getattr(worker, "hardware_specs", None) or {}
    raw = specs.get("host_bound_ports") if isinstance(specs, dict) else None
    if not isinstance(raw, dict):
        return set()
    if _host_bound_ports_is_stale(specs, now or datetime.utcnow(),
                                  _host_bound_ports_stale_intervals(db)):
        return set()
    out: Set[PortKey] = set()
    for proto, ports in raw.items():
        p = str(proto).strip().lower()
        if p not in ("tcp", "udp") or not isinstance(ports, list):
            continue
        for port in ports:
            if isinstance(port, bool) or not isinstance(port, int):
                continue
            if 1 <= port <= 65535:
                out.add((port, p))
    return out


def observed_own_project_ports(worker: models.WorkerLicense,
                               project: str) -> Set[PortKey]:
    """The (host_port, protocol) pairs bound on this worker by the compose
    PROJECT `project` specifically, as it reports them in
    `host_bound_ports_by_project` ({"modstack1g0": {"tcp": [8080]}}).

    A redeploy of a stack onto its own host must not block on its OWN running
    containers: compose stops them before the new ones bind, so those ports
    are not a real collision. Returns empty when the worker does not report
    the by-project view (older binary) or reports the project with no ports —
    in the first case the caller falls back to the DB-side
    `_deployed_revision_self_exclusion`, in the second it trusts the live
    view (the project's containers bind nothing here right now).
    """
    specs = getattr(worker, "hardware_specs", None) or {}
    by_project = specs.get("host_bound_ports_by_project") \
        if isinstance(specs, dict) else None
    if not isinstance(by_project, dict) or not project:
        return set()
    per_proto = by_project.get(project)
    if not isinstance(per_proto, dict):
        return set()
    out: Set[PortKey] = set()
    for proto, ports in per_proto.items():
        p = str(proto).strip().lower()
        if p not in ("tcp", "udp") or not isinstance(ports, list):
            continue
        for port in ports:
            if isinstance(port, bool) or not isinstance(port, int):
                continue
            if 1 <= port <= 65535:
                out.add((port, p))
    return out


def _excluded_stack_project(db: Session,
                            exclude_stack_ids: Optional[Iterable[Any]]) -> Optional[str]:
    """The compose project name of a stack in `exclude_stack_ids`, when there
    is a single one to identify (the redeploy case). None when there is no
    excluded stack, its row is gone, or several are excluded (ambiguous — do
    not self-exclude). Mirrors
    `routers/persistent_stacks/_shared.py:_project_name` exactly."""
    ids = _id_set(exclude_stack_ids)
    if len(ids) != 1:
        return None
    stack_id = next(iter(ids))
    stack = db.query(models.PersistentStack).filter(
        models.PersistentStack.id == stack_id).first()
    if stack is None:
        return None
    refs = getattr(stack, "storage_refs", None) or {}
    return (refs.get("project") or f"modstack{stack.id}g0")


def _reports_by_project(worker: models.WorkerLicense) -> bool:
    """Whether the worker's stored hardware_specs carry the per-project
    bound-port view at all (any binary new enough to report it). Older
    workers (e.g. v1.0.77) store only the flat `host_bound_ports`, and the
    self-exclusion below falls back to a DB-side answer for those."""
    specs = getattr(worker, "hardware_specs", None) or {}
    if not isinstance(specs, dict):
        return False
    return isinstance(specs.get("host_bound_ports_by_project"), dict)


def _deployed_revision_self_exclusion(db: Session,
                                      exclude_stack_ids: Optional[Iterable[Any]],
                                      worker_name: Optional[str]) -> Set[PortKey]:
    """FX.141 — DB-side self-exclusion for workers that report ONLY the flat
    `host_bound_ports` (no `host_bound_ports_by_project`).

    Returns the fixed host ports the excluded stack's own CURRENTLY-DEPLOYED
    revision publishes on `worker_name`, so a redeploy is not refused with
    "host port … in use" by its OWN still-running containers. Non-empty only
    when the attribution is positive on every hop:

      * exactly one stack is excluded (the single redeploy case — never
        guess for an ambiguous set);
      * `stack_observations` shows that stack's project RUNNING on this
        worker, fresh within the observation grace window — a stale or
        missing observation never authorises the exclusion;
      * a successfully COMPLETED deploy/migrate task for that stack exists
        and its payload carries a compose spec — the spec that is ACTUALLY
        running on the host, not the new spec being placed.

    Anything less certain returns the empty set and the guard behaves
    exactly as it did before this fallback (block on the observed port).
    Deliberately subtracts only what the stack was ALREADY RUNNING here,
    never what the NEW spec requests — a DIFFERENT stack's hold on the same
    port is never weakened by this.
    """
    ids = _id_set(exclude_stack_ids)
    if len(ids) != 1 or not worker_name:
        return set()
    stack_id = next(iter(ids))

    # 1788d CE carve: services/stack_observation.py is paid (manifest deny
    # `CORE/API/**/stack_observation.py`). This helper asks "is this stack
    # provably still running on this worker?" - with observations absent the
    # platform has no evidence, so it declines the self-exclusion and placement
    # proceeds on its normal rules.
    from utils.paid_import import optional_paid
    _observation = optional_paid("services.stack_observation")
    if _observation is None:
        return set()

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=_observation.observation_grace_seconds())
    obs = (db.query(models.StackObservation)
           .filter(models.StackObservation.stack_id == stack_id,
                   models.StackObservation.worker_name == worker_name,
                   models.StackObservation.running > 0,
                   models.StackObservation.observed_at >= cutoff)
           .order_by(models.StackObservation.observed_at.desc())
           .first())
    if obs is None:
        return set()

    task = (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer()
                    == stack_id,
                    models.ApiTaskQueue.task_payload["stack_op"].as_string()
                    .in_(["deploy", "migrate"]),
                    models.ApiTaskQueue.status
                    == models.ApiTaskStatus.COMPLETED)
            .order_by(models.ApiTaskQueue.completed_at.desc().nulls_last(),
                      models.ApiTaskQueue.id.desc())
            .first())
    if task is None:
        return set()
    payload = task.task_payload or {}
    out: Set[PortKey] = set()
    for entry in requested_host_ports(payload.get("compose_spec")):
        out.add((entry["host"], entry["protocol"]))
    return out


def reserved_host_ports(db: Session, worker_name: str,
                        exclude_stack_ids: Optional[Iterable[Any]] = None) -> Set[PortKey]:
    """The fixed host-port (port, protocol) pairs RESERVED by other stacks
    already assigned to `worker_name`.

    A stack is "assigned to W" once `target_worker = W` and it is in an active
    lifecycle state (`ACTIVE_STACK_STATUSES`) — a stack mid-deploy already holds
    its port for the host. `exclude_stack_ids` lets a stack be judged without
    counting itself (a redeploy onto its own host must not block itself).
    """
    excl = _id_set(exclude_stack_ids)
    q = db.query(models.PersistentStack).filter(
        models.PersistentStack.target_worker == worker_name,
        models.PersistentStack.status.in_(ACTIVE_STACK_STATUSES),
    )
    if excl:
        q = q.filter(~models.PersistentStack.id.in_(excl))
    out: Set[PortKey] = set()
    for stack in q.all():
        for entry in requested_host_ports(stack.compose_spec):
            out.add((entry["host"], entry["protocol"]))
    return out


# =============================================================================
# per-worker verdict + fleet reason
# =============================================================================

def host_port_conflict(db: Session, worker: models.WorkerLicense,
                       spec: Union[Dict[str, Any], str, None],
                       exclude_stack_ids: Optional[Iterable[Any]] = None) -> Optional[str]:
    """Why `worker` cannot host `spec`'s fixed host ports, or None when it can.

    Blocks ONLY on a requested FIXED port that is observed in use or reserved
    by another assigned stack on this worker. `host: "any"` never blocks. The
    returned string is the per-worker clause ("host port 8110/tcp in use"); the
    fleet-wide "…on every eligible worker" form is `host_port_shortfall_reason`.

    Self-exclusion (GW.2 fix): when a SINGLE stack is excluded (a redeploy),
    the ports its OWN running containers hold on this worker — as reported by
    the worker in `host_bound_ports_by_project` — are NOT a conflict: compose
    stops the old containers before the new ones bind. A worker that does not
    report the by-project view (older binary, e.g. v1.0.77) is self-excluded
    from the database instead: `_deployed_revision_self_exclusion` subtracts
    what the stack's last successfully-deployed revision publishes on this
    worker, gated on a fresh stack observation saying the project is running
    here. A worker whose live by-project view says the project binds nothing
    is trusted as-is (no fallback). See both helpers.
    """
    wanted = requested_host_ports(spec)
    if not wanted:
        return None
    excl = _id_set(exclude_stack_ids)
    observed = observed_bound_ports(db, worker)
    if excl:
        own_project = _excluded_stack_project(db, excl)
        if own_project:
            excluded = observed_own_project_ports(worker, own_project)
            if not excluded and not _reports_by_project(worker):
                excluded = _deployed_revision_self_exclusion(
                    db, excl, worker.worker_name)
            observed -= excluded
    reserved = reserved_host_ports(db, worker.worker_name, exclude_stack_ids=excl)
    for entry in wanted:
        key: PortKey = (int(entry["host"]), str(entry["protocol"]).lower())
        if key in observed or key in reserved:
            return f"host port {key[0]}/{key[1]} in use"
    return None


_CONFLICT_RE = re.compile(r"host port (\d+)/(tcp|udp) in use")


def host_port_conflict_message(worker: models.WorkerLicense,
                               conflict: str) -> str:
    """A user-facing sentence for a `host_port_conflict` result: names the port,
    the compose project that holds it on this worker when the worker reports a
    by-project view (`host_bound_ports_by_project`), and what to do about it.
    Falls back to the bare conflict text when it cannot be parsed."""
    m = _CONFLICT_RE.search(conflict or "")
    if not m:
        return f"{conflict} on {worker.worker_name}"
    port, proto = int(m.group(1)), m.group(2)
    specs = getattr(worker, "hardware_specs", None) or {}
    by_project = specs.get("host_bound_ports_by_project") \
        if isinstance(specs, dict) else None
    holders: List[str] = []
    if isinstance(by_project, dict):
        for proj, per_proto in by_project.items():
            ports = per_proto.get(proto) if isinstance(per_proto, dict) else None
            if isinstance(ports, list) and port in ports:
                holders.append(str(proj))
    who = (f"project {', '.join(sorted(holders))}" if holders
           else "another container or stack")
    return (f"Host port {port}/{proto} is already used by {who} on "
            f"{worker.worker_name}; change the host port in the compose "
            f"(or stop what uses it) and deploy again.")


def host_port_shortfall_reason(db: Session,
                               spec: Union[Dict[str, Any], str, None],
                               workers: List[models.WorkerLicense],
                               exclude_stack_ids: Optional[Iterable[Any]] = None) -> Optional[str]:
    """Fleet-wide reason when NO worker in `workers` can host the requested
    fixed host ports, else None.

    A stack is one compose project on one host, so it is port-placeable iff SOME
    single worker has every requested fixed port free. This mirrors the
    claim-guard's per-worker verdict so an operator sees WHY a stack is stuck,
    not just that it is. Format: "host port 8110/tcp in use on every eligible
    worker"; with several fixed ports the clause lists them.
    """
    wanted = requested_host_ports(spec)
    if not wanted or not workers:
        return None
    excl = _id_set(exclude_stack_ids)
    for w in workers:
        if host_port_conflict(db, w, spec, exclude_stack_ids=excl) is None:
            return None
    pairs = sorted({(int(e["host"]), str(e["protocol"]).lower()) for e in wanted})
    label = ", ".join(f"{p}/{pr}" for p, pr in pairs)
    return f"host port {label} in use on every eligible worker"


def deploy_host_port_refusal(db: Session,
                             spec: Union[Dict[str, Any], str, None],
                             target: models.WorkerLicense,
                             exclude_stack_ids: Optional[Iterable[Any]] = None
                             ) -> Optional[str]:
    """D76 (owner 11-18): the user-facing sentence for a Deploy that names its
    target worker (a `pinned_worker` or an explicit `to_worker`) when THAT
    worker already holds one of the stack's fixed host ports, else None.

    The unpinned path answers the same question through
    `pick_worker_for_host_ports` (it re-homes onto a free worker and only
    refuses when EVERY eligible worker is busy). A PIN has nowhere to re-home
    to, so the pin branch needs the per-worker verdict — and it needs it at
    Deploy time, before a task exists: without it the deploy queues, the
    claim-time guard parks the stack at "deploying", and the operator learns
    only after the 10-minute give-up window.

    Same primitives as Preview's advisory warning and the claim guard, so the
    three surfaces can never disagree: `host_port_conflict` (which already
    self-excludes the redeploying stack's OWN running containers, including a
    claimed generation project on `storage_refs["project"]`) and
    `host_port_conflict_message` (names the holder project when the worker
    reports the by-project view). Fail-open: a probe error refuses nothing —
    the claim-time guard and its give-up window still apply.
    """
    try:
        if not requested_host_ports(spec):
            return None
        conflict = host_port_conflict(db, target, spec,
                                      exclude_stack_ids=exclude_stack_ids)
        if conflict is None:
            return None
        return host_port_conflict_message(target, conflict)
    except Exception:  # noqa: BLE001 — advisory: never block a deploy on a probe
        return None


def host_port_clause(pairs: Iterable[PortKey]) -> str:
    """The splice-able port clause for a COMBINED unplaceable message, where
    some eligible workers were excluded for ports and others for unrelated
    reasons (VRAM, hardware). Callers that already know which workers fell out
    for which reason can append this rather than a bare "unplaceable"."""
    pairs = sorted(set(pairs))
    if not pairs:
        return ""
    label = ", ".join(f"{p}/{pr}" for p, pr in pairs)
    return f"host port {label} in use"


# =============================================================================
# placement picker
# =============================================================================

def pick_worker_for_host_ports(db: Session,
                               spec: Union[Dict[str, Any], str, None],
                               home_zone: Optional[str] = None,
                               exclude_stack_ids: Optional[Iterable[Any]] = None
                               ) -> Tuple[Optional[models.WorkerLicense], Optional[str]]:
    """Placement picker that treats fixed host ports as a resource.

    Returns one of:
      * `(worker, None)` — a fixed host port is requested and `worker` is an
        eligible host where it is free; the deploy should be pinned there,
      * `(None, reason)` — a fixed host port is requested but NO eligible
        worker can host it (unplaceable),
      * `(None, None)` — NO fixed host port is requested, so ports constrain
        nothing and the deploy stays UNPINNED (PENDING, placed at claim time by
        the accelerator guard). A host port must never silently pin a deploy
        that does not ask for one.

    `host: "any"` never blocks (the worker allocates a free port at deploy), so
    a spec whose only `host_ports` are `any` is the `(None, None)` case.

    `home_zone`, when set, orders candidates so the zoned worker is preferred
    (matching `_pick_worker`'s zone scoping) but still yields to a port conflict.
    """
    if not requested_host_ports(spec):
        return None, None  # no fixed port — do not pin

    from crud import persistent_stacks_domain as psd_crud

    candidates = list(psd_crud.list_licensed_workers(db))
    if home_zone:
        candidates.sort(key=lambda w: 0
                        if (getattr(w, "network_zone", None) or "local") == home_zone
                        else 1)
    if not candidates:
        return None, "No approved worker is available to host the stack: add or approve a worker (Dashboard > Workers)"

    for w in candidates:
        if host_port_conflict(db, w, spec, exclude_stack_ids=exclude_stack_ids) is None:
            return w, None

    reason = host_port_shortfall_reason(db, spec, candidates,
                                        exclude_stack_ids=exclude_stack_ids)
    return None, reason


# =============================================================================
# recording the worker-allocated mapping (host: "any")
# =============================================================================

def record_host_port_allocation(db: Session, stack: models.PersistentStack,
                                mapping: Any) -> None:
    """Persist the host-port mapping the worker ALLOCATED for `host: "any"`
    requests (GW.2 allocates a free port from a pushed range and reports the
    chosen mapping back) onto the stack's `storage_refs`, so `GET /{stack_id}`
    shows it (`schemas.PersistentStack` already serialises `storage_refs`).

    No new column: `storage_refs` is the stack's existing JSON home for
    deployment facts and rides on every read. `mapping` is the worker's report
    as-is (e.g. `{service: [{target, host, protocol}, ...]}` or a flat list of
    such); it is stored verbatim under `storage_refs["host_port_mappings"]`.
    """
    if stack is None or not mapping:
        return
    refs = dict(stack.storage_refs or {})
    refs["host_port_mappings"] = mapping
    stack.storage_refs = refs  # reassign so SQLAlchemy detects the JSONB change
    db.add(stack)

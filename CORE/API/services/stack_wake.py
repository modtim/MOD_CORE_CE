"""Universal, on-demand wake/sleep for the managed AI stacks a node depends on.

generalized-stack-wake — ONE home for the policy that decides, for any node that
needs a managed AI stack (a NIM, the brand VLM, an SDXL server, a served/wizard
LLM), whether to wake that stack, leave it alone, or make the node wait — and,
after the node finishes, whether to sleep the stack back.

This is a GENERALIZATION of the proven `services/nim_ephemeral` behavior, not a
new invention (design D3). The three rules the platform owner asked for:

  1. Stack ALREADY RUNNING  → use it, and LEAVE IT ON. We never sleep a stack we
     did not wake (wake-ownership, D4).
  2. Stack OFF + it FITS    → wake it (after freeing only SAFELY-freeable
     competitors), run, then sleep it back AGGRESSIVELY the moment the last node
     needing it finishes — because we woke it (D5/D6).
  3. Stack OFF + WON'T FIT   → the node WAITS on capacity using the scheduler's
     EXISTING "held, awaiting resources" behavior. Not a failure, not a new task
     state (D7); the normal dispatch loop retries the wake when room frees.

Why these design choices (cite the D-decisions in design.md):

  * D4 — wake-ownership, NOT a refcount column. `PersistentStack.nim_wake_owned`
    (alias `wake_owned`) is set true ONLY when we actually woke the stack. How
    many jobs still need it is counted by querying `ApiTaskQueue`, exactly as the
    NIM path already does — the queue IS the refcount, so a column would only
    duplicate queue truth and risk drifting from it.
  * D5 — aggressive sleep with a QUEUE-LOOKAHEAD guard. On release we sleep
    immediately UNLESS (a) another PENDING/RUNNING task needs this same stack
    (stay warm, no flap), or (b) we do not own the wake (leave-on-if-on). The
    PENDING inclusion is what makes aggressive sleep safe from flapping.
  * D6 — capacity-aware wake that PRESERVES the baked-in one-per-GPU NIM eviction.
    We free only competitors that are safely-freeable (idle, or wake-owned with
    no in-flight task needing them); a stack with live work is NEVER force-evicted.
    The NIM family delegates its eviction to `nim_ephemeral._stop_competitors`
    UNCHANGED; this layer calls it, it does not replace or weaken it.
  * D9 — ISO: every wake / sleep / evict appends an `AuditEvent` (actor, stack,
    action, prior state, reason). Nothing is destructive: sleeping/evicting flips
    `desired_state`/state fields and stops a container — it never deletes a
    `PersistentStack` row or its history.

Every DB access here goes through the ordinary `db.query(...)` / `db.add(...)` /
`db.commit()` surface and every external side effect (task dispatch, pub/sub,
capacity arithmetic) is a lazily-imported helper, so the whole policy is unit-
testable with a mocked session and no live DB / GPU (see tests/test_stack_wake.py).
"""
from __future__ import annotations

import copy
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from services import platform_settings as _psr
from product_names import themed_logger

logger = themed_logger(__name__, 'stacks')

# ---------------------------------------------------------------------------
# Platform settings (admin-toggleable, never env vars).
# ---------------------------------------------------------------------------
_psr.register(
    "stack_wake.vlm_sdxl_sleepback_enabled",
    bool,
    True,
    group="stack_wake",
    description="Rollout 8.1 — auto-sleep the brand VLM / SDXL stacks when "
                "idle. When OFF, VLM/SDXL keep their historical "
                "never-auto-slept behavior. Turning ON routes them through "
                "the same aggressive sleep-back every other managed stack "
                "already gets; the first wake after a sleep-back waits "
                "~1 min for the model to load.",
)

# ---------------------------------------------------------------------------
# CE-launch Gate 4 carve: PAID predictive stack-optimization module (optional).
# ---------------------------------------------------------------------------
# `services/stack_predictive.py` holds the PREDICTIVE decisions (warm-time-aware
# sleep-back, predictive eviction ordering) that used to live inline here, behind
# a feature flag. CE ships WITHOUT that module; this guarded
# import is the ONLY thing that changes here for the carve — every call site below
# (`plan_placement`, `release_stack`, `_resident_slack`) already checks
# `stack_predictive is None` (module absent -> CE) alongside the existing feature
# flag, and falls back to the pre-existing BASIC lifecycle unchanged in either
# case. This must never fail importing `stack_wake` itself.
try:
    from services import stack_predictive
except ImportError:  # noqa: BLE001 — CE build: the Paid module is simply absent
    stack_predictive = None

# ---------------------------------------------------------------------------
# Tunables (env-overridable; defaults preserve today's behavior)
# ---------------------------------------------------------------------------
# D5 belt-and-suspenders: an optional deferral before the sleep, to catch the
# race where the next task is enqueued microseconds after the lookahead check.
# Default 0 = sleep immediately (the aggressive behavior the owner asked for).
STACK_SLEEP_GRACE_SECONDS = 0.0

# D10 readiness confirmation (only consulted when a caller passes wait=True). How
# long ensure_stack_ready polls a family's readiness probe before giving up and
# reporting not-ready (a timeout is NOT a failure — the node still waits/retries).
# Read at call time so a deployment/test can retune without a code change.
STACK_READY_TIMEOUT_S = float(os.getenv("STACK_READY_TIMEOUT_S", "120"))
# Interval between readiness re-checks for the row-status / HTTP probes.
STACK_READY_POLL_INTERVAL_S = float(os.getenv("STACK_READY_POLL_INTERVAL_S", "3"))
# Per-attempt HTTP timeout for the served-LLM / VLM / SDXL endpoint pings.
STACK_READY_HTTP_TIMEOUT_S = float(os.getenv("STACK_READY_HTTP_TIMEOUT_S", "4"))


def _vlm_sdxl_sleepback_enabled(db=None) -> bool:
    """Rollout 8.1 — whether the NEW aggressive sleep-back is applied to the
    brand VLM / SDXL stacks. DEFAULT OFF (platform setting
    `stack_wake.vlm_sdxl_sleepback_enabled`), so VLM/SDXL keep their
    CURRENT behavior (wake-on-demand, never auto-slept) until the platform
    owner enables this in the admin UI. NIM and the served/wizard LLM are
    always managed regardless of this setting.

    Read at call time (not import) so a deployment can flip it via the admin
    UI (or DB) without a code change, and so a test can override per-case.
    Pass `db` to participate in the caller's transaction (needed for tests)."""
    return bool(_psr.get_setting("stack_wake.vlm_sdxl_sleepback_enabled",
                                 db=db))


def _predictive_capability_allowed() -> bool:
    """Whether the predictive layer's license/tier check passes. The layer is
    the paid `services/stack_predictive.py`, absent from Community Edition
    builds entirely (see the module docstring above): without it nothing
    predictive can run, so this is simply False."""
    if stack_predictive is None:
        return False
    return stack_predictive.capability_allowed()


def _predictive_sleep_enabled() -> bool:
    """Whether the PREDICTIVE, warm-time-aware arbitration is used for
    sleep-back AND eviction. DEFAULT OFF — when off (and always in a build
    without the predictive layer), `release_stack` keeps the binary "any
    in-flight task" lookahead and `plan_placement` frees only safely-freeable
    (idle) competitors. When on, sleep-back sleeps a stack only when it is
    `recoverable` (soonest next-need across RUNNING runs > warm_time + margin),
    and contention frees `recoverable` residents first — evicting a
    non-recoverable one only as a logged, minimised forced-stall. Read at call
    time so a deploy/test can flip it without a code change."""
    if not _predictive_capability_allowed():
        return False
    return stack_predictive.sleep_flag_on()


# The task-queue states in which a task still NEEDS its stack — the queue-as-
# refcount (D4/D5). PENDING is included deliberately: work already lined up keeps
# the stack warm (the lookahead / no-flap guard).
def _inflight_statuses():
    import models
    return [models.ApiTaskStatus.PENDING, models.ApiTaskStatus.ASSIGNED,
            models.ApiTaskStatus.CLAIMED, models.ApiTaskStatus.RUNNING]


# Audit event names (D9). Stable strings so an operator query / the audit view
# can filter the full wake/sleep/evict trail.
AUDIT_WAKE = "stack.wake"
AUDIT_SLEEP = "stack.sleep"
AUDIT_EVICT = "stack.evict"
AUDIT_READY = "stack.ready"
# A wake whose `start` was dispatched but the reconciler then stamped as FAILED
# (see _failed_start_reason). The `stack.wake` row already recorded the REQUEST;
# this records that the outcome was the opposite of `stack.ready`, so an
# operator query sees the dead stack and the node that was handed nothing.
# Exact-match consumers (event_name == / .in_([...])) never confuse it with
# AUDIT_WAKE, so wake counts stay honest.
AUDIT_WAKE_FAILED = "stack.wake_failed"


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StackRef:
    """Which managed stack a node resolves to (D2). Identifies the
    `PersistentStack` ROW (by name + tenant) plus the family that governs the
    family-specific bits (NIM keeps its own eviction/probe; VLM/SDXL are behind
    the rollout flag)."""
    key: str            # the node_kind (or logical key, e.g. "wizard-llm")
    stack_name: str     # PersistentStack.name to find the row
    tenant_id: int
    family: str         # "nim" | "vlm" | "sdxl" | "llm" | "generic"


@dataclass
class EnsureResult:
    """Outcome of `ensure_stack_ready`. `held` is the D7 signal to the caller:
    the stack could not be placed, so LEAVE THE TASK in the existing awaiting-
    resources hold (do NOT fail it) — the next dispatch retries the wake."""
    ready: bool = False       # stack is (optimistically) usable now
    woke: bool = False        # THIS call woke it (so it is wake-owned + auto-slept)
    held: bool = False        # won't-fit → node should wait on capacity (D7)
    stack_id: Optional[int] = None
    host: Optional[str] = None
    reason: str = ""


@dataclass
class ReleaseResult:
    slept: bool = False
    reason: str = ""


# ---------------------------------------------------------------------------
# D2 — central node -> stack resolution, reusing the maps that already exist
# ---------------------------------------------------------------------------
def _node_kind_and_hint(node) -> tuple:
    """Pull (node_kind, requires_stack hint) out of whatever shape the caller
    has — a bare node_kind string, a task/graph dict, or a NodeDefinition-like
    object. The explicit `requires_stack` hint (D2 step 1) may live at the top
    level or under a `metadata`/`node_data` mapping."""
    if node is None:
        return None, None
    if isinstance(node, str):
        return node, None
    if isinstance(node, dict):
        nk = node.get("node_kind")
        hint = node.get("requires_stack")
        if hint is None:
            for sub in ("metadata", "node_data", "data"):
                m = node.get(sub)
                if isinstance(m, dict) and m.get("requires_stack"):
                    hint = m.get("requires_stack")
                    break
        return nk, hint
    # object with attributes
    nk = getattr(node, "node_kind", None)
    hint = getattr(node, "requires_stack", None)
    return nk, hint


def resolve_required_stack(node, *, tenant_id: Optional[int] = None) -> Optional[StackRef]:
    """Resolve which managed stack `node` needs — the ONE place this is decided.

    Resolution order (D2):
      1. explicit `requires_stack` node-metadata hint, if present;
      2. else the existing central node_kind -> stack knowledge:
         `nim_ephemeral.NIM_NODE_KINDS`, the brand VLM/SDXL kind keys, and the
         served/wizard LLM key.
    Returns a `StackRef`, or `None` for a node that needs no stack — in which
    case the universal ensure/release steps are BOTH no-ops (the whole point:
    every node passes through, nodes without a stack are unaffected).

    Existing NIM/VLM/SDXL nodes resolve with ZERO node-definition changes; a new
    stack-backed node is one central-map entry (or one `requires_stack` line).
    """
    node_kind, hint = _node_kind_and_hint(node)
    # Platform-owned managed stacks live under tenant 1 today (same convention
    # as brand_image_stacks.BRAND_STACKS_TENANT_ID / nim NIM_STACKS_TENANT_ID).
    tid = tenant_id if tenant_id is not None else _default_tenant_id()

    # (1) explicit hint wins — lets anything the maps can't infer opt in.
    if hint:
        return StackRef(key=node_kind or str(hint), stack_name=str(hint),
                        tenant_id=tid, family="generic")

    if not node_kind:
        return None

    # (2a) NIM node kinds (Studio Voice, VSR, ...).
    try:
        from services import nim_ephemeral as _nim
        spec = _nim.NIM_NODE_KINDS.get(node_kind)
        if spec is not None:
            return StackRef(key=node_kind, stack_name=spec.stack_name,
                            tenant_id=tid, family="nim")
    except Exception:  # noqa: BLE001 — resolution must never wedge dispatch
        logger.debug("stack_wake: NIM resolve failed for %s", node_kind, exc_info=True)

    # (2b/c) brand VLM / SDXL consuming nodes.
    try:
        from services import brand_image_stacks as _brand
        if node_kind in _brand.VLM_CLIENT_NODE_KINDS:
            return StackRef(key=node_kind, stack_name=_brand.VLM_STACK_NAME,
                            tenant_id=tid, family="vlm")
        sdxl_name = _brand.SDXL_CLIENT_STACK_BY_KIND.get(node_kind)
        if sdxl_name:
            return StackRef(key=node_kind, stack_name=sdxl_name,
                            tenant_id=tid, family="sdxl")
    except Exception:  # noqa: BLE001
        logger.debug("stack_wake: brand resolve failed for %s", node_kind, exc_info=True)

    # (2d) the served / wizard LLM (the fast generator, alias `ollama`).
    if node_kind in ("wizard-llm", "wizard_llm", "llm-call", "llm_call"):
        # managed_ai_stacks is Paid: on the CE carve it is absent, so the served
        # LLM has no managed stack to resolve to — the same None this branch
        # already returns for any unresolvable kind. optional_paid keeps the
        # paid path byte-identical and stops the boot traceback.
        from utils.paid_import import optional_paid
        _mai = optional_paid("services.managed_ai_stacks")
        if _mai is None:
            logger.debug("stack_wake: managed_ai_stacks not in this edition")
            return None
        try:
            return StackRef(key="wizard-llm", stack_name=_mai.GENERATOR_STACK_NAME,
                            tenant_id=tid, family="llm")
        except Exception:  # noqa: BLE001
            logger.debug("stack_wake: llm resolve failed", exc_info=True)

    return None


def _default_tenant_id() -> int:
    try:
        from services import brand_image_stacks as _brand
        return _brand.BRAND_STACKS_TENANT_ID
    except Exception:  # noqa: BLE001
        return int(os.getenv("BRAND_STACKS_TENANT_ID", "1"))


def _kinds_for_stack(ref: StackRef) -> set:
    """Every node_kind that resolves to the SAME stack as `ref` — the reverse of
    `resolve_required_stack`. This is what widens NIM's "sibling of the same
    node_kind" queue query to D5's "any task whose resolved stack == this stack":
    a stack fanned across several node kinds (or two SDXL generate kinds sharing
    one server) is kept warm / considered in-flight by ALL of them."""
    kinds: set = set()
    try:
        from services import nim_ephemeral as _nim
        for nk, spec in _nim.NIM_NODE_KINDS.items():
            if spec.stack_name == ref.stack_name:
                kinds.add(nk)
    except Exception:  # noqa: BLE001
        pass
    try:
        from services import brand_image_stacks as _brand
        if ref.stack_name == _brand.VLM_STACK_NAME:
            kinds |= set(_brand.VLM_CLIENT_NODE_KINDS)
        for nk, sname in _brand.SDXL_CLIENT_STACK_BY_KIND.items():
            if sname == ref.stack_name:
                kinds.add(nk)
    except Exception:  # noqa: BLE001
        pass
    if ref.family == "llm":
        kinds |= {"wizard-llm", "wizard_llm", "llm-call", "llm_call"}
    kinds.add(ref.key)
    return kinds


# ---------------------------------------------------------------------------
# Row + state helpers
# ---------------------------------------------------------------------------
def _find_row(db, ref: StackRef):
    """The PersistentStack row for `ref` (lowest id wins, tenant then the
    platform tenant as fallback — same lookup the brand/NIM resolvers use)."""
    import models
    tenants = [ref.tenant_id]
    dflt = _default_tenant_id()
    if dflt not in tenants:
        tenants.append(dflt)
    for tid in tenants:
        row = (db.query(models.PersistentStack)
               .filter(models.PersistentStack.name == ref.stack_name,
                       models.PersistentStack.tenant_id == tid)
               .order_by(models.PersistentStack.id.asc())
               .first())
        if row is not None:
            return row
    return None


def _active_statuses():
    from utils.accelerator_fit import ACTIVE_STACK_STATUSES
    return ACTIVE_STACK_STATUSES


def _is_running(row) -> bool:
    """True when the stack is up (or coming up) and MEANT to be up: it holds its
    card now. `desired_state=running` alone is tolerated because the reconciler
    intermittently flaps a healthy container's status off "running" (the VLM
    resolver bug) — we must not treat that flap as "off" and re-wake."""
    if row is None:
        return False
    return (row.status in _active_statuses()) or (row.desired_state == "running")


def _stack_has_inflight_task(db, ref: StackRef, *, exclude_task_id: Optional[int] = None) -> bool:
    """D5 queue lookahead: is ANY task that needs this same stack still in flight
    (PENDING/ASSIGNED/CLAIMED/RUNNING)? This is the refcount — counted from the
    queue, never a column (D4). Widened from NIM's same-node_kind check to every
    node_kind that resolves to this stack (`_kinds_for_stack`)."""
    import models
    kinds = _kinds_for_stack(ref)
    # `.as_string()`, NOT `.astext`: task_payload is a custom JSONB TypeDecorator
    # whose comparator has no `astext`, so `.astext` raises AttributeError. That
    # made _stack_has_inflight_task throw on EVERY call, so release_stack (and
    # thus sleep_back on /complete, /failed and the cache path) always failed —
    # NIMs never auto-slept. The only thing still turning them off was the
    # one-per-GPU competitor eviction; once that was made VRAM-aware (NIMs
    # co-reside now) nothing slept them at all. See nim-sleepback-astext-and-ownership.
    q = (db.query(models.ApiTaskQueue)
         .filter(models.ApiTaskQueue.tenant_id == ref.tenant_id,
                 models.ApiTaskQueue.status.in_(_inflight_statuses()),
                 models.ApiTaskQueue.task_payload["node_kind"].as_string().in_(list(kinds))))
    if exclude_task_id is not None:
        q = q.filter(models.ApiTaskQueue.id != exclude_task_id)
    return q.first() is not None


# ---------------------------------------------------------------------------
# Singleflight — first waker wakes, the rest attach (design "thundering wake")
# ---------------------------------------------------------------------------
_locks_guard = threading.Lock()
_locks: dict = {}


def _lock_for(ref: StackRef) -> threading.Lock:
    key = (ref.tenant_id, ref.stack_name)
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _locks[key] = lk
        return lk


# ---------------------------------------------------------------------------
# Audit (D9) — append-only, non-destructive
# ---------------------------------------------------------------------------
def _audit(db, ref: StackRef, action: str, prior_state: str, reason: str,
           actor: Optional[int], row=None) -> None:
    """Append one AuditEvent for a wake/sleep/evict. No commit here — it rides
    the SAME transaction as the state flip it records, so the trail and the
    change are atomic. Best-effort: a bookkeeping failure must never fail the
    wake/sleep it is recording."""
    try:
        import models
        db.add(models.AuditEvent(
            tenant_id=ref.tenant_id,
            user_id=actor,
            event_name=action,
            details={
                "stack": ref.stack_name,
                "stack_id": getattr(row, "id", None),
                "family": ref.family,
                "node_kind": ref.key,
                "prior_state": prior_state,
                "reason": reason,
            },
        ))
    except Exception:  # noqa: BLE001
        logger.debug("stack_wake: audit append failed for %s/%s",
                     ref.stack_name, action, exc_info=True)


def _weights_source(db, row) -> str:
    """Best-effort marker for whether the woken stack's weights came from the
    LOCAL model cache ("cached") or had to be fetched cold ("fetched").

    Nothing in the wake path reports an explicit fetch event — the worker loads
    weights inside the container and stays silent. The one signal that exists
    in the system is `StackObservation.last_running_at`: if THIS host has run
    this stack before, its model-cache bind-mount (a NIM's `/opt/nim/.cache`,
    a vLLM's HF cache) is primed, so the wake serves from cache; otherwise a
    first wake on that host is a cold fetch. This mirrors the reconciler's
    warm-host notion (`stack_reconciler._warm_hosts`) exactly. Anything
    unresolvable (no host recorded, observation table unreadable) -> "unknown"
    — we never fabricate a marker.
    """
    try:
        if not (row.id and row.target_worker):
            return "unknown"
        import models
        row2 = (db.query(models.StackObservation)
                .filter(models.StackObservation.stack_id == row.id,
                        models.StackObservation.worker_name
                        == row.target_worker,
                        models.StackObservation.last_running_at.isnot(None))
                .first())
        return "cached" if row2 is not None else "fetched"
    except Exception:  # noqa: BLE001 — the marker must never fail the wake
        logger.debug("stack_wake: weights-source check failed for %s",
                     getattr(row, "name", "?"), exc_info=True)
        return "unknown"


def _audit_ready(db, ref: StackRef, row, actor: Optional[int],
                 wake_duration_s: float) -> None:
    """Record the wake -> READY transition, ONCE per wake (D9 / GA.LSW.4b).

    The `stack.wake` row records the REQUEST; this row records the OUTCOME —
    how long until the woken stack was ready to serve (`wake_duration_s`,
    ready time minus wake request time) and whether its weights came from the
    local model cache or a cold fetch (`weights`). Written after the state-flip
    transaction has committed (the wake path commits before the readiness
    check), on its own commit so the ready record is durable even if the
    caller's later work rolls back. Best-effort: a bookkeeping failure must
    never fail the wake it is recording.
    """
    try:
        import models
        db.add(models.AuditEvent(
            tenant_id=ref.tenant_id,
            user_id=actor,
            event_name=AUDIT_READY,
            details={
                "stack": ref.stack_name,
                "stack_id": getattr(row, "id", None),
                "family": ref.family,
                "node_kind": ref.key,
                "prior_state": ("status=running,desired=running,"
                                "wake_owned=True"),
                "reason": "woken stack is ready to serve",
                "wake_duration_s": round(max(0.0, float(wake_duration_s)), 3),
                "weights": _weights_source(db, row),
            },
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("stack_wake: stack.ready audit append failed for %s",
                     ref.stack_name, exc_info=True)


def _prior_state(row) -> str:
    """A compact snapshot of the stack's state BEFORE we change it, for the
    audit record's `prior_state`."""
    return f"status={getattr(row, 'status', '?')},desired={getattr(row, 'desired_state', '?')}," \
           f"wake_owned={getattr(row, 'nim_wake_owned', '?')}"


def _publish(channel: str = "task:available:CPU") -> None:
    try:
        from routers.worker_tasks import _publish_event
        _publish_event(channel)
    except Exception:  # noqa: BLE001 — pub/sub is a nudge, not correctness
        logger.debug("stack_wake: publish %s failed", channel, exc_info=True)


# ---------------------------------------------------------------------------
# D6 — capacity-aware placement + safe eviction
# ---------------------------------------------------------------------------
def _demand_for(row) -> dict:
    from routers.persistent_stacks import _aggregate_accelerator_demand
    return _aggregate_accelerator_demand(getattr(row, "accelerator_requirements", None) or {})


def _worker_by_name(db, name: str):
    import models
    return (db.query(models.WorkerLicense)
            .filter(models.WorkerLicense.worker_name == name)
            .first())


def _safely_freeable(db, competitor, requester_ref: StackRef) -> bool:
    """A competitor is safely-freeable iff it is IDLE, or wake-owned with NO
    in-flight task needing it (D6). A stack with live work is never touched.

    "Idle / no in-flight task needing it" is answered from the queue (D4/D5):
    if any PENDING/RUNNING task resolves to the competitor's stack, it is in use
    and off-limits. We additionally require wake-ownership OR an idle state:
    a stack an OPERATOR stood up (not wake-owned) is left alone even when its
    queue is momentarily empty — evicting someone's deliberately-pinned model to
    make room is exactly the "force-evict in-use work" the spec forbids."""
    if getattr(competitor, "keep_running", False):
        return False  # c7: pinned floor — never freeable, even idle/wake-owned
    try:
        comp_ref = StackRef(key=competitor.name, stack_name=competitor.name,
                            tenant_id=competitor.tenant_id, family="generic")
        if _stack_has_inflight_task(db, comp_ref):
            return False  # live work needs it → never evict
    except Exception:  # noqa: BLE001 — if we cannot prove it idle, do not evict
        logger.debug("stack_wake: in-flight check failed for competitor %s",
                     getattr(competitor, "name", "?"), exc_info=True)
        return False
    # Wake-owned with an empty queue is safe (we or a prior on-demand job own it).
    if getattr(competitor, "nim_wake_owned", False):
        return True
    # Not wake-owned: only safe if it is not actually holding the card (idle).
    return competitor.status not in _active_statuses()


def _competitors_on_host(db, row, host: str) -> list:
    """Other GPU-demanding stacks currently placed on `host` (the requester
    excluded). These are the candidates the requester may need to free."""
    import models
    rows = (db.query(models.PersistentStack)
            .filter(models.PersistentStack.target_worker == host,
                    models.PersistentStack.id != getattr(row, "id", None))
            .all())
    # c7 (30.6.7 c): a keep_running stack is NEVER an eviction candidate in ANY
    # tier — the floor is applied at candidate time, so its devices stay counted
    # as used by available_devices(...) and the fit maths stays correct.
    return [c for c in rows if _demand_for(c)
            and not getattr(c, "keep_running", False)]


def _resident_slack(db, competitor) -> tuple:
    """Time-aware read of a resident competitor for eviction ranking (task 2.5,
    PAID). Thin CE-side forwarding shim: the real next-need slack model (reads
    `stack_demand.recoverable_detail`) lives in the optional `stack_predictive`
    module (see the guarded import at the top of this file) — moved there
    verbatim by the CE-launch Gate 4 carve. Absent module, or any fault, degrades
    to the same conservative reading the pre-carve code used: NOT recoverable,
    zero slack (do not evict it)."""
    if stack_predictive is None:
        return False, 0.0, 0.0
    try:
        return stack_predictive.resident_slack(db, competitor)
    except Exception:  # noqa: BLE001 — unprovable slack → treat as non-recoverable
        logger.debug("stack_wake: resident-slack read failed for %s",
                     getattr(competitor, "name", "?"), exc_info=True)
        return False, 0.0, 0.0


def _stack_model_repos(row) -> set:
    """GA.LSW.3 — HF repo ids this stack's served models resolve to: the weights
    that may ALREADY be cached on a host. Read off the compose spec's declared
    ``x-mod.served.model`` refs — the same authority the managed-AI pages use
    (``ai_config_service.managed_ai_wanted_models``). Empty when the stack
    declares no served model (e.g. a NIM, whose weights are not in the HF cache
    a worker scans), so the preference is inert for those."""
    repos = set()
    spec = getattr(row, "compose_spec", None) or {}
    for svc in ((spec.get("services") or {}).values()):
        ref = ((svc.get("x-mod") or {}).get("served") or {}).get("model")
        if isinstance(ref, str) and ref:
            repos.add(ref.split(":", 1)[0])
    return repos


def _cached_eligible_host(db, row, demand) -> Optional[str]:
    """GA.LSW.3 — weight-locality PREFERENCE, never a constraint. Among the
    online workers that can actually host `row`'s accelerator demand, return
    one that already reports the stack's weights cached
    (``hardware_specs['cached_models']`` — the HF repos on disk, see the Go
    worker's ``ScanCachedModels``), or None.

    A cached-but-ineligible host is never returned: eligibility is the same
    ``satisfies(available_devices(...), demand)`` arithmetic the claim-time
    guard applies, so a host we pin here is also one the guard will accept (a
    pin to a host the guard vetoes would strand the task ASSIGNED forever).
    None means "no cached eligible host" and the caller leaves placement
    exactly as the unpinned claim-time guard would do it."""
    try:
        import models
        from datetime import datetime, timezone, timedelta
        from utils.accelerator_fit import available_devices, satisfies
        from services.manager_lifecycle import silent_for
        from services.nim_ephemeral import WORKER_ONLINE_GRACE_S
        repos = _stack_model_repos(row)
        if not repos or not demand:
            return None
        now = datetime.now(timezone.utc)
        grace = timedelta(seconds=WORKER_ONLINE_GRACE_S)
        workers = (db.query(models.WorkerLicense)
                   .filter(models.WorkerLicense.status
                           == models.WorkerStatus.LICENSED)
                   .all())
        for w in workers:
            if silent_for(w.last_heartbeat, now) > grace:
                continue
            cached = {str(m) for m in
                      ((w.hardware_specs or {}).get("cached_models") or [])
                      if isinstance(m, str)}
            if not (repos & cached):
                continue
            if satisfies(available_devices(db, w), demand):
                return w.worker_name
    except Exception:  # noqa: BLE001 — the preference must never fail the wake
        logger.debug("stack_wake: weight-locality host lookup failed",
                     exc_info=True)
    return None


def plan_placement(db, ref: StackRef, row, *, for_prewarm: bool = False) -> tuple:
    """Decide whether `row` can be woken on its host, and what to free first.

    Returns one of:
      ("fits", [])                 — fits now, no eviction needed
      ("fits_after_evict", [rows]) — fits once these competitors are slept
      ("no_fit", [])               — cannot be made to fit → caller HOLDS

    Uses the shared `accelerator_fit` arithmetic (the same rule the SQL claim
    guard and the rebalancer use) so every placement decision agrees. An
    unpinned stack (no target host) is left to the claim-time guard to place —
    we return "fits" so the wake is dispatched unpinned rather than refused here,
    mirroring nim_ephemeral._pick_host returning None. EXCEPTION (GA.LSW.3): an
    unpinned stack is first offered a weight-locality PREFERENCE — if some
    eligible host already has the weights cached, the wake is pinned there
    (preference only: it narrows, never widens, the guard's eligible set).

    Eviction policy:
      * DEFAULT (predictive flag off) — frees only SAFELY-freeable competitors
        (idle, or wake-owned with no in-flight task); if that is not enough,
        no_fit. This is today's behavior, unchanged.
      * PREDICTIVE (paid layer, feature-flagged) — after the free ones, evict
        RECOVERABLE residents first (most slack first: they can be re-warmed
        before their own next need). If room still isn't found: a pre-warm DEFERS
        (no_fit) rather than evicting a non-recoverable resident into a stall; a
        real demand (for_prewarm=False) evicts the non-recoverable resident whose
        eviction produces the least predicted stall (priority-weighted), logging
        the tradeoff, and never silently drops a present demand.
    """
    from utils.accelerator_fit import available_devices, satisfies
    demand = _demand_for(row)
    if not demand:
        return "fits", []
    # W33: on the k8s stack backend the kube scheduler owns GPU fit — never hold
    # on, or evict peers from, a docker host's VRAM view (a k8s "stop" is a real
    # Deployment scale-down of another stack). No fit there = pod Pending.
    from services.stack_backend_dispatch import k8s_owns_placement
    if k8s_owns_placement(db, getattr(row, "tenant_id", None)):
        return "fits", []
    host = getattr(row, "target_worker", None) or getattr(row, "pinned_worker", None)
    if not host:
        # GA.LSW.3 — weight-locality PREFERENCE: if some ELIGIBLE host already
        # has the stack's weights cached, pin the wake there so the cold-fetch
        # (a multi-minute HF download on first start) is skipped. Pure
        # preference: it only ever NARROWS the claim-time guard's eligible set
        # to a host that same guard would accept, and when no host is cached
        # (or the lookup fails) the wake goes out UNPINNED exactly as before.
        # Never rewrites an explicit pin above (an operator pin outranks this).
        pref = _cached_eligible_host(db, row, demand)
        if pref:
            row.target_worker = pref  # `_wake` dispatches to row.target_worker
            logger.info("stack_wake: %s — weights already cached on %s; "
                        "preferring that host for the wake",
                        ref.stack_name, pref)
        return "fits", []
    worker = _worker_by_name(db, host)
    if worker is None:
        return "fits", []  # unknown host inventory → let claim-time guard decide
    row_id = getattr(row, "id", None)
    avail = available_devices(db, worker, exclude_stack_id=row_id)
    if satisfies(avail, demand):
        return "fits", []

    competitors = _competitors_on_host(db, row, host)

    def _fits_after(evict_list) -> bool:
        # available_devices already excludes `row`; excluding the evicted
        # competitors too models the state after they sleep.
        exclude = [row_id] + [c.id for c in evict_list]
        return satisfies(available_devices(db, worker, exclude_stack_id=exclude),
                         demand)

    # Tier 1 — safely-freeable (idle / owned-no-inflight): evicting these is free.
    safe = [c for c in competitors if _safely_freeable(db, c, ref)]
    if safe and _fits_after(safe):
        return "fits_after_evict", safe

    if not _predictive_sleep_enabled() or stack_predictive is None:
        # CE / legacy (module absent or flag off): only the safely-freeable set,
        # else hold. Unchanged from the pre-carve behavior.
        return "no_fit", []

    # ---- Predictive, time-aware eviction (task 2.5) — PAID, delegated ----
    # Tiers 2 (recoverable-first) + 3 (forced stall) live in stack_predictive.py.
    # `_fits_after` and `_resident_slack` are threaded in BY NAME (not reimported
    # inside the Paid module) so both tiers keep using this call's exact fit
    # arithmetic / resident-slack lookup — the same ones a test double may have
    # monkeypatched on this module.
    return stack_predictive.plan_evict_predictive(
        db, ref, competitors=competitors, safe=safe, for_prewarm=for_prewarm,
        fits_after=_fits_after, resident_slack_fn=_resident_slack)


def _evict(db, ref: StackRef, row, competitors: list, actor: Optional[int]) -> None:
    """Sleep the given safely-freeable competitors to make room (D6). For the NIM
    family we hand off to the baked-in one-per-GPU path UNCHANGED; for the rest we
    run the same non-destructive stop (desired_state flip + `stop` dispatch) and
    audit each eviction."""
    if not competitors:
        return
    # NIM keeps its proven, one-per-GPU eviction exactly (constraint in Impact):
    # nim_ephemeral.ensure_ready already invoked _stop_competitors before it ever
    # reaches this layer, so here we only handle the generalized (VLM/SDXL/LLM)
    # families and never re-implement or weaken the NIM path.
    from routers.persistent_stacks import _dispatch_stack_task, _project_name
    from services.stack_backend_dispatch import stack_op_dispatchable
    for c in competitors:
        prior = _prior_state(c)
        c.desired_state = "stopped"
        c.nim_wake_owned = False  # ownership released with the stop
        c.status_reason = f"slept to free GPU for {ref.stack_name}"
        # W30: stack_op_dispatchable subsumes the host guard — docker mode is
        # exactly `bool(c.target_worker)` (a stop must run where the
        # containers are; an unpinned stop would let any worker claim it and
        # "stop" a project it does not host, see stop_stack), and k8s mode
        # allows the hostless dispatch (W15/W18 in _dispatch_stack_task routes
        # the op to K8sStackBackend before worker_name is used). The
        # desired_state flip above still records the intent for the
        # reconciler.
        if stack_op_dispatchable(db, c):
            _dispatch_stack_task(db, c.target_worker, c.tenant_id, "stop",
                                 _project_name(c), stack_id=c.id)
        comp_ref = StackRef(key=c.name, stack_name=c.name, tenant_id=c.tenant_id,
                            family="generic")
        _audit(db, comp_ref, AUDIT_EVICT, prior,
               f"evicted (safely-freeable) to place {ref.stack_name}", actor, row=c)
        logger.info("stack_wake: evicting safe competitor %s to free GPU for %s",
                    c.name, ref.stack_name)


def _wake(db, ref: StackRef, row, actor: Optional[int]) -> None:
    """Flip the stack to desired_state=running, dispatch a start, take
    wake-ownership, and audit. Mirrors the existing on-demand VLM wake
    (`resolve_vlm_client`) and NIM `start` — a plain `compose start`, offline-safe
    and no image pull. Deploy of a never-deployed / torn-down stack is left to the
    family entry point (NIM) or the reconciler (`cache_backed`); this layer's job
    is the wake DECISION + ownership + audit, not re-implementing deploy."""
    from routers.persistent_stacks import _dispatch_stack_task, _project_name
    from services.stack_backend_dispatch import stack_op_dispatchable
    prior = _prior_state(row)
    row.desired_state = "running"
    row.status_reason = None
    row.nim_wake_owned = True   # WE woke it → we own it → we will sleep it back
    if stack_op_dispatchable(db, row):
        _dispatch_stack_task(db, row.target_worker, row.tenant_id, "start",
                             _project_name(row), stack_id=row.id)
    _audit(db, ref, AUDIT_WAKE, prior, "woke on demand for a node that needs it",
           actor, row=row)
    _publish()
    logger.info("stack_wake: waking %s (host=%s)", ref.stack_name,
                row.target_worker or "(claim-time)")


# ---------------------------------------------------------------------------
# D10 — readiness confirmation as a GENERIC, DECLARATIVE probe
#
# Starting a container is generic (`_wake` flips desired_state + dispatches a
# start for ANY family). But "started" != "serving": a NIM's gRPC service, an
# Ollama/served-LLM HTTP endpoint, and a VLM/SDXL container all take time to
# answer, and a node that hits a not-yet-listening endpoint fails. So readiness
# confirmation lives HERE — but as a DECLARATION, not a family branch:
#
#   readiness_probe_for(db, ref, row) -> ProbeSpec   (what to check + where)
#   _run_probe(db, ref, row, spec, timeout_s)        (a generic executor)
#
# The probe SPEC is sourced from what a stack ALREADY declares in its kind
# definition — nothing is invented: a NIM's `NimSpec` declares grpc_port (+
# grpc_service) -> a `grpc` probe; the brand VLM/SDXL kinds declare their HTTP
# port -> an `http` probe; a served LLM's compose alias+port -> an `http` probe.
# `family` is only the DEFAULT probe TYPE when nothing more specific is declared;
# the endpoint/port/path come from the stack's own declaration. A per-row
# `readiness_probe` value (if a deployment sets one on the PersistentStack — read
# defensively, NO new DB column/migration) overrides the derived spec. Anything
# undeclared falls back to a `status` probe (row reaches ACTIVE_STACK_STATUSES).
#
# `_run_probe` returns Optional[bool]:
#   True  -> confirmed serving
#   False -> polled to timeout without becoming ready (NOT an error, NOT a fail)
#   None  -> nothing probeable -> caller falls back to OPTIMISTIC ready.
# Every probe is defensive: any exception is swallowed and treated as not-ready,
# so a probe NEVER raises out of ensure_stack_ready.
# ---------------------------------------------------------------------------
@dataclass
class ProbeSpec:
    """A declared readiness check. `type` is one of http|grpc|tcp|status; the
    endpoint bits (host/port/path) come from the stack's kind declaration (or a
    per-row override). `path` may be a single str or a list of candidate paths
    for the http executor; None uses the health/models defaults."""
    type: str                       # "http" | "grpc" | "tcp" | "status"
    host: Optional[str] = None
    port: Optional[int] = None
    path: Optional[Any] = None      # str | list[str] | None (http only)
    timeout_s: Optional[float] = None


_DEFAULT_HTTP_PATHS = ("/v1/models", "/api/tags", "/health")


def _poll_until(check, timeout_s: float) -> bool:
    """Retry a single-shot readiness `check()` (bool) until it passes or the
    timeout. Any exception from `check` counts as not-ready and is retried."""
    deadline = time.time() + timeout_s
    while True:
        try:
            if check():
                return True
        except Exception:  # noqa: BLE001 — a failed attempt just means retry
            pass
        if time.time() >= deadline:
            return False
        time.sleep(STACK_READY_POLL_INTERVAL_S)


def _tcp_open_once(host: str, port: int, timeout_s: float) -> bool:
    """True if a TCP connection to host:port is accepted (the port is listening).
    Mirrors nim_ephemeral._grpc_port_open — a listening port means "ready to be
    dialed", which is the proven gate for a gRPC inference server."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception:  # noqa: BLE001
        return False


def _grpc_ready_once(host: str, port: int, timeout_s: float) -> bool:
    """gRPC readiness: a real Health/Check SERVING response when the grpc health
    libs are importable, else the proven TCP-accept fallback (nim's grpc gate).
    Never raises — a not-yet-serving server just reports not-ready."""
    try:
        import grpc
        from grpc_health.v1 import health_pb2, health_pb2_grpc
    except Exception:  # noqa: BLE001 — no grpc libs -> port-accept is the gate
        return _tcp_open_once(host, port, timeout_s)
    try:
        with grpc.insecure_channel(f"{host}:{port}") as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            resp = stub.Check(health_pb2.HealthCheckRequest(), timeout=timeout_s)
            return resp.status == health_pb2.HealthCheckResponse.SERVING
    except Exception:  # noqa: BLE001 — not serving yet -> retry
        return False


def _poll_row_active(db, ref: StackRef, row, timeout_s: float) -> bool:
    """Poll the stack row's status into ACTIVE_STACK_STATUSES until active or the
    timeout. Re-reads the row each pass so a reconciler flipping it to running is
    observed. This is the readiness signal for the VLM/SDXL/generic families,
    which have no cheap in-band gRPC/HTTP handshake we can universally rely on."""
    active = _active_statuses()
    deadline = time.time() + timeout_s
    cur = row
    while True:
        if cur is not None and getattr(cur, "status", None) in active:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(STACK_READY_POLL_INTERVAL_S)
        fresh = _find_row(db, ref)
        if fresh is not None:
            cur = fresh


def _poll_http_ok(base_url: str, timeout_s: float,
                  paths=("/v1/models", "/api/tags", "/health")) -> bool:
    """GET a health/models URL on a served endpoint, retrying until it answers or
    the timeout. "Answers" = any HTTP response with status < 500 on any candidate
    path (covers OpenAI-compatible `/v1/models`, Ollama-native `/api/tags`, and
    llama.cpp `/health`); a connection refused/timeout means "not listening yet"
    and is retried. `requests` is imported lazily so this module still imports in
    a minimal venv."""
    import requests  # lazy: keeps stack_wake importable without requests
    deadline = time.time() + timeout_s
    per_attempt = STACK_READY_HTTP_TIMEOUT_S
    base = base_url.rstrip("/")
    last = None
    while True:
        for p in paths:
            try:
                r = requests.get(base + p, timeout=per_attempt)
                if getattr(r, "status_code", 599) < 500:
                    return True
                last = f"HTTP {getattr(r, 'status_code', '?')}"
            except Exception as e:  # noqa: BLE001 — not listening yet -> retry
                last = str(e)
        if time.time() >= deadline:
            logger.debug("stack_wake: endpoint %s not answering (%s)", base, last)
            return False
        time.sleep(STACK_READY_POLL_INTERVAL_S)


def _derive_served_llm_base_url(row) -> Optional[str]:
    """Best-effort `http://<alias>:<port>` for a served LLM, derived from the
    stack row the way the served-LLM path resolves it: the compose service's
    network alias + its in-container port (served LLMs are reached BY NAME on the
    platform network — `http://ollama:11434` — not via a published host port).

    Falls back to the managed generator's documented alias/port. It NEVER
    invents an endpoint: if nothing is derivable it returns None, in which
    case the LLM family is treated as not-probeable (optimistic) rather than
    silently probed against a hard-coded alias. A slot must never resolve to
    a made-up URL (ai-config stack binding 8.3)."""
    from utils.paid_import import optional_paid
    # k8s_stack_endpoints is a PAID module (k8s reduction): on CE there is no
    # in-cluster Service DNS, so we fall through to the compose-spec/alias
    # derivation below.
    _k8s_ep = optional_paid("services.k8s_stack_endpoints")
    if _k8s_ep is not None:
        u = _k8s_ep.url_for(row)
        if u:
            return u
    # (1) faithful: parse the compose_spec for a network alias + internal port.
    try:
        spec = getattr(row, "compose_spec", None) or {}
        services = (spec.get("services") or {}) if isinstance(spec, dict) else {}
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            alias = None
            nets = svc.get("networks") or {}
            if isinstance(nets, dict):
                for n in nets.values():
                    if isinstance(n, dict) and n.get("aliases"):
                        alias = n["aliases"][0]
                        break
            port = None
            cmd = svc.get("command")
            if isinstance(cmd, (list, tuple)):
                for i, tok in enumerate(cmd):
                    if str(tok) == "--port" and i + 1 < len(cmd):
                        port = str(cmd[i + 1])
                        break
            if port is None:
                ports = svc.get("ports") or []
                if ports:
                    port = str(ports[0]).split(":")[-1]
            if alias and port:
                return f"http://{alias}:{port}"
    except Exception:  # noqa: BLE001 — derivation must never wedge the wake
        logger.debug("stack_wake: llm compose-spec URL derive failed", exc_info=True)
    # (2) fall back to the managed generator's stable alias/port.
    _mai = optional_paid("services.managed_ai_stacks")
    if _mai is None:
        logger.debug("stack_wake: managed_ai_stacks not in this edition "
                     "(no managed generator alias to fall back to)")
        return None
    try:
        alias = getattr(_mai, "_GGUF_ROLE_ALIASES", {}).get("generator", "ollama")
        port = getattr(_mai, "_GGUF_ROLE_PORTS", {}).get("generator", 11434)
        return f"http://{alias}:{port}"
    except Exception:  # noqa: BLE001 — no derivable endpoint: unknown, never invented
        logger.warning("stack_wake: no served-LLM endpoint derivable for row %s "
                       "(8.3: slot must not resolve to a hard-coded alias)",
                       getattr(row, "name", "?"))
        return None


def _split_url(url: str):
    """(host, port, path) from a URL, defensively. path is None when empty/'/'.
    stdlib-only so this works in a minimal venv."""
    from urllib.parse import urlparse
    p = urlparse(url)
    path = p.path if (p.path and p.path != "/") else None
    return p.hostname, p.port, path


def _derive_vlm_sdxl_url(db, ref: StackRef) -> Optional[str]:
    """The VLM/SDXL HTTP endpoint the brand kind declares, via the brand resolver
    (host.docker.internal:<published port><path>). This is the declared `http`
    probe target; when it cannot be derived, readiness_probe_for falls back to a
    `status` probe."""
    try:
        from services import brand_image_stacks as _brand
        if ref.family == "vlm":
            res = _brand.resolve_vlm_client(db, ref.tenant_id)
        else:
            res = _brand.resolve_sdxl_client(db, ref.tenant_id, ref.key)
        if res:
            _worker, candidates = res
            if candidates:
                return candidates[0]
    except Exception:  # noqa: BLE001
        logger.debug("stack_wake: vlm/sdxl URL derive failed", exc_info=True)
    return None


def _row_probe_override(row) -> Optional[ProbeSpec]:
    """A per-row declarative override, if a deployment set `readiness_probe` on
    the PersistentStack. Read DEFENSIVELY: it is NOT a declared column/migration —
    we only use it when it happens to be present and well-formed."""
    try:
        raw = getattr(row, "readiness_probe", None)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, dict) or not raw.get("type"):
        return None
    try:
        return ProbeSpec(type=str(raw.get("type")), host=raw.get("host"),
                         port=raw.get("port"), path=raw.get("path"),
                         timeout_s=raw.get("timeout_s"))
    except Exception:  # noqa: BLE001
        return None


def readiness_probe_for(db, ref: StackRef, row) -> Optional[ProbeSpec]:
    """Resolve the DECLARED readiness probe for `ref`/`row` — ONE place a family's
    check is sourced from what the stack already declares (D10):

      * per-row `readiness_probe` override (if present) wins;
      * NIM      -> `grpc` probe on NimSpec.grpc_port (host = its alias);
      * LLM      -> `http` probe on the served alias:port (compose-derived);
      * VLM/SDXL -> `http` probe on the brand kind's declared host:port/path;
      * anything else / undeclared -> `status` probe (row -> ACTIVE_STACK_STATUS).

    Never raises: a resolution fault degrades to the `status` probe."""
    try:
        override = _row_probe_override(row)
        if override is not None:
            return override

        if ref.family == "nim":
            from services import nim_ephemeral as _nim
            spec = _nim.NIM_NODE_KINDS.get(ref.key)
            if spec is not None:
                return ProbeSpec(type="grpc", host=spec.alias, port=spec.grpc_port)

        if ref.family == "llm":
            base = _derive_served_llm_base_url(row)
            if base:
                host, port, _path = _split_url(base)
                return ProbeSpec(type="http", host=host, port=port)  # default paths

        if ref.family in ("vlm", "sdxl"):
            url = _derive_vlm_sdxl_url(db, ref)
            if url:
                host, port, path = _split_url(url)
                return ProbeSpec(type="http", host=host, port=port, path=path)
    except Exception:  # noqa: BLE001 — resolution must never wedge the wake
        logger.debug("stack_wake: readiness_probe_for(%s/%s) failed",
                     ref.stack_name, ref.family, exc_info=True)

    # Default / undeclared: the row-status probe (works for any family).
    return ProbeSpec(type="status")


def _run_probe(db, ref: StackRef, row, spec: Optional[ProbeSpec],
               timeout_s: float):
    """Generic executor: run whatever `spec` declares until ready or timeout.
    Returns Optional[bool] (True ready / False timed-out / None not-probeable).
    Defensive: any error is swallowed and reported as not-ready — a probe NEVER
    raises out of here."""
    if spec is None:
        return None
    try:
        ptype = (spec.type or "status").lower()
        to = spec.timeout_s if spec.timeout_s is not None else timeout_s
        from utils.paid_import import optional_paid
        # k8s_stack_endpoints is a PAID module (k8s reduction): on CE there is no
        # in-cluster Service DNS, so probe_host is unavailable and we fall back
        # to the declared spec.host / target_worker.
        _k8s_ep = optional_paid("services.k8s_stack_endpoints")
        host = (((_k8s_ep.probe_host(db, ref, row, spec) if _k8s_ep is not None else None)
                 or spec.host
                 or getattr(row, "target_worker", None) or "host.docker.internal"))

        if ptype == "status":
            return _poll_row_active(db, ref, row, to)
        if ptype == "http":
            base = f"http://{host}:{spec.port}" if spec.port else f"http://{host}"
            return _poll_http_ok(base, to, _http_paths(spec.path))
        if ptype == "grpc":
            if not spec.port:
                return None  # can't dial without a port -> not probeable
            return _poll_until(lambda: _grpc_ready_once(host, spec.port,
                                                        STACK_READY_HTTP_TIMEOUT_S), to)
        if ptype == "tcp":
            if not spec.port:
                return None
            return _poll_until(lambda: _tcp_open_once(host, spec.port,
                                                      STACK_READY_HTTP_TIMEOUT_S), to)
        # Unknown declared type -> degrade to the status probe.
        return _poll_row_active(db, ref, row, to)
    except Exception:  # noqa: BLE001 — a broken probe is not-ready, never a crash
        logger.debug("stack_wake: probe %s for %s raised",
                     getattr(spec, "type", "?"), ref.stack_name, exc_info=True)
        return False


def _http_paths(path) -> tuple:
    """Normalise a ProbeSpec.path (str | list | None) into the http candidate
    paths. None -> the health/models defaults."""
    if path is None:
        return _DEFAULT_HTTP_PATHS
    if isinstance(path, (list, tuple)):
        return tuple(str(p) for p in path) or _DEFAULT_HTTP_PATHS
    return (str(path),)


def _start_failed_reason(db, ref: StackRef, row) -> Optional[str]:
    """Evidence that the `start` THIS wake dispatched already FAILED.

    The reconciler/worker-task result handler stamps a failed start by leaving
    `status` off the active set and writing `status_reason` ("<op> failed", or
    the worker's own text — live incident 2026-10-09, stack 8:
    "compose start failed: exit status 1 / no container found for project ...").
    `_wake` clears status_reason BEFORE it dispatches, so anything stamped here
    was written AFTER our start — it is not a leftover from an earlier op.

    Returns that reason, or None when the row gives no failed-start evidence.
    Deliberately NOT triggered by a merely non-active status on its own: the
    reconciler stamps status="running" asynchronously, so "still stopped, no
    reason" is the legitimate optimistic case (the row says the stack is meant
    to be up and nothing said otherwise). Never raises.
    """
    try:
        fresh = _find_row(db, ref) or row
        if getattr(fresh, "status", None) in _active_statuses():
            return None  # the stack is up — a stale stamp cannot un-say that
        raw = getattr(fresh, "status_reason", None)
        reason = str(raw).strip() if raw is not None else ""
        low = reason.lower()
        # The failed-start vocabulary: the result handler stamps `reason or
        # "start failed"` / `f"{stack_op} failed"`, and a worker's own text
        # names the start it could not run ("compose start failed: ...").
        # Requiring BOTH words keeps the reconciler's unrelated stamps (e.g.
        # "... This is a host problem, not a failed deploy.") out of here.
        if "start" in low and ("fail" in low or "error" in low):
            return reason
        return None
    except Exception:  # noqa: BLE001 — a failed re-read is "no evidence"
        logger.debug("stack_wake: failed-start re-read failed for %s",
                     ref.stack_name, exc_info=True)
        return None


def _confirm_ready(db, ref: StackRef, row, wait: bool) -> bool:
    """Turn a probe outcome into the EnsureResult.ready flag.

    * wait=False (default): NO probe is run — ready is optimistic exactly as
      today (this is what keeps every current caller byte-for-byte unchanged).
    * wait=True: resolve the declared probe and poll it up to
      STACK_READY_TIMEOUT_S; a None result (not-probeable) falls back to
      optimistic ready; True/False pass through (a timeout -> False, i.e.
      not-ready but not a failure)."""
    if not wait:
        return True
    try:
        spec = readiness_probe_for(db, ref, row)
    except Exception:  # noqa: BLE001 — resolution never wedges the wake
        spec = None
    result = _run_probe(db, ref, row, spec, STACK_READY_TIMEOUT_S)
    if result is None:
        return True  # not probeable -> optimistic (don't crash, don't block)
    return bool(result)


# ---------------------------------------------------------------------------
# Public API — ensure / release
# ---------------------------------------------------------------------------
def ensure_stack_ready(db, stack: Optional[StackRef], *, task=None,
                       actor: Optional[int] = None, wait: bool = False,
                       for_prewarm: bool = False) -> EnsureResult:
    """Ensure `stack` is ready before a node that needs it runs.

    Behavior (the three rules):
      * `stack is None`      → NO-OP (the node needs no stack). ready=True.
      * already RUNNING      → use as-is, do NOT take wake-ownership, do NOT sleep
                               it later (leave-on-if-on, D4). woke=False.
      * OFF + fits           → wake it (freeing only safely-freeable competitors
                               first), take wake-ownership. woke=True.
      * OFF + won't fit      → HELD (held=True): the caller must leave the task in
                               the scheduler's existing awaiting-resources hold —
                               NOT fail it, NOT invent a state (D7). The next
                               dispatch retries the wake when capacity frees.

    Singleflight per stack: the first waker wakes; concurrent callers for the same
    stack attach behind the lock and then find it already running.
    """
    if stack is None:
        return EnsureResult(ready=True, reason="no stack required (no-op)")

    row = _find_row(db, stack)
    if row is None:
        # No managed row for this stack (e.g. a NIM whose family entry point
        # creates it). Nothing this generalized layer can wake; report not-ready
        # without failing — the family path or a seed handles creation.
        logger.debug("stack_wake: no PersistentStack row for %s (tenant %s)",
                     stack.stack_name, stack.tenant_id)
        return EnsureResult(ready=False, reason="no managed stack row")

    if _is_running(row):
        # Rule 1: already up — borrow it, ownership untouched (leave-on-if-on).
        # With wait=True we still CONFIRM it is actually serving (D10) — "desired
        # running" can lag the real endpoint; with wait=False this is optimistic.
        ready = _confirm_ready(db, stack, row, wait)
        return EnsureResult(ready=ready, woke=False, stack_id=row.id,
                            host=getattr(row, "target_worker", None),
                            reason="already running (left on)" if ready
                            else "already running; not ready before timeout")

    lock = _lock_for(stack)
    with lock:
        # Re-read under the lock: another waker may have woken it while we waited.
        row = _find_row(db, stack)
        if row is None:
            return EnsureResult(ready=False, reason="no managed stack row")
        if _is_running(row):
            ready = _confirm_ready(db, stack, row, wait)
            return EnsureResult(ready=ready, woke=False, stack_id=row.id,
                                host=getattr(row, "target_worker", None),
                                reason="already running (left on)" if ready
                                else "already running; not ready before timeout")

        # Preserve the historical call signature exactly for the default path (so
        # existing callers / test doubles that expect plan_placement(db, ref, row)
        # keep working); only pass the pre-warm flag when it is actually set.
        if for_prewarm:
            decision, evictable = plan_placement(db, stack, row, for_prewarm=True)
        else:
            decision, evictable = plan_placement(db, stack, row)
        if decision == "no_fit":
            # Rule 3: won't fit and nothing safe frees enough → HOLD. No state
            # change, no failure; the node waits on the existing hold (D7).
            logger.info("stack_wake: %s cannot fit and nothing is safely-"
                        "evictable — holding the node (awaiting resources)",
                        stack.stack_name)
            return EnsureResult(ready=False, held=True, stack_id=row.id,
                                reason="won't fit; held for capacity")
        if decision == "fits_after_evict":
            _evict(db, stack, row, evictable, actor)

        wake_t0 = time.monotonic()  # wake request time (GA.LSW.4b duration anchor)
        _wake(db, stack, row, actor)
        stack_id = row.id
        host = row.target_worker
        db.commit()  # publish the transitions so the worker acts on them

        # D10: with wait=True, poll the declared readiness probe until the woken
        # endpoint actually serves (or times out). With wait=False (default) this
        # is optimistic-ready exactly as before — no probe runs. A timeout is
        # ready=False but NOT a failure (woke stays True; the node still waits).
        #
        # GA.LSW.4b / live 2026-10-09 stack 8: the optimistic fallback must NOT
        # fire when the START ITSELF failed. `_confirm_ready` reports "nothing
        # probeable" (no readiness probe declared) as optimistic-ready, which
        # handed a dead stack to the node — a guaranteed failure while the
        # platform believed it was serving. So before declaring ready, re-read
        # the row: if the reconciler already stamped this wake's `start` as
        # failed, the wake FAILED. That is a real failure (not a hold, not a
        # timeout): the caller fails the node fast with the reason the system
        # recorded, instead of running against nothing.
        # Scoped to wait=True: with wait=False the caller opted out of
        # confirmation entirely and stays byte-for-byte as before (the
        # documented invariant above).
        failed_start = _start_failed_reason(db, stack, row) if wait else None
        if failed_start:
            logger.error("stack_wake: %s wake FAILED — start did not come up "
                         "(status=%s): %s", stack.stack_name,
                         getattr(row, "status", "?"), failed_start)
            _audit(db, stack, AUDIT_WAKE_FAILED, _prior_state(row),
                   f"stack {stack.stack_name} failed to start: {failed_start}",
                   actor, row=row)
            # The wake transaction already committed above, so this row needs its
            # own commit to be durable (same contract as _audit_ready).
            try:
                db.commit()
            except Exception:  # noqa: BLE001 — audit bookkeeping never fails the wake
                try:
                    db.rollback()
                except Exception:
                    pass
            return EnsureResult(ready=False, woke=True, stack_id=stack_id,
                                host=host,
                                reason=(f"stack {stack.stack_name} failed to "
                                        f"start: {failed_start}"))
        ready = _confirm_ready(db, stack, row, wait)
        if ready:
            # GA.LSW.4b: record the wake->ready transition ONCE per wake — the
            # wake row records the request, this records the outcome (duration +
            # weights source) so wake_metrics can compute the duration split.
            _audit_ready(db, stack, row, actor,
                         wake_duration_s=time.monotonic() - wake_t0)
        return EnsureResult(ready=ready, woke=True, stack_id=stack_id, host=host,
                            reason="woke on demand" if ready
                            else "woke; not ready before timeout")


def release_stack(db, stack: Optional[StackRef], *, task=None,
                  actor: Optional[int] = None) -> ReleaseResult:
    """Release `stack` after a node that needed it terminates (success/fail/
    cancel). Sleeps it back IMMEDIATELY unless a guard keeps it up:

      * `stack is None`        → NO-OP.
      * VLM/SDXL + flag OFF     → NO-OP (rollout guard: keep their current
                                  never-auto-sleep behavior until enabled, 8.1).
      * another PENDING/RUNNING task needs the same stack → STAY WARM (D5
                                  lookahead / no-flap).
      * not wake-owned          → LEAVE ON (D4 leave-on-if-on).
      * otherwise               → sleep now: desired_state=stopped + `stop`
                                  dispatch, clear ownership, audit (D5/D9).
    """
    if stack is None:
        return ReleaseResult(slept=False, reason="no stack required (no-op)")

    if stack.family in ("vlm", "sdxl") and not _vlm_sdxl_sleepback_enabled(db):
        # Rollout guard (8.1): the VLM/SDXL sleep-back is a behavior CHANGE for
        # stacks that never auto-slept before. Default off = current behavior.
        return ReleaseResult(slept=False, reason="vlm/sdxl sleep-back disabled by flag")

    exclude_task_id = getattr(task, "id", None)

    # Guard 1 — should we keep it warm? Two policies, flag-selected:
    #
    #   PREDICTIVE (paid layer, feature-flagged, task 2.5) — sleep only when the
    #   stack is RECOVERABLE: the soonest predicted next-need across RUNNING runs
    #   is far enough out that it can be cold-warmed back in time (or nothing needs
    #   it). Otherwise keep it warm, because sleeping it would guarantee a stall it
    #   could not recover from before its next need. This replaces the binary
    #   "any in-flight task" check with a COMPUTED grace from real upcoming demand.
    #   (The just-finished node is already COMPLETED, so it never counts as a
    #   future need — no exclude_task_id needed here.)
    #
    #   BINARY (default, flag off) — today's behavior: keep warm iff ANY task that
    #   needs this stack is still in flight (PENDING/ASSIGNED/CLAIMED/RUNNING).
    if _predictive_sleep_enabled() and stack_predictive is not None:
        # PAID, delegated: the warm-time-aware next-need slack model lives in
        # stack_predictive.py (CE-launch Gate 4 carve). Module absent or flag off
        # -> the `else` binary lookahead below, unchanged from pre-carve.
        rec, slack, warm = stack_predictive.predictive_recoverable(db, stack)
        if rec is False:
            logger.info("stack_wake: keeping %s warm — next need in %.1fs is within "
                        "warm-time %.1fs+margin (predictive)", stack.stack_name,
                        slack if slack is not None else -1.0, warm)
            return ReleaseResult(slept=False,
                                 reason="kept warm — next need within warm-time (predictive)")
        if rec is None and _stack_has_inflight_task(
                db, stack, exclude_task_id=exclude_task_id):
            return ReleaseResult(slept=False, reason="sibling task in flight")
    else:
        if _stack_has_inflight_task(db, stack, exclude_task_id=exclude_task_id):
            logger.info("stack_wake: keeping %s warm — another task needs it (lookahead)",
                        stack.stack_name)
            return ReleaseResult(slept=False, reason="sibling task in flight")

    row = _find_row(db, stack)
    if row is None or row.desired_state == "stopped" or row.status == "torn_down":
        return ReleaseResult(slept=False, reason="already stopped / no row")

    # c7 (30.6.7 c): pinned floor — a keep_running stack is never slept back
    # after an on-demand job. Return BEFORE any mutation (ownership, desired
    # state and dispatch all stay untouched).
    if getattr(row, "keep_running", False):
        return ReleaseResult(slept=False, reason="keep_running (pinned)")

    # Guard 2 — wake-ownership (D4). Never sleep a stack we did not wake.
    if not row.nim_wake_owned:
        logger.info("stack_wake: leaving %s up — it was already on (not wake-owned)",
                    stack.stack_name)
        return ReleaseResult(slept=False, reason="not wake-owned (left on)")

    # Optional grace deferral (D5 belt-and-suspenders): re-check the lookahead
    # after a short wait to catch a task enqueued microseconds after guard 1.
    if STACK_SLEEP_GRACE_SECONDS > 0:
        time.sleep(STACK_SLEEP_GRACE_SECONDS)
        if _stack_has_inflight_task(db, stack, exclude_task_id=exclude_task_id):
            return ReleaseResult(slept=False, reason="sibling arrived during grace")

    from routers.persistent_stacks import _dispatch_stack_task, _project_name
    from services.stack_backend_dispatch import stack_op_dispatchable
    prior = _prior_state(row)
    row.desired_state = "stopped"
    row.nim_wake_owned = False   # ownership released with the stop
    row.status_reason = "slept after on-demand job (aggressive sleep-back)"
    # W30: stack_op_dispatchable subsumes the host guard — docker mode is
    # exactly `bool(row.target_worker)` (an unpinned stop must not be
    # enqueued), and k8s mode dispatches hostless (W15/W18 routes the op to
    # K8sStackBackend before worker_name is used).
    if stack_op_dispatchable(db, row):
        _dispatch_stack_task(db, row.target_worker, row.tenant_id, "stop",
                             _project_name(row), stack_id=row.id)
        _publish()
    _audit(db, stack, AUDIT_SLEEP, prior,
           "aggressive sleep-back: no other task needs it", actor, row=row)
    logger.info("stack_wake: sleeping %s after job (freeing GPU)", stack.stack_name)
    return ReleaseResult(slept=True, reason="slept (aggressive)")


# ---------------------------------------------------------------------------
# Convenience entry points for the dispatch pipeline
# ---------------------------------------------------------------------------
def ensure_for_node(db, node, *, tenant_id: Optional[int] = None, task=None,
                    actor: Optional[int] = None, wait: bool = False,
                    for_prewarm: bool = False) -> EnsureResult:
    """Universal ensure hook: resolve the node's stack and ensure it. No-op when
    the node needs no stack. This is the single call the node-dispatch path makes
    for EVERY node (D1)."""
    ref = resolve_required_stack(node, tenant_id=tenant_id)
    return ensure_stack_ready(db, ref, task=task, actor=actor, wait=wait,
                              for_prewarm=for_prewarm)


def _payload_has_awaiting_stack(payload) -> bool:
    """True when payload carries the GA.GSW.4a `awaiting_stack` marker either at
    the top level or nested under `node_data` — the same two locations
    hold_filter (GA.GSW.4b) refuses."""
    if not isinstance(payload, dict):
        return False
    if payload.get("awaiting_stack") is not None:
        return True
    nd = payload.get("node_data")
    return isinstance(nd, dict) and nd.get("awaiting_stack") is not None


def redrive_held_tasks(db) -> int:
    """GA.GSW.4c re-driver: sweep PENDING/ASSIGNED ApiTaskQueue rows whose payload
    still carries the `awaiting_stack` marker (set at dispatch time GA.GSW.4a when
    no fit existed, then refused by hold_filter GA.GSW.4b) and try to clear it now
    that some capacity may have freed.

    For each held row we re-run `ensure_for_node` on the same node the claim path
    would have refused. When a fit now exists (result is not `held`) the marker is
    popped — task.task_payload is reassigned to a fresh dict (WITHOUT the marker)
    so the JSONB column is dirtied and the task becomes claimable again; the count
    of cleared rows is returned. When the task is STILL held we leave the marker
    in place and move on. One row's failure (ensure raises, payload unreadable)
    must never stop the others: log and continue.
    """
    import models
    from sqlalchemy import or_
    target_statuses = [models.ApiTaskStatus.PENDING,
                        models.ApiTaskStatus.ASSIGNED]
    tp = models.ApiTaskQueue.task_payload
    rows = (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.status.in_(target_statuses),
                    or_(tp["awaiting_stack"].as_string().isnot(None),
                        tp["node_data"]["awaiting_stack"].as_string().isnot(None)))
            .all())

    cleared = 0
    for task in rows:
        payload = getattr(task, "task_payload", None)
        if not _payload_has_awaiting_stack(payload):
            continue
        try:
            node_kind = (isinstance(payload, dict) and
                         payload.get("node_kind"))
            requires_stack = (isinstance(payload, dict) and
                              payload.get("awaiting_stack"))
            if requires_stack is None:
                nd = payload.get("node_data")
                if isinstance(nd, dict):
                    requires_stack = nd.get("awaiting_stack")
            node = {"node_kind": node_kind, "requires_stack": requires_stack}
            result = ensure_for_node(
                db, node, tenant_id=task.tenant_id, task=task)
        except Exception:  # noqa: BLE001 — one row must never stop the sweep
            logger.debug("stack_wake: redrive failed for task %s",
                         getattr(task, "id", "?"), exc_info=True)
            continue

        if result.held:
            # Still no fit — keep the marker, the claim path keeps refusing it.
            continue

        # A fit exists now: drop the marker. Reassign task_payload to a NEW dict
        # so the JSONB column is dirtied and the change is flushed by the caller.
        try:
            new_payload = copy.deepcopy(payload)
            new_payload.pop("awaiting_stack", None)
            nd = new_payload.get("node_data")
            if isinstance(nd, dict):
                nd.pop("awaiting_stack", None)
            task.task_payload = new_payload
            db.flush()
            cleared += 1
        except Exception:  # noqa: BLE001 — report but do not abort the sweep
            logger.debug("stack_wake: failed to clear marker on task %s",
                         getattr(task, "id", "?"), exc_info=True)
    if rows:
        db.commit()
    return cleared


def requires_stack_from_payload(payload) -> Optional[str]:
    """The user-assigned `requires_stack` hint of a task payload, resolved in the
    SAME order as the dispatch side (routers.worker_tasks.
    _ensure_user_assigned_stack): `payload['node_data']['requires_stack']`
    first, then top-level `payload['requires_stack']`. None when absent."""
    if not isinstance(payload, dict):
        return None
    node_data = payload.get("node_data")
    hint = node_data.get("requires_stack") if isinstance(node_data, dict) else None
    if not hint:
        hint = payload.get("requires_stack")
    return hint or None


def release_for_task(db, task, *, actor: Optional[int] = None) -> ReleaseResult:
    """Universal release hook: resolve the finished task's stack and release it.
    No-op when the task's node needs no stack (D1). Called on node terminal
    (success/failure/cancel)."""
    try:
        payload = getattr(task, "task_payload", None) or {}
        node_kind = payload.get("node_kind")
        tenant_id = getattr(task, "tenant_id", None)
        lookup_key = {"node_kind": node_kind}
        hint = requires_stack_from_payload(payload)
        if hint:
            lookup_key["requires_stack"] = hint
    except Exception:  # noqa: BLE001
        return ReleaseResult(slept=False, reason="unreadable task")
    ref = resolve_required_stack(lookup_key, tenant_id=tenant_id)
    return release_stack(db, ref, task=task, actor=actor)


def ensure_llm_ready_for_base_url(db, base_url: str, *,
                                  tenant_id: Optional[int] = None,
                                  actor: Optional[int] = None) -> EnsureResult:
    """Wizard-LLM entry point (D8). Bring the served LLM behind `base_url` up on
    demand — wake if off and it fits, wait (held) if it does not, leave it up if
    already running — so a wizard request never fails merely because the model was
    off. Best-effort: any fault returns a not-ready result rather than raising,
    because the LLM call path must degrade to its own error handling, never crash
    on the wake attempt."""
    try:
        ref = resolve_required_stack("wizard-llm", tenant_id=tenant_id)
        if ref is None:
            return EnsureResult(ready=False, reason="wizard-llm not resolvable")
        return ensure_stack_ready(db, ref, actor=actor)
    except Exception:  # noqa: BLE001
        logger.debug("stack_wake: wizard LLM ensure failed for %s", base_url,
                     exc_info=True)
        return EnsureResult(ready=False, reason="ensure raised (ignored)")

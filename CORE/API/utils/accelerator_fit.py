"""Accelerator fit — one implementation, shared by every caller.

Two things decide whether a worker can host a stack's accelerator demand, and
both must give the same answer everywhere or the system contradicts itself:

  - the claim-time guard in worker_tasks._try_claim_task, which decides whether
    a worker may claim a queued stack task
  - stack_rebalancer, which decides whether a move is worth proposing
  - the unrunnable sweep, which explains why nothing can take the task

Two implementations of "can this worker run this" is a bug factory: the mover
proposes a host the claimer then refuses, and the stack sits mid-migrate with
nothing explaining why. So the arithmetic lives here.

CAPACITY, NOT LIVE VRAM. Fit reads the declared per-device capacity from
AcceleratorProfile rather than the live VRAM snapshot. A live sample races with
a task that started a second ago, so placing against it is a coin flip; declared
capacity minus what is already committed is a stable, defensible number.

DEMANDS ARE THE RESERVATION. A stack that is running on a worker is holding its
accelerators for its whole lifetime — vLLM claims its memory fraction at load
and never gives it back. So committed capacity is derived from the stacks
already placed there; no separate reservation table is needed, and there is no
way for a reservation record to drift out of sync with reality.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Mapping, Optional

logging.getLogger(__name__)
logger = logging.getLogger(__name__)

# The states in which a stack HOLDS its target_worker's VRAM. Public because
# placement is not the only decision that turns on it: whether a redeploy must
# be pinned to the host it already occupies is the same question, and answering
# it from a second hand-written tuple is how the two drift apart.
ACTIVE_STACK_STATUSES = ("running", "deploying", "migrating")


# stack-training-and-prewarm §"Fit-gate reads measured VRAM": when this flag is
# on, a NAMED stack's VRAM demand is resolved from its MEASURED serving VRAM
# (StackProfile.serving_vram_gb) in preference to the declared min_vram_gb guess,
# falling back to the declared value whenever the stack is unprofiled. Default-off
# so the fit-gate keeps its declared-only behavior until characterization has run
# and been validated in the field. Read per call so the flag can be flipped
# (and unit-tested) without a restart.
def _measured_vram_enabled() -> bool:
    return os.getenv("FIT_GATE_MEASURED_VRAM", "").strip().lower() in ("1", "true", "yes", "on")


def _override_demand_vram(demand: dict, measured_gb: float) -> dict:
    """Rescale a declared demand so its total VRAM equals the measured serving VRAM.

    The measurement is a single whole-stack number; a declared demand is a
    per-device vector (one entry per service/device, grouped by kind). Scaling
    every entry by measured/declared_total keeps the descending-domination shape
    the fit checks rely on while making the totals match reality. In the ordinary
    single-service case this is exactly "replace the one declared value with the
    measured one".
    """
    total = sum(float(v) for reqs in demand.values() for v in reqs)
    if total <= 0:
        # Nothing declared to attach the measurement to (no kind to split it
        # across) — keep the declared demand rather than invent a placement.
        return demand
    factor = float(measured_gb) / total
    return {kind: [float(v) * factor for v in reqs] for kind, reqs in demand.items()}


def resolve_stack_demand(db, stack_name: Optional[str], declared_demand: dict) -> dict:
    """A named stack's EFFECTIVE VRAM demand: measured serving VRAM when known,
    else the declared guess.

    This is the single place the measured-VRAM preference is applied, so both the
    demand a running stack contributes to `committed_demands` and any candidate
    demand a caller tests for a named stack resolve identically. Transparent by
    default: with the flag off, with no stack name (a raw node demand — deliverable
    #2), or when the stack is unprofiled, it returns the declared demand unchanged,
    so worker_can_host/satisfies and every existing caller keep their behavior.
    """
    if not declared_demand or stack_name is None or not _measured_vram_enabled():
        return declared_demand
    from utils.paid_import import optional_paid
    # stack_profile is a PAID module: on CE there is no measured serving VRAM,
    # so the declared demand is returned unchanged (the transparent default).
    _stack_profile = optional_paid("services.stack_profile")
    if _stack_profile is None:
        return declared_demand
    measured = _stack_profile.serving_vram_for(db, stack_name)
    if measured is None:
        return declared_demand
    return _override_demand_vram(declared_demand, measured)


def device_map(db, worker) -> Dict[str, List[float]]:
    """Declared per-device capacity for a worker: {kind: [vram desc]}.

    "" holds every device regardless of kind so a demand that names no kind can
    match anything.
    """
    import models
    out: Dict[str, List[float]] = {}
    if not getattr(worker, "hardware_profile_id", None):
        return out
    rows = (db.query(models.AcceleratorProfile.kind, models.AcceleratorProfile.vram_gb)
            .filter(models.AcceleratorProfile.hardware_profile_id == worker.hardware_profile_id,
                    models.AcceleratorProfile.vram_gb.isnot(None)).all())
    for kind, vram in rows:
        out.setdefault("", []).append(float(vram))
        out.setdefault(str(kind).strip().lower(), []).append(float(vram))
    for k in out:
        out[k].sort(reverse=True)
    return out


def _exclusion_set(exclude_stack_id) -> set:
    """Accept one stack id, several, or none.

    A single id answers "can this stack move here"; a SET answers "could this
    group fit if it were not already running" — which is what a rung serving a
    pair needs, and what a single id cannot express.
    """
    if exclude_stack_id is None:
        return set()
    if isinstance(exclude_stack_id, (int,)):
        return {exclude_stack_id}
    return {int(x) for x in exclude_stack_id}


# ---------------------------------------------------------------------------
 # WHOLE-CARD CHARGE (exclusive/pinned stacks)
 #
 # `_consume` is PACKING: two vLLM fractions share one card. That is right for
 # a stack the scheduler is free to co-tenant, but wrong for a stack whose
 # placement PINNED whole cards to it: gpu_reservation.plan_gpu_assignment
 # writes task_payload["gpu_assignment"] with mode "exclusive" when the card
 # is dedicated (pick_cards exclusive=True, or a leftover too small for
 # anything else). A vLLM pinned at --gpu-memory-utilization 0.9 physically
 # holds the card for its lifetime; charging it its declared guess (say 8 of
 # 24 GB) advertises 16 GB of "available" that OOMs the next task. So an
 # exclusive assignment charges the WHOLE card, read from the same placement
 # data cards_in_use reads (ApiTaskQueue rows, never a second ledger).
# ---------------------------------------------------------------------------

# Task statuses whose gpu_assignment is a real placement: COMPLETED (the
# assignment stays in the payload after the task finishes — cards_in_use
# source 2) or in flight (ASSIGNED/CLAIMED/RUNNING). PENDING has no
# assignment yet; FAILED/EXPIRED/CANCELLED rows are not placements.
_ASSIGNMENT_STATUSES = ("assigned", "claimed", "running", "completed")


def _latest_assignments_from_rows(rows) -> Dict[int, Mapping]:
    """Pure half of the placement read: rows newest-first -> {stack_id: gpu_assignment}.

    One pass, first row per stack wins (the caller orders `id desc`). A payload
    without a usable gpu_assignment (not a Mapping, no cards list) is skipped
    so an older task's assignment is not resurrected behind it.
    """
    out: Dict[int, Mapping] = {}
    for sid, payload in rows:
        if sid is None or sid in out:
            continue
        assignment = (payload or {}).get("gpu_assignment") if isinstance(payload, Mapping) else None
        if isinstance(assignment, Mapping) and isinstance(assignment.get("cards"), list):
            out[sid] = assignment
    return out


def _is_exclusive(assignment) -> bool:
    """Does this placement dedicate WHOLE cards (mode "exclusive", cards non-empty)?

    Missing mode defaults to "shared" exactly as cards_in_use does, so a
    placement written by an older release keeps today's packing behaviour.
    """
    if not isinstance(assignment, Mapping):
        return False
    cards = assignment.get("cards")
    if not isinstance(cards, list) or not cards:
        return False
    return str(assignment.get("mode") or "shared").strip().lower() == "exclusive"


def _exclusive_assignments(db, stack_ids: List[int]) -> Dict[int, dict]:
    """{stack_id: gpu_assignment} for the stacks whose LATEST placement is exclusive.

    One query for all ids (never per stack). The latest qualifying deploy/migrate
    task per stack wins — a stack in `deploying`/`migrating` carries its
    assignment only on the in-flight row, so in-flight statuses count too.
    Only mode == "exclusive" with a non-empty cards list is returned; shared
    placements keep today's packing behaviour. A DB/ledger gap must never wedge
    scheduling, so any error logs and answers {} (same pattern as cards_in_use).
    """
    if not stack_ids:
        return {}
    try:
        import models
        from sqlalchemy import func
        Q = models.ApiTaskQueue
        A = models.ApiTaskStatus
        rows = (db.query(Q.task_payload["stack_id"].as_integer(), Q.task_payload)
                .filter(Q.task_payload["stack_id"].as_integer().in_(list(stack_ids)),
                        Q.task_payload["stack_op"].as_string().in_(["deploy", "migrate"]),
                        Q.status.in_([A["ASSIGNED"], A["CLAIMED"], A["RUNNING"],
                                      A["COMPLETED"]]),
                        func.jsonb_exists(Q.task_payload, "gpu_assignment"))
                .order_by(Q.id.desc()).all())
    except Exception as e:  # noqa: BLE001 — a ledger gap must never wedge placement
        logger.warning("accelerator_fit: exclusive-placement read skipped: %s", e)
        return {}
    latest = _latest_assignments_from_rows(rows)
    return {sid: a for sid, a in latest.items()
            if str(a.get("mode") or "shared").strip().lower() == "exclusive"}


def _whole_card_demand(assignment: dict, fallback: dict) -> dict:
    """An exclusive assignment as a demand: one entry per pinned card's FULL size.

    `card_vram_mb / 1024.0` is the card's declared capacity, so `_consume`'s
    best-fit (smallest card with `have >= req`) zeroes exactly that card. A
    hair of epsilon keeps a `vram_gb*1024/1024` float round-trip from leaving a
    sub-epsilon sliver that `_usable` would then report as free; the epsilon is
    far below the 0.5 GB usable threshold, so it cannot change a decision.
    Cards with a missing/non-positive size fall back to the stack's own
    declared demand entry for them; a malformed payload never raises.
    """
    cards = assignment.get("cards")
    if not isinstance(cards, list):
        return fallback
    out: Dict[str, List[float]] = {}
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        kind = str(card.get("kind") or "").strip().lower()
        try:
            mb = float(card.get("card_vram_mb") or 0)
        except (TypeError, ValueError):
            mb = 0.0
        if mb > 0:
            out.setdefault(kind, []).append(mb / 1024.0 - 1e-6)
        else:
            # Unsized card: charge the stack's own declared entry for it.
            fb = fallback.get(kind) or fallback.get("") or []
            if fb:
                out.setdefault(kind, []).append(float(fb[0]))
    for k in out:
        out[k].sort(reverse=True)
    return out or fallback


def committed_demands(db, worker_name: str, exclude_stack_id=None) -> List[dict]:
    """Aggregate demands of stacks already placed on this worker.

    `exclude_stack_id` lets a stack be re-evaluated for its own move without
    counting itself as competing for the card it is about to leave.
    """
    import models
    from routers.persistent_stacks import _aggregate_accelerator_demand
    q = (db.query(models.PersistentStack)
         .filter(models.PersistentStack.target_worker == worker_name,
                 models.PersistentStack.status.in_(ACTIVE_STACK_STATUSES)))
    excluded = _exclusion_set(exclude_stack_id)
    if excluded:
        q = q.filter(~models.PersistentStack.id.in_(excluded))
    rows = q.all()
    # Belt-and-braces with the SQL exclusion filter above: an excluded stack
    # must never contribute — not its declared demand, and not its whole-card
    # charge.
    if excluded:
        rows = [s for s in rows if getattr(s, "id", None) not in excluded]
    # One placement query for every ACTIVE stack on this worker (the status
    # filter above already dropped stopped/failed/torn_down stacks, so a
    # stopped stack's stale completed-task assignment is never consulted).
    placements = _exclusive_assignments(db, [s.id for s in rows if s.id is not None])
    out = []
    for s in rows:
        d = _aggregate_accelerator_demand(s.accelerator_requirements or {})
        if not d:
            continue
        # A running stack HOLDS its measured serving VRAM, not its declared
        # guess — resolve so co-residency/eviction match reality.
        d = resolve_stack_demand(db, s.name, d)
        a = placements.get(s.id)
        if a and _is_exclusive(a):
            # Pinned exclusive: the engine holds the WHOLE card(s) — replace
            # the declared/measured demand with one entry per pinned card's
            # full size (shared placements keep the packing demand above).
            d = _whole_card_demand(a, d)
        out.append(d)
    return out


def _consume(devices: Dict[str, List[float]], demand: dict) -> Dict[str, List[float]]:
    """Charge a demand against device capacity, reducing the device it lands on.

    PACKING, not whole-device exclusion. Multiple CUDA processes share a card
    routinely — vLLM's --gpu-memory-utilization is a fraction, so two instances
    at 0.55 and 0.35 coexist on one card. Reserving a whole device per demand
    would make it impossible to run two models on a single-GPU host, which is a
    completely ordinary thing to want.

    Shared stays packing; EXCLUSIVE is handled upstream, not here: a stack the
    placement pinned to whole cards (gpu_assignment mode "exclusive") arrives
    as a demand of one entry per card's FULL size, so this same best-fit zeroes
    exactly the card the engine (vLLM --gpu-memory-utilization 0.9) really holds.
    `_consume`'s semantics for shared demands are untouched.

    First-fit-decreasing onto the smallest device that still fits: it leaves the
    big cards free for big demands instead of fragmenting them on small ones.
    """
    remaining = {k: list(v) for k, v in devices.items()}
    pool = remaining.get("", [])
    for kind, reqs in (demand or {}).items():
        key = kind or ""
        kind_pool = remaining.get(key, [])
        for req in sorted(reqs, reverse=True):
            req = float(req)
            # Best fit = smallest device with room, to avoid carving up a big
            # card that a later large demand will need.
            idx = None
            for i, have in enumerate(kind_pool):
                if have >= req and (idx is None or have < kind_pool[idx]):
                    idx = i
            if idx is None:
                continue
            before = kind_pool[idx]
            after = before - req
            kind_pool[idx] = after
            # A device is one physical card. Charging it under "cuda" must also
            # charge it under "" (and vice versa) or the same card's capacity
            # gets spent twice under two different keys.
            for other_key, other_pool in remaining.items():
                if other_key == key:
                    continue
                for j, v in enumerate(other_pool):
                    if v == before:
                        other_pool[j] = after
                        break
        remaining[key] = sorted(kind_pool, reverse=True)
    for k in remaining:
        remaining[k] = sorted(remaining[k], reverse=True)
    return remaining


# A device with only a sliver left is not usable capacity — reporting it as
# available makes a fully-committed card look free (its remainder is a non-empty
# list containing ~0) and lets both the node-task pool guard and the
# last-accelerator warning silently misfire.
_MIN_USABLE_GB = 0.5


def _usable(devices: Dict[str, List[float]]) -> Dict[str, List[float]]:
    """Drop remainders too small to host anything."""
    return {k: [v for v in vals if v >= _MIN_USABLE_GB] for k, vals in devices.items()}


def available_devices(db, worker, exclude_stack_id=None) -> Dict[str, List[float]]:
    """Declared capacity minus what stacks already on this worker are holding."""
    devices = device_map(db, worker)
    if not devices:
        return devices
    for d in committed_demands(db, worker.worker_name, exclude_stack_id):
        devices = _consume(devices, d)
    return _usable(devices)


def satisfies(devices: Dict[str, List[float]], demand: dict) -> bool:
    """Can this worker host `demand`?

    Two conditions, both necessary:

      1. every individual demand fits on SOME single device — a model loads
         into one device's memory, so 24+24 is not a 48 and never will be;
      2. the total demanded per kind fits the total available of that kind —
         processes DO share a card, so two models can co-tenant one GPU when
         their combined footprint fits.

    Deliberately expressed as these two checks rather than exact bin-packing,
    because the SQL claim guard has to compute the identical answer and exact
    packing is not reasonably expressible there. Keeping one rule that both
    implementations can evaluate is worth more than the precision: three
    components disagreeing about "can this worker run this" is how a mover
    proposes a host the claimer then refuses.

    Known imprecision: demands [30, 30] against devices [50, 10] pass both
    checks but cannot actually pack. That needs a multi-service stack with
    awkward sizes — and our own design puts independent services in separate
    stacks, so a stack's demand is normally a single entry, where these checks
    are exact. The cost of being wrong is a deploy that fails legibly rather
    than a silent mis-placement.
    """
    if not demand:
        return True
    for kind, reqs in demand.items():
        key = kind or ""
        pool = devices.get(key, [])
        if not pool:
            return False
        biggest = max(pool)
        for req in reqs:
            if float(req) > biggest:
                return False
        if sum(float(r) for r in reqs) > sum(pool):
            return False
    # Cross-key check: a physical card sits under BOTH its named kind and "",
    # so per-kind sums above double-count it — one 24 GB card would approve
    # 20 GB of cuda AND 20 GB of kindless demand (40 on 24). `_consume` already
    # cross-charges; this is the read-side equivalent. Total demand across ALL
    # keys must fit total PHYSICAL capacity, which is the sum of the named-kind
    # pools (device_map puts each card in exactly one named kind, plus "").
    total_demanded = sum(float(r) for reqs in demand.values() for r in reqs)
    total_physical = sum(v for k, pool in devices.items() if k for v in pool)
    if total_demanded > total_physical:
        return False
    return True


def worker_can_host(db, worker, demand: dict, exclude_stack_id: Optional[int] = None) -> bool:
    """Full check: does this worker have room for `demand` right now?"""
    if not demand:
        return True
    return satisfies(available_devices(db, worker, exclude_stack_id), demand)


def capacity_report(db, worker) -> dict:
    """What an operator needs to see: total devices, what stacks are holding,
    and what is left for ordinary accelerated tasks.

    Reserved capacity is invisible otherwise — a worker hosting a vLLM stack
    still advertises "cuda", and the only symptom of the card being spoken for
    is GPU tasks quietly never being scheduled there.
    """
    total = device_map(db, worker)
    avail = available_devices(db, worker)
    out = {}
    for kind, devices in total.items():
        if not kind:
            continue
        free = avail.get(kind, [])
        out[kind] = {
            "total": len(devices),
            "available": len(free),
            "reserved_by_stacks": len(devices) - len(free),
            "total_vram_gb": sorted(devices, reverse=True),
            "available_vram_gb": sorted(free, reverse=True),
        }
    return out


def would_exhaust_accelerators(db, worker, demand: dict) -> Optional[str]:
    """Warn when placing `demand` here would leave nothing for node tasks.

    On a single-GPU site, hosting managed AI means no GPU node tasks run at
    all. That is a legitimate trade, but the operator has to be told at the
    point of decision rather than discovering it through a queue that silently
    never drains.
    """
    if not demand:
        return None
    before = available_devices(db, worker)
    # Filter through the same usable-capacity threshold available_devices uses:
    # a card with a sliver left is exhausted for practical purposes, and
    # treating [0.0] as "still available" makes this warning never fire.
    after = _usable(_consume(before, demand))
    exhausted = []
    for kind, devices in before.items():
        if not kind or not devices:
            continue
        if not after.get(kind):
            exhausted.append(kind)
    if not exhausted:
        return None
    return (f"This will use the last available {', '.join(sorted(exhausted))} device(s) on "
            f"{worker.worker_name}. Accelerated node tasks will not be scheduled there while "
            f"this stack runs.")


# ---------------------------------------------------------------------------
# Hardware presence — "could this worker EVER run it", as distinct from
# worker_can_host's "does it have room right now".
#
# A worker that is merely full may free up; a worker with no AMD card will never
# run a ROCm stack, and a 16 GB card will never hold a 40 GB model. Pinning to
# the second kind produces a stack that is reported unplaceable forever, so the
# pin UI greys those hosts out and the pin endpoint refuses them.
#
# Only PROVABLE absence rules a worker out. A device whose VRAM was never
# measured (a unified-memory APU such as Strix Halo reports its GPU with no
# vram_gb) is present with unknown size, and a worker with no hardware profile
# yet is unknown altogether — neither is refused, because refusing on a guess
# would lock out exactly the machines whose reporting is incomplete.
# ---------------------------------------------------------------------------

_KIND_LABELS = {"cuda": "NVIDIA (CUDA)", "rocm": "AMD (ROCm)", "intel": "Intel",
                "npu": "NPU", "": ""}

# Reported card capacity sits a little under the marketing number (a "16 GB"
# card reports 15.99), and demands are declared in round numbers. Without slack
# a 16 GB model would be refused on the 16 GB card it was sized for.
_VRAM_SLACK_GB = 0.5

# Unified-memory NVIDIA devices (DGX Spark / GX10, Grace Hopper / Superchip)
# have no private framebuffer: NVML answers "Not Supported" for memory.total,
# so nvidia-smi prints [N/A] and the worker's NVML read reports 0 VRAM every
# beat while the card works perfectly — the whole pool is system RAM. A 0
# reading from such a device is NOT evidence of a broken driver, so it must
# never quarantine the card (the stack-39 409 on gb10). Matched on the device
# NAME because NVML exposes no unified-memory attribute; the benchmark row
# (or, when unmeasured, system RAM) supplies the size instead.
_UNIFIED_MEMORY_NAME_RE = re.compile(r"(gb[\s-]*10|grace|dgx[\s-]*spark|nvidia[\s-]*a[\s-]*[0-9]{3}[\s-]*h)")

# AMD APUs (Strix Halo / "AMD RYZEN AI MAX+ 395 w/ Radeon 8060S", whose GPU
# agent is just the gfx ISA id "gfx1151") are the ROCm half of the same blindspot
# as the NVIDIA unified-memory devices above: the iGPU shares system RAM, so the
# HIP read reports 0 dedicated VRAM every beat while the card works perfectly, and
# 1141 would otherwise quarantine "rocm" and make a re-deployed rocm stack return
# 409. Matched on the NAME because HIP exposes no unified-memory attribute. Kept
# deliberately tight: a discrete AMD card ("Radeon AI PRO R9000", the gfx1201
# family, gfx1100) MUST NOT match, so the ISA ids are pinned to the 115x APU
# generation and the marketing names to the APU-branded 8xx0S iGPU. ROCm only —
# the same words on a cuda row mean nothing.
_AMD_APU_UNIFIED_NAME_RE = re.compile(r"(gfx[\s-]*115\d|radeon[\s()a-z]{0,8}8\d{2}0s\b|ryzen[\s-]*ai|strix)")


def is_unified_memory_device(name: Optional[str], kind: str = "") -> bool:
    """True for a shared-memory (unified pool) accelerator whose NVML read is
    expected to report 0 / N/A device memory."""
    text = str(name or "").strip().lower()
    if not text:
        return False
    if bool(_UNIFIED_MEMORY_NAME_RE.search(text)):
        return True
    return str(kind or "").strip().lower() == "rocm" and bool(
        _AMD_APU_UNIFIED_NAME_RE.search(text))


def installed_devices(db, worker):
    """Every usable accelerator the worker has, sized or not.

    Returns `(devices, dead)`: devices is {kind: [vram or None]} over the
    SCHEDULABLE cards; dead is {kind: why} for kinds whose every card is
    quarantined (failed / unsupported / admin-disabled). A quarantined card is
    not present for placement purposes — the claim path withholds its queue
    (accelerator_quarantine.filter_worker_queues), so a stack pinned onto it
    would sit queued forever with nothing saying why.

    `(None, {})` when the worker has no hardware profile — nothing is known,
    which is not the same as knowing there is nothing.
    """
    import models
    from utils.accelerator_quarantine import UNSCHEDULABLE_STATUSES
    if not getattr(worker, "hardware_profile_id", None):
        return None, {}
    rows = (db.query(models.AcceleratorProfile.kind, models.AcceleratorProfile.vram_gb,
                     models.AcceleratorProfile.benchmark_status,
                     models.AcceleratorProfile.admin_disabled,
                     models.AcceleratorProfile.name)
            .filter(models.AcceleratorProfile.hardware_profile_id == worker.hardware_profile_id)
            .all())
    out: Dict[str, List[Optional[float]]] = {}
    dead: Dict[str, str] = {}
    # 1141 — a broken-NVML worker (driver/library mismatch, persistenced socket
    # gone) reports 0 VRAM every beat while crud/accelerator_cud.upsert_accelerators
    # refuses to overwrite the benchmark-sized vram_gb with a 0, so the row below
    # still reads as a present, sized card and placement keeps re-placing stacks
    # onto the dead card. The heartbeat handler records the disagreement in
    # hardware_specs JSONB; here the affected kinds are quarantined so hardware_gap
    # names the zero reading instead of treating the card as present-but-unsized.
    _hw = getattr(worker, "hardware_specs", None)
    _tele = _hw.get("gpu_telemetry_unhealthy_kinds") or [] if isinstance(_hw, dict) else []
    _tele_dead = bool(isinstance(_hw, dict) and _hw.get("gpu_telemetry_unhealthy"))
    _tele_why = (_hw.get("gpu_telemetry_unhealthy_reason")
                 or "GPU telemetry reported 0 VRAM") if _tele_dead else ""
    for kind, vram, status, disabled, dev_name in rows:
        k = str(kind).strip().lower()
        # A unified-memory device (GX10/Grace) reports 0/N/A framebuffer by
        # design — the telemetry flag beside it is a false positive, so the
        # card stays placeable and keeps its benchmark size.
        if _tele_dead and (not _tele or k in _tele) and not is_unified_memory_device(dev_name, k):
            dead.setdefault(k, _tele_why)
            continue
        if disabled or status in UNSCHEDULABLE_STATUSES:
            dead.setdefault(k, "disabled by an admin" if disabled else
                            "the driver can't use it" if status == "unsupported" else
                            "it failed its hardware check")
            continue
        v = float(vram) if vram is not None else None
        out.setdefault("", []).append(v)
        out.setdefault(k, []).append(v)
    # A kind with at least one live card is not dead.
    return out, {k: why for k, why in dead.items() if k not in out}


def _gb(v: float) -> str:
    return f"{v:.0f} GB" if abs(v - round(v)) < 0.05 else f"{v:.1f} GB"


def hardware_gap(devices: Optional[Dict[str, List[Optional[float]]]], demand: dict,
                 dead: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Why this hardware can never host `demand`, or None if it might.

    `dead` is installed_devices' {kind: why} for quarantined kinds, so a card
    that exists but cannot be used is named as such rather than as missing.
    Pure, so the reason text is testable without a database.
    """
    if not demand or devices is None:
        return None
    for kind, reqs in demand.items():
        key = (kind or "").strip().lower()
        label = _KIND_LABELS.get(key, key.upper())
        gpu = f"{label} GPU" if label else "GPU"
        have = devices.get(key, [])
        need = sorted((float(r) for r in reqs), reverse=True)
        if not have:
            why = (dead or {}).get(key)
            if why:
                return f"The {gpu} on this system is out of service ({why})"
            return f"No {gpu} on this system"
        if len(have) < len(need):
            return (f"Needs {len(need)} {gpu}s; this system has {len(have)}")
        if any(v is None for v in have):
            continue  # present but unsized — cannot prove it is too small
        sized = sorted(have, reverse=True)
        # Domination, as in _aggregate_accelerator_demand: the i-th largest card
        # must hold the i-th largest demand, since a model loads into one card.
        for i, req in enumerate(need):
            if sized[i] + _VRAM_SLACK_GB < req:
                if len(need) == 1:
                    return (f"Needs a {gpu} with {_gb(req)}; the largest here "
                            f"has {_gb(sized[0])}")
                big_enough = sum(1 for v in sized if v + _VRAM_SLACK_GB >= req)
                return (f"Needs {len(need)} {gpu}s with {_gb(req)} each; this "
                        f"system has {big_enough}")
    return None


def hardware_ineligible(db, stack, workers, device_cache: Optional[dict] = None) -> Dict[str, str]:
    """{worker_name: reason} for each worker whose hardware can never host `stack`.

    `device_cache` (worker_name -> installed_devices) lets a list endpoint
    evaluate every stack against every worker without re-querying per pair.
    """
    from routers.persistent_stacks import _aggregate_accelerator_demand
    demand = _aggregate_accelerator_demand(getattr(stack, "accelerator_requirements", None) or {})
    if not demand:
        return {}
    out = {}
    for w in workers:
        if device_cache is not None:
            if w.worker_name not in device_cache:
                device_cache[w.worker_name] = installed_devices(db, w)
            devices, dead = device_cache[w.worker_name]
        else:
            devices, dead = installed_devices(db, w)
        reason = hardware_gap(devices, demand, dead)
        if reason:
            out[w.worker_name] = reason
    return out

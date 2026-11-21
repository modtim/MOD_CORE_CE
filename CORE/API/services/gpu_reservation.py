"""Soft GPU VRAM reservation + card-picking logic (§14.1).

Pure-ish helpers used by the scheduler/placement layer to *reserve* a VRAM
budget for a node and to *pick* which worker cards it should live on. There is
no HOT logic here — this module only computes numbers and chooses cards from
in-memory inputs, so it is safe to import from workers, the API, and tests
without touching Docker, NIMs, or the container stack.

Two responsibilities:

1. ``vram_reservation_mb`` — decide how much VRAM (MB) to reserve for a
   node_kind.  Prefers the *learned* peak VRAM the platform recorded in the
   ``resource_patterns`` table (``ResourcePattern.avg_peak_vram_mb``), grows it
   by a margin, and clamps to a floor/cap.  Falls back to the node's declared
   minimum, then to a module default.  Any DB error degrades to declared/default
   (never raises) — the scheduler must not die because a stats lookup failed.

2. ``pick_cards`` / ``gpu_count_for_node`` — choose concrete worker cards and
   read the node's GPU count from its metadata.

``pick_cards`` works on two tiny dataclasses (``Card`` / ``Reservation``).  The
shape is also accepted as plain ``dict``/object: any attribute or key named
``index`` (card id), ``total_vram_mb`` (card total), ``tenant_id``,
``reserved_mb`` and ``mode`` (``"shared"`` / ``"exclusive"``) on a reservation
is honoured, so callers may pass ORM rows, dataclass fields, or dicts.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants (all env-overridable for on-prem/SaaS tuning).
# ---------------------------------------------------------------------------

#: Default VRAM growth margin applied to the learned peak: reservation = peak x (1 + margin).
_MARGIN = float(os.environ.get("MOD_GPU_VRAM_MARGIN", "0.15"))
#: Lower bound on any returned reservation (512 MB).  Keeps tiny nodes schedulable.
_FLOOR_MB = 512
#: Upper bound on any returned reservation (96 GB).  Never over-reserve a card.
_CAP_MB = 96 * 1024
#: Fallback reservation when nothing is learned or declared (4 GB).
_DEFAULT_MB = 4096

#: Same rounding slack the SQL claim guard applies to a worker's reported
#: card size (`utils.accelerator_fit._VRAM_SLACK_GB`), in MB. A card marketed
#: as 48 GB reports ~47.988 GB usable (`AcceleratorProfile.vram_gb`); a stack
#: whose declared/learned need is the round 48 GB (49152 MB) must still fit
#: it, or `pick_cards` refuses every real card of that size forever. Without
#: this, the claim-time guard here (strict `>=`, no slack) disagrees with the
#: SQL accelerator_guard in `routers/worker_tasks.py` (which already pads by
#: this same constant) — a task claims fine at the SQL layer, then this
#: picker bounces it straight back to ASSIGNED with `gpu_backoff_until`,
#: forever, every poll (see 2026-09-28 stack #27 incident).
try:
    from utils.accelerator_fit import _VRAM_SLACK_GB as _CARD_SLACK_GB
except Exception:  # pragma: no cover - import guard, mirrors module style
    _CARD_SLACK_GB = 0.5
_VRAM_SLACK_MB = _CARD_SLACK_GB * 1024


def _clamp_mb(value: float) -> int:
    """Clamp ``value`` into ``[_FLOOR_MB, _CAP_MB]`` and return an int."""
    try:
        return int(max(_FLOOR_MB, min(_CAP_MB, value)))
    except (TypeError, ValueError):
        return _DEFAULT_MB


# ---------------------------------------------------------------------------
# VRAM reservation
# ---------------------------------------------------------------------------

def vram_reservation_mb(
    node_kind: str,
    tenant_id: Optional[int],
    declared_min_mb: Union[int, float, None],
    db: Any,
    margin: Optional[float] = None,
) -> Tuple[int, str]:
    """Reserve how much VRAM (MB) a node of ``node_kind`` should claim.

    Resolution order:

    1. **Learned p95** — the 95th percentile of the ``peak_vram_mb`` values
       workers have reported in the last 50 COMPLETED ``ApiTaskQueue`` rows
       for this ``node_kind`` (``result_payload->>'peak_vram_mb'``), grown by
       ``margin`` and clamped.  Returns source ``"learned_p95"``.  A true tail
       estimate (requires >= 5 usable samples), so burstier runs are sized
       without inflating the margin.  Platform-wide per node kind
       (``tenant_id`` accepted for future tenant-scoping).
    2. **Learned peak** — the platform's recorded peak VRAM for this
       ``node_kind`` (``ResourcePattern.avg_peak_vram_mb``), grown by ``margin``
       and clamped.  Returns source ``"learned_peak"``.  This is a running
       average of per-run peaks; the margin absorbs burstier runs.
    3. **Declared min** — ``declared_min_mb`` when > 0.  Source ``"declared"``.
    4. **Default** — :data:`_DEFAULT_MB`.  Source ``"default"``.

    Any failure looking up the learned p95/peak (missing rows, DB error,
    malformed value) falls through to the next tier rather than raising, so a
    broken stats lookup can never take the scheduler down.

    Returns ``(mb, source)`` where ``source`` is one of
    ``{"learned_p95", "learned_peak", "declared", "default"}``.
    """
    effective_margin = _MARGIN if margin is None else margin

    p95 = _learned_p95_vram_mb(db, node_kind, tenant_id)
    if p95 and p95 > 0:
        reservation = _clamp_mb(p95 * (1.0 + max(0.0, effective_margin)))
        logger.debug(
            "VRAM reserve node_kind=%s tenant=%s learned_p95=%.0f -> %d MB",
            node_kind, tenant_id, p95, reservation,
        )
        return reservation, "learned_p95"

    learned = _learned_peak_vram_mb(db, node_kind, tenant_id)
    if learned and learned > 0:
        reservation = _clamp_mb(learned * (1.0 + max(0.0, effective_margin)))
        logger.debug(
            "VRAM reserve node_kind=%s tenant=%s learned=%.0f -> %d MB",
            node_kind, tenant_id, learned, reservation,
        )
        return reservation, "learned_peak"

    if declared_min_mb and float(declared_min_mb) > 0:
        reservation = _clamp_mb(float(declared_min_mb))
        logger.debug(
            "VRAM reserve node_kind=%s tenant=%s declared=%.0f -> %d MB",
            node_kind, tenant_id, declared_min_mb, reservation,
        )
        return reservation, "declared"

    logger.debug(
        "VRAM reserve node_kind=%s tenant=%s using default %d MB",
        node_kind, tenant_id, _DEFAULT_MB,
    )
    return _DEFAULT_MB, "default"


def _learned_p95_vram_mb(db: Any, node_kind: str, tenant_id: Optional[int]) -> Optional[float]:
    r"""Return the p95 of worker-reported peak VRAM (MB) for ``node_kind``, or None.

    ONE query: ``percentile_cont(0.95) WITHIN GROUP (ORDER BY peak)`` over the
    last 50 COMPLETED ``api_task_queue`` rows of this node kind (node kind is
    the ``task_payload->>'node_kind'`` JSON field; there is no dedicated
    column) that carry a numeric ``result_payload->>'peak_vram_mb'``.  The
    inner subquery filters on ``status`` + node kind first and takes
    ``id DESC LIMIT 50``; a regex guard (``~ '^[0-9]+(\.[0-9]+)?$'``) keeps malformed
    worker payloads from crashing the ``::float`` cast.

    Returns ``None`` (caller falls through to learned_peak/declared/default)
    when fewer than 5 usable samples exist, or on ANY error.  The query runs
    in a savepoint (``db.begin_nested()``) so a failed lookup rolls back just
    the savepoint and never poisons the surrounding session or raises.
    """
    try:
        from sqlalchemy import text
    except Exception as exc:  # pragma: no cover - import guard
        logger.debug("could not import sqlalchemy.text for p95 VRAM lookup: %s", exc)
        return None

    sql = text(
        r"""
        SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY peak) AS p95,
               count(*) AS n
        FROM (
            SELECT (t.result_payload->>'peak_vram_mb')::float AS peak
            FROM (
                SELECT result_payload
                FROM api_task_queue
                WHERE status = 'COMPLETED'
                  AND task_payload->>'node_kind' = :node_kind
                ORDER BY id DESC
                LIMIT 50
            ) t
            WHERE t.result_payload->>'peak_vram_mb' ~ '^[0-9]+(\.[0-9]+)?$'
        ) s
        """
    )
    try:
        with db.begin_nested():
            row = db.execute(sql, {"node_kind": node_kind}).first()
    except Exception as exc:
        # DB error (dead connection, cast failure, ...): degrade to the next tier.
        logger.debug("learned p95 VRAM lookup for %s failed: %s", node_kind, exc)
        return None

    if not row:
        return None
    try:
        p95 = float(row[0])
        n = int(row[1])
    except (TypeError, ValueError, IndexError):
        return None
    if n < 5 or not p95 > 0:
        return None
    return p95


def _learned_peak_vram_mb(db: Any, node_kind: str, tenant_id: Optional[int]) -> Optional[float]:
    """Return the platform's learned peak VRAM (MB) for ``node_kind``, or None.

    Reads ``ResourcePattern.avg_peak_vram_mb`` — the per-node_kind peak that
    ``pattern_updater`` records after each completed task.  We take the largest
    such value across all (queue_name, size_tier) rows for the node kind, since
    the scheduler must size for the node's worst observed tier.

    This is intentionally defensive: the table stores an *average of peaks*
    rather than raw samples, and ResourcePattern is not tenant-scoped, so we
    query by ``node_kind`` only.  Every failure mode (no rows, DB exception,
    NULL/unparseable value) returns ``None`` so the caller can fall through.
    """
    try:
        from models import ResourcePattern
    except Exception as exc:  # pragma: no cover - import guard
        logger.debug("could not import ResourcePattern for %s: %s", node_kind, exc)
        return None

    try:
        rows = db.query(ResourcePattern.avg_peak_vram_mb).filter(
            ResourcePattern.node_kind == node_kind,
        ).all()
    except Exception as exc:
        # DB error (dead connection, bad query, ...): degrade to declared/default.
        logger.debug("learned VRAM lookup for %s failed: %s", node_kind, exc)
        return None

    best: Optional[float] = None
    for (value,) in rows:
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if f > 0 and (best is None or f > best):
            best = f
    return best


# ---------------------------------------------------------------------------
# Card / Reservation model for pick_cards
# ---------------------------------------------------------------------------

@dataclass
class Card:
    """A single GPU card on a worker.

    Fields the picker consumes:
      * ``index``       — stable card id (used as the in_use key).
      * ``total_vram_mb` — total VRAM on the card, in MB.

    Anything else (worker id, gpu model, ...) is ignored by the picker.
    Accepts plain dicts with those keys too.
    """

    index: str
    total_vram_mb: float


@dataclass
class Reservation:
    """A single existing reservation occupying part of a card.

    Fields the picker consumes:
      * ``tenant_id``   — which tenant owns this reservation.
      * ``reserved_mb`` — VRAM MB held back by this reservation.
      * ``mode``        — ``"shared"`` or ``"exclusive"``.
    Accepts plain dicts with those keys too.
    """

    tenant_id: Optional[int]
    reserved_mb: float
    mode: str = "shared"


def _as_card(card: Union[Card, Mapping[str, Any]]) -> Card:
    if isinstance(card, Card):
        return card
    return Card(
        index=str(card["index"]),
        total_vram_mb=float(card["total_vram_mb"]),
    )


def _as_reservation(res: Union[Reservation, Mapping[str, Any]]) -> Reservation:
    if isinstance(res, Reservation):
        return res
    return Reservation(
        tenant_id=res.get("tenant_id"),
        reserved_mb=float(res.get("reserved_mb", 0) or 0),
        mode=str(res.get("mode", "shared")),
    )


# ---------------------------------------------------------------------------
# Card picking
# ---------------------------------------------------------------------------

def _card_id(card: Card) -> str:
    return str(card.index)


def _card_free_mb(card: Card, reservations: List[Reservation]) -> float:
    """Free VRAM (MB) on ``card`` given its current reservations."""
    held = sum(r.reserved_mb for r in reservations)
    return max(0.0, card.total_vram_mb - held)


def _card_joinable(card_id: str, reservations: List[Reservation], tenant_id: Optional[int],
                   cross_tenant_ok: bool) -> bool:
    """True when a new tenant's reservation can be placed on this card."""
    if not reservations:
        # Empty cards are always joinable (a fit is chosen below separately).
        return True
    # An existing exclusive reservation makes the card non-joinable.
    if any(r.mode == "exclusive" for r in reservations):
        return False
    # Cross-tenant enforcement.
    if not cross_tenant_ok:
        if any(r.tenant_id != tenant_id for r in reservations):
            return False
    return True


def pick_cards(
    cards: Sequence[Union[Card, Mapping[str, Any]]],
    in_use: Mapping[str, Sequence[Union[Reservation, Mapping[str, Any]]]],
    need_count: int,
    need_mb: int,
    tenant_id: Optional[int],
    cross_tenant_ok: bool,
) -> Optional[Tuple[str, List[str]]]:
    """Pick worker cards for a node that needs ``need_count`` cards of ``need_mb``.

    Returns ``(mode, [card_ids...])`` on success or ``None`` when the node
    cannot be placed.

    Placement rules:

    * ``need_count > 1`` — must be ``need_count`` **empty** cards (no existing
      reservations).  Mode is always ``"exclusive"``.  Picks the smallest
      sufficient cards first.  Returns ``None`` if fewer than ``need_count``
      empty cards with ``total_vram_mb >= need_mb`` exist.
    * otherwise (``need_count == 1``), in order of preference:
      1. **Best-fit shared** — among non-empty *joinable* cards whose free VRAM
         is >= ``need_mb``, choose the card with the least leftover (best fit).
         Mode ``"shared"``.
      2. **Empty card** — an empty card with ``total_vram_mb >= need_mb``.
         Mode ``"shared"`` when the card is at least half full after the
         reservation (``total - need >= need``) so it can still host more,
         otherwise ``"exclusive"``.
      3. ``None`` — no joinable card can satisfy the request.
    """
    if need_count < 1:
        need_count = 1

    cards = [_as_card(c) for c in cards]

    # ---- multi-card: only empty cards, exclusive ----
    if need_count > 1:
        empty_sufficient = [
            c for c in cards
            if not in_use.get(_card_id(c))
            and c.total_vram_mb + _VRAM_SLACK_MB >= need_mb
        ]
        empty_sufficient.sort(key=lambda c: c.total_vram_mb)
        chosen = empty_sufficient[:need_count]
        if len(chosen) < need_count:
            logger.debug(
                "pick_cards: need %d empty cards (>= %d MB) but only %d available",
                need_count, need_mb, len(chosen),
            )
            return None
        return "exclusive", [_card_id(c) for c in chosen]

    # ---- single card ----
    # Best-fit shared across non-empty joinable cards.
    best: Optional[Tuple[str, float]] = None
    for c in cards:
        cid = _card_id(c)
        reservations = [_as_reservation(r) for r in in_use.get(cid, ())]
        if not reservations:
            continue  # empty cards handled below
        if not _card_joinable(cid, reservations, tenant_id, cross_tenant_ok):
            continue
        free = _card_free_mb(c, reservations)
        if free >= need_mb:
            leftover = free - need_mb
            if best is None or leftover < best[1]:
                best = (cid, leftover)

    if best is not None:
        return "shared", [best[0]]

    # Empty card with enough total VRAM.
    for c in sorted((x for x in cards if not in_use.get(_card_id(x))),
                    key=lambda c: c.total_vram_mb):
        if c.total_vram_mb + _VRAM_SLACK_MB >= need_mb:
            leftover = c.total_vram_mb - need_mb
            mode = "shared" if leftover >= need_mb else "exclusive"
            return mode, [_card_id(c)]

    logger.debug("pick_cards: no card can hold %d MB (tenant=%s cross_tenant_ok=%s)",
                 need_mb, tenant_id, cross_tenant_ok)
    return None


# ---------------------------------------------------------------------------
# GPU count from node metadata
# ---------------------------------------------------------------------------

def gpu_count_for_node(metadata: Mapping[str, Any]) -> int:
    """Read a node's GPU count from its ``metadata`` dict.

    Recognises the shapes the platform actually writes:

      * ``metadata["gpu"]`` = ``{"required": bool, "count": int}``
        (see ``node_builder/service_from_node.py`` / the Go ``GPURequest``
        contract).
      * ``metadata["gpu_count"]`` (integer), and ``metadata["gpu"]["count"]``.
      * ``metadata["gpu_required"]`` / ``metadata["gpu"]["required"]`` as a bool
        when no explicit count is present.

    Returns at least 1 whenever a GPU is required but no count is given, and
    never returns a value below 1.  Returns 0 only when GPUs are explicitly
    marked not required.
    """
    if not isinstance(metadata, Mapping):
        return 0

    gpu = metadata.get("gpu")
    gpu_meta: Optional[Mapping[str, Any]] = gpu if isinstance(gpu, Mapping) else None

    required: Optional[bool] = None
    count: Optional[int] = None

    if gpu_meta is not None:
        if gpu_meta.get("required") is not None:
            required = bool(gpu_meta.get("required"))
        count = gpu_meta.get("count")
    if "gpu_required" in metadata:
        required = bool(metadata.get("gpu_required"))
    if "gpu_count" in metadata:
        count = metadata.get("gpu_count")

    # Explicit "no GPU required" wins outright (CPU / non-GPU node).
    if required is False:
        return 0

    # An explicit count always wins (respect 0; floor negatives at 1 only when
    # a GPU is required).
    if count is not None:
        try:
            c = int(count)
        except (TypeError, ValueError):
            c = None
        if c is not None:
            return max(1, c) if required is True else max(0, c)

    # GPU required (explicitly or implied by a gpu block) but no count:
    # default to one whole GPU.
    if required is True:
        return 1
    if gpu_meta is not None and required is None:
        return 1

    return 0


# ---------------------------------------------------------------------------
# Claim-time card planning (14.2)
# ---------------------------------------------------------------------------

#: Returned by :func:`plan_gpu_assignment` when the worker has cards but none
#: can take the task right now (the caller releases it back to the queue).
UNPLACEABLE = "UNPLACEABLE"

#: Task statuses (``ApiTaskStatus`` names) whose ``gpu_assignment`` still
#: holds VRAM on a card.
_HOLDING_STATUSES = ("ASSIGNED", "CLAIMED", "RUNNING")

#: FX.165 p1a — an ASSIGNED row only holds while it was written within this
#: many seconds (the platform-settings lease below). CLAIMED/RUNNING rows
#: always hold: a worker has them.
_ASSIGNED_HOLD_LEASE_DEFAULT_S = 120


def _assigned_hold_lease_s(db: Any) -> int:
    """Seconds an ASSIGNED-but-unclaimed row may keep holding its cards.

    One platform-settings read (in-process cached, TTL-bounded) per
    :func:`cards_in_use` call, never per row. A failed/unreadable read falls
    back to the module default rather than to "hold nothing" or "hold
    forever": the lease is a leak bound, so a neutral, sane number is safer
    than either extreme. ``db is None`` (pure-planning callers) takes the
    same default — no session to read the override from.
    """
    if db is None:
        return _ASSIGNED_HOLD_LEASE_DEFAULT_S
    try:
        from services.platform_settings import get_setting
        return int(get_setting("gpu.assigned_hold_lease_seconds", db=db))
    except Exception as e:  # noqa: BLE001 — a settings gap must not wedge a claim
        logger.debug("gpu_reservation: assigned-hold lease read failed (%s)", e)
        return _ASSIGNED_HOLD_LEASE_DEFAULT_S


def cross_tenant_sharing_ok() -> bool:
    """May two tenants share one GPU card?

    ``MOD_GPU_CROSS_TENANT_SHARING`` wins when set. Otherwise SaaS refuses
    (tenants never share a card) and on-prem allows it (one organisation).
    """
    raw = os.environ.get("MOD_GPU_CROSS_TENANT_SHARING")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return os.environ.get("DEPLOYMENT_MODE", "onprem").strip().lower() != "saas"


def gpu_tenant_compatible(
    gpu_current_tenant_ids: Iterable[Optional[int]],
    task_tenant_id: Optional[int],
    task_tenant_dedicated: bool,
    other_dedicated_tenant_ids: Iterable[Optional[int]],
) -> bool:
    """GSEC FX.80 p4a (enterprise dedicated GPU) — pure tenant-isolation
    predicate for placing one task on one GPU card.

    Rules (dedicated tenants get a card to themselves, on BOTH sides):

    * ``task_tenant_dedicated`` True — the task's tenant is a dedicated GPU
      tenant. It may land on the card ONLY if NO other tenant currently has
      work there: any ``gpu_current_tenant_ids`` entry different from
      ``task_tenant_id`` makes placement forbidden.
    * a card currently runs a DEDICATED tenant's work (i.e.
      ``other_dedicated_tenant_ids`` contains a tenant other than this
      task's tenant) and this task belongs to a DIFFERENT tenant — forbidden,
      so ordinary tenants never share a card with a dedicated tenant.
    * otherwise (two ordinary tenants, or the same tenant's own cards) —
      allowed, leaving the pre-existing cross-tenant sharing policy to decide
      separately.

    Pure / DB-free by design (the scheduler passes it the sets it already
    knows). ``None`` tenant ids (platform/system work) are treated as
    non-dedicated and non-conflicting unless a dedicated tenant is present.
    """
    current = {str(t) for t in gpu_current_tenant_ids if t is not None}
    own = None if task_tenant_id is None else str(task_tenant_id)
    other_dedicated = {str(t) for t in other_dedicated_tenant_ids if t is not None}

    if task_tenant_dedicated:
        # A dedicated tenant's task needs a card with no other tenant's work.
        return not (current - {own})
    # An ordinary task may not land on a card running a dedicated tenant's
    # work (unless it IS that same dedicated tenant's own task).
    return not (other_dedicated - {own})


def _dedicated_tenant_ids(db: Any) -> Set[int]:
    """GSEC FX.80 p4c — the set of tenants with ``gpu.dedicated`` True.

    One bounded query over the (small) live tenant-scoped
    ``gpu.dedicated`` rows, cached by the caller per planning call — the
    setting is tenant-scoped (a platform row has no tenant to promise), so
    this is the whole "which tenants are dedicated" answer in a single read.
    ``db is None`` (older-worker / pure-planning callers) → empty set, i.e.
    no dedicated tenants, so the filter is a no-op and behaviour is
    identical to pre-p4c.
    """
    if db is None:
        return set()
    import models
    from services.platform_settings import DEDICATED_GPU_SETTING_KEY
    rows = (db.query(models.PlatformSetting.tenant_id,
                     models.PlatformSetting.value)
            .filter(models.PlatformSetting.key == DEDICATED_GPU_SETTING_KEY,
                    models.PlatformSetting.scope == "tenant",
                    models.PlatformSetting.deleted_at.is_(None))
            .all())
    return {tid for tid, val in rows if tid is not None and val is True}


def _dedicated_tenant_filter(
    dedicated: Set[int],
    in_use: Dict[str, List[Reservation]],
    task_tenant_id: Optional[int],
) -> Optional[Dict[str, Tuple[bool, Set[int]]]]:
    """GSEC FX.80 p4c — per-card dedicated-GPU tenant filter for the planners.

    Pure / in-memory (the caller passes the dedicated-tenant set it fetched
    ONCE per planning call — see :func:`_dedicated_tenant_ids`; no query here).
    For each candidate card it computes the dedicated tenants actually ON that
    card and returns ``{card_id: (task_tenant_dedicated, dedicated_on_card)}``
    — the two inputs :func:`gpu_tenant_compatible` needs, precomputed so the
    wiring does no DB work per card.

    Returns ``None`` (== "filter off, leave the pool alone") when the task's
    tenant is not dedicated AND no candidate card runs a dedicated tenant's
    work — the common, dedicated-GPU-free case, so the pool is untouched and
    ``pick_cards`` runs exactly as pre-p4c.

    ``task_tenant_id`` None (platform/system work) still gets a mapping when a
    dedicated holder is present; the wiring lets such a task through — the
    documented choice that a dedicated tenant's card must not run OTHER
    TENANTS' work but tenant-less platform maintenance may share (and a
    None holder is never an "other tenant", so it never blocks a dedicated
    tenant either).
    """
    if not dedicated:
        return None
    task_dedicated = task_tenant_id is not None and task_tenant_id in dedicated
    per_card: Dict[str, Tuple[bool, Set[int]]] = {}
    any_dedicated_holder = False
    for cid, res in in_use.items():
        card_tids = {r.tenant_id for r in res if r.tenant_id is not None}
        card_ded = {t for t in card_tids if t in dedicated}
        if card_ded:
            any_dedicated_holder = True
        per_card[cid] = (task_dedicated, card_ded)
    if task_dedicated or any_dedicated_holder:
        return per_card
    return None


# ---------------------------------------------------------------------------
# GSEC FX.80 p4c — the RESIDUE half of placement (the worker's scrub ledger
# seen from the API side)
# ---------------------------------------------------------------------------
# ``gpu_tenant_compatible`` above answers "may these tenants be ON this card
# at the same time". It says nothing about the card's HISTORY: a card that
# already ran tenant A's work and is now empty is still carrying A's VRAM
# residue, so handing it to tenant B without a scrub is the same cross-tenant
# leak the worker's own ledger refuses (gpu_tenant_scrub.go: on a tenant
# switch the worker scrubs BEFORE it starts the other tenant's job, and a
# failed scrub means "run refuses to start the other tenant's job on that
# card"). Placement must agree with the worker or the two halves disagree —
# the API pins a card the worker will then refuse forever, every poll.
#
# So the dedicated-GPU wiring applies the rule in BOTH directions:
#
# * a dedicated tenant never SHARES a card with another tenant concurrently
#   (``gpu_tenant_compatible``, already wired below);
# * a card SWITCHING tenants — any live holder (dedicated or ordinary) or a
#   card that previously ran another tenant's job and has since released its
#   VRAM — may host this task only with a successful scrub record for that
#   card, i.e. ``card_clear_for_tenant_switch`` from
#   services/gpu_scrub_evidence.py (FX.80 p3) must say True. Fail-closed:
#   no record and a newest-failed record both refuse the card, and the task
#   takes the existing UNPLACEABLE + ``gpu_backoff_until`` hand-back, which
#   is exactly the design note's "the next different-tenant claim returns
#   UNPLACEABLE (the existing 20 s backoff path) rather than running
#   unscrubbed" (docs/security-audit/gpu-tenant-isolation-design.md §2.1
#   item 4).
#
# Default behaviour is unchanged when the rule is off: the rule is gated by
# the existing ``worker.gpu_tenant_scrub`` platform setting (registered in
# services/platform_settings.py, default False == today's behaviour, pushed
# to the worker over the heartbeat ``tunables`` map), so an untouched fleet
# never pays a single audit read and placement is byte-identical to pre-p4c.
# No new setting and no env var: the worker's scrub gate IS the promise, and
# an API that demanded scrub evidence the worker was never told to produce
# would refuse every hosted card forever.
def _scrub_rule_on(db: Any) -> bool:
    """Is the residue rule live for THIS planning call?

    One platform-settings read (in-process cached, TTL-bounded) per planning
    call, never per card. ``db is None`` (older-worker / pure-planning
    callers) and any unreadable read both say OFF: the rule is an extra
    refusal, so a failed read must never wedge a claim — the claim keeps
    today's behaviour and the worker's own ledger still refuses the dirty
    card at exec time (fail-closed where it can be, fail-open where the
    evidence itself is unreachable).
    """
    if db is None:
        return False
    try:
        from services.platform_settings import get_setting
        return bool(get_setting("worker.gpu_tenant_scrub", db=db))
    except Exception as e:  # noqa: BLE001 — a settings gap must not wedge a claim
        logger.debug("gpu_reservation: scrub-rule gate read failed (%s)", e)
        return False


def _scrub_card_key(worker: Any, card: Mapping[str, Any]) -> Optional[str]:
    """The audit key a scrub record for ``card`` is filed under.

    The worker files its scrub records under the card's docker device
    identity — the NVIDIA uuid when reported, the host index otherwise
    (gpu_tenant_scrub.go ``scrubCardKey``), so this mirrors it EXACTLY
    (uuid-or-index, not ``worker_cards``' ``<worker>:<kind>:<index>`` fallback:
    the audit row is already scoped by ``worker_name``, which is what makes
    two uuid-less cards on two workers distinct). A worker with no name
    (a pure-planning caller) has no ledger to consult, so there is no key.
    """
    if not getattr(worker, "worker_name", None):
        return None
    uuid = card.get("device_uuid")
    return str(uuid) if uuid else str(card.get("index"))


def _scrub_evidence(db: Any) -> Optional[Any]:
    """The FX.80 p3 scrub-evidence module, or None on a CE tree.

    services/gpu_scrub_evidence.py is not in the open-core manifest, so a
    Community-Edition install has no scrub records to consult and the rule
    must degrade to neutral (no extra refusal) — same guard as the
    heartbeat's ingest site (routers/worker_management.py) so the two halves
    of the rule are stripped together.
    """
    from utils.paid_import import optional_paid
    return optional_paid("services.gpu_scrub_evidence")


def _tenant_label(tenant_id: Optional[int]) -> str:
    """The worker's tenant label for ``tenant_id`` (gpu_tenant_scrub.go
    ``tenantLabelOf``): a task with no tenant id is ``"unknown"``, not a
    wildcard, so switching to/from platform work scrubs like switching
    between two named tenants."""
    return str(tenant_id) if tenant_id is not None else "unknown"


def _card_scrub_ok(db: Any, worker: Any, card_key: Optional[str],
                   holders: List[Reservation], task_tenant_id: Optional[int]) -> bool:
    """May this task land on a card given that card's tenant HISTORY?

    The residue half of the placement rule, stated exactly as FX.80 p3
    states it (``card_clear_for_tenant_switch``'s own contract: "the caller
    passes the predecessor it already read from the reservation ledger
    (``cards_in_use``)"), so it adds ONE indexed audit read per candidate
    card and no new ledger:

    * ``holders`` — the card's live reservations from ``cards_in_use``
      (task holders AND resident stacks). Each is a tenant whose work has
      been on this card; landing a different tenant's task on the card is
      the switch the worker's own ledger (gpu_tenant_scrub.go
      ``tenantScrubLast`` / ``needsScrub``) would name, and that ledger
      scrubs BEFORE starting the other tenant's job and refuses to start
      it when the scrub fails. Placement must agree or the two halves
      disagree — the API pins a card the worker then refuses forever.
    * an empty card has no predecessor at all, which p3 reads as allowed
      ("nothing to separate") — so a fresh card costs nothing and the
      common fresh-worker path stays read-free.

    Same tenant, no predecessor, and unknown->unknown need no scrub; every
    other switch needs a SUCCESSFUL scrub record for that card (fail-closed:
    no record and a newest-failed record both refuse). True == allowed.
    """
    evidence = _scrub_evidence(db)
    if evidence is None or card_key is None:  # CE carve / no ledger to consult
        return True
    dst = _tenant_label(task_tenant_id)
    for res in holders:
        src = _tenant_label(res.tenant_id)
        if not evidence.card_clear_for_tenant_switch(
                db, worker_name=str(worker.worker_name), card=card_key,
                from_tenant=src, to_tenant=dst):
            return False
    return True


def worker_cards(db: Any, worker: Any, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """The worker's schedulable, sized GPU cards (of ``kind`` when given).

    ``kind`` is any accelerator kind the worker reports (``"cuda"``,
    ``"rocm"``, ``"intel"``, ...) — this function is kind-agnostic, callers
    decide which kind(s) they need. ``card_id`` is the device uuid when
    known, so two workers that see the same physical card (a WSL worker and
    a native Windows worker on one box) share one reservation ledger; it
    falls back to ``<worker>:<kind>:<index>`` — kind-qualified, because
    ``index`` is an ordinal WITHIN KIND on the host (devices.go), so on a
    hip+intel box an unqualified ``<worker>:<index>`` made the Intel index-0
    and the AMD index-0 the SAME card and merged their reservations. Stale
    DB reservations keep the old unqualified id and simply stop matching
    (treated as released, never as a false collision). Admin-disabled,
    failed/unsupported
    and unsized cards are skipped. ``pci_bus_id`` (when reported) lets the
    worker map a pinned card to its exact ``/dev/dri`` node without guessing
    from index — see Worker's ``gpu_assignment.go``.
    """
    import models
    from utils.accelerator_quarantine import UNSCHEDULABLE_STATUSES
    if not getattr(worker, "hardware_profile_id", None):
        return []
    AP = models.AcceleratorProfile
    rows = (db.query(AP.kind, AP.index, AP.uuid, AP.vram_gb,
                     AP.benchmark_status, AP.admin_disabled, AP.pci_bus_id)
            .filter(AP.hardware_profile_id == worker.hardware_profile_id)
            .all())
    cards: List[Dict[str, Any]] = []
    for kind_, index, uuid, vram_gb, status, disabled, pci_bus_id in rows:
        if disabled or status in UNSCHEDULABLE_STATUSES or not vram_gb:
            continue
        normalized_kind = str(kind_).strip().lower()
        if kind and normalized_kind != kind:
            continue
        cards.append({
            "card_id": uuid or f"{worker.worker_name}:{normalized_kind}:{index}",
            "index": index,
            "kind": normalized_kind,
            "total_vram_mb": float(vram_gb) * 1024.0,
            "device_uuid": uuid,
            "pci_bus_id": pci_bus_id,
        })
    return cards


def cards_in_use(db: Any, card_ids: Iterable[str]) -> Dict[str, List[Reservation]]:
    """Live reservations on ``card_ids``, across ALL workers.

    Two sources, both keyed by card id (so a physical card seen by two
    workers is accounted once):

    1. The ``gpu_assignment`` of every task still holding VRAM
       (claimed/running — always; assigned — only while it was written within
       the ``gpu.assigned_hold_lease_seconds`` lease, see below).
    2. 10-04 (Dell GPU0 OOM): the RESIDENT VRAM of stacks whose status is
       ``running``/``starting`` on a worker — their deploy task has
       completed and no longer counts under source 1, but the engine is
       still loaded on the cards. The card placement is read from the
       stack's latest COMPLETED deploy/migrate task (the claim-time
       guard writes ``gpu_assignment`` into the task payload and it stays
       there after completion — that is exactly what a stuck RUNNING
       deploy task leaked forever in incident 2026-09-28, so a completed
       one is equally a placement record). The reservation releases
       naturally when the stack leaves ``running`` (stopped/failed/
       torn_down rows are simply not read here). A stack with an
       in-flight deploy/migrate task that already carries a
       ``gpu_assignment`` is EXCLUDED from source 2 — source 1 counts it
       once, and counting both would double-hold the same cards.
    """
    import models
    from sqlalchemy import func
    wanted = {str(c) for c in card_ids}
    out: Dict[str, List[Reservation]] = {}
    if not wanted:
        return out
    Q = models.ApiTaskQueue
    # jsonb_exists = the JSONB `?` operator (the custom JSONB type has no
    # has_key comparator).
    #
    # FX.165 p1a — the ASSIGNED half of the holding set is LEASED. CLAIMED/RUNNING
    # rows hold unconditionally (a worker has the task, its claim is its own
    # expiry's job); an ASSIGNED row only holds while it was WRITTEN within the
    # lease (`api_task_queue.updated_at`, whose onupdate=func.now() is bumped by
    # the very write that stamped `gpu_assignment` — the hand-back in
    # routers/worker_tasks.py, the scheduler placement). An ASSIGNED row older
    # than the lease is a task nobody is going to finalize on those cards
    # (handed back unplaceable, or orphaned by a crashed scheduler), so it must
    # stop reserving VRAM — that leak is what made persistent-stack-deploy wait
    # 4044s on worker-worker-01/02-GPU (and 2026-09-28's "all cards full"
    # forever). The lease is a platform setting, not an env var.
    from sqlalchemy import and_, or_
    from datetime import datetime as _dt, timedelta, timezone
    A = models.ApiTaskStatus
    lease_hold = or_(
        Q.status.in_([A["CLAIMED"], A["RUNNING"]]),
        and_(Q.status == A["ASSIGNED"],
             Q.updated_at >= _dt.now(timezone.utc)
             - timedelta(seconds=_assigned_hold_lease_s(db))))
    rows = (db.query(Q.tenant_id, Q.task_payload)
            .filter(lease_hold)
            .filter(func.jsonb_exists(Q.task_payload, "gpu_assignment"))
            .all())
    for tenant_id, payload in rows:
        assignment = (payload or {}).get("gpu_assignment")
        if not isinstance(assignment, Mapping):
            continue
        mb = float(assignment.get("vram_limit_mb") or 0)
        mode = str(assignment.get("mode") or "shared")
        for card in assignment.get("cards") or ():
            cid = str(card.get("card_id") or "")
            if cid in wanted:
                out.setdefault(cid, []).append(
                    Reservation(tenant_id=tenant_id, reserved_mb=mb, mode=mode))
    # Source 2 — resident VRAM of running stacks (see docstring). Both
    # supporting queries are scoped to the running stacks' ids, so the cost
    # is bounded by the (small) number of running GPU stacks.
    try:
        S = models.PersistentStack
        running = (db.query(S.id, S.tenant_id)
                   .filter(S.status.in_(("running", "starting")))
                   .all())
        if running:
            running_ids = [s.id for s in running]
            # In-flight deploys/migrates already holding a gpu_assignment:
            # their VRAM is source 1; the resident read would double-count.
            in_flight_stack_ids = {
                sid for (sid,) in db.query(Q.task_payload["stack_id"].as_integer())
                .filter(Q.task_payload["stack_id"].as_integer().in_(running_ids),
                        lease_hold,
                        Q.task_payload["stack_op"].as_string().in_(
                            ["deploy", "migrate"]),
                        func.jsonb_exists(Q.task_payload, "gpu_assignment"))
                .distinct().all()
                if sid is not None
            }
            remainder = [s.id for s in running if s.id not in in_flight_stack_ids]
            completed_by_stack: Dict[int, Any] = {}
            if remainder:
                for sid, payload in (db.query(
                                            Q.task_payload["stack_id"].as_integer(),
                                            Q.task_payload)
                                     .filter(Q.status == models.ApiTaskStatus.COMPLETED,
                                             Q.task_payload["stack_op"].as_string()
                                             .in_(["deploy", "migrate"]),
                                             func.jsonb_exists(Q.task_payload,
                                                               "gpu_assignment"),
                                             Q.task_payload["stack_id"]
                                             .as_integer().in_(remainder))
                                     .order_by(Q.id.desc()).all()):
                    if sid is not None and sid not in completed_by_stack:
                        completed_by_stack[sid] = payload
            for s in running:
                if s.id in in_flight_stack_ids:
                    continue  # counted under source 1 — no double-hold
                assignment = (completed_by_stack.get(s.id) or {}).get("gpu_assignment")
                if not isinstance(assignment, Mapping):
                    continue  # never GPU-pinned — nothing to charge
                mb = float(assignment.get("vram_limit_mb") or 0)
                if mb <= 0:
                    continue
                mode = str(assignment.get("mode") or "shared")
                for card in assignment.get("cards") or ():
                    cid = str(card.get("card_id") or "")
                    if cid in wanted:
                        out.setdefault(cid, []).append(
                            Reservation(tenant_id=s.tenant_id, reserved_mb=mb,
                                        mode=mode))
    except Exception as e:  # noqa: BLE001 — a ledger gap must never wedge a claim
        logger.warning("cards_in_use: resident-stack accounting skipped: %s", e)
    return out


def plan_gpu_assignment(
    db: Any,
    worker: Any,
    *,
    node_kind: str,
    tenant_id: Optional[int],
    gpu_count: int,
    declared_min_mb: Optional[int],
    exclusive: bool = False,
    kind: Optional[str] = None,
) -> Union[None, str, Dict[str, Any]]:
    """Choose physical cards for a task the worker is claiming.

    Returns ``None`` when the worker reports no sized cards (an older worker:
    keep today's behaviour), :data:`UNPLACEABLE` when no card fits right now,
    or the assignment written to ``task_payload["gpu_assignment"]``::

        {"mode", "cards": [{"card_id", "device_uuid", "index", "kind",
          "card_vram_mb"}], "vram_limit_mb", "source"}

    ``exclusive=True`` takes whole empty cards only; ``kind`` limits the
    candidate cards to one accelerator kind (e.g. ``"cuda"``).
    """
    cards = worker_cards(db, worker, kind)
    if not cards:
        return None
    by_id = {c["card_id"]: c for c in cards}
    in_use = cards_in_use(db, by_id)
    need_mb, source = vram_reservation_mb(node_kind, tenant_id, declared_min_mb, db)
    need_count = max(1, int(gpu_count or 1))

    pool = [Card(index=c["card_id"], total_vram_mb=c["total_vram_mb"]) for c in cards]
    if exclusive:
        pool = [c for c in pool if not in_use.get(c.index)]
    # GSEC FX.80 p4c (Q63=A) — enterprise dedicated-GPU isolation: drop
    # candidate cards the task's tenant may not share. The dedicated-tenant
    # set is fetched ONCE per planning call and the filter is None in the
    # common case (no dedicated tenants involved), so pick_cards then runs
    # exactly as before. DOCUMENTED CHOICE: platform/system tasks
    # (tenant None) bypass the filter — a dedicated tenant's card must not
    # run OTHER TENANTS' work, but tenant-less platform maintenance may
    # share (and is never an "other tenant" to a dedicated tenant).
    if tenant_id is not None and in_use:
        # in_use empty ⇒ every candidate card is empty, on which a dedicated
        # tenant (and everyone else) is always placeable, so the dedicated
        # filter is a no-op — skip its one bounded read on that common path.
        ded = _dedicated_tenant_filter(_dedicated_tenant_ids(db), in_use, tenant_id)
        if ded is not None:
            def _ok(card: Card, _ded: Dict = ded) -> bool:
                card_tids = [r.tenant_id for r in in_use.get(card.index, ())]
                task_ded, others = _ded.get(card.index, (False, frozenset()))
                return gpu_tenant_compatible(card_tids, tenant_id, task_ded, others)
            pool = [c for c in pool if _ok(c)]
    # GSEC FX.80 p4c — the RESIDUE half: a card whose ledger says another
    # tenant's work has been on it may host this task only with a
    # successful scrub record (services/gpu_scrub_evidence.card_clear_for_
    # tenant_switch, fail-closed). Gated by the worker's own scrub switch
    # (`worker.gpu_tenant_scrub`, default OFF) so an untouched fleet pays
    # one cached settings read and nothing else, and placement stays
    # byte-identical to pre-p4c. Only consulted when a card HAS holders —
    # an empty card has no predecessor to separate (p3's own reading), so
    # the fresh-worker path stays read-free.
    if in_use and _scrub_rule_on(db):
        # The audit trail files a scrub under the card's docker device
        # identity (uuid-or-index), which is NOT always this planner's card
        # id (uuid-less cards get the `<worker>:<kind>:<index>` id), so the keys
        # are taken from the raw worker_cards rows, once per call.
        keys = {cid: _scrub_card_key(worker, c) for cid, c in by_id.items()}

        def _clear(card: Card) -> bool:
            return _card_scrub_ok(db, worker, keys.get(card.index),
                                  in_use.get(card.index, ()), tenant_id)
        pool = [c for c in pool if _clear(c)]
    picked = pick_cards(pool, in_use, need_count, need_mb, tenant_id,
                        cross_tenant_sharing_ok())
    if picked is None:
        return UNPLACEABLE
    mode, ids = picked
    if exclusive:
        mode = "exclusive"
    return {
        "mode": mode,
        "cards": [{
            "card_id": cid,
            "device_uuid": by_id[cid]["device_uuid"],
            "index": by_id[cid]["index"],
            "kind": by_id[cid]["kind"],
            "card_vram_mb": int(by_id[cid]["total_vram_mb"]),
            "pci_bus_id": by_id[cid].get("pci_bus_id"),
        } for cid in ids],
        "vram_limit_mb": int(need_mb),
        "source": source,
    }


def _gpu_pin_from_compose(compose: Any) -> Dict[str, List[str]]:
    """Admin-chosen GPU pin (GPUCAP): read the top-level ``x-mod.gpu_pin`` block.

    ``{service_name: [card_id, ...]}`` — the ALLOWED POOL per service, written
    by ``PUT /stacks/{id}/gpu-pin`` (P1) as a compose extension so it survives
    every re-parse/rollback and rides to the claim-time planner on the task
    payload's ``compose_spec`` string (no DB read, no migration).

    TOLERANT by design: the write-time endpoint validates; the planner must
    never wedge a claim over a malformed block, so anything unexpected (not a
    dict, not a list, non-str ids, blank/over-long ids, duplicates) is skipped
    and this never raises. Rule 1: absent / empty / not-a-dict = no pin.
    """
    if not isinstance(compose, dict):
        return {}
    ext = compose.get("x-mod")
    if not isinstance(ext, dict):
        return {}
    raw = ext.get("gpu_pin")
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for svc, ids in raw.items():
        if not isinstance(svc, str) or not isinstance(ids, list):
            continue
        clean: List[str] = []
        seen: Set[str] = set()
        for cid in ids:
            if not isinstance(cid, str):
                continue
            cid = cid.strip()
            if not cid or len(cid) > 128 or cid in seen:
                continue
            seen.add(cid)
            clean.append(cid)
        if clean:
            out[svc] = clean
    return out


def plan_service_gpu_assignments(
    db: Any,
    worker: Any,
    *,
    node_kind: str,
    tenant_id: Optional[int],
    services: Sequence[Mapping[str, Any]],
    pins: Optional[Mapping[str, Sequence[str]]] = None,
    refusal: Optional[dict] = None,
) -> Union[None, str, Dict[str, Dict[str, Any]]]:
    """Plan physical cards for EACH GPU-declaring service in one persistent-
    stack deploy task (§14.7b), instead of one kind/size for the whole task.

    ``services`` is one entry per compose service with an
    ``x-mod.accelerator`` block: ``{"name", "kind", "gpu_count",
    "declared_min_mb", "exclusive"}`` — the same per-need shape
    :func:`plan_gpu_assignment` takes for a whole task, just one per service.

    Returns ``None`` when the worker has no sized cards of any kind these
    services need (older worker: caller keeps today's unpinned behaviour),
    :data:`UNPLACEABLE` when ANY service can't be placed right now (the whole
    task hands back — a stack is one compose project, never half-deployed),
    or ``{service_name: <assignment dict>}`` where each assignment has
    :func:`plan_gpu_assignment`'s per-task shape.

    Services are planned in the given order, folding each pick into the
    in-use ledger before the next service is placed, so two same-kind
    services in THIS task that both fit one shared card actually land on
    (and share) it — matching what ``pick_cards`` would do for them as two
    already-committed separate tasks.

    GPUCAP (admin GPU pin, stacks only): ``pins`` is an optional
    ``{service_name: [card_id, ...]}`` ALLOWED POOL (from
    :func:`_gpu_pin_from_compose` of the task's compose ``x-mod.gpu_pin``).
    A pinned service's candidate pool is intersected with its pinned ids
    BEFORE the exclusive/dedicated/scrub filters, which still run unchanged
    — a pin never widens sharing, never bypasses ``admin_disabled``,
    dedicated-tenant isolation or the scrub rule, and never falls back to
    the worker's other cards. When a PINNED service cannot be placed and
    ``refusal`` is passed, it is filled with ``{"code", "detail"}`` naming
    the service, the pinned ids and needed-vs-available MB, so the caller
    can show the admin a legible refusal instead of a silent retry loop;
    unpinned services keep returning plain :data:`UNPLACEABLE`.
    """
    pins_map: Dict[str, List[str]] = {}
    if isinstance(pins, Mapping):
        for _pname, _pids in pins.items():
            if not isinstance(_pname, str) or not isinstance(_pids, Sequence) \
                    or isinstance(_pids, str):
                continue
            pins_map[_pname] = [c for c in _pids if isinstance(c, str) and c.strip()]
    kinds = {(s.get("kind") or "cuda") for s in services}
    by_id: Dict[str, Dict[str, Any]] = {}
    for kind in kinds:
        for c in worker_cards(db, worker, kind):
            by_id[c["card_id"]] = c
    if not by_id:
        return None

    in_use: Dict[str, List[Reservation]] = {
        cid: list(res) for cid, res in cards_in_use(db, by_id.keys()).items()}
    cross_ok = cross_tenant_sharing_ok()
    # GSEC FX.80 p4c (Q63=A) — same dedicated-GPU isolation as
    # plan_gpu_assignment: each service's candidate pool is filtered by
    # gpu_tenant_compatible before pick_cards (None in the common case).
    # The dedicated-tenant set is fetched ONCE per planning call (all
    # services share one tenant); the per-card mapping is recomputed after
    # each pick so a dedicated tenant's fold-in below is visible to the next
    # service's filter. DOCUMENTED CHOICE: a tenant-None (platform/system)
    # deploy bypasses the filter, same as the task path. If in_use is empty
    # at the start, every holder that ever appears is a pick folded in by
    # THIS task (the same tenant — always compatible), so the dedicated set
    # is a no-op and its read is skipped on that common path.
    dedicated = (_dedicated_tenant_ids(db)
                 if tenant_id is not None and in_use else set())
    # GSEC FX.80 p4c — the residue half, same rule as the task path: a card
    # another tenant's work has been on needs a successful scrub record
    # before this stack's service may land on it. Read ONCE per planning
    # call (the setting is cached in-process), and skipped when nothing
    # holds VRAM yet — every holder that then appears is a pick folded in
    # by THIS task, i.e. the same tenant, which needs no scrub.
    scrub_on = bool(in_use) and _scrub_rule_on(db)
    # Audit keys (docker device identity, uuid-or-index) for the same
    # reason as the task path — taken once for the whole planning call.
    scrub_keys = ({cid: _scrub_card_key(worker, c) for cid, c in by_id.items()}
                  if scrub_on else {})
    out: Dict[str, Dict[str, Any]] = {}
    for svc in services:
        kind = svc.get("kind") or "cuda"
        pool = [Card(index=c["card_id"], total_vram_mb=c["total_vram_mb"])
                for c in by_id.values() if c["kind"] == kind]
        if not pool:
            return UNPLACEABLE
        need_mb, source = vram_reservation_mb(
            node_kind, tenant_id, svc.get("declared_min_mb"), db)
        need_count = max(1, int(svc.get("gpu_count") or 1))
        exclusive = bool(svc.get("exclusive"))
        # GPUCAP — the admin pin is an ALLOWED SET, not a forced assignment:
        # intersect the pool with the pinned ids, then let the existing
        # exclusive / dedicated-tenant / scrub filters run on it UNCHANGED.
        # A pinned id that is not on this worker (or was dropped by
        # worker_cards because it is admin-disabled / unschedulable / unsized)
        # leaves nothing to place — no fallback to the worker's other cards,
        # and a legible refusal so the admin sees WHY instead of a 20 s loop.
        pin_ids = pins_map.get(str(svc["name"]))
        pinned = bool(pin_ids)
        if pinned:
            pin_set = set(pin_ids)
            pinned_pool = [c for c in pool if c.index in pin_set]
            if not pinned_pool:
                if refusal is not None:
                    refusal["code"] = "pin_no_cards_on_worker"
                    refusal["detail"] = (
                        f"service {svc['name']} is pinned to cards "
                        f"{sorted(pin_set)} but none of them is schedulable on "
                        f"worker {getattr(worker, 'worker_name', '?')} "
                        "(disabled, unschedulable, unsized, or not a card of "
                        f"this worker of kind {kind})")
                return UNPLACEABLE
            pool = pinned_pool
        if exclusive:
            pool = [c for c in pool if not in_use.get(c.index)]
        ded = _dedicated_tenant_filter(dedicated, in_use, tenant_id)
        if ded is not None:
            def _ok(card: Card, _ded: Dict = ded) -> bool:
                card_tids = [r.tenant_id for r in in_use.get(card.index, ())]
                task_ded, others = _ded.get(card.index, (False, frozenset()))
                return gpu_tenant_compatible(card_tids, tenant_id, task_ded, others)
            pool = [c for c in pool if _ok(c)]
        if scrub_on:
            def _clear(card: Card) -> bool:
                return _card_scrub_ok(db, worker, scrub_keys.get(card.index),
                                      in_use.get(card.index, ()), tenant_id)
            pool = [c for c in pool if _clear(c)]
        picked = pick_cards(pool, in_use, need_count, need_mb, tenant_id, cross_ok)
        if picked is None:
            if pinned and refusal is not None:
                # Classify so the admin gets a config error, not a busy-card
                # retry: same slack rule pick_cards uses
                # (total_vram_mb + _VRAM_SLACK_MB >= need_mb).
                fits = [c for c in pinned_pool
                        if c.total_vram_mb + _VRAM_SLACK_MB >= need_mb]
                if not fits:
                    refusal["code"] = "pin_card_too_small"
                    refusal["detail"] = (
                        f"service {svc['name']} needs {need_mb} MB but the "
                        f"pinned card(s) are too small: "
                        + ", ".join(f"{c.index}={int(c.total_vram_mb)} MB"
                                    for c in pinned_pool))
                else:
                    refusal["code"] = "pin_cards_busy"
                    refusal["detail"] = (
                        f"service {svc['name']} needs {need_mb} MB on "
                        f"{need_count} pinned card(s); they fit but are busy "
                        f"(held by other work or refused by tenant "
                        f"isolation/scrub): "
                        + ", ".join(f"{c.index}={int(c.total_vram_mb)} MB"
                                    for c in pinned_pool))
            return UNPLACEABLE
        mode, ids = picked
        if exclusive:
            mode = "exclusive"
        out[svc["name"]] = {
            "mode": mode,
            "cards": [{
                "card_id": cid,
                "device_uuid": by_id[cid]["device_uuid"],
                "index": by_id[cid]["index"],
                "kind": by_id[cid]["kind"],
                "card_vram_mb": int(by_id[cid]["total_vram_mb"]),
                "pci_bus_id": by_id[cid].get("pci_bus_id"),
            } for cid in ids],
            "vram_limit_mb": int(need_mb),
            "source": source,
        }
        # Fold this pick into the ledger so the next service in this same
        # task sees it (a fresh cards_in_use() read would not, since nothing
        # is committed to the DB until the whole task's plan succeeds).
        for cid in ids:
            in_use.setdefault(cid, []).append(
                Reservation(tenant_id=tenant_id, reserved_mb=float(need_mb), mode=mode))
    return out

"""
Simplified Queue Selector - Post-Phase 3d

With resource-aware admission control, queue selection is simple:
- Just pick the hardware type (CPU, CUDA, ROCm, Intel)
- Resource admission control handles capacity management
- No need for size-based queues (Tiny, Small, Mid, Large, etc.)

Old System (Complex):
- Routes based on file size: CPU_Tiny, CPU_Small, CPU_Large, etc.
- Workers listen to specific size queues based on their RAM
- 24 different queues to manage

New System (Simple):
- Routes based on hardware type: CPU, CUDA, ROCm, Intel
- Workers accept tasks based on available resources (RAM/CPU)
- 4 queues total (plus default)
- Better resource utilization (high concurrency)

Example:
    - Task needs GPU → Route to CUDA queue
    - Task needs 10GB RAM → Worker checks if it has 10GB free
    - If yes → Accept and reserve 10GB
    - If no → Reject and requeue (another worker picks it up)
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# MAC.1.s8b — the queue a native macOS (Apple Silicon) task routes to. The
# Go worker side (MAC.1.s7/s8a) reports the `metal` hardware tag and
# subscribes to exactly this queue name.
METAL_QUEUE = "Metal"


def _native_platforms(node_def: Any) -> Dict[str, Any]:
    """The `native.platforms` map declared by a native NodeDefinition.

    Reads `custom_metadata` in the two shapes the schema/wizard accept —
    `native: {platforms: {os: {...}}}` and a bare `platforms` map — and
    returns {} when the node is not a native one (`execution != 'native'`)
    or declares no platforms. Pure read, never mutates.
    """
    md = getattr(node_def, "custom_metadata", None)
    if not isinstance(md, dict) or md.get("execution") != "native":
        return {}
    block = md.get("native")
    platforms = None
    if isinstance(block, dict):
        platforms = block.get("platforms")
    if not isinstance(platforms, dict):
        platforms = md.get("platforms")
    return platforms if isinstance(platforms, dict) else {}


def _native_metal_queue(node_def: Any) -> Optional[str]:
    """MAC.1.s8b — "Metal" for a native node whose recipe names darwin.

    A native node has no image and no gpu_variants, so the variants rule
    below would route it to "CPU" and a Linux container worker would sit on
    it forever (platform_guard already refuses it there). Routing it to the
    Metal queue is what makes the Mac worker (hardware tag `metal`,
    SupportedQueues -> ["Metal"], MAC.1.s8a) the only pool that sees it.
    Native nodes whose recipe names only other platforms (windows/linux)
    keep their existing queue — unchanged behaviour.
    """
    platforms = _native_platforms(node_def)
    if "darwin" in platforms:
        return METAL_QUEUE
    return None


def select_queue_for_node(node_def: Any) -> str:
    """Derive the ApiTaskQueue.queue_name for a NodeDefinition at enqueue
    time.

    MAC.1.s8b exception to the gpu_variants rule: a native node (custom_
    metadata `execution: native` + a `native.platforms` map) has no image and
    no gpu_variants, so the variants rule would route it to "CPU" — a Linux
    container worker that can never run it. A native node whose recipe names
    `darwin` routes to the "Metal" queue instead (the queue a Mac worker
    subscribes to, MAC.1.s8a). Other native platforms (windows/linux) keep
    their existing queue.

    Source of truth: `NodeDefinition.gpu_variants` (a dict mapping
    backend tag → image tag, written by the wizard's
    /build-and-register-local from the set of variants that actually
    built successfully).

    Rule:
      - 0 variants (null / empty)           → "CPU"
      - exactly 1 variant                   → that variant uppercased
                                              ("CUDA", "ROCM", "INTEL",
                                              "NPU")
      - 2+ variants                         → "ACCELERATOR" (meta-queue;
                                              any accelerator-capable
                                              worker polls it, then
                                              variants_guard filters at
                                              claim time).

    Why not read `NodeDefinition.process_type`: the wizard can ship a
    node that built for {cuda, intel} — there's no single process_type
    that honestly describes it. gpu_variants is the ground truth; we
    derive here instead of trying to keep process_type in sync.

    Accepts `None` (e.g. a workflow that references a node_kind without a
    NodeDefinition row) and falls back to "CPU".
    """
    if node_def is None:
        return "CPU"
    # MAC.1.s8b — native darwin (macOS) recipes route to the Metal queue
    # before the gpu_variants rule: such a node has no image and no variants,
    # so the rule below would call it "CPU" (a container worker it can never
    # run on). Windows/linux native nodes fall through unchanged.
    metal = _native_metal_queue(node_def)
    if metal:
        logger.info(
            "Native darwin node routed to the Metal queue "
            "(macOS native recipe, MAC.1.s8b)"
        )
        return metal
    variants = getattr(node_def, "gpu_variants", None) or {}
    if not isinstance(variants, dict) or not variants:
        return "CPU"
    if len(variants) == 1:
        return next(iter(variants)).upper()
    return "ACCELERATOR"


def select_queue_for_node_data(node_def: Any, node_data: Any) -> str:
    """Queue for a workflow node, accounting for build-DAG VARIANT nodes.

    A cross-architecture build DAG (node-builder) emits per-variant build/
    smoke/push/sample-test nodes — `test-and-repair-cuda`, `push-rocm`, … —
    all of which reference the SAME generic system build-task NodeDefinition
    (`system-test-and-repair-one-variant`, etc.). That NodeDefinition has no
    `gpu_variants`, so `select_queue_for_node` returns "CPU" for every one of
    them — sending a cuda/rocm/intel variant's build + smoke + sample-test to a
    CPU worker, where the accelerator runtime test can't actually run. The real
    hardware target is on the graph node itself: `data.variant`.

    Rule: when `node_data["variant"]` is a concrete accelerator (anything other
    than the "hero" CPU-reference build or "cpu"/"default"), route to that
    accelerator's queue (uppercased — matches the worker's SupportedQueues()).
    Otherwise fall back to the NodeDefinition-derived queue. New accelerators
    self-extend (no allowlist) — same philosophy as the rest of the routing.
    """
    # MAC.1.s8b — a native darwin node wins over both the build-DAG variant
    # override and the gpu_variants rule: it has no image to build for, so a
    # `data.variant` on such a graph node must not pull it onto a CUDA/ROCM
    # container worker. Non-native nodes take the original expression
    # byte-for-byte.
    metal = _native_metal_queue(node_def)
    if metal:
        return metal
    return variant_queue_override(node_data) or select_queue_for_node(node_def)


def variant_queue_override(node_data: Any) -> Optional[str]:
    """The accelerator queue implied by a build-DAG node's ``data.variant``,
    or None when there is no concrete-accelerator override.

    Build-DAG variant nodes (test-and-repair-cuda, push-rocm, …) all share the
    generic system build-task NodeDefinition (no gpu_variants); their real
    hardware target is on the graph node as ``data.variant``. Returns that
    variant uppercased (CUDA/ROCM/INTEL/NPU/…) so build + smoke + sample-test
    land on matching-hardware workers. Returns None for the hero CPU-reference
    build, "cpu"/"default", or a missing/empty variant — the caller then falls
    back to its own routing (NodeDefinition.gpu_variants at root dispatch,
    node_data.process_type at downstream dispatch). New accelerators
    self-extend (no allowlist)."""
    variant = node_data.get("variant") if isinstance(node_data, dict) else None
    if isinstance(variant, str):
        vq = variant.strip().upper()
        if vq and vq not in ("CPU", "DEFAULT", "HERO"):
            return vq
    return None


# GO.9 — honest per-accelerator labels + verified-first routing.
#
# Four labels a variant can show, worst (never route) to informational:
#   INCOMPATIBLE    — node_def.incompatible_reasons has this key (build/port
#                      failure). Never selected.
#   NOT_RECOMMENDED — node_def.not_recommended_variants has this key (builds
#                      + runs, but flagged slow/divergent on this repo).
#                      Selected only when no better tier has a candidate.
#   VERIFIED        — node_def.variant_verification_status[variant] ==
#                      "verified". Preferred over unverified.
#   UNVERIFIED      — everything else eligible (pending / untested / no
#                      status recorded yet). The safe default — a variant
#                      that simply hasn't been smoke-tested yet is not
#                      penalized the way an incompatible/not-recommended one
#                      is (P9e's existing "NULL means try it once" rule).
STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"
STATUS_INCOMPATIBLE = "incompatible"
STATUS_NOT_RECOMMENDED = "not_recommended"

# Verification statuses that mean "known broken" — same set worker_tasks.py's
# gpu_variants gate already drops from the offered variants before a task
# ever reaches the claim SQL.
_BAD_VERIFICATION_STATUSES = {"failed", "numerical_drift"}


def variant_status_label(
    variant: str,
    incompatible_reasons: Optional[Dict[str, Any]] = None,
    verification_status: Optional[Dict[str, Any]] = None,
    not_recommended_variants: Optional[Dict[str, Any]] = None,
) -> str:
    """Single compact status label for one accelerator variant, honoring the
    same precedence used for routing: incompatible > not_recommended >
    verified > unverified."""
    incompatible_reasons = incompatible_reasons or {}
    verification_status = verification_status or {}
    not_recommended_variants = not_recommended_variants or {}
    if variant in incompatible_reasons:
        return STATUS_INCOMPATIBLE
    vs = verification_status.get(variant)
    if vs in _BAD_VERIFICATION_STATUSES:
        return STATUS_INCOMPATIBLE
    if variant in not_recommended_variants:
        return STATUS_NOT_RECOMMENDED
    if vs == STATUS_VERIFIED:
        return STATUS_VERIFIED
    return STATUS_UNVERIFIED


def compute_variant_status(node_def: Any) -> Dict[str, Dict[str, Any]]:
    """Per-variant `{status, perf_ratio}` for every accelerator variant a
    NodeDefinition knows about — union of its live `gpu_variants` keys and
    any keys only surviving in `incompatible_reasons` (a variant that failed
    is removed from gpu_variants by `patch_variant_status`, so it would
    otherwise vanish from the palette instead of showing as incompatible).

    Additive/derived only — reads existing columns (`gpu_variants`,
    `incompatible_reasons`, `variant_verification_status`,
    `not_recommended_variants`, `variant_performance`); adds no new storage.
    Used by the node list/detail API to give the WorkflowBuilder palette +
    node detail an honest per-accelerator label instead of pretending every
    listed variant is equally trustworthy.
    """
    gpu_variants = getattr(node_def, "gpu_variants", None) or {}
    incompatible_reasons = getattr(node_def, "incompatible_reasons", None) or {}
    verification_status = getattr(node_def, "variant_verification_status", None) or {}
    not_recommended_variants = getattr(node_def, "not_recommended_variants", None) or {}
    performance = getattr(node_def, "variant_performance", None) or {}

    if not isinstance(gpu_variants, dict):
        gpu_variants = {}
    if not isinstance(incompatible_reasons, dict):
        incompatible_reasons = {}

    variants = set(gpu_variants.keys()) | set(incompatible_reasons.keys())
    result: Dict[str, Dict[str, Any]] = {}
    for variant in sorted(variants):
        perf = performance.get(variant) if isinstance(performance, dict) else None
        ratio = perf.get("ref_cuda_ratio") if isinstance(perf, dict) else None
        result[variant] = {
            "status": variant_status_label(
                variant, incompatible_reasons, verification_status,
                not_recommended_variants,
            ),
            "perf_ratio": ratio,
        }
    return result


def apply_variant_preference_tiers(
    variants: Dict[str, str],
    incompatible_reasons: Optional[Dict[str, Any]] = None,
    verification_status: Optional[Dict[str, Any]] = None,
    not_recommended_variants: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Narrow an already-`{variant: image_ref}`-shaped dict to the GO.9
    preference tiers: incompatible variants are dropped entirely, and among
    what remains, verified variants are preferred over unverified, with
    not_recommended variants used only when nothing else is eligible.

    Pure function — takes the candidate `variants` dict explicitly so a
    caller that already applied its own filtering (e.g. worker_tasks.py's
    smoke-failed/drift gate) can layer this preference on top of its result
    instead of recomputing from scratch. Never raises — falls back to the
    input `variants` on any unexpected shape so a malformed verification
    column degrades to today's behaviour rather than stranding the task.
    """
    if not isinstance(variants, dict) or not variants:
        return dict(variants) if isinstance(variants, dict) else {}
    incompatible_reasons = incompatible_reasons if isinstance(incompatible_reasons, dict) else {}
    verification_status = verification_status if isinstance(verification_status, dict) else {}
    not_recommended_variants = (
        not_recommended_variants if isinstance(not_recommended_variants, dict) else {}
    )

    # Never pick incompatible (includes smoke-failed/drift, same as the
    # existing worker_tasks.py gate).
    eligible = {
        v: img for v, img in variants.items()
        if v not in incompatible_reasons
        and verification_status.get(v) not in _BAD_VERIFICATION_STATUSES
    }
    if not eligible:
        # Everything is incompatible — fail open to the input set rather
        # than stranding the task with zero variants; unrunnable_tasks_sweep
        # already surfaces this case in the System Health banner.
        return dict(variants)

    verified = {v: img for v, img in eligible.items()
                if verification_status.get(v) == STATUS_VERIFIED}
    if verified:
        return verified

    # No verified variant yet — prefer anything not flagged not_recommended;
    # only fall back to not_recommended when it's literally all that's left.
    non_demoted = {v: img for v, img in eligible.items()
                   if v not in not_recommended_variants}
    return non_demoted or eligible


def eligible_variants_for_node(node_def: Any) -> Dict[str, str]:
    """The `{variant: image_ref}` subset of `node_def.gpu_variants` a run
    should actually be offered to — `apply_variant_preference_tiers` seeded
    straight from a NodeDefinition's own columns.

    This is the "several variants can serve a run" selection point: it
    narrows the set the claim-time `variants_guard` (worker_tasks.py) will
    intersect against worker hardware_tags, so a worker whose only matching
    tag maps to a demoted variant will not claim the task while a
    better-tiered variant's worker pool exists.
    """
    gpu_variants = getattr(node_def, "gpu_variants", None) or {}
    if not isinstance(gpu_variants, dict) or not gpu_variants:
        return {}
    return apply_variant_preference_tiers(
        gpu_variants,
        getattr(node_def, "incompatible_reasons", None),
        getattr(node_def, "variant_verification_status", None),
        getattr(node_def, "not_recommended_variants", None),
    )


def select_queue_for_workflow(
    project_info: Dict[str, Any],
    parent_outputs: Dict[str, Any],
    base_process_type: str = 'CPU',
    node_kind: str = 'unknown',
    **kwargs  # Accept extra args for backward compatibility
) -> str:
    """
    Select queue based on hardware type (simple type-based routing).

    With resource-aware admission control, we don't need size-based queues.
    Just pick the hardware type, and let resource admission decide which
    worker can accept the task based on actual available RAM/CPU.

    Args:
        project_info: ProjectInfo from task trigger configuration (not used)
        parent_outputs: Parent node outputs (not used)
        base_process_type: Processing type from node metadata
                          ('CPU', 'CUDA', 'ROCm', 'Intel', or legacy 'GPU')
        node_kind: node kind (not used, kept for compatibility)
        **kwargs: Extra arguments (ignored, kept for compatibility)

    Returns:
        Simple queue name: 'CPU', 'CUDA', 'ROCm', or 'Intel'
    """

    # Legacy support: map 'GPU' to 'CUDA' for backward compatibility
    if base_process_type == 'GPU':
        logger.info("Legacy 'GPU' process type detected, mapping to 'CUDA'")
        base_process_type = 'CUDA'

    # Validate process type
    valid_types = ['CPU', 'CUDA', 'ROCm', 'Intel']
    if base_process_type not in valid_types:
        logger.warning(f"Unknown process type '{base_process_type}', defaulting to 'CPU'")
        base_process_type = 'CPU'

    logger.info(
        f"✓ Queue selected: {base_process_type} "
        "(resource admission control will determine worker acceptance)"
    )

    return base_process_type


# Backward compatibility aliases
def select_queue_by_data_size(
    file_location=None,
    file_size_bytes=None,
    base_process_type='CPU'
) -> str:
    """
    Backward compatibility wrapper.

    Old code might call this directly. Just return the hardware type.
    """
    logger.info(
        "select_queue_by_data_size() called (deprecated), "
        "using simplified type-based routing"
    )
    return select_queue_for_workflow(
        project_info={},
        parent_outputs={},
        base_process_type=base_process_type
    )

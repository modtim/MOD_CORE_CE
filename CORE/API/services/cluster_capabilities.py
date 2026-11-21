"""Cluster-capability detection for the wizard's base-image rail.

What problem this solves: the wizard's LLM picks a Dockerfile FROM line
during initial node generation. Without knowing what hardware the
datacenter actually has, it tends to default to nvidia/cuda "just in
case" — which produces a 3 GB image that won't run at all on an
AMD-only or Intel-only deployment, and bloats CPU-only deployments
needlessly.

The rail in this module reads each licensed worker's `queues` list
(reported on heartbeat) and rolls them up to a deployment-wide
capability set. The wizard's LLM prompts then carry the available set,
and the response validator refuses bases that require a capability
not in the set.

`pick_best_capability` defines the perf hierarchy used to pick the
HERO variant — "highest accelerator the cluster has, falling back to
CPU." Variants for the other available capabilities still get built
in the variant pass; the hero is just the one that gets the bare
`:latest` tag.

Priority (descending):
  1. cuda      — NVIDIA datacenter / desktop GPUs
  2. rocm      — AMD datacenter / desktop GPUs (MI series, RDNA3+)
  3. intel     — Intel discrete GPUs (Arc, Max) via XPU/oneAPI
  4. npu       — Intel/Qualcomm/Apple NPU
  5. cpu       — universal fallback; every deployment has CPU

The capability strings line up with the worker's queue tags (which
the heartbeat reports as upper-case: "CUDA", "ROCM", "INTEL", "NPU",
"CPU") and with the variant keys the build-task uses ("cuda", "rocm",
"intel", "npu", "cpu"), so the same string flows end-to-end.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Set

from sqlalchemy.orm import Session

import models
from utils.registries import platform_image


# Perf-rank HINTS used only for ordering when multiple capabilities
# are present. NOT a filter — any tag a worker advertises is honored
# even if it's not on this list (a brand-new accelerator a customer
# brings in will show up as a variant automatically; the wizard's
# Containerizer LLM is expected to pick a sensible base image for
# unknown tags, and the library_profiler's recipe cache learns from
# the first successful build for that accelerator).
#
# Ranking: lower number = higher priority for "hero variant" selection.
# Unknown accelerator tags rank between known accelerators and CPU,
# so a worker reporting "habana"/"tpu"/"inferentia"/etc gets picked
# over CPU as the hero. Within the unknown bucket, ordering is
# alphabetical for determinism.
_PRIORITY_HINTS: dict = {
    "cuda":  10,
    "rocm":  20,
    "intel": 30,
    "xpu":   30,  # alias for intel-XPU
    "npu":   40,
    "cpu":   1000,
}
_UNKNOWN_ACCEL_RANK = 100  # between known accels and CPU


def rank_capability(cap: str) -> tuple:
    """Return a sort key for ``cap``. Lower = higher priority.

    Known capabilities use their hardcoded rank; unknown capabilities
    slot in between known accelerators and CPU, ordered alphabetically
    within their tier for determinism. CPU is always last.
    """
    norm = (cap or "").strip().lower()
    if norm in _PRIORITY_HINTS:
        return (_PRIORITY_HINTS[norm], norm)
    return (_UNKNOWN_ACCEL_RANK, norm)


# Routing-only tags that workers emit but aren't real hardware
# capabilities. Filtered out of capability sets so the wizard doesn't
# offer "build a 'default' variant" or similar nonsense.
_ROUTING_ALIASES: Set[str] = {"default", "accelerator", "all", ""}

# Worker capability FLAG tags that describe topology, NOT an execution
# accelerator. They must not appear in the accelerator-capability set: this set
# drives the node wizard's process_type picker and the build's variant fan-out,
# so a leaked flag makes the wizard offer e.g. a "service-host" accelerator and
# tries to build a variant for it. Mirrors the Tag* constants in
# Worker/internal/capabilities/platform.go:
#   - service-host   → host can run a long-lived SERVICE other machines connect
#                      into (a STACK concern, not a node/accelerator concern)
#   - gpu-runtime    → host can execute accelerated containers (a precondition,
#                      not an accelerator type; the concrete accel — cuda/rocm/…
#                      — is advertised separately)
#   - bare-metal-gpu → accelerator access is native vs a virtualisation bridge
#                      (a topology qualifier, not an accelerator)
_NON_ACCELERATOR_TAGS: Set[str] = {
    "service-host", "gpu-runtime", "bare-metal-gpu",
}


# Optional aliases collapsed during normalization. Workers from older
# Go releases sometimes emit slightly different names for the same
# capability; collapse them so downstream sees one canonical form.
_CAPABILITY_ALIASES: dict = {
    "oneapi":   "intel",
    "openvino": "intel",
    # xpu intentionally NOT aliased to intel — Intel Arc / Xe / Ponte
    # Vecchio cards use the "xpu" runtime distinct from the openvino
    # one. Worker can advertise either or both; we keep them as
    # separate variants so the wizard can target each.
}


# Substring patterns in a Dockerfile's FROM line that mark the base as
# requiring a specific accelerator. Mirrors _GPU_BASE_MARKERS in
# repair_loop.py but classified per-capability. This is a HINT table
# — bases for unknown capabilities aren't pre-validated by this
# module; the build itself will surface compatibility issues.


# Substring patterns in a Dockerfile's FROM line that mark the base as
# requiring a specific accelerator. Mirrors _GPU_BASE_MARKERS in
# repair_loop.py but classified per-capability. Substrings are matched
# case-insensitively; longest match doesn't matter because each
# substring is unique to one capability.
_BASE_IMAGE_CAPABILITY: dict = {
    "cuda": (
        "nvidia/cuda",
        "nvcr.io/nvidia",
        "tensorrt",
    ),
    "rocm": (
        "rocm/",
        "rocm:",
    ),
    "intel": (
        "intel/intel-extension",
        "intel/oneapi",
        "openvino/",
    ),
    "npu": (
        # No public NPU base images yet; placeholder for when they
        # arrive (Qualcomm Snapdragon / Apple ANE / Intel NPU runtimes).
        "qcom/",
        "openvino/.*npu",
    ),
}


def _normalize_queues_to_capabilities(queues: Iterable) -> Set[str]:
    """Map a worker's `queues` list to capability tags.

    Routing-only tags ("default", "accelerator", empty) are dropped.
    Otherwise everything passes through, lowercased + alias-collapsed.
    A worker advertising a brand-new accelerator tag the platform has
    never seen will surface that tag as a buildable variant — the
    wizard's Containerizer LLM picks the base image, and the
    library_profiler caches the resulting recipe after first build.
    """
    caps: Set[str] = set()
    for q in (queues or []):
        if not isinstance(q, str):
            continue
        norm = q.strip().lower()
        if norm in _ROUTING_ALIASES:
            continue
        # Drop topology/capability FLAG tags (service-host, gpu-runtime,
        # bare-metal-gpu) — they are not execution accelerators and must not
        # surface as a buildable variant or a process_type option in the node
        # wizard. service-host in particular is a STACK-hosting concern.
        if norm in _NON_ACCELERATOR_TAGS:
            continue
        # Collapse known aliases (oneapi→intel etc.) but keep
        # everything else as-is so new accelerators self-extend.
        caps.add(_CAPABILITY_ALIASES.get(norm, norm))
    return caps


def get_cluster_capabilities(db: Session, *, tenant_id: Optional[int] = None) -> Set[str]:
    """Return the union of capabilities available across active workers.

    "Active" means LICENSED + not paused. We don't filter on
    last_heartbeat recency: a flaky network shouldn't change the
    wizard's base-image picker on a per-second timescale. If a worker
    is genuinely gone, the operator should revoke its license.

    `tenant_id` optionally narrows to workers visible to a specific
    tenant (owner_tenant_id matches OR tenant id is in
    assigned_tenant_ids). When None, returns the platform-wide set.

    Always returns at least `{"cpu"}` — every datacenter has CPU; no
    cluster is so degenerate it can't run a CPU image. This guarantees
    `pick_best_capability` never has to handle the empty case.
    """
    q = db.query(models.WorkerLicense).filter(
        models.WorkerLicense.deleted_at.is_(None),
        models.WorkerLicense.status == models.WorkerStatus.LICENSED,
        models.WorkerLicense.paused.is_(False),
    )
    workers = q.all()
    caps: Set[str] = {"cpu"}
    # Owner types that are visible to every tenant. PLATFORM_POOL is
    # the shared fleet; PLATFORM_ONLY runs system tasks but the
    # capability is still part of "what this datacenter has."
    PUBLIC_OWNER_TYPES = (
        models.WorkerOwnerType.PLATFORM_POOL,
        models.WorkerOwnerType.PLATFORM_ONLY,
    )
    for w in workers:
        if tenant_id is not None:
            assigned = w.assigned_tenant_ids or []
            visible = (
                w.owner_tenant_id == tenant_id
                or str(tenant_id) in assigned
                or w.owner_type in PUBLIC_OWNER_TYPES
                or w.owner_type == models.WorkerOwnerType.PLATFORM_ASSIGNED
                and str(tenant_id) in assigned
            )
            if not visible:
                continue
        caps |= _normalize_queues_to_capabilities(w.queues)
    return caps


def pick_best_capability(available: Set[str]) -> str:
    """Pick the highest-perf capability in `available`.

    Ranks via `rank_capability`: known accelerators rank highest in
    a hint-ordered way; unknown accelerators (e.g. "habana", "tpu",
    "inferentia") rank between known accelerators and CPU, ordered
    alphabetically; CPU is always last. Falls back to "cpu" when the
    set is empty.
    """
    if not available:
        return "cpu"
    return sorted(available, key=rank_capability)[0]


def _extract_from_image(dockerfile_text: str) -> Optional[str]:
    """Return the FROM image (first directive). Same parser as
    repair_loop.extract_from_line; duplicated to keep this module
    standalone."""
    if not dockerfile_text:
        return None
    for raw in dockerfile_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", line, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def implied_capability_for_base(from_image: str) -> str:
    """Return the capability a given FROM image requires to run.

    Defaults to "cpu" — generic images (python:*, debian:*, ubuntu:*)
    run anywhere. Only accelerator-specific bases get classified to
    cuda / rocm / intel / npu.
    """
    if not from_image:
        return "cpu"
    lower = from_image.lower()
    for cap, markers in _BASE_IMAGE_CAPABILITY.items():
        for marker in markers:
            if re.search(marker, lower):
                return cap
    return "cpu"


def validate_base_for_cluster(
    dockerfile_text: str,
    available_capabilities: Set[str],
    *,
    user_preference: Optional[str] = None,
) -> List[str]:
    """Reject Dockerfiles whose FROM image can't run on this cluster.

    Two checks:
      1. The base's implied capability must be in `available_capabilities`.
         If not, the image is unrunnable here — refuse and tell the LLM
         what's actually available.
      2. If `user_preference` is set and the base disagrees, refuse.
         Soft check — only applies when the user was specific (CPU
         requested + GPU picked = bloat); doesn't fire when the user
         said CUDA and the LLM picked a CPU base (the inner repair
         loop catches missing CUDA libs at runtime).

    Returns rejection reasons. Empty list = clean.
    """
    rejections: List[str] = []
    from_image = _extract_from_image(dockerfile_text)
    if not from_image:
        return ["Dockerfile has no FROM directive"]

    implied = implied_capability_for_base(from_image)
    if implied not in available_capabilities:
        avail_sorted = sorted(available_capabilities)
        rejections.append(
            f"base image '{from_image}' requires '{implied}' but this "
            f"cluster only has {avail_sorted}. Pick a base whose runtime "
            f"is in {avail_sorted}."
        )
        return rejections  # fatal — short-circuit

    if user_preference:
        pref = user_preference.strip().lower()
        # Collapse known aliases (oneapi/openvino → intel) but pass
        # anything else through unchanged so new accelerator tags work.
        pref_cap = _CAPABILITY_ALIASES.get(pref, pref)
        if pref_cap == "cpu" and implied != "cpu":
            rejections.append(
                f"base image '{from_image}' is {implied}-flavored but the "
                "user asked for CPU. Pick a CPU base (python:3.11-slim, "
                "debian:bookworm-slim, ubuntu:22.04) — adding accelerator "
                "libraries to a CPU node bloats the image by GBs with no "
                "runtime benefit."
            )
    return rejections


def describe_capabilities_for_llm(
    available: Set[str],
    *,
    user_process_type: Optional[str] = None,
) -> str:
    """Build a one-paragraph hint for the LLM's system prompt.

    Tells the LLM what's available, what to prefer, and what NOT to
    pick. Phrased so the model treats it as a hard requirement.

    When ``user_process_type`` is provided, the recommendation
    targets the USER'S choice — not the cluster's highest-perf
    accelerator. This is the right behavior at wizard time: the user
    (or the analyzer that inferred process_type from the source)
    has already decided what compute this node needs. The cluster
    capabilities list still ships so the LLM knows what's RUNNABLE
    if the user's pick is somehow unsupported, but the recommendation
    line follows intent, not hardware ranking.

    Without ``user_process_type`` (legacy callers), falls back to the
    pick_best_capability() recommendation — preserves the cluster's
    best as the implied default.

    Unknown accelerator tags (anything not in _PRIORITY_HINTS) are
    surfaced WITH a note instructing the LLM to use its general
    knowledge to pick a sensible base — the platform doesn't curate
    bases for capabilities it's never seen, so the LLM gets the lead.
    """
    sorted_avail = sorted(available, key=rank_capability)
    unknown_accels = sorted(
        cap for cap in available
        if cap not in _PRIORITY_HINTS and cap != "cpu"
    )
    lines = [
        f"This deployment's cluster has these accelerator capabilities "
        f"available: {sorted_avail}.",
    ]
    if user_process_type:
        # Normalize whatever shape the caller passed (CPU / cpu / CUDA / cuda / xpu / ...)
        target = (user_process_type or "").strip().lower()
        target = _CAPABILITY_ALIASES.get(target, target)
        if target == "cpu":
            lines.append(
                "**The user chose CPU for this node.** Pick a CPU base "
                "(python:3.11-slim, debian:bookworm-slim, ubuntu:22.04, "
                f"or a curated `{platform_image('base-python:*')}` / "
                "`base-distroless-python:*` / `base-alpine:*` etc). "
                "Do NOT use a CUDA / ROCm / Intel / NPU base — those are "
                "AVAILABLE on this cluster but unnecessary for this node "
                "and bloat the image by GBs."
            )
        elif target in available:
            lines.append(
                f"**The user chose `{target}` for this node.** Pick a base "
                f"image targeting `{target}`. Other accelerator runtimes "
                f"are available on this cluster but the user has explicitly "
                f"selected `{target}` — do not switch."
            )
        else:
            # User asked for something the cluster can't actually run.
            # Surface as a warning so the LLM picks something realistic.
            sensible = pick_best_capability(available)
            lines.append(
                f"WARNING: the user requested `{target}` but no worker in "
                f"this cluster advertises that capability. Fall back to "
                f"`{sensible}` (the cluster's best available) and emit a "
                f"base image targeting that. Note the divergence in the "
                f"glue artifact's description so the user sees it."
            )
    else:
        # Legacy / no-user-pref caller. Recommend cluster's best.
        best = pick_best_capability(available)
        lines.append(
            f"Pick the hero base image targeting **{best}** unless the "
            f"source clearly cannot use it."
        )
        # Say what NOT to pick, not only what to prefer. Recommending ROCm is
        # a preference the model may weigh against the source; naming CUDA as
        # unavailable is a fact it cannot weigh against, and without it an
        # AMD-only cluster gets nodes no worker can run.
        #
        # Built from the KNOWN capabilities only. An accelerator this platform
        # has never seen is absent from `available` because nobody advertises
        # it, not because it is unsupported — forbidding those would re-impose
        # the closed set that ranking hints exist to avoid. CPU is never
        # forbidden: every cluster can run it.
        forbidden = sorted(
            cap for cap in _PRIORITY_HINTS
            if cap != "cpu" and cap not in available
            # `xpu` is an alias for the same hardware family as `intel`; naming
            # both reads as two separate missing accelerators.
            and not (cap == "xpu" and "intel" in available)
            and not (cap == "intel" and "xpu" in available)
        )
        if forbidden:
            lines.append(
                f"Do NOT pick a base image for {forbidden} — those "
                f"accelerators are not available on any worker in this "
                f"cluster, and a node built against them cannot be scheduled."
            )
    if unknown_accels:
        lines.append(
            f"Note: {unknown_accels} are accelerator tags this platform "
            f"hasn't seen before. Pick the most appropriate publicly-"
            f"available base image for them based on your general "
            f"knowledge of the vendor (e.g. `habana` → `vault.habana.ai/"
            f"gaudi-docker/...`; `tpu` → `gcr.io/cloud-tpu-...`). The "
            f"library_profiler will learn from the first successful "
            f"build and reuse the recipe on subsequent runs."
        )
    return " ".join(lines)

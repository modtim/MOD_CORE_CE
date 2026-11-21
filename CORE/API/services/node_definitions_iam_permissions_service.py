"""Business logic for `routers/node_definitions_domains/iam_permissions.py`
(Gate 2b migration, openspec: stack-training-and-prewarm).

Data access -> `crud/node_definitions_iam_permissions.py` (the ONLY place
`db.query` lives for this sub-module). The wizard/build helper primitives
(`_detect_deep_cuda_with_hints`, `_enqueue_container_task`,
`_retry_parse_build_results`, `_retry_build_corrections_prompt`, `logger`) are
NOT owned by this migration — they stay defined in `_shared.py` and are
imported directly from there, exactly as the pre-migration router did (via
`from ._shared import *`).

404-vs-403 trap (shared migration rule #5): `crud.get_task_by_id` is
tenant-UNSCOPED. `retry_with_corrections` below resolves the task under retry
by id FIRST (raising `NotFoundError` if it plain doesn't exist), THEN checks
`task_payload["triggered_by_user_id"]` against the caller (raising
`ForbiddenError` if it exists but belongs to someone else) — a tenant-scoped
fetch here would collapse that legitimate 403 into a 404.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session

import crud.node_definitions_iam_permissions as crud
from routers.node_definitions_domains._shared import (
    _detect_deep_cuda_with_hints,
    _enqueue_container_task,
    _retry_build_corrections_prompt,
    _retry_parse_build_results,
    logger,
)
from services._base import ConflictError, NotFoundError, ServiceError


class ForbiddenError(ServiceError):
    """Authenticated caller lacks authorization for the action (router -> 403).

    `services._base` has no `ForbiddenError`, so it is declared here per the
    Gate 2b migration rules (shared rule #5).
    """

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


def predict_variants(db: Session, *, request: "PredictVariantsRequest") -> Dict[str, Any]:  # noqa: F821
    """Return the GPU variants the wizard WOULD emit for this source, without
    running the LLM or charging credits. Verbatim MOVE of the pre-migration
    handler body — see `routers/node_definitions_domains/iam_permissions.py`
    for the endpoint docstring (unchanged, not duplicated here).
    """
    # P9y — variants whose circuit breaker has tripped. The Containerizer
    # marks variants as "circuit-broken" when they've recently failed
    # too many builds in a row; the UI shows a distinct chip state for
    # them. Populated below if the detection pass surfaces any; default
    # empty so the response shape is stable.
    circuit_broken: list[str] = []

    # Build the "source_content" we sniff against. Multi-file: concat
    # the file bodies so imports in any file surface the right variants.
    source_parts: list[str] = []
    if request.source_files:
        for f in request.source_files:
            if isinstance(f, dict) and isinstance(f.get("body"), str):
                source_parts.append(f["body"])
    if request.additional_instructions:
        source_parts.append(request.additional_instructions)
    if request.repo_url:
        # Repo mode: pull the same sentinel-file slice /analyze-repo
        # uses so the import sniffer gets real source code (README +
        # entrypoint + manifests) instead of an empty string. Best-
        # effort — the variant detector falls back to "no GPU" cleanly
        # if we can't sample.
        try:
            from utils.repo_fetcher import fetch_repo_snippet
            sample, _ = fetch_repo_snippet(
                request.repo_url, auth_token=request.repo_auth_token,
            )
            if sample:
                source_parts.append(sample)
        except Exception as e:  # noqa: BLE001
            logger.debug("predict-variants: repo sample failed: %s", e)
    source_content = "\n\n".join(source_parts)

    # hero_from hint (optional). The detector only uses it for
    # back-compat with older call sites; import sniffing is primary.
    hero_from = request.source_image or ""

    # What "enabled" means: whatever the live worker pool actually
    # advertises. Workers are the source of truth — if hardware is
    # checked in and heartbeating, the wizard offers a variant for
    # it. New accelerator tags a worker brings in (habana, tpu,
    # inferentia, anything) flow through without code changes; the
    # Containerizer LLM gets the tag in its prompt and picks a base
    # image based on general knowledge, and the library_profiler
    # learns the recipe from the first successful build.
    #
    # The legacy admin-whitelist (PlatformGPUSettings.hardware_tags)
    # is intentionally NOT consulted — it was a closed list that
    # filtered out worker-advertised tags it didn't recognize,
    # producing the exact false "Admin-enabled but no worker" reports
    # you'd see when a customer added intel/npu/etc workers without
    # also editing the whitelist.
    worker_tags: set[str] = set()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        active_workers = crud.list_active_worker_licenses(db, heartbeat_cutoff=cutoff)
        for w in active_workers:
            hw = (w.hardware_specs or {}).get("hardware_tags") or []
            for t in hw:
                if isinstance(t, str):
                    norm = t.strip().lower()
                    # Same routing-alias filter as cluster_capabilities
                    # so workers' "default"/"accelerator" routing tags
                    # don't surface as variants.
                    if norm and norm not in ("default", "accelerator", "all"):
                        worker_tags.add(norm)
    except Exception as e:
        logger.debug("predict-variants: worker-tag union failed: %s", e)

    enabled = sorted(worker_tags) if worker_tags else []
    not_matched: list[str] = []

    # Import sniff only decides pre-check state — the UI shows every
    # `enabled` entry, but starts a checkbox unchecked for variants
    # the source doesn't actually exercise. User can flip any of them.
    import re as _re

    def _used(src: str, module: str) -> bool:
        return bool(_re.search(
            rf"^\s*(?:from\s+{_re.escape(module)}(?:\.\w+)*\s+import|"
            rf"import\s+(?:\w+(?:\.\w+)*\s*,\s*)*{_re.escape(module)}\b)",
            src, _re.MULTILINE,
        ))

    uses_any_gpu_framework = any([
        _used(source_content, "torch"),
        _used(source_content, "tensorflow"),
        _used(source_content, "jax"),
        _used(source_content, "intel_extension_for_pytorch"),
        _used(source_content, "openvino"),
        _used(source_content, "onnxruntime"),
    ])
    _ = hero_from  # reserved for future disambiguation; unused today

    # Image-mode submissions have NO source content to sniff (the user
    # picked a published image — there's no script for the regex to read).
    # The "pure-CPU" heuristic below would mis-fire and pre-skip every
    # GPU variant, leaving the user with an unbuildable spec. Detect
    # this case (source_content empty AND a source_image was supplied)
    # and pre-check everything the platform supports. wizard-mode-rebuild
    # §3 will generalize this to "try every variant always" — this is
    # the minimum fix for Image-mode submissions today.
    image_mode_no_script = (
        not source_content.strip() and bool(request.source_image)
    )

    if image_mode_no_script:
        # Image mode: we don't know what the image needs, but the
        # adapter layers per-accelerator stacks on top, so default
        # to attempting every enabled backend. User can untick any
        # they don't want.
        suggested = list(enabled)
    elif uses_any_gpu_framework:
        # Script with GPU imports: pre-check every usable backend.
        suggested = list(enabled)
    else:
        # Pure-CPU source: pre-check only CPU. User can still tick
        # GPU boxes if they know better.
        suggested = [v for v in enabled if v == "cpu"]

    # Infer which backend the HERO Dockerfile will target. The hero
    # FROM line is almost always derived from `source_image` (either
    # the user's picked base on the Image tab, or the curated base
    # for Script/Dockerfile tabs). Mirrors infer_hero_variant in
    # gpu_variants.py so the Review/Preview tab label is correct
    # BEFORE the Containerizer runs — otherwise the wizard was
    # guessing whichever whitelist entry sorted first (e.g. 'intel'
    # when the whitelist is alphabetical + Intel precedes CUDA).
    hero_hint = (request.source_image or "").lower()
    if "cuda" in hero_hint or "nvidia" in hero_hint or "tensorrt" in hero_hint:
        hero = "cuda"
    elif "rocm" in hero_hint or "/amd" in hero_hint:
        hero = "rocm"
    elif "intel" in hero_hint or "oneapi" in hero_hint or "xpu" in hero_hint:
        hero = "intel"
    elif "openvino" in hero_hint or "npu" in hero_hint:
        hero = "npu"
    elif "python" in hero_hint and "cuda" not in hero_hint:
        hero = "cpu"
    elif enabled:
        # No unambiguous hint from the FROM line; fall back to cuda
        # if the whitelist allows it (most common), otherwise the
        # first enabled backend.
        hero = "cuda" if "cuda" in enabled else enabled[0]
    else:
        hero = "cpu"

    # P8d — deep-CUDA detection. Repos like hustvl/4DGaussians ship
    # .cu / .cuh C++/CUDA kernels compiled via setup.py's
    # CUDAExtension / torch.utils.cpp_extension.load. The source-level
    # adapter can't port those to ROCm/Intel/NPU just by rewriting
    # `.to("cuda")`. When we detect this shape we surface a
    # `deep_cuda: true` flag + fingerprints so the wizard's Review
    # step can grey out non-CUDA backends with a clear reason.
    deep_cuda, deep_cuda_reasons, deep_cuda_hints = _detect_deep_cuda_with_hints(
        request.source_files,
    )
    incompatible_reasons: dict[str, str] = {}
    if deep_cuda:
        # Keep CUDA offered; drop everything else from `suggested`
        # and record per-variant reasons for the UI to surface.
        why = (
            "this repo ships compiled CUDA kernels (" +
            ", ".join(deep_cuda_reasons[:3]) +
            ") — ROCm/Intel/NPU ports would need hipify / dpct / "
            "OpenVINO work the source-level adapter can't do today"
        )
        for v in enabled:
            if v != "cuda":
                incompatible_reasons[v] = why
        suggested = ["cuda"] if "cuda" in enabled else []

    # §4 chunk 4 — base-image C-extension heuristic. When the user's
    # `source_image` matches a known CUDA-baked pattern (pytorch:*-cuda*,
    # nvcr.io/nvidia/*, etc.), refuse non-CUDA variants up front so the
    # wizard UI greys out their checkboxes (otherwise the build silently
    # skips them). Independent of deep_cuda which fingerprints SOURCE
    # files — this looks at the BASE IMAGE alone. Manual-test repro
    # 2026-05-22 spec=36 (Pytorch) — UI showed all 5 checkboxes pickable
    # but the build only ran CUDA.
    if (request.source_image or "").strip():
        from node_builder.build_dag.variant_strategy import variants_for_mode
        from node_builder.build_plan.types import BuildPlan
        _cext_plan = BuildPlan(
            mode="image",
            language="container",
            from_image_ref=request.source_image.strip(),
            confidence=0.95,
        )
        for v in variants_for_mode(_cext_plan):
            if v.compatibility != "compatible" and v.key in enabled:
                # deep_cuda's reason wins when both fire (its message
                # is more specific to the repo's compiled kernels).
                if v.key not in incompatible_reasons:
                    incompatible_reasons[v.key] = (
                        v.incompatibility_reason or "base image incompatible"
                    )
        # If the C-ext heuristic refused everything but CUDA, prune the
        # suggested list to match.
        if incompatible_reasons:
            suggested = [
                s for s in suggested if s not in incompatible_reasons
            ]

    # Script/codegen source: run the SAME gate the build uses (variants_for_mode
    # → _plan_has_gpu_capable_library). If the build would refuse every
    # accelerator variant (a pure-CPU node like Pillow / numpy), don't OFFER any
    # — return no GPU variants so the UI shows "single CPU image" instead of
    # pickable cuda/rocm boxes the build would then reject.
    if source_content.strip() and not deep_cuda and not (request.source_image or "").strip():
        try:
            from node_builder.build_dag.variant_strategy import variants_for_mode
            from node_builder.build_plan.types import BuildPlan
            _script_plan = BuildPlan(
                mode="script", language="python",
                script_body=source_content, confidence=0.9,
            )
            _compatible = {
                v.key for v in variants_for_mode(_script_plan)
                if v.compatibility == "compatible"
            }
            if _compatible <= {"cpu"}:
                enabled = []
                suggested = []
        except Exception as e:  # noqa: BLE001 — never block the review step
            logger.debug("predict-variants: script variant gate failed: %s", e)

    return {
        "variants": enabled,
        "suggested": suggested,
        "hero": hero,
        # Tags the admin enabled but no currently-active worker
        # provides. UI surfaces these as a muted "you need a worker
        # with these tags" hint rather than silently dropping them.
        "skipped_by_admin": not_matched,
        "source_image": request.source_image,
        "deep_cuda": deep_cuda,
        "deep_cuda_reasons": deep_cuda_reasons,
        # One-click build hints — the Containerizer's Dockerfile
        # patcher reads these so the LLM doesn't have to know about
        # git submodules or CUDA arch lists. See the deep_cuda
        # patches applied in DockerContainers/Containerizer/scripts/
        # tools/create_container.py.
        "deep_cuda_hints": deep_cuda_hints,
        # Variant → reason string for the ones this source can't run
        # on. Empty {} when nothing is deep-CUDA.
        "incompatible_reasons": incompatible_reasons,
        # P9y — variants the circuit breaker has tripped. Separate
        # field so the UI can render a distinct "auto-disabled" chip
        # state without conflating with source-level incompat.
        "circuit_broken_variants": circuit_broken,
    }


def retry_with_corrections(
    db: Session,
    *,
    request: "RetryWithCorrectionsRequest",  # noqa: F821
    current_user: "models.User",  # noqa: F821
) -> Dict[str, Any]:
    """Dispatch a new Containerizer task that retries the failed one with
    per-variant failure context appended to ADDITIONAL_INSTRUCTIONS. Verbatim
    MOVE of the pre-migration handler body — see
    `routers/node_definitions_domains/iam_permissions.py` for the endpoint
    docstring (unchanged, not duplicated here).

    Raises `NotFoundError` (404), `ForbiddenError` (403) or `ConflictError`
    (409) — the router maps each back to the exact `HTTPException` shape
    (status + detail body, string or dict) the pre-migration inline code
    raised.
    """
    failed = crud.get_task_by_id(db, request.task_id)
    if failed is None:
        raise NotFoundError("task not found")

    payload = failed.task_payload or {}

    # Ownership: only the user who triggered the original task can retry.
    # Fetched tenant-UNSCOPED above — this ownership check is what turns a
    # legitimate 403 into a 403 rather than a 404 (shared rule #5).
    triggered_by = payload.get("triggered_by_user_id")
    if triggered_by is not None and triggered_by != current_user.id:
        raise ForbiddenError("not your task")

    # Walk parent_task_id back to the origin to count retry depth.
    # Each retry stamps `parent_task_id` on its task_payload (no DB
    # schema column needed). Cap the loop at 5 hops as a defensive
    # backstop against pathological data.
    chain_depth = 0
    cur = failed
    for _ in range(5):
        cur_payload = cur.task_payload or {}
        parent_id = cur_payload.get("parent_task_id")
        if parent_id is None:
            break
        chain_depth += 1
        cur = crud.get_task_by_id(db, parent_id)
        if cur is None:
            break

    if chain_depth >= 2:
        raise ConflictError(
            "retry_cap_reached",
            details={
                "error": "retry_cap_reached",
                "message": "Already retried 2 times from the origin task. "
                           "Open the Preview step's Dockerfile editor and "
                           "fix manually.",
            },
        )

    original_env = dict(payload.get("container_env") or {})
    if not original_env:
        raise ConflictError(
            "task has no container_env to replay",
            details="task has no container_env to replay",
        )

    result_payload = failed.result_payload or {}
    log_tail = result_payload.get("log_tail") or ""
    failures = _retry_parse_build_results(log_tail)
    corrections = _retry_build_corrections_prompt(failures)

    existing_instr = original_env.get("ADDITIONAL_INSTRUCTIONS", "")
    original_env["ADDITIONAL_INSTRUCTIONS"] = existing_instr + corrections

    new_task_id = _enqueue_container_task(
        db, current_user.tenant_id, "containerizer:latest", original_env,
        "albedo_generate_retry",
    )

    # Stamp the chain link + carry forward wizard_metadata so the
    # new task's /generation-artifacts/{id} response renders the
    # same way the origin's did.
    new_row = crud.get_task_by_id(db, new_task_id)
    if new_row is not None:
        new_payload = dict(new_row.task_payload or {})
        new_payload["parent_task_id"] = failed.id
        new_payload["retry_chain_depth"] = chain_depth + 1
        if "wizard_metadata" in payload:
            new_payload["wizard_metadata"] = payload["wizard_metadata"]
        new_payload["triggered_by_user_id"] = current_user.id
        new_row.task_payload = new_payload
        db.commit()

    return {
        "task_id": new_task_id,
        "parent_task_id": failed.id,
        "retry_chain_depth": chain_depth + 1,
        "failures_replayed": len(failures),
    }

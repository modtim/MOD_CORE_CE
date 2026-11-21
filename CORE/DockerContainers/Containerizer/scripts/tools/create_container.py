import json
import os
import sys
import re
import asyncio
from typing import Dict, Any, Optional, Tuple

from .llm_providers import get_provider
from .kernel_porter_telemetry import emit_port_outcome as _emit_port_outcome


def _emit_port_telemetry_safe(variant: str, plan: Any, started_at: float) -> None:
    """Phase 6 — wrap the kernel_porter_telemetry emit call in defensive
    error-handling so a malformed plan can't break the port pass. Called
    from `_one_port` once per variant with the monotonic start time."""
    try:
        import time as _time
        wall_ms = int((_time.monotonic() - started_at) * 1000)
        report = getattr(plan, "port_report", None)
        tool = getattr(report, "tool", "") if report else ""
        unsupported = getattr(report, "unsupported_apis", []) if report else []
        files_translated = getattr(report, "files_translated", []) if report else []
        translator_version = getattr(report, "translator_version", "") if report else ""
        # The HIP path rides hipify_warning_count in dockerfile_env (see
        # kernel_porter._plan_port_hip). Strip it out so downstream
        # consumers of dockerfile_env don't see a private key.
        df_env = getattr(plan, "dockerfile_env", {}) or {}
        hipify_warnings = None
        if "_MOD_PORT_HIPIFY_WARNINGS" in df_env:
            try:
                hipify_warnings = int(df_env.pop("_MOD_PORT_HIPIFY_WARNINGS"))
            except (TypeError, ValueError):
                hipify_warnings = None
        _emit_port_outcome(
            variant=variant,
            tool=str(tool or "unknown"),
            compatibility=str(getattr(plan, "compatibility", "unknown")),
            wall_ms=wall_ms,
            unsupported_count=len(unsupported) if unsupported else 0,
            files_translated=len(files_translated) if files_translated else 0,
            reason=(getattr(plan, "incompat_reason", None) or None),
            porter_version=str(translator_version or "v1"),
            hipify_warning_count=hipify_warnings,
        )
    except Exception:  # noqa: BLE001
        # Telemetry must never break the port pass.
        pass

SYSTEM_PROMPT = (
    "You are an expert DevOps engineer specializing in containerization. "
    "Generate production-ready Dockerfiles based on source code analysis. "
    "The input may be a full repository, a project folder, or a single script. "
    "ALWAYS respond with valid JSON only - no markdown, no explanations, "
    "no code blocks. Just pure JSON that can be parsed directly."
)

ALBEDO_PATTERN_SYSTEM_PROMPT = (
    "You are an expert DevOps engineer. You produce THREE files that follow the "
    "MOD_CORE 'Albedo' workflow-node pattern: (1) a Dockerfile, (2) an entrypoint.sh "
    "shell script that reads inputs from /data/input/ and writes outputs to /data/output/, "
    "and (3) a build-and-push.sh script that tags and pushes the image to a target registry. "
    "ALWAYS respond with valid JSON only - no markdown, no explanations, no code blocks. "
    "Just pure JSON that can be parsed directly."
)


async def create_container_tool(
    source_summary: str,
    source_tree: str,
    source_content: str,
    project_name: Optional[str] = None,
    additional_instructions: Optional[str] = None,
    max_context_chars: int = 50000,
) -> Dict[str, Any]:
    """
    Generate a Dockerfile using the configured LLM provider.

    Accepts source analysis from either gitingest (repos) or source_scanner
    (local scripts/folders). The format is the same: summary, tree, content.

    Streams chunks to stderr for live progress.
    """
    try:
        provider = get_provider()

        # Truncate content if needed
        truncated_content = source_content
        if len(source_content) > max_context_chars:
            truncated_content = source_content[:max_context_chars] + "\n\n... [Content truncated due to length] ..."

        extra = ""
        if additional_instructions:
            extra = f"\n\nADDITIONAL INSTRUCTIONS:\n{additional_instructions}"

        user_prompt = f"""Based on the following source code analysis, generate a comprehensive and production-ready Dockerfile.

PROJECT SUMMARY:
{source_summary}

DIRECTORY STRUCTURE:
{source_tree}

SOURCE CODE CONTEXT:
{truncated_content}{extra}

Please generate a Dockerfile that:
1. Uses appropriate base images for the detected technology stack
2. Includes proper dependency management
3. Sets up the correct working directory structure
4. Exposes necessary ports
5. Includes health checks where appropriate
6. Follows Docker best practices (multi-stage builds if beneficial, minimal layers, etc.)
7. Handles environment variables and configuration
8. Sets up proper user permissions for security

If you detect multiple services or a complex architecture, provide a main Dockerfile and suggest docker-compose.yml structure.

IMPORTANT: Respond ONLY with a valid JSON object. Do not include any markdown formatting, explanations, or code blocks. The response must be parseable JSON.

Required JSON format:
{{
  "dockerfile": "FROM python:3.9-slim\\nWORKDIR /app\\nCOPY . .\\nRUN pip install -r requirements.txt\\nEXPOSE 8000\\nCMD [\\"python\\", \\"app.py\\"]",
  "base_image_reasoning": "Explanation of why you chose the base image",
  "technology_stack": "Detected technologies and frameworks",
  "port_recommendations": ["8000", "80"],
  "additional_notes": "Any important setup or deployment notes",
  "docker_compose_suggestion": "Optional docker-compose.yml content if multiple services detected"
}}"""

        # Stream to stderr for live progress
        print("  Generating...", end="", file=sys.stderr, flush=True)

        dockerfile_response = ""
        chunk_count = 0
        async for chunk in provider.stream(SYSTEM_PROMPT, user_prompt):
            dockerfile_response += chunk
            chunk_count += 1
            if chunk_count % 20 == 0:
                print(".", end="", file=sys.stderr, flush=True)

        print(" done.", file=sys.stderr, flush=True)

        # Parse
        dockerfile_data = _parse_llm_response(dockerfile_response)

        return {
            "success": True,
            "dockerfile": dockerfile_data.get("dockerfile", ""),
            "base_image_reasoning": dockerfile_data.get("base_image_reasoning", ""),
            "technology_stack": dockerfile_data.get("technology_stack", ""),
            "port_recommendations": dockerfile_data.get("port_recommendations", []),
            "additional_notes": dockerfile_data.get("additional_notes", ""),
            "docker_compose_suggestion": dockerfile_data.get("docker_compose_suggestion"),
            "project_name": project_name or "generated-project",
        }

    except Exception as e:
        return {"success": False, "error": str(e), "project_name": project_name or "unknown-project"}


async def create_albedo_pattern_tool(
    source_summary: str,
    source_tree: str,
    source_content: str,
    project_name: str,
    image_name: str,
    tenant_id: str,
    registry_target: str = "local",
    local_registry: str = "registry:5000",
    harbor_registry: str = "ghcr.io",
    harbor_project: str = "mod_core",
    additional_instructions: Optional[str] = None,
    max_context_chars: int = 50000,
    source_filename: str = "",
    source_files: Optional[list] = None,
) -> Dict[str, Any]:
    """
    Generate Albedo-pattern artifacts (Dockerfile, entrypoint.sh, build-and-push.sh)
    using the configured LLM. These three files together form a MOD_CORE workflow
    node that can be built and pushed to a registry.

    registry_target: "local" targets {local_registry}/tenant-{tenant_id}/{image_name}
                     "harbor" targets {harbor_registry}/{harbor_project}/{image_name}
    """
    try:
        provider = get_provider()

        truncated_content = source_content
        if len(source_content) > max_context_chars:
            truncated_content = source_content[:max_context_chars] + "\n\n... [Content truncated due to length] ..."

        if registry_target == "local" and not (local_registry or "").strip():
            # CE LAN: the API threads LOCAL_REGISTRY="" when the tenant has
            # no registry. Tag the image bare (the local-tag convention the
            # gpu_variants dict already uses) and skip the push — `/tenant-N/x`
            # is not a valid docker ref and there is nowhere to push.
            full_image_example = f"{image_name}:latest"
            registry_hint = (
                f"Target registry: NONE (local images only). The build-and-push.sh MUST tag as "
                f"'{full_image_example}' with bare local tags (no registry prefix, no tenant segment) "
                f"and MUST NOT attempt docker push or docker login. Do not invent a registry host."
            )
        elif registry_target == "local":
            full_image_example = f"{local_registry}/tenant-{tenant_id}/{image_name}:latest"
            registry_hint = (
                f"Target registry: LOCAL. The build-and-push.sh MUST tag and push as "
                f"'{full_image_example}'. Use REGISTRY={local_registry} and "
                f"PROJECT=tenant-{tenant_id} as defaults (overridable via env)."
            )
        else:
            full_image_example = f"{harbor_registry}/{harbor_project}/{image_name}:latest"
            registry_hint = (
                f"Target registry: ghcr. The build-and-push.sh MUST tag and push as "
                f"'{full_image_example}'. Use REGISTRY={harbor_registry} and "
                f"PROJECT={harbor_project} as defaults (overridable via env)."
            )

        extra = ""
        if additional_instructions:
            extra = f"\n\nADDITIONAL INSTRUCTIONS:\n{additional_instructions}"

        user_prompt = f"""Based on the following source analysis, produce THREE files that together
form a MOD_CORE workflow node following the Albedo pattern.

PROJECT NAME: {project_name}
IMAGE NAME (without tag or registry): {image_name}
TENANT ID: {tenant_id}

PROJECT SUMMARY:
{source_summary}

DIRECTORY STRUCTURE:
{source_tree}

SOURCE CODE CONTEXT:
{truncated_content}{extra}

PATTERN REQUIREMENTS:

1. Dockerfile
   - **BASE IMAGE — pick from this EXACT allowlist. DO NOT invent
     new tags, DO NOT append suffixes like `-slim` or `-runtime`
     to ghcr.io/modtim paths (those suffixes only exist on
     public docker.io/python/node images, NOT on our MOD bases).**

     MOD curated bases (ghcr.io/modtim) (use these for accelerator-aware builds):
       ghcr.io/modtim/base-cuda:12.4-python3.11   ← CUDA (use for any torch/tinycudann/nerfstudio/gsplat work)
       ghcr.io/modtim/base-rocm:6.1-python3.11    ← AMD ROCm
       ghcr.io/modtim/base-intel-xpu:2025.0       ← Intel Arc / Xe / oneAPI
       ghcr.io/modtim/base-npu:openvino-2025      ← OpenVINO NPU
       ghcr.io/modtim/base-python:3.11            ← CPU Python work

     Public bases (use when a MOD base isn't a match):
       python:3.11-slim / python:3.12-slim   ← plain CPU Python
       node:20-slim / node:22-slim           ← Node.js
       ubuntu:22.04 / debian:bookworm-slim   ← when you need apt
       nvidia/cuda:12.4.1-devel-ubuntu22.04  ← CUDA with nvcc (compiling extensions)
       nvidia/cuda:12.4.1-runtime-ubuntu22.04 ← CUDA without nvcc (smaller)
       pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime  ← pre-built PyTorch CUDA

     Hallucinated tags to NEVER emit (these don't exist):
       ghcr.io/modtim/base-cuda:12.4-python3.11-slim  ← no -slim suffix
       nvidia/cuda:12.4.1-base                                          ← only -runtime/-devel/-cudnn variants
       nvidia/cuda:12.4.0-base                                          ← same
       ghcr.io/modtim/nerfstudio-cuda:*                ← no library-specific bases

   - **COPY the entrypoint with inline chmod** using the single
     directive `COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh`.
     Do NOT use the two-step COPY + `RUN chmod +x` pattern — the
     curated MOD base images run as non-root user `mod` (uid 10001),
     and chmod on a root-owned COPY fails with "Operation not
     permitted". --chmod sets the mode during COPY under root
     privilege before the USER directive takes effect.
   - WORKDIR /data
   - ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
   - No exposed ports; no CMD.

   - **CRITICAL — pip install EVERY third-party import** that appears
     in the user's source. Walk the SOURCE CODE CONTEXT. For each
     `import X` or `from X import …` where X is NOT part of the
     Python standard library, add X to a `RUN pip install
     --no-cache-dir ...` line. Pin versions only when the source
     already pins them; otherwise leave unpinned.

   - **GPU / CUDA correctness.** When the chosen FROM is a CUDA
     variant (image name contains `cuda`) AND the source imports
     `torch` / `torchvision` / `torchaudio`, install those specific
     packages from PyTorch's matching CUDA wheel index — and EVERY
     OTHER pip package in a SEPARATE `RUN pip install` line
     (because `--index-url` REPLACES the default PyPI index for the
     whole invocation and makes transformers/pillow/etc.
     unresolvable). Two-line pattern:
         RUN pip install --no-cache-dir transformers pillow numpy
         RUN pip install --no-cache-dir \
             --index-url https://download.pytorch.org/whl/cu124 \
             torch torchvision
     Pick the cuXYZ suffix to match the base's CUDA version (cu121
     for 12.1, cu124 for 12.4, cu118 for 11.8). Without this,
     `pip install torch` against PyPI produces a CPU-only wheel
     that breaks `.to("cuda")` at runtime.
     For ROCm bases, use
     `--index-url https://download.pytorch.org/whl/rocm6.1` for the
     torch line. CPU / non-accelerated bases: plain PyPI, single
     pip install line is fine.

2. entrypoint.sh
   - #!/bin/bash with 'set -e' and 'set -x'
   - Finds the input file(s) in /data/input/
   - Fails loudly if /data/input/ is empty
   - Runs the containerized logic, writing results to /data/output/
   - Creates /data/output/ if missing
   - **CRITICAL — when SOURCE CODE CONTEXT above contains a Python
     (or other script) body**, the 3-file output must remain
     self-contained. Do NOT reference a separate `user_script.py`
     that isn't in the build context. Instead, materialise the
     user's code inside entrypoint.sh via a heredoc, then execute
     it. The canonical shape for a Python script:
         cat > /tmp/run.py <<'PYEOF'
         # ... the user's script verbatim ...
         PYEOF
         python3 /tmp/run.py
     Preserve the user's code byte-for-byte inside the heredoc
     (indentation, comments, everything). Do not rewrite or
     summarise it. Do not add a COPY directive in the Dockerfile
     that references the user script — the Dockerfile only ever
     COPYs entrypoint.sh.

3. build-and-push.sh
   - #!/usr/bin/env bash with 'set -e'
   - Usage: ./build-and-push.sh [tag] [push]  (tag defaults to 'latest')
   - {registry_hint}
   - Auto-derives the image name from the parent directory when not overridden
   - Builds with 'docker build -t <full_image> .'
   - Only pushes when the second arg is literally 'push'
   - Prints the final image reference at the end

IMPORTANT: Respond ONLY with a valid JSON object. No markdown, no code fences, no prose.

Required JSON format:
{{
  "dockerfile": "FROM ...\\n...",
  "entrypoint_sh": "#!/bin/bash\\nset -e\\n...",
  "build_and_push_sh": "#!/usr/bin/env bash\\nset -e\\n...",
  "image_name": "{image_name}",
  "full_image_reference": "{full_image_example}",
  "base_image_reasoning": "Why you picked the base image",
  "technology_stack": "Detected technologies",
  "additional_notes": "Anything the reviewer should know"
}}"""

        print("  Generating Albedo-pattern artifacts...", end="", file=sys.stderr, flush=True)

        response_text = ""
        chunk_count = 0
        async for chunk in provider.stream(ALBEDO_PATTERN_SYSTEM_PROMPT, user_prompt):
            response_text += chunk
            chunk_count += 1
            if chunk_count % 20 == 0:
                print(".", end="", file=sys.stderr, flush=True)

        print(" done.", file=sys.stderr, flush=True)

        data = _parse_llm_response(response_text)
        hero_dockerfile = data.get("dockerfile", "")

        # Deterministic entrypoint.sh correctness pass. The weak generator
        # repeats two mechanical shell bugs: a glob inside quotes in a test
        # (`[ -s "$IN/*.txt" ]`, never expands) and `mkdir -p X && exit N`
        # (shell precedence spuriously exits on success). Fix both here so the
        # sanitized script feeds BOTH the adapter fan-out AND the returned
        # artifact. Idempotent + targeted; leaves everything else byte-for-byte.
        try:
            from tools.dockerfile_post_process import sanitize_entrypoint_sh
            _ep = data.get("entrypoint_sh", "") or ""
            if _ep.strip():
                _ep_fixed, _ep_notes = sanitize_entrypoint_sh(_ep)
                if _ep_notes:
                    data["entrypoint_sh"] = _ep_fixed
                    for _n in _ep_notes:
                        print(f"[post-process] entrypoint.sh: {_n}",
                              file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[post-process] entrypoint.sh sanitize failed (continuing): {e}",
                  file=sys.stderr, flush=True)

        # Inline-source module names — the basenames (sans extension) of the
        # user's pasted script(s). These are SOURCE baked into the image, never
        # PyPI packages, so a hallucinated `RUN pip install <that name>` must be
        # scrubbed (see strip_self_pip_installs).
        _inline_modules = _inline_module_names(
            source_files, source_filename, source_content,
        )

        # P8d-2: one-click deep-CUDA patching. When the API's detector
        # flagged this as a deep-CUDA source (compiled .cu kernels,
        # CUDAExtension setup.py, git submodules), apply deterministic
        # Dockerfile patches AFTER the LLM's output so the image actually
        # builds on the first try — submodule init + TORCH_CUDA_ARCH_LIST
        # + FORCE_CUDA. Idempotent; no-op when hints aren't present.
        deep_cuda_hints_env = os.getenv("DEEP_CUDA_HINTS", "").strip()
        if deep_cuda_hints_env:
            try:
                deep_cuda_hints = json.loads(deep_cuda_hints_env)
                from tools.deep_cuda_patches import apply_deep_cuda_patches
                hero_dockerfile = apply_deep_cuda_patches(
                    hero_dockerfile, deep_cuda_hints,
                )
                print(
                    f"[deep-cuda] patched hero Dockerfile: "
                    f"submodule={deep_cuda_hints.get('needs_submodule_init')} "
                    f"cuda_arch={deep_cuda_hints.get('cuda_arch_list')}",
                    file=sys.stderr, flush=True,
                )
            except Exception as e:
                print(f"[deep-cuda] patch pass failed (continuing unpatched): {e}",
                      file=sys.stderr, flush=True)

        # Defensive snap to canonical curated tag. The constraint block
        # in node_definitions.py tells the LLM to use exact full refs,
        # but LLMs occasionally emit plausible-looking near-misses
        # (base-cuda:12.4 vs base-cuda:12.4-python3.11). When the repo
        # matches a curated base unambiguously, rewrite to the
        # canonical tag so the hero build doesn't 404 at FROM. Variant
        # Dockerfiles are protected separately by gpu_variants.
        if hero_dockerfile:
            try:
                from tools.canonical_base_snap import snap_to_canonical
                hero_dockerfile, _snapped = snap_to_canonical(hero_dockerfile)
                if _snapped:
                    _from_after = re.search(
                        r"^FROM\s+(\S+)", hero_dockerfile, re.MULTILINE,
                    )
                    print(
                        f"[base-snap] rewrote hero FROM to canonical: "
                        f"{_from_after.group(1) if _from_after else '?'}",
                        file=sys.stderr, flush=True,
                    )
            except Exception as e:
                print(f"[base-snap] snap pass failed (continuing): {e}",
                      file=sys.stderr, flush=True)

        # Deterministic Dockerfile post-process — handles known broken
        # upstream patterns the LLM faithfully translates from research
        # repos' setup.sh:
        #   - pillow-simd → Pillow (unmaintained since 2022; no Python
        #     3.11 wheel; source build dies on missing zlib/libjpeg
        #     headers — every TRELLIS-class repo trips this).
        #   - USER root injection after FROM so build-time apt-get
        #     doesn't fail with "are you root?" — curated bases set
        #     USER mod for runtime hardening, and synthesise_variant
        #     handles this for variants but the hero gets the LLM's
        #     raw output.
        # Variants inherit the pillow swap because they're synthesised
        # from the (now-patched) hero; variants get their own USER root
        # injection from synthesise_variant.
        if hero_dockerfile:
            try:
                from tools.dockerfile_post_process import (
                    apply_pip_replacements,
                    copyleft_apt_warnings,
                    inject_system_packages_for_pip,
                    inject_user_root_after_from,
                    inject_user_runtime_at_end,
                )
                hero_dockerfile, _pip_counts = apply_pip_replacements(hero_dockerfile)
                _swapped = {k: v for k, v in _pip_counts.items() if v > 0}
                if _swapped:
                    print(
                        f"[post-process] pip replacements: {_swapped}",
                        file=sys.stderr, flush=True,
                    )
                # Scrub hallucinated `pip install <user's-own-script-name>`.
                from tools.dockerfile_post_process import strip_self_pip_installs
                hero_dockerfile, _self_removed = strip_self_pip_installs(
                    hero_dockerfile, _inline_modules,
                )
                if _self_removed:
                    print(
                        f"[post-process] scrubbed self-pip-install of inline "
                        f"source module(s): {list(_self_removed)}",
                        file=sys.stderr, flush=True,
                    )
                hero_dockerfile, _injected = inject_user_root_after_from(hero_dockerfile)
                if _injected:
                    print(
                        "[post-process] injected USER root after FROM",
                        file=sys.stderr, flush=True,
                    )
                # Add apt deps for pip packages that need a system lib /
                # binary (pdf2image→poppler-utils, opencv→libgl1). Lands
                # after the USER root above; without it the smoke probe
                # fails at runtime and the LLM repair loop thrashes.
                hero_dockerfile, _sys_pkgs = inject_system_packages_for_pip(hero_dockerfile)
                if _sys_pkgs:
                    print(
                        f"[post-process] injected system packages: {list(_sys_pkgs)}",
                        file=sys.stderr, flush=True,
                    )
                    for _w in copyleft_apt_warnings(_sys_pkgs):
                        print(f"[post-process] LICENSE WARNING: {_w}",
                              file=sys.stderr, flush=True)
                # Phase B (#1) — synth-declared system packages and
                # dockerfile_extras. describe-synthesize emitted a
                # script that subprocess-shells out to binaries (ffmpeg,
                # openMVG_main_*, etc.) and declared the install
                # mechanism. The synth's validator already gates that
                # they cover every shelled-out binary; here we just bake
                # them deterministically into the Dockerfile.
                from tools.dockerfile_post_process import (
                    inject_synth_apt_packages,
                    inject_synth_dockerfile_extras,
                )
                _synth_pkgs_csv = os.getenv("SYSTEM_PACKAGES", "").strip()
                if _synth_pkgs_csv:
                    _synth_pkgs = tuple(
                        p.strip() for p in _synth_pkgs_csv.split(",") if p.strip()
                    )
                    hero_dockerfile, _added = inject_synth_apt_packages(
                        hero_dockerfile, _synth_pkgs,
                    )
                    if _added:
                        print(
                            f"[post-process] injected synth apt packages: {list(_added)}",
                            file=sys.stderr, flush=True,
                        )
                _synth_extras = os.getenv("DOCKERFILE_EXTRAS", "")
                if _synth_extras and _synth_extras.strip():
                    hero_dockerfile, _ext_injected = inject_synth_dockerfile_extras(
                        hero_dockerfile, _synth_extras,
                    )
                    if _ext_injected:
                        print(
                            "[post-process] injected synth dockerfile_extras "
                            f"({len(_synth_extras)} bytes)",
                            file=sys.stderr, flush=True,
                        )
                # Guarantee a runnable ENTRYPOINT before the runtime-USER pass
                # (so USER mod lands right before it). The curated bases set
                # ENTRYPOINT ["/usr/bin/tini", "--"] with NO CMD; a generated
                # Dockerfile that sets neither its own ENTRYPOINT nor a CMD
                # inherits a bare tini that execs nothing and prints its usage
                # message at runtime. This is the root cause of the "Multiply
                # Numbers" 7-attempt repair thrash.
                from tools.dockerfile_post_process import ensure_entrypoint
                hero_dockerfile, _ep_injected = ensure_entrypoint(hero_dockerfile)
                if _ep_injected:
                    print(
                        "[post-process] injected missing ENTRYPOINT "
                        '(["/bin/bash", "/app/entrypoint.sh"]) — base tini had '
                        "nothing to exec",
                        file=sys.stderr, flush=True,
                    )
                # Pair the build-time USER root with a runtime USER mod
                # at the end — admission gate rejects images whose
                # final USER is root (CMMC AC-6 least-privilege).
                hero_dockerfile, _runtime_injected = inject_user_runtime_at_end(hero_dockerfile)
                if _runtime_injected:
                    print(
                        "[post-process] injected USER mod before entrypoint",
                        file=sys.stderr, flush=True,
                    )
            except Exception as e:
                print(f"[post-process] pass failed (continuing): {e}",
                      file=sys.stderr, flush=True)

        # Synthesise one Dockerfile per compatible GPU variant + swap
        # build-and-push.sh for the loop-over-variants version. This is
        # deterministic string manipulation (no extra LLM calls) — the
        # hero Dockerfile is the template; variants differ only by FROM
        # and torch --index-url, everything else is byte-identical.
        from tools.gpu_variants import (
            ALL_KNOWN_VARIANTS, real_variants_only,
            detect_compatible_variants, infer_hero_variant,
            render_build_push, render_gpu_variants_dict,
        )
        variant_names: list[str] = []
        variant_dockerfiles: Dict[str, str] = {}
        gpu_variants_map: Dict[str, str] = {}
        if hero_dockerfile:
            hero_from_match = re.search(r"^FROM\s+(\S+)", hero_dockerfile, re.MULTILINE)
            hero_from = hero_from_match.group(1) if hero_from_match else ""
            # detect returns the variants where REAL synthesis makes
            # sense (torch imports → cuda/rocm/tensorrt, ipex →
            # intel/oneapi, openvino/onnxrt → npu, always cpu). We
            # then fill in the rest of Dashboard's hardware-tag list
            # with STUB Dockerfiles (tpu/metal/groq/fpga/vulkan) so a
            # tenant who enables those tags later still has something
            # to inspect + edit.
            variant_names = detect_compatible_variants(source_content, hero_from)
            # Respect the platform admin's Settings → GPU Compatibility
            # whitelist. Only emit Dockerfiles for tags they've enabled.
            # Absent / empty → use all detected variants (developer default).
            # Intersect the whitelist with the detector's list BEFORE
            # generating per-variant Dockerfiles so we don't pay for
            # synthesis on tags that'll just get dropped.
            enabled_csv = os.getenv("ENABLED_HW_TAGS", "").strip()
            if enabled_csv:
                enabled = {t.strip().lower() for t in enabled_csv.split(",") if t.strip()}
                variant_names = [v for v in variant_names if v in enabled]
            # User's Review-step skip toggles. The wizard's
            # /predict-variants endpoint showed them the list; they
            # unchecked the ones they don't care about; the API
            # forwards those as SKIPPED_VARIANTS CSV so we don't pay
            # the LLM adapter cost for something the user already
            # said no to.
            skipped_csv = os.getenv("SKIPPED_VARIANTS", "").strip()
            if skipped_csv:
                skipped = {t.strip().lower() for t in skipped_csv.split(",") if t.strip()}
                variant_names = [v for v in variant_names if v not in skipped]
            # Retire stubs: only emit Dockerfiles for variants the
            # source actually supports. A pure-CPU script returns {}
            # here, so the hero image is the only artefact (no
            # `-cpu` suffix tag).
            variant_dockerfiles = real_variants_only(
                hero_dockerfile, variant_names,
            )
            # Apply deep-CUDA patches to each variant Dockerfile,
            # with the VARIANT kwarg (Phase 9) so non-CUDA variants
            # emit their own arch envs (PYTORCH_ROCM_ARCH on ROCm,
            # SYCL_TARGETS on Intel, none on NPU/Metal/CPU) instead
            # of the CUDA-only TORCH_CUDA_ARCH_LIST. Submodule init
            # applies regardless of variant — repos still need the
            # submodule content to compile, no matter what backend.
            if deep_cuda_hints_env:
                try:
                    deep_cuda_hints = json.loads(deep_cuda_hints_env)
                    from tools.deep_cuda_patches import apply_deep_cuda_patches
                    variant_dockerfiles = {
                        v: apply_deep_cuda_patches(df, deep_cuda_hints, variant=v)
                        for v, df in variant_dockerfiles.items()
                    }
                except Exception as e:
                    print(f"[deep-cuda] variant patch pass failed: {e}",
                          file=sys.stderr, flush=True)
            # P9a: kernel_porter. For each non-CUDA variant, run the
            # deterministic port pipeline: preflight (can this backend
            # handle these APIs?) → translation (hipify-clang / dpct /
            # ONNX) → build-system patches → variant-specific Dockerfile
            # env/apt. Output is (a) per-variant transformed source_files
            # and (b) a patched Dockerfile carrying the variant's
            # dockerfile_env and dockerfile_apt blocks.
            #
            # Variants the porter marks INCOMPATIBLE are removed from
            # variant_dockerfiles here; their `incompat_reason` lands in
            # `port_incompatible_reasons` and is surfaced to the wizard
            # + ImageBuilder via the task result.
            port_plans: Dict[str, Any] = {}
            port_source_files: Dict[str, list] = {}   # variant → list[{filename, body, ...}]
            port_incompatible_reasons: Dict[str, str] = {}
            port_not_recommended: Dict[str, str] = {}
            if deep_cuda_hints_env and (source_files or source_content):
                try:
                    deep_cuda_hints = json.loads(deep_cuda_hints_env)
                    from tools.kernel_porter import plan_port, apply_port

                    # Build a plain list-of-dicts shape the porter
                    # understands; mirrors files_input below.
                    if source_files:
                        _base_files = [
                            {
                                "filename": str(f.get("filename", "") or ""),
                                "body": str(f.get("body", "") or ""),
                                "language": str(f.get("language", "") or ""),
                                "is_entrypoint": bool(f.get("is_entrypoint", False)),
                            }
                            for f in source_files
                        ]
                    else:
                        _base_files = [{
                            "filename": (
                                source_filename or
                                _default_source_filename(source_content)
                            ),
                            "body": source_content or "",
                            "is_entrypoint": True,
                        }]

                    # Port-pass concurrency: 2 for HIP/SYCL, 4 for NPU/Metal.
                    # HIP/SYCL spawn hipify/dpct subprocesses that CPU-grind;
                    # NPU/Metal are pure Python heuristics.
                    _port_sem_heavy = asyncio.Semaphore(2)
                    _port_sem_light = asyncio.Semaphore(4)

                    async def _one_port(variant: str) -> Tuple[str, Any, list]:
                        sem = (
                            _port_sem_heavy
                            if variant in ("rocm", "intel")
                            else _port_sem_light
                        )
                        async with sem:
                            import time as _time
                            _t0 = _time.monotonic()
                            plan = await plan_port(variant, _base_files, deep_cuda_hints)
                            if plan.compatibility == "incompatible":
                                _emit_port_telemetry_safe(variant, plan, _t0)
                                return variant, plan, list(_base_files)
                            new_files, new_df = await apply_port(
                                plan,
                                _base_files,
                                variant_dockerfiles.get(variant, ""),
                            )
                            if new_df:
                                variant_dockerfiles[variant] = new_df
                            _emit_port_telemetry_safe(variant, plan, _t0)
                            return variant, plan, new_files

                    # Only port variants we're actually building, and
                    # skip the CUDA hero (it's the reference backend).
                    _port_targets = [
                        v for v in variant_dockerfiles.keys() if v != "cuda"
                    ]
                    # P9f.2 — emit a stage marker so the wizard's log-tail
                    # stage inference can label this interval "Porting"
                    # instead of sitting on "Generating" or jumping
                    # straight to "Building". No-op when there's nothing
                    # to port (pure CPU repo, or deep_cuda hints absent).
                    if _port_targets:
                        print(
                            f"[port] pass starting ({len(_port_targets)} variants: "
                            f"{', '.join(_port_targets)})",
                            file=sys.stderr, flush=True,
                        )
                    _port_results = await asyncio.gather(*[
                        _one_port(v) for v in _port_targets
                    ], return_exceptions=True)
                    for res in _port_results:
                        if isinstance(res, Exception):
                            print(
                                f"[port] exception during port-pass: {res}",
                                file=sys.stderr, flush=True,
                            )
                            continue
                        v, plan, files = res
                        port_plans[v] = plan.to_dict()
                        if plan.compatibility == "incompatible":
                            port_incompatible_reasons[v] = plan.incompat_reason or ""
                            variant_dockerfiles.pop(v, None)
                            print(
                                f"[port] {v}: INCOMPATIBLE — "
                                f"{plan.incompat_reason}",
                                file=sys.stderr, flush=True,
                            )
                        else:
                            if plan.compatibility == "not_recommended":
                                port_not_recommended[v] = plan.incompat_reason or ""
                            port_source_files[v] = files
                            print(
                                f"[port] {v}: {plan.compatibility} "
                                f"(tool={plan.port_report.tool}, "
                                f"translated={len(plan.port_report.files_translated)})",
                                file=sys.stderr, flush=True,
                            )
                except Exception as e:
                    print(f"[port] pass failed (continuing unpatched): {e}",
                          file=sys.stderr, flush=True)

            # For the gpu_variants dict published in the metadata, only
            # include backends that have a real (non-stub) image AND
            # are in the enabled-tags set if one was provided. Stubs
            # are inspection/edit artifacts; they don't belong in the
            # scheduling dict until the operator finishes them.
            variant_names_for_dag = [
                v for v in variant_names if v in variant_dockerfiles
            ]
            hero_variant = infer_hero_variant(hero_from)
            # Replace the LLM-generated build-and-push with the one that
            # iterates all Dockerfile.<variant> files. Registry + project
            # derive from the same `registry_hint` inputs already resolved
            # above.
            build_and_push = render_build_push(
                registry=(local_registry if registry_target == "local" else harbor_registry),
                project=(f"tenant-{tenant_id}" if registry_target == "local" else harbor_project),
                image_name=image_name,
                hero_variant=hero_variant,
            )
            gpu_variants_map = render_gpu_variants_dict(
                image_name=image_name, tag="latest",
                variants=variant_names_for_dag,
            )
        else:
            build_and_push = data.get("build_and_push_sh", "")

        # Per-variant SOURCE adaptation. The hero Python (or R, Julia,
        # shell, ...) was written to target one backend — typically
        # CUDA. Other variants need rewrites to their backend's device
        # strings, API calls, and dep imports. See variant_adapter.py
        # for the full rationale.
        #
        # IMPORTANT: we adapt the USER SOURCE files, NOT the shell
        # wrapper (entrypoint.sh). The wrapper is just `python3 main.py`
        # or equivalent; the device code lives in the .py/.R/.jl file
        # itself. ImageBuilder bakes each variant's adapted source into
        # its own image while keeping the shared entrypoint.sh unchanged.
        #
        # Multi-file: `source_files` (a list of {filename, body}) is
        # authoritative when provided — one node can mix Python +
        # R + shell. Backward compat: when only the legacy single-
        # paste flow ran, we synthesise a one-element list from
        # source_content + source_filename (or a sensible default
        # filename if the caller didn't pass one).
        from tools.variant_adapter import adapt_node_async, SourceFile
        files_input: list[SourceFile]
        if source_files:
            files_input = [
                SourceFile(
                    filename=str(f.get("filename", "")) or "main",
                    body=str(f.get("body", "")),
                    language=str(f.get("language", "")),
                    is_entrypoint=bool(f.get("is_entrypoint", False)),
                )
                for f in source_files
            ]
            # Exactly one entrypoint. If none flagged, mark the first.
            if not any(f.is_entrypoint for f in files_input):
                files_input[0].is_entrypoint = True
        else:
            fallback_name = source_filename or _default_source_filename(source_content)
            files_input = [SourceFile(
                filename=fallback_name, body=source_content, is_entrypoint=True,
            )]

        # The LLM's entrypoint.sh typically INLINES the user's Python
        # via a heredoc (cat > /tmp/run.py <<'PYEOF' … PYEOF). That
        # Python body contains `.to("cuda")` and the like, and since
        # entrypoint.sh was "shared across variants" before this, every
        # variant's image shipped the hero's CUDA-targeting Python even
        # when ROCm / Intel / NPU base images had no CUDA runtime.
        #
        # Fix: feed entrypoint.sh through the adapter too. We tag it
        # language='python' so the adapter applies Python rewrites
        # (device strings, torch→openvino, ipex imports) — it doesn't
        # matter that the file also has shell around the heredoc; the
        # LLM sees both and preserves the shell structure while
        # adapting the Python inside.
        hero_entrypoint = data.get("entrypoint_sh", "") or ""
        if hero_entrypoint.strip():
            files_input.append(SourceFile(
                filename="entrypoint.sh",
                body=hero_entrypoint,
                language="python",   # force Python rewrite path for inlined body
                is_entrypoint=False, # NOT the Python entrypoint (the user's .py is)
            ))

        source_variants: Dict[str, Dict[str, str]] = {}
        variant_adaptations: Dict[str, Dict[str, Any]] = {}
        if files_input and variant_dockerfiles:
            adapt_targets = list(variant_dockerfiles.keys())
            print(
                f"[adapter] fanning out over {len(adapt_targets)} variants "
                f"× {len(files_input)} files = "
                f"{len(adapt_targets) * len(files_input)} adaptations",
                file=sys.stderr, flush=True,
            )
            try:
                tenant_id_int: Optional[int] = None
                try:
                    tenant_id_int = int(tenant_id) if tenant_id is not None else None
                except (TypeError, ValueError):
                    tenant_id_int = None
                # Parallel fan-out across variants. vLLM's scheduler
                # absorbs ~2 concurrent requests cleanly so N variants
                # finish in roughly ceil(N/2)×per-call time. Order-of-
                # arrival isn't guaranteed, but results are keyed by
                # variant so the caller reassembles correctly. Repeat
                # runs with unchanged script bodies hit the per-tenant
                # Redis cache (variant_adapter Phase 1c) and skip the
                # LLM entirely. file_concurrency also 4 so multi-file
                # work parallelises inside each variant too.
                node_results = await adapt_node_async(
                    files_input, adapt_targets, tenant_id=tenant_id_int,
                    variant_concurrency=4,
                )
                for v, node_res in node_results.items():
                    source_variants[v] = {
                        fn: fr.adapted_source for fn, fr in node_res.files.items()
                    }
                    variant_adaptations[v] = {
                        "compat": node_res.compat,
                        "incompatible": node_res.incompatible,
                        "incompatible_reason": node_res.incompatible_reason,
                        "files": {
                            fn: {
                                "changes":  [c.to_dict() for c in fr.changes],
                                "warnings": [w.to_dict() for w in fr.warnings],
                                "language": fr.language,
                                "incompatible": fr.incompatible,
                                "incompatible_reason": fr.incompatible_reason,
                                "llm_used": fr.llm_used,
                            }
                            for fn, fr in node_res.files.items()
                        },
                    }
            except Exception as e:  # noqa: BLE001
                print(
                    f"[adapter] fan-out failed, using hero for all variants: {e}",
                    file=sys.stderr, flush=True,
                )
                # Soft fallback: every variant gets the hero sources.
                # Build still proceeds — adapter failure shouldn't
                # block the wizard when the LLM has a bad day.
                for v in variant_dockerfiles:
                    source_variants[v] = {f.filename: f.body for f in files_input}
                    variant_adaptations[v] = {
                        "compat": "limited",
                        "incompatible": False,
                        "incompatible_reason": "",
                        "files": {
                            f.filename: {
                                "changes": [],
                                "warnings": [{
                                    "severity": "warn",
                                    "message": f"Adapter fan-out failed: {e}",
                                }],
                                "language": f.detected_language(),
                                "incompatible": False,
                                "incompatible_reason": "",
                                "llm_used": False,
                            }
                            for f in files_input
                        },
                    }

        # Drop any variant the adapter marked incompatible for the
        # whole node — we don't build or register those.
        for v, adapt in list(variant_adaptations.items()):
            if adapt.get("incompatible"):
                variant_dockerfiles.pop(v, None)
                gpu_variants_map.pop(v, None)

        # P9a: merge port-pass output INTO the adapter output per variant.
        # The port owns kernel + build-system files (.cu → .hip,
        # setup.py CUDAExtension→DPCPPExtension, requirements.txt
        # additions). The adapter owns framework source (.py / .R /
        # .sh etc. device-string rewrites). When the port renamed or
        # rewrote a file, the port's version wins — avoids burning
        # LLM tokens on re-translating already-ported kernel code.
        if 'port_source_files' in locals() and port_source_files:
            for v, ported in port_source_files.items():
                if v not in source_variants:
                    continue
                # Kernel + build-system filenames the port owns. If the
                # port renamed a file (.cu → .hip), the original name is
                # gone from `ported` and the new name is present.
                _port_owned_suffixes = (
                    ".cu", ".cuh", ".hip", ".dp.cpp",
                    "setup.py", "setup.cfg", "pyproject.toml",
                    "requirements.txt",
                )
                _port_index = {f.get("filename", ""): f for f in ported}
                # Add/override port-owned files in the adapter's
                # source_variants[v]. Port-emitted renames (e.g.
                # foo.cu → foo.hip) delete the CUDA name and add the
                # HIP name in the same operation.
                adapter_files = source_variants[v]
                port_names = set(_port_index.keys())
                # Remove adapter entries for files the port renamed
                # away (name no longer present in ported list but
                # was a port-owned type).
                to_remove = [
                    name for name in list(adapter_files.keys())
                    if any(name.endswith(s) for s in _port_owned_suffixes)
                    and name not in port_names
                ]
                for name in to_remove:
                    adapter_files.pop(name, None)
                # Add/override port-owned entries.
                for name, f in _port_index.items():
                    if any(name.endswith(s) for s in _port_owned_suffixes):
                        adapter_files[name] = f.get("body", "")

        # Hero variant label (cuda / rocm / intel / npu / cpu) derived
        # from the hero's FROM line. The wizard uses this to name the
        # first tab in the Preview — the hero Dockerfile isn't a
        # generic 'hero', it's THIS specific backend (typically CUDA).
        # Falls back to 'hero' if the FROM line doesn't match a known
        # backend pattern.
        hero_variant_label = "hero"
        if hero_dockerfile:
            try:
                hero_variant_label = infer_hero_variant(hero_from) or "hero"
            except Exception:
                hero_variant_label = "hero"

        # Multi-vendor planner. For every backend the platform whitelist
        # enabled but the simple-recipe path didn't produce (today: intel
        # and npu), emit a stub Dockerfile + incompatibility entry so the
        # wizard's per-variant tab renders something actionable instead
        # of sitting on "pending" forever. See backends/ package and
        # docs/MULTIVENDOR_ROADMAP.md for the architecture.
        try:
            from tools.backends import plan_unsupported_variants
            enabled_for_planner = (
                set(t.strip().lower() for t in (enabled_csv or "").split(",")
                    if t.strip())
                if enabled_csv else
                # No whitelist set → use the full known-variants list so
                # the planner still surfaces stubs for intel/npu by default.
                {"cuda", "rocm", "cpu", "intel", "npu"}
            )
            # CPU-only node gate. A pure-CPU node — no torch/tf/jax/ipex/
            # openvino/onnxrt import, no CUDA repo (deep_cuda hints), and no
            # accelerator variant the simple-recipe path detected — can never
            # run on a GPU/NPU backend. Emitting stub tabs for cuda/rocm/intel/
            # npu (and even a "cpu" stub) just produces phantom "incompatible"
            # variant tabs and a misleading "Porting" stage. Zero the planner
            # whitelist so its gap is empty and it returns no stubs; the hero
            # (CPU) image is the sole artefact. Accelerator-capable nodes are
            # unaffected — they still get the full stub + escape-hatch fan-out.
            _accel_capable = (
                bool(set(variant_names) - {"cpu"})
                or bool(deep_cuda_hints_env)
                or any(v != "cpu" for v in variant_dockerfiles.keys())
            )
            if not _accel_capable:
                print(
                    "[multivendor-planner] CPU-only node (no accelerator-"
                    "portable code) — skipping variant stub fan-out",
                    file=sys.stderr, flush=True,
                )
                enabled_for_planner = set()
            # The hero source bundle, keyed by filename. The planner
            # copies this verbatim into source_variants[gap_v] so the
            # wizard's diff view has a concrete starting point.
            hero_files_dict = {f.filename: f.body for f in files_input}
            joined_source = "\n".join(f.body for f in files_input)
            plan = plan_unsupported_variants(
                source_text=joined_source,
                source_files=hero_files_dict,
                source_filenames=[f.filename for f in files_input],
                enabled_variants=enabled_for_planner,
                detected_variants=variant_dockerfiles.keys(),
                image_name=image_name,
                # Phase 2: Intel XPU source-rewriter ships. When the
                # classifier doesn't block, the planner runs the recipe
                # and the variant lands as compat=native.
                # Phase 3: Intel NPU OpenVINO recipe ships. Same flow,
                # but constrained to architectures on the optimum-intel
                # allowlist. The Dockerfile bakes the OV IR via
                # `optimum-cli export openvino` at build time.
                implemented_recipes=("intel", "npu"),
            )
            for v, df in plan.dockerfiles.items():
                variant_dockerfiles.setdefault(v, df)
            for v, files in plan.source_variants.items():
                source_variants.setdefault(v, files)
            for v, adapt in plan.variant_adaptations.items():
                variant_adaptations.setdefault(v, adapt)
            # Reuse port_incompatible_reasons if the deep-CUDA branch
            # already initialised it; otherwise create a dict here so
            # both code paths converge on the same field name in the
            # API response.
            if 'port_incompatible_reasons' not in locals():
                port_incompatible_reasons = {}
            for v, reason in plan.incompatible_reasons.items():
                # Surface in the same dict the source-level adapter
                # uses, so the API's incompatible_reasons field
                # carries both kinds of reasons in one place.
                if v not in port_incompatible_reasons:
                    port_incompatible_reasons[v] = reason
        except Exception as e:
            print(f"[multivendor-planner] planner failed (continuing without "
                  f"stubs): {e}", file=sys.stderr, flush=True)

        # Split-RUN heuristic (issue #2b/#2d). Post-process every
        # generated Dockerfile so mega-`RUN pip install` and
        # `RUN apt-get install` blocks above 5 packages are split into
        # per-VCS / per-pinned / per-apt RUNs. Failures in build logs
        # then identify the offending package on its own line instead
        # of burying it inside a 14-package install. Pure function;
        # idempotent (a second pass is a no-op since the splits are
        # already below threshold).
        try:
            from tools.split_run_heuristic import split_long_runs
            if hero_dockerfile:
                hero_dockerfile = split_long_runs(hero_dockerfile)
            for _v in list(variant_dockerfiles.keys()):
                _df = variant_dockerfiles.get(_v) or ""
                if _df:
                    variant_dockerfiles[_v] = split_long_runs(_df)
        except Exception as _e:
            # Heuristic is best-effort. A bug here MUST NOT block
            # artifact emission — the original mega-RUN still works,
            # just with the noisier failure mode the heuristic was
            # meant to mitigate. Surface the warning so it's caught
            # in container logs.
            print(
                f"[split-run] post-process failed (continuing unsplit): {_e}",
                file=sys.stderr, flush=True,
            )

        # Harbor tag verification (issue #2a). Hit Harbor's manifest
        # endpoint for every FROM ref we generated, so hallucinated
        # tags surface as structured failure entries before the user
        # waits 5 minutes for Phase 2 to fail. Best-effort; a
        # transport error or 5xx falls through silently (the build
        # attempt itself remains the authoritative signal). 404 →
        # one BUILD_RESULT_JSON line per (variant, hallucinated tag)
        # so the FE row renders a per-variant failure card AND the
        # retry-with-corrections endpoint can re-prompt the LLM with
        # the actual catalogue.
        try:
            from tools.harbor_tag_verify import (
                verify_from_refs, httpx_fetch_factory,
            )
            _harbor_user = os.getenv("HARBOR_USERNAME", "").strip()
            _harbor_pass = os.getenv("HARBOR_PASSWORD", "").strip()
            _harbor_reg = os.getenv("HARBOR_REGISTRY", "ghcr.io").strip()
            # Verification only runs when we have creds. Without auth
            # most Harbor tenants reject manifest reads; better to
            # skip than to spam 401s.
            if _harbor_user and _harbor_pass and _harbor_reg:
                _fetch = httpx_fetch_factory()
                _by_variant = {"hero": hero_dockerfile or ""}
                _by_variant.update({
                    v: variant_dockerfiles.get(v) or ""
                    for v in variant_dockerfiles.keys()
                })
                for _v, _df in _by_variant.items():
                    if not _df:
                        continue
                    _result = verify_from_refs(
                        _df,
                        harbor_registry=_harbor_reg,
                        username=_harbor_user,
                        password=_harbor_pass,
                        fetch=_fetch,
                    )
                    for (_ref, _registry, _repo, _tag) in _result["missing"]:
                        _failure = {
                            "variant": _v,
                            "status": "failed",
                            "stage": "verify",
                            "dockerfile": (
                                "Dockerfile" if _v == "hero"
                                else f"Dockerfile.{_v}"
                            ),
                            "line": 1,
                            "command": f"FROM {_ref}",
                            "error": (
                                f"Registry manifest 404 for {_repo}:{_tag}. "
                                f"Available tags: "
                                + ", ".join(
                                    _result["available_tags"].get(_repo, []) or ["(unknown)"]
                                )
                            )[:500],
                            "error_class": "from_image_not_found",
                        }
                        # One JSON line per failure, prefixed so the
                        # FE row's parser picks it up.
                        print(
                            f"BUILD_RESULT_JSON: {json.dumps(_failure)}",
                            file=sys.stderr, flush=True,
                        )
                    for (_ref, _reason) in _result["skipped"]:
                        # Skipped refs (5xx, transport errors, off-target
                        # registry) just log a hint — not a failure.
                        if "503" in _reason or "transport error" in _reason:
                            print(
                                f"[registry-verify] skipped {_ref}: {_reason} — build may fail at runtime",
                                file=sys.stderr, flush=True,
                            )
        except Exception as _e:
            # Same best-effort posture as split-RUN: a bug here must
            # not block artifact emission.
            print(
                f"[registry-verify] verification failed (continuing): {_e}",
                file=sys.stderr, flush=True,
            )

        # Pip dry-resolve (issue #2b). Pre-flights every pip-install
        # package against PyPI (and the Intel XPU wheel index when
        # `--extra-index-url` is set on the line) so hallucinated
        # version pins like `gradio==6.0.1` fail BEFORE the Phase 2
        # build runs. Each failed package gets its own
        # BUILD_RESULT_JSON envelope with error_class
        # "pip_install_failed" — the FE row's existing parser
        # surfaces these as per-variant failure cards. Best-effort:
        # network failures and pip-not-installed both fall through
        # silently (the build remains the authoritative signal).
        # Off by default until we've confirmed PyPI throughput
        # is fine in stage; flip MOD_PIP_DRY_RESOLVE=1 to enable.
        if os.getenv("MOD_PIP_DRY_RESOLVE", "").strip().lower() in ("1", "true", "yes"):
            try:
                from tools.dep_dry_resolve import dry_resolve_dockerfile_pips
                _by_variant = {"hero": hero_dockerfile or ""}
                _by_variant.update({
                    v: variant_dockerfiles.get(v) or ""
                    for v in variant_dockerfiles.keys()
                })
                for _v, _df in _by_variant.items():
                    if not _df:
                        continue
                    _failures = dry_resolve_dockerfile_pips(_df, timeout_per_pkg_s=20)
                    for _fail in _failures:
                        _envelope = {
                            "variant": _v,
                            "status": "failed",
                            "stage": "verify",
                            "dockerfile": (
                                "Dockerfile" if _v == "hero"
                                else f"Dockerfile.{_v}"
                            ),
                            "line": None,
                            "command": _fail.get("command"),
                            "error": _fail.get("error", "")[:500],
                            "error_class": "pip_install_failed",
                        }
                        print(
                            f"BUILD_RESULT_JSON: {json.dumps(_envelope)}",
                            file=sys.stderr, flush=True,
                        )
            except Exception as _e:
                print(
                    f"[pip-dry-resolve] verification failed (continuing): {_e}",
                    file=sys.stderr, flush=True,
                )

        # Apt static-index dry-resolve (issue #2d / Option A). Catches
        # apt typos + missing-package-in-distro before the build runs.
        # Doesn't catch repo-config / apt-get update failures — those
        # need actual sidecar execution which is handled at
        # ImageBuilder time (Option B). Always-on by default since
        # it's HTTPS-only and free; flip MOD_APT_DRY_RESOLVE=0 to
        # disable for any future debugging need.
        if os.getenv("MOD_APT_DRY_RESOLVE", "1").strip().lower() not in ("0", "false", "no"):
            try:
                from tools.dep_dry_resolve import dry_resolve_dockerfile_apt
                _by_variant = {"hero": hero_dockerfile or ""}
                _by_variant.update({
                    v: variant_dockerfiles.get(v) or ""
                    for v in variant_dockerfiles.keys()
                })
                for _v, _df in _by_variant.items():
                    if not _df:
                        continue
                    _failures = dry_resolve_dockerfile_apt(_df)
                    for _fail in _failures:
                        _envelope = {
                            "variant": _v,
                            "status": "failed",
                            "stage": "verify",
                            "dockerfile": (
                                "Dockerfile" if _v == "hero"
                                else f"Dockerfile.{_v}"
                            ),
                            "line": None,
                            "command": _fail.get("command"),
                            "error": _fail.get("error", "")[:500],
                            "error_class": "apt_install_failed",
                        }
                        print(
                            f"BUILD_RESULT_JSON: {json.dumps(_envelope)}",
                            file=sys.stderr, flush=True,
                        )
            except Exception as _e:
                print(
                    f"[apt-dry-resolve] verification failed (continuing): {_e}",
                    file=sys.stderr, flush=True,
                )

        return {
            "success": True,
            "dockerfile": hero_dockerfile,
            "dockerfile_variants": variant_dockerfiles,
            "gpu_variants": gpu_variants_map,
            "hero_variant": hero_variant_label,
            "entrypoint_sh": data.get("entrypoint_sh", ""),
            "source_variants": source_variants,
            "variant_adaptations": variant_adaptations,
            # P9a: port-pass outputs. Per-variant PortPlan (translated
            # files, report, apt pkgs), incompatible/not-recommended
            # maps surface to the API's /generation-artifacts/{id}
            # response so the wizard can grey out NPU on a Gaussian
            # splatting repo with a specific reason instead of
            # shipping a broken image.
            "port_plans": port_plans if 'port_plans' in locals() else {},
            "port_incompatible_reasons": (
                port_incompatible_reasons
                if 'port_incompatible_reasons' in locals()
                else {}
            ),
            "port_not_recommended": (
                port_not_recommended
                if 'port_not_recommended' in locals()
                else {}
            ),
            "source_files": [
                {"filename": f.filename, "body": f.body,
                 "language": f.detected_language(),
                 "is_entrypoint": f.is_entrypoint}
                for f in files_input
            ],
            "build_and_push_sh": build_and_push,
            "image_name": data.get("image_name", image_name),
            "full_image_reference": data.get("full_image_reference", full_image_example),
            "base_image_reasoning": data.get("base_image_reasoning", ""),
            "technology_stack": data.get("technology_stack", ""),
            "additional_notes": data.get("additional_notes", ""),
            "project_name": project_name,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "project_name": project_name}


def _inline_module_names(
    source_files: Optional[list],
    source_filename: str,
    source_content: str,
) -> set:
    """Return the set of the user's inline SOURCE module names — the basename
    (sans extension) of every pasted script. These are materialised into the
    image as source, so they must never be treated as PyPI packages. Used by
    ``strip_self_pip_installs`` to kill hallucinated
    ``RUN pip install <own-script-name>`` lines (the `describe.py` →
    `pip install describe` failure).
    """
    names: set = set()

    def _add(fn) -> None:
        if not fn:
            return
        base = str(fn).replace("\\", "/").rsplit("/", 1)[-1].strip()
        stem = base.rsplit(".", 1)[0] if "." in base else base
        if stem and stem.isidentifier():
            names.add(stem)

    if source_files:
        for f in source_files:
            if isinstance(f, dict):
                _add(f.get("filename"))
    _add(source_filename)
    # Only fall back to a content-inferred filename when nothing explicit was
    # given, so we don't add a broad default like "main" when we already know
    # the real script names.
    if not names:
        _add(_default_source_filename(source_content))
    return names


def _default_source_filename(source_content: str) -> str:
    """Pick a sensible filename when the caller (old single-paste flow)
    didn't supply one. Extension is inferred from the shebang / obvious
    content signals so the adapter's language detection still routes
    correctly. Kept narrow — most heuristics already live in
    variant_adapter.detect_language; this just provides a plausible
    filename hint that feeds into that detector.
    """
    head = (source_content or "")[:200]
    if head.startswith("#!"):
        if "python" in head:  return "main.py"
        if "Rscript" in head: return "main.R"
        if "julia" in head:   return "main.jl"
        if "node" in head:    return "main.js"
        if "bash" in head or "/sh" in head: return "main.sh"
    if "import torch" in (source_content or "") or "def " in (source_content or ""):
        return "main.py"
    if "<?php" in (source_content or ""):
        return "main.php"
    return "main.py"  # Python is the overwhelming default today.


def _parse_llm_response(raw: str) -> Dict[str, Any]:
    """Parse the LLM response as JSON, with multiple fallback strategies."""
    # 1. Direct JSON parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2. Extract from markdown code blocks
    try:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if m:
            return json.loads(m.group(1))
    except (json.JSONDecodeError, AttributeError):
        pass

    # 3. Find nested JSON block with "dockerfile" key
    try:
        start = raw.find('{')
        if start >= 0:
            depth = 0
            for i, ch in enumerate(raw[start:], start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        return json.loads(raw[start:i + 1])
    except (json.JSONDecodeError, ValueError):
        pass

    # 4. Last resort: extract Dockerfile-looking lines
    dockerfile_content = raw
    if 'FROM ' in raw:
        docker_cmds = {'FROM', 'RUN', 'COPY', 'WORKDIR', 'EXPOSE', 'CMD',
                       'ENTRYPOINT', 'ENV', 'ARG', 'USER', 'VOLUME', 'LABEL',
                       'ADD', 'HEALTHCHECK', 'SHELL', 'STOPSIGNAL', 'ONBUILD'}
        lines = raw.split('\n')
        out = []
        active = False
        for line in lines:
            s = line.strip()
            if any(s.startswith(c + ' ') or s == c for c in docker_cmds):
                active = True
                out.append(line)
            elif active and (s.startswith('#') or s == ''):
                out.append(line)
            elif active and s:
                break
        if out:
            dockerfile_content = '\n'.join(out).strip()

    return {
        "dockerfile": dockerfile_content,
        "base_image_reasoning": "Generated as plain text response",
        "technology_stack": "Could not parse detailed analysis",
        "port_recommendations": [],
        "additional_notes": "Response was not in expected JSON format",
        "docker_compose_suggestion": None,
    }

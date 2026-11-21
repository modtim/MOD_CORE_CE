"""Synthesise GPU-variant Dockerfiles from the hero Dockerfile.

A user's wizard submission produces ONE "hero" Dockerfile tied to the
base image they picked (typically CUDA — the most common GPU in prod).
But workers in the fleet can have any of: NVIDIA, AMD/ROCm, Intel XPU,
NPU/OpenVINO, or CPU-only. For the node to be scheduleable on any of
them, we emit a companion Dockerfile per compatible GPU backend and
populate the metadata's ``gpu_variants`` dict so the scheduler can pick the
right image tag per worker.

This module synthesises the variants DETERMINISTICALLY from the hero
Dockerfile — we do NOT re-invoke the LLM per variant. Variants differ
only in two places:

  1. The ``FROM`` directive (different curated MOD base image)
  2. The torch-wheel ``--index-url`` (different CUDA/ROCm version)

Everything else — pip deps, COPY, entrypoint, WORKDIR — is invariant.
Synthesis is a string transform.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, Optional


# Registered variants. Each maps to: the base image MOD publishes for
# that backend, and the torch-wheel index the pip install line should
# point at (None = use default PyPI, i.e. CPU wheels).
#
# Keep keys in sync with the existing MOD node metadata pattern seen
# in the benchmark workflow.
# Full MOD-buildable variant matrix. Keys match the hardware tags
# used by the Settings → GPU Compatibility UI (see
# CORE/Dashboard/js/settings.js HW_TAG_LABELS). Variants we DO have
# a curated base image for get an entry here; ones MOD can't build
# without a user-provided base (tpu, metal, groq, fpga, vulkan) are
# omitted — the wizard skips them gracefully and the tenant can
# still enable them in Settings to route tasks to custom images.
VARIANT_BASES: Dict[str, str] = {
    # NVIDIA CUDA — also used for TensorRT which is a CUDA subset.
    "cuda":     "ghcr.io/modtim/base-cuda:12.4-python3.11",
    "tensorrt": "ghcr.io/modtim/base-cuda:12.4-python3.11",
    # AMD ROCm
    "rocm":     "ghcr.io/modtim/base-rocm:6.1-python3.11",
    # Intel Arc / Xe — oneAPI is Intel's umbrella name for the same stack.
    "intel":    "ghcr.io/modtim/base-intel-xpu:2025.0",
    "oneapi":   "ghcr.io/modtim/base-intel-xpu:2025.0",
    # NPU / OpenVINO
    "npu":      "ghcr.io/modtim/base-npu:openvino-2025",
    # CPU fallback
    "cpu":      "ghcr.io/modtim/base-python:3.11",
}

VARIANT_TORCH_INDEX: Dict[str, Optional[str]] = {
    "cuda":     "https://download.pytorch.org/whl/cu124",
    "tensorrt": "https://download.pytorch.org/whl/cu124",  # shares CUDA wheel path
    "rocm":     "https://download.pytorch.org/whl/rocm6.1",
    "intel":    None,   # IPEX wheels from a separate channel; PyPI torch is fine.
    "oneapi":   None,   # same as intel
    "npu":      None,   # OpenVINO typically skips torch entirely.
    "cpu":      None,   # Default PyPI wheel is CPU.
}


# Per-variant pip package adjustments. Applied by synthesise_variant to
# the hero Dockerfile's pip install line so each variant's image ships
# the dep set its backend actually needs. Deterministic; no LLM call.
#
# Two operations per variant:
#   add:    packages to prepend to the pip install list (if not already present)
#   remove: packages to strip from the list (e.g. drop torch for NPU)
#
# This is orthogonal to the per-file SOURCE adaptation the LLM adapter
# does — the adapter rewrites `.to("cuda")` → `.to("xpu")`, but it
# can't change the installed dep set. Keeping the deps deterministic
# means a tenant-level build is reproducible even if the LLM's source
# rewrite choices shift between runs.
#
# Variants not listed leave the hero's pip install line alone.
VARIANT_PIP_PACKAGES: Dict[str, Dict[str, list[str]]] = {
    "intel": {
        "add":    ["intel-extension-for-pytorch"],
        "remove": [],
    },
    "oneapi": {
        "add":    ["intel-extension-for-pytorch"],
        "remove": [],
    },
    "npu": {
        # OpenVINO is the NPU runtime; torch has no business being in
        # the image (adapter's Python rewrite uses ov.Core instead).
        # Strip torch / torchvision / torchaudio; add openvino wheels.
        "add":    ["openvino"],
        "remove": ["torch", "torchvision", "torchaudio"],
    },
    "tpu": {
        # torch_xla is the Python-side bridge to TPUs.
        "add":    ["torch_xla"],
        "remove": [],
    },
    "cpu": {
        # CPU shouldn't pay the CUDA-wheel cost; nothing specific to
        # add, but if the hero pinned a GPU-specific wheel we'd want
        # to drop it. Left as a no-op default; operator can override.
        "add":    [],
        "remove": [],
    },
}

# The full set of hardware tags the Dashboard's Settings →
# GPU Compatibility screen exposes. Kept in sync with
# CORE/Dashboard/js/settings.js HW_TAG_LABELS. Detect functions
# intersect against this list; real_variants_only emits Dockerfiles
# only for the entries that have a curated MOD base image in
# VARIANT_BASES.
ALL_KNOWN_VARIANTS: list[str] = [
    "cuda", "tensorrt", "rocm", "intel", "oneapi",
    "npu", "tpu", "metal", "groq", "fpga", "vulkan", "cpu",
]


# Stub Dockerfile rendering was removed in Phase 1 of the variant-
# aware wizard rollout. The old shape emitted a placeholder
# base-python:3.11 image for every tag that wasn't in VARIANT_BASES,
# so a pandas-only script shipped 13 near-identical images to Harbor
# — wasted disk + wasted pull time and never actually accelerated
# anything. Use `real_variants_only` instead; unsupported tags are
# simply omitted.


_FROM_RE = re.compile(r"^\s*FROM\s+\S+", re.MULTILINE)
_TORCH_INDEX_RE = re.compile(
    r"--index-url\s+https://download\.pytorch\.org/whl/[A-Za-z0-9._-]+",
)

# A pip install line whose only real arguments are torch-family
# packages. Used to delete the torch install entirely for variants
# where it has no business (NPU / OpenVINO — they use their own
# inference runtimes and don't need torch).
_TORCH_ONLY_PIP_LINE = re.compile(
    r"""
    ^[ \t]*RUN[ \t]+pip[ \t]+install          # RUN pip install
    (?:[ \t]+--no-cache-dir)?                 # optional no-cache
    (?:[ \t]+--index-url[ \t]+\S+)?           # optional index url
    (?:[ \t]+(?:torch|torchvision|torchaudio))+  # one+ torch packages
    [ \t]*$                                    # end of line
    """,
    re.MULTILINE | re.VERBOSE,
)


def detect_compatible_variants(source_content: str, hero_from: str) -> list[str]:
    """Return the ordered list of variants this node should have.

    CPU is always present as the universal fallback. GPU variants are
    added based on what the source imports + what the hero's FROM
    hints at:

      - Any torch / tensorflow / jax import OR a CUDA/ROCm FROM →
        cuda + rocm (the torch ecosystem is portable across them)
      - ``intel_extension_for_pytorch`` import → intel
      - ``openvino`` / ``onnxruntime`` import → npu

    Missing variants are just omitted — no entry in gpu_variants means
    the scheduler won't dispatch this node to that hardware.
    """
    src = source_content or ""
    out: list[str] = []

    # Module-use detection. A module counts as used if it appears
    # on EITHER side of an `import … X, Y, Z` comma list or as
    # `from X…` — a naive `import X` check misses
    # `import os, pathlib, torch`, which is how half the Python
    # scripts in the wild write imports.
    def _used(module: str) -> bool:
        return bool(re.search(
            rf"^\s*(?:from\s+{re.escape(module)}(?:\.\w+)*\s+import|"
            rf"import\s+(?:\w+(?:\.\w+)*\s*,\s*)*{re.escape(module)}\b)",
            src, re.MULTILINE,
        ))

    uses_torch    = _used("torch")
    uses_tf       = _used("tensorflow")
    uses_jax      = _used("jax")
    uses_ipex     = _used("intel_extension_for_pytorch")
    uses_openvino = _used("openvino")
    uses_onnxrt   = _used("onnxruntime")
    uses_any_gpu_framework = any([
        uses_torch, uses_tf, uses_jax, uses_ipex, uses_openvino, uses_onnxrt
    ])

    def _add(v: str) -> None:
        if v not in out:
            out.append(v)

    # torch / tf / jax → CUDA + ROCm + TensorRT (TensorRT is a CUDA
    # subset, same base image, same wheel channel). Pure-Python
    # scripts with no GPU framework imports get only CPU — we
    # don't manufacture GPU variants for something that doesn't
    # need one.
    if uses_torch or uses_tf or uses_jax:
        _add("cuda")
        _add("rocm")
        _add("tensorrt")
    # IPEX extends torch specifically for Intel Arc / Xe; emit both
    # the "intel" and "oneapi" tags (same base image) so tenants
    # who labelled their workers with either tag get matched.
    if uses_ipex:
        _add("intel")
        _add("oneapi")
    # OpenVINO / ONNX Runtime don't need torch — they're their own
    # inference stacks. Emit NPU; do NOT add cuda/rocm/tensorrt
    # unless torch was ALSO imported (the `if uses_torch` above
    # will have handled it).
    if uses_openvino or uses_onnxrt:
        _add("npu")

    # CPU is always emitted as the universal fallback, even for
    # scripts with no GPU framework imports (everything runs on CPU
    # given enough time). Last in the list because it's the slowest.
    _add("cpu")

    # Note: we deliberately do NOT sniff hero_from for additional
    # signals. The base image the wizard picked is itself derived
    # from the source analysis, so reading it back in here would
    # double-count and give pure-Python scripts spurious GPU
    # variants. hero_from is kept as a parameter for forward-
    # compatibility (future rules may want to disambiguate e.g.
    # IPEX vs OpenVINO when a user explicitly pins a base image).
    _ = hero_from   # intentionally unused; kept for API stability
    _ = uses_any_gpu_framework

    return out


def synthesise_variant(hero_dockerfile: str, variant: str) -> str:
    """Produce the Dockerfile text for `variant` by transforming the hero.

    - Swaps the FROM line to the variant's base image.
    - Rewrites the torch `--index-url` to the variant's wheel index,
      or removes the flag when the variant has no specific index
      (CPU/Intel/NPU use the default PyPI).
    """
    if variant not in VARIANT_BASES:
        raise ValueError(f"unknown variant: {variant}")
    out = hero_dockerfile

    new_from = f"FROM {VARIANT_BASES[variant]}"
    out = _FROM_RE.sub(new_from, out, count=1)

    # Insert a variant-unique LABEL right after the FROM so every
    # variant's manifest has a distinct digest — even for variants
    # that happen to produce byte-identical layers (e.g. cpu vs the
    # default wheel path of intel). Without this, two variants with
    # no backend-specific dependencies hash to the same manifest and
    # Harbor rejects the second push with 412 Precondition Failed
    # when a project vuln policy is set.
    #
    # Also inject `USER root` immediately after the LABEL. EVERY MOD
    # base image (CORE/DockerContainers/BaseImages/*/Dockerfile) ends
    # with `USER mod` (uid=10001) for runtime hardening. When a hero
    # Dockerfile (typically authored against a root-default upstream
    # like python:3.11-slim) is FROM-swapped to one of these MOD bases,
    # any subsequent `RUN apt-get update` / `apt-get install` fails
    # with "Permission denied" on /var/lib/apt/lists/partial because
    # the build is now running as uid 10001. Surfaced as exit code 100
    # mid-build with no diagnostic. Resetting USER to root here keeps
    # build-time RUNs working; runtime hardening is the operator's
    # responsibility (a `USER mod` near the user's ENTRYPOINT, or
    # docker run --user).
    lines = out.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("FROM "):
            lines.insert(i + 1, f'LABEL mod.gpu_variant="{variant}"')
            lines.insert(i + 2, "USER root")
            break
    out = "\n".join(lines)

    # NPU / OpenVINO doesn't need torch — it has its own inference
    # runtime. Strip any torch-only pip install line entirely so the
    # image stays lean and doesn't pull ~2 GB of wheels it won't use.
    if variant == "npu":
        out = _TORCH_ONLY_PIP_LINE.sub("", out)
        # Double newlines left by the removal
        out = re.sub(r"\n{3,}", "\n\n", out)

    target_index = VARIANT_TORCH_INDEX.get(variant)
    if target_index:
        out = _TORCH_INDEX_RE.sub(f"--index-url {target_index}", out)
    else:
        # CPU/Intel/NPU: strip the --index-url flag entirely and
        # collapse the whitespace so we don't leave a 40-space gap
        # where the flag used to live. Two-pass:
        # 1) remove an entire continuation-line that carried nothing
        #    but the flag (common LLM output shape: each flag on its
        #    own \\-continued line).
        # 2) remove any inline leftover on shared lines.
        # 3) collapse multi-space runs outside of leading indentation.
        out = re.sub(
            r"[ \t]*--index-url\s+https://download\.pytorch\.org/whl/[A-Za-z0-9._-]+[ \t]*\\?\n",
            "",
            out,
        )
        out = _TORCH_INDEX_RE.sub("", out)
        # Collapse horizontal whitespace but preserve single-leading
        # indentation for nested pip-continuation lines. Apply only
        # to runs of 2+ internal spaces (not at line start).
        out = re.sub(r"(?<=\S) {2,}", " ", out)

    # Per-variant pip package swaps — add backend-specific wheels
    # (ipex for Intel, openvino for NPU) and strip wheels the variant
    # can't use (torch on NPU). Runs AFTER the torch-index rewrite so
    # the add/remove targets apply to the already-index-corrected
    # install line. No-op for variants not in VARIANT_PIP_PACKAGES.
    pip_adjust = VARIANT_PIP_PACKAGES.get(variant)
    if pip_adjust:
        out = _apply_pip_adjustments(
            out, add=pip_adjust.get("add", []), remove=pip_adjust.get("remove", []),
        )

    # Per-variant entrypoint substitution is NOT done here — that would
    # require rewriting COPY statements in the Dockerfile, which breaks
    # on multi-source COPYs, `COPY . /app`, non-standard ENTRYPOINT
    # paths, etc. Instead ImageBuilder keeps the filename stable
    # (entrypoint.sh) and overwrites that file in the build context
    # with each variant's adapted source immediately before calling
    # `docker build`. The loop is serial so there's no race.
    return out


# Match a pip-install RUN line plus its backslash-continuation tail.
# We anchor on `pip[3]? install` to avoid mangling unrelated RUN
# statements (e.g. `RUN apt-get install ...`). The capture is wide
# because Dockerfile authors put all kinds of flags here (--no-cache-dir,
# --upgrade, --index-url handled separately above). DOTALL lets the
# pattern cross newlines via the explicit `\\\n` escape.
_PIP_INSTALL_BLOCK_RE = re.compile(
    r"(RUN\s+(?:pip3?|python3?\s+-m\s+pip)\s+install(?:\s|\\\n)+)"
    r"((?:[^\n\\]|\\\n)*)",
    re.MULTILINE,
)

# Split a captured pip-install body into tokens we can manipulate.
# Handles backslash-continuations ("\\\n"), extra whitespace, and the
# common pattern of one package per line. Preserves flag tokens like
# --upgrade / --no-cache-dir so they survive the rewrite.
def _tokenize_pip_body(body: str) -> list[str]:
    # Join continuation lines, then split on whitespace.
    flat = re.sub(r"\\\n\s*", " ", body).strip()
    return flat.split()


def _apply_pip_adjustments(dockerfile: str, *, add: list[str], remove: list[str]) -> str:
    """Rewrite every `RUN pip install …` block: strip tokens whose
    package name matches `remove` (case-insensitive, ignoring version
    pins), then append any `add` tokens that aren't ALREADY PRESENT
    ANYWHERE ELSE in the Dockerfile's pip installs.

    Preserves flag tokens (--no-cache-dir, --upgrade, etc.) — only
    package tokens are filtered. Comparing on the normalised package
    name (e.g. `torch>=2.0.0` → `torch`) so version-pinned deps still
    match.

    Global-presence check matters when a Dockerfile has multiple
    `RUN pip install` blocks (common when authors split CPU wheels
    from GPU wheels): we add each requested package exactly once,
    appended to the LAST block, rather than once per block.
    """
    if not add and not remove:
        return dockerfile

    remove_lc = {r.lower() for r in remove}
    add_lc = [a for a in add]

    def _pkg_name(token: str) -> str:
        # Package tokens look like `torch`, `torch==2.0`, `torch>=2`,
        # `torch[cuda]`, etc. The canonical name is what comes before
        # any version / extras marker. Flags start with `-` so they
        # survive unchanged.
        if token.startswith("-"):
            return ""
        return re.split(r"[<>=!~\[]", token, maxsplit=1)[0].strip().lower()

    # Pass 1: walk all pip-install blocks, apply the `remove` filter,
    # collect the names present across the whole Dockerfile post-
    # removal. The resulting set drives the `add` decision in pass 2.
    matches = list(_PIP_INSTALL_BLOCK_RE.finditer(dockerfile))
    if not matches:
        return dockerfile

    filtered_bodies: list[list[str]] = []
    global_present: set[str] = set()
    for m in matches:
        tokens = _tokenize_pip_body(m.group(2))
        kept: list[str] = []
        for t in tokens:
            name = _pkg_name(t)
            if name and name in remove_lc:
                continue
            kept.append(t)
            if name:
                global_present.add(name)
        filtered_bodies.append(kept)

    # Pass 2: figure out which `add` packages still need appending.
    to_append = [a for a in add_lc if a.lower() not in global_present]

    # Rebuild the dockerfile in order, appending unseen packages to
    # the LAST block only. Start from the end and patch each block in
    # reverse so `dockerfile` offsets stay valid while we slice.
    out = dockerfile
    for idx, m in reversed(list(enumerate(matches))):
        head = m.group(1)
        body_tokens = filtered_bodies[idx]
        if idx == len(matches) - 1 and to_append:
            body_tokens = body_tokens + to_append
        new_block = head + " ".join(body_tokens)
        out = out[: m.start()] + new_block + out[m.end():]

    return out


def all_variants(
    hero_dockerfile: str, variants: Iterable[str],
) -> Dict[str, str]:
    """Return {variant_name: dockerfile_text} including the hero as-is."""
    out: Dict[str, str] = {}
    for v in variants:
        out[v] = synthesise_variant(hero_dockerfile, v)
    return out


def real_variants_only(
    hero_dockerfile: str, compatible: Iterable[str],
) -> Dict[str, str]:
    """Emit a Dockerfile ONLY for variants the source actually supports.

    Called in place of `all_known_variants_with_stubs` — the old
    function produced a stub for every tag in ALL_KNOWN_VARIANTS,
    which meant a pandas-only script shipped 13 images (12 of which
    were near-identical base-python containers with no GPU story).
    Replacing it with this honours the rule "only build what the
    source can actually use": compatible comes from the language-aware
    detector and the platform admin's GPU-compatibility whitelist,
    already intersected.

    If `compatible == ["cpu"]`, the caller should NOT even use variant
    suffixes on the tag — the hero image itself IS the only build. We
    signal that by returning {} so `gpu_variants_map` collapses to the
    empty dict in the node metadata and the scheduler treats the node
    as any-hardware.
    """
    compatible_set = set(compatible)
    if compatible_set == {"cpu"} or not compatible_set:
        return {}
    return {
        v: synthesise_variant(hero_dockerfile, v)
        for v in compatible_set
        if v in VARIANT_BASES  # skip variants we can't deterministically synthesise
    }


# ---------------------------------------------------------------------------
# build-and-push.sh template that iterates every Dockerfile.<variant>
# ---------------------------------------------------------------------------

BUILD_PUSH_TEMPLATE = """\
#!/usr/bin/env bash
# Build + push the MOD node across every GPU variant emitted by the wizard.
# The metadata's `gpu_variants` dict references each of these image tags so the
# worker scheduler can dispatch the node to whichever backend a worker has.
#
# Usage:
#   ./build-and-push.sh              # build every Dockerfile.* with tag 'latest'
#   ./build-and-push.sh 1.0.0        # custom tag
#   ./build-and-push.sh latest push  # build + push every variant
#   ./build-and-push.sh 1.0.0 push
set -e

TAG="${{1:-latest}}"
ACTION="${{2:-}}"

REGISTRY="${{REGISTRY:-{registry}}}"
PROJECT="${{PROJECT:-{project}}}"
IMAGE_NAME="${{IMAGE_NAME:-{image_name}}}"

for DF in Dockerfile Dockerfile.*; do
    [ -f "$DF" ] || continue
    if [ "$DF" = "Dockerfile" ]; then
        VARIANT="{hero_variant}"
    else
        VARIANT="${{DF#Dockerfile.}}"
    fi
    FULL="${{REGISTRY}}/${{PROJECT}}/${{IMAGE_NAME}}:${{TAG}}-${{VARIANT}}"
    echo ""
    echo "==> Building ${{VARIANT}}: ${{FULL}}"
    docker build -f "$DF" -t "${{FULL}}" .
    if [ "$ACTION" = "push" ]; then
        echo "==> Pushing ${{FULL}}"
        docker push "${{FULL}}"
    fi
    echo "${{FULL}}"
done

echo ""
echo "Done. gpu_variants dict for the metadata:"
echo "gpu_variants = {{"
for DF in Dockerfile Dockerfile.*; do
    [ -f "$DF" ] || continue
    if [ "$DF" = "Dockerfile" ]; then
        VARIANT="{hero_variant}"
    else
        VARIANT="${{DF#Dockerfile.}}"
    fi
    echo "    \\"${{VARIANT}}\\": \\"${{IMAGE_NAME}}:${{TAG}}-${{VARIANT}}\\","
done
echo "}}"
"""


# ---------------------------------------------------------------------------
# No-registry (CE LAN) build-and-push.sh: bare local tags, no push.
# ---------------------------------------------------------------------------

BUILD_PUSH_NO_REGISTRY_TEMPLATE = """\
#!/usr/bin/env bash
# Build every MOD node variant as a LOCAL image (no registry configured).
# The metadata's `gpu_variants` dict references each of these image tags so the
# worker scheduler can dispatch the node to whichever backend a worker has.
#
# Usage:
#   ./build-and-push.sh              # build every Dockerfile.* with tag 'latest'
#   ./build-and-push.sh 1.0.0        # custom tag
#   (push is skipped: there is no registry to push to)
set -e

TAG="${{1:-latest}}"

IMAGE_NAME="${{IMAGE_NAME:-{image_name}}}"

for DF in Dockerfile Dockerfile.*; do
    [ -f "$DF" ] || continue
    if [ "$DF" = "Dockerfile" ]; then
        VARIANT="{hero_variant}"
    else
        VARIANT="${{DF#Dockerfile.}}"
    fi
    FULL="${{IMAGE_NAME}}:${{TAG}}-${{VARIANT}}"
    echo ""
    echo "==> Building ${{VARIANT}}: ${{FULL}}"
    docker build -f "$DF" -t "${{FULL}}" .
    echo "${{FULL}}"
done

echo ""
echo "Done. gpu_variants dict for the metadata:"
echo "gpu_variants = {{"
for DF in Dockerfile Dockerfile.*; do
    [ -f "$DF" ] || continue
    if [ "$DF" = "Dockerfile" ]; then
        VARIANT="{hero_variant}"
    else
        VARIANT="${{DF#Dockerfile.}}"
    fi
    echo "    \\"${{VARIANT}}\\": \\"${{IMAGE_NAME}}:${{TAG}}-${{VARIANT}}\\","
done
echo "}}"
"""


def render_build_push(
    *, registry: str, project: str, image_name: str, hero_variant: str,
) -> str:
    """Produce a build-and-push.sh that iterates every Dockerfile variant.

    With no registry configured (CE LAN: the API threads LOCAL_REGISTRY="")
    the image is tagged bare — `<image_name>:<tag>-<variant>`, the same
    local-tag convention render_gpu_variants_dict emits — and the push is
    skipped. `tenant-N` is a registry PROJECT namespace; with no registry
    there is nothing to namespace into, and `/tenant-N/x` is not a valid
    docker ref.
    """
    if not (registry or "").strip():
        return BUILD_PUSH_NO_REGISTRY_TEMPLATE.format(
            image_name=image_name, hero_variant=hero_variant,
        )
    return BUILD_PUSH_TEMPLATE.format(
        registry=registry, project=project,
        image_name=image_name, hero_variant=hero_variant,
    )


def infer_hero_variant(hero_from: str) -> str:
    """Guess which variant the hero Dockerfile targets from its FROM line."""
    h = (hero_from or "").lower()
    if "cuda" in h:
        return "cuda"
    if "rocm" in h:
        return "rocm"
    if "intel" in h:
        return "intel"
    if "npu" in h or "openvino" in h:
        return "npu"
    return "cpu"


def render_gpu_variants_dict(
    image_name: str, tag: str, variants: Iterable[str],
) -> Dict[str, str]:
    """Return the metadata-compatible {variant: image_name:tag-variant} dict."""
    return {v: f"{image_name}:{tag}-{v}" for v in variants}

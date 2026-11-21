"""Intel XPU (Arc / Battlemage / PVC / iGPU) recipe — real, not stub.

Pattern-matched source rewriter (no LLM) + production Dockerfile.

What the rewriter does:

    1. After the first `import torch` statement, insert
       `import intel_extension_for_pytorch as ipex  # noqa: F401`.
    2. Swap every `.to("cuda")` / `.to('cuda')` / `device='cuda'` /
       `torch.device("cuda")` for the xpu equivalent.
    3. Swap `torch.cuda.<X>` for `torch.xpu.<X>` for the API names IPEX
       has direct equivalents for (synchronize, is_available,
       device_count, current_device, set_device, empty_cache,
       memory_allocated, amp.autocast).
    4. Force `pin_memory=True` to `pin_memory=False` (Intel DataLoader
       gets warnings; perf is similar without it).
    5. After detected model load patterns
       (`Model.from_pretrained(...)`), insert
       `model_var = ipex.optimize(model_var)` on the next line.

Pattern-matched, deliberately conservative: false negatives (miss a
rewrite) are fine — `ipex.optimize` is idempotent and the runtime
catches missed cuda calls with a clear error. False positives (rewrite
something we shouldn't) ship broken images, so each rule has a tight
scope.

The negative classifier already refuses sources that use `flash_attn`,
custom `.cu` files, NCCL, FP8, or `vllm_server`, so the recipe doesn't
have to handle those — those variants are emitted as `incompatible`
upstream of this code.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from ..recipe_interface import (
    FileChange, FileWarning, RecipeOutput, RewrittenFile,
)


# ---------------------------------------------------------------------------
# Dockerfile template
# ---------------------------------------------------------------------------

# IPEX 2.6 → torch 2.6.0+xpu, oneAPI 2025.x base. Pinned tightly. Bump
# only after a CI smoke run on real Intel hardware passes.
#
# Image name + tag MUST match the canonical entry in
# scripts/tools/gpu_variants.py::VARIANT_BASES — divergence here causes
# `docker build` to fail with `... not found` because the recipe's
# Dockerfile FROM doesn't resolve to anything in Harbor. (Past breakage:
# this used to read base-intel:ipex2.6-python3.11, which never existed
# in the registry — only base-intel-xpu:2025.0 does.)
_BASE_IMAGE = "ghcr.io/modtim/base-intel-xpu:2025.0"

_DOCKERFILE_TEMPLATE = """\
FROM {base_image}
LABEL mod.gpu_variant="intel"
LABEL mod.recipe="intel_xpu"
LABEL mod.recipe_phase="2"

# base-intel-xpu ends with `USER mod` for runtime hardening. Reset to
# root so build-time RUNs (pip install, COPY --chmod, apt-get if added
# later) don't fail with permission errors. Mirrors the USER root
# injection in synthesise_variant for hero-derived variant Dockerfiles.
USER root

WORKDIR /data

# IPEX-tuned PyTorch + IPEX runtime. Pinned to a known-good triplet;
# CI sweeps on Battlemage/Arc Pro will bump this when a newer combo
# passes the smoke probe. Note: torch is the vanilla PyPI build; IPEX
# adds the XPU runtime. Intel's wheel index ships `torch-2.5.1+cxx11.abi`
# under the `+xpu` channel — there's no `torch==2.6.0+xpu` wheel.
# Pinning `+xpu` on torch fails resolution.
RUN pip install --no-cache-dir \\
        --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/ \\
        torch==2.6.0 torchvision \\
        intel-extension-for-pytorch==2.6.10+xpu

{hero_pip_block}

COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh

ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
"""


def render_dockerfile(*, hero_pip_packages: List[str] = (), **_: object) -> str:
    """Render the Intel XPU Dockerfile.

    `hero_pip_packages` mirrors the application-level pip deps from the
    hero Dockerfile (transformers, pillow, etc.). The torch ecosystem
    is replaced; everything else passes through.
    """
    if hero_pip_packages:
        block = "RUN pip install --no-cache-dir " + " ".join(
            shell_quote(p) for p in hero_pip_packages
        )
    else:
        block = "# (no application-level pip dependencies)"
    return _DOCKERFILE_TEMPLATE.format(
        base_image=_BASE_IMAGE,
        hero_pip_block=block,
    )


def shell_quote(s: str) -> str:
    """Quote a pip dependency string for safe inclusion in a shell
    RUN line. We don't expect spaces in pip names but versions can
    have them in conditional markers."""
    if all(c.isalnum() or c in "._-=<>!~+@" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Source rewrites
# ---------------------------------------------------------------------------

# Each rule is (regex, replacement, reason). Replacement uses sub
# back-references via the regex groups. Order matters — the first
# matching rule wins per substring; subsequent rules don't re-match
# already-rewritten code.
_PY_RULES: Tuple[Tuple[re.Pattern, str, str], ...] = (
    # `.to("cuda")` / `.to('cuda')`
    (
        re.compile(r"\.to\((\"|')cuda(\"|')\)"),
        r'.to("xpu")',
        "Device 'cuda' → 'xpu' for Intel XPU.",
    ),
    # `.to("cuda:0")` / `.to('cuda:1')` etc — strip device index, IPEX picks one
    (
        re.compile(r"\.to\((\"|')cuda:\d+(\"|')\)"),
        r'.to("xpu")',
        "Device 'cuda:N' → 'xpu' (IPEX selects the first available XPU).",
    ),
    # `device="cuda"` keyword
    (
        re.compile(r"device\s*=\s*(\"|')cuda(\"|')"),
        r'device="xpu"',
        "device='cuda' kwarg → 'xpu'.",
    ),
    # `torch.device("cuda")`
    (
        re.compile(r"torch\.device\((\"|')cuda(\"|')\)"),
        r'torch.device("xpu")',
        "torch.device('cuda') → torch.device('xpu').",
    ),
    # `torch.cuda.<X>` → `torch.xpu.<X>` for direct-equivalent APIs
    (
        re.compile(r"torch\.cuda\.(synchronize|is_available|device_count|"
                   r"current_device|set_device|empty_cache|"
                   r"memory_allocated|memory_reserved|"
                   r"max_memory_allocated|reset_peak_memory_stats)\b"),
        r"torch.xpu.\1",
        "torch.cuda.* → torch.xpu.* (IPEX provides direct equivalents).",
    ),
    # `torch.cuda.amp.<X>` → `torch.xpu.amp.<X>`
    (
        re.compile(r"torch\.cuda\.amp\.(autocast|GradScaler)\b"),
        r"torch.xpu.amp.\1",
        "torch.cuda.amp → torch.xpu.amp (IPEX has its own amp namespace).",
    ),
    # `pin_memory=True` → `pin_memory=False`. CUDA-specific perf path;
    # a no-op on XPU and prints warnings.
    (
        re.compile(r"\bpin_memory\s*=\s*True\b"),
        "pin_memory=False",
        "pin_memory=True is CUDA-specific; disabled for Intel XPU "
        "(harmless perf delta).",
    ),
)

# Patterns that match a model-load expression so we know where to
# insert the `ipex.optimize(...)` call. Captured group is the
# variable name on the LHS of the assignment.
#
# We deliberately scope to classes that look like nn.Module subclasses
# (Model / ForCausalLM / ForConditionalGeneration / etc.) because
# `ipex.optimize` is a no-op + warning on tokenizers, processors, and
# config objects — not breaking, but noisy in logs.
_MODEL_LOAD_PATTERNS = (
    re.compile(
        r"^(\s*)(\w+)\s*=\s*("
        r"[A-Z][A-Za-z0-9_]*Model|"
        r"[A-Z][A-Za-z0-9_]*For[A-Z][A-Za-z0-9_]+|"
        r"AutoModel(?:For[A-Z][A-Za-z0-9_]+)?"
        r")\.from_pretrained\(",
        re.MULTILINE,
    ),
    re.compile(
        r"^(\s*)(\w+)\s*=\s*pipeline\(",
        re.MULTILINE,
    ),
)


def _rewrite_python(body: str) -> Tuple[str, List[FileChange], List[FileWarning]]:
    """Apply the Python rewrite rules. Returns (new body, changes,
    warnings)."""
    changes: List[FileChange] = []
    warnings: List[FileWarning] = []
    new_body = body

    for pattern, replacement, reason in _PY_RULES:
        # Process line by line so we can capture before/after for the
        # change-list with accurate line numbers.
        new_lines = []
        for lineno, line in enumerate(new_body.splitlines(keepends=True), start=1):
            if pattern.search(line):
                rewritten = pattern.sub(replacement, line)
                if rewritten != line:
                    changes.append(FileChange(
                        before=line.rstrip("\n"),
                        after=rewritten.rstrip("\n"),
                        line=lineno,
                        reason=reason,
                    ))
                new_lines.append(rewritten)
            else:
                new_lines.append(line)
        new_body = "".join(new_lines)

    # Insert `import intel_extension_for_pytorch` after the first line
    # that brings torch in. Handles bare `import torch`, multi-name
    # imports (`import os, pathlib, torch`), aliases (`import torch as T`),
    # and `from torch...` forms.
    if "import intel_extension_for_pytorch" not in new_body:
        torch_import_pat = re.compile(
            r"^[ \t]*(?:"
            r"from\s+torch(?:\.\w+)*\s+import\s+\S+|"      # from torch import X
            r"import\s+(?:[\w]+\s*,\s*)*torch\b[^\n]*"     # import [a, b,] torch [as T]
            r")\s*$",
            re.MULTILINE,
        )
        m = torch_import_pat.search(new_body)
        if m:
            ipex_import = "import intel_extension_for_pytorch as ipex  # noqa: F401\n"
            insert_at = m.end()
            new_body = (
                new_body[:insert_at] + "\n" + ipex_import +
                new_body[insert_at:].lstrip("\n")
            )
            lineno = new_body[:insert_at].count("\n") + 2
            changes.append(FileChange(
                before="(no ipex import)",
                after=ipex_import.rstrip("\n"),
                line=lineno,
                reason="Inserted IPEX import — required to register XPU "
                       "device with PyTorch.",
            ))
        else:
            warnings.append(FileWarning(
                severity="info",
                message="No torch import found — IPEX import not "
                        "inserted; if this file uses torch indirectly "
                        "the operator should add the import manually.",
            ))

    # Append `ipex.optimize(model)` after every detected model load.
    for pattern in _MODEL_LOAD_PATTERNS:
        rebuilt: List[str] = []
        last_end = 0
        for m in pattern.finditer(new_body):
            indent = m.group(1)
            varname = m.group(2)
            # Find the close of this assignment expression (may span
            # multiple lines). Walk forward to the line ending after
            # the parens balance to zero.
            paren_depth = 0
            end = m.start()
            while end < len(new_body):
                ch = new_body[end]
                if ch == "(":
                    paren_depth += 1
                elif ch == ")":
                    paren_depth -= 1
                    if paren_depth == 0:
                        nl = new_body.find("\n", end)
                        end = nl + 1 if nl != -1 else len(new_body)
                        break
                end += 1
            if paren_depth != 0:
                continue  # malformed — skip

            # Avoid double-applying if `ipex.optimize` is already on
            # the next line.
            after = new_body[end:end + 200]
            if f"{varname} = ipex.optimize" in after:
                continue

            rebuilt.append(new_body[last_end:end])
            insertion = f"{indent}{varname} = ipex.optimize({varname})\n"
            rebuilt.append(insertion)
            lineno = new_body[:m.start()].count("\n") + 1
            changes.append(FileChange(
                before=f"(no ipex.optimize call after {varname} load)",
                after=insertion.rstrip("\n"),
                line=lineno + 1,
                reason="Inserted ipex.optimize() call — gives IPEX a "
                       "chance to apply XPU graph optimisations.",
            ))
            last_end = end
        if last_end:
            rebuilt.append(new_body[last_end:])
            new_body = "".join(rebuilt)

    return new_body, changes, warnings


def rewrite_source(source_files: Dict[str, str]) -> RecipeOutput:
    """Rewrite every Python (and shell-with-embedded-Python) file in
    `source_files` for Intel XPU. Non-Python files pass through.

    Skip-if-no-torch: if NONE of the input files import torch (the IPEX
    rewrite has nothing to bite on AND no model loads to optimise),
    return ``incompatible_reason`` instead of an empty rewrite. The
    Intel variant Dockerfile pulls ~5 GB of base-intel-xpu + IPEX +
    transformers and pushing it across stage→platform Harbor took
    11 min for a tiny non-ML node on stage. Marking the variant
    incompatible makes the build_dag adapter (`build_dag/adapter.py:48`)
    skip the variant entirely — saves the build, smoke, and push
    tasks for a variant that would just be a fat CPU container with
    zero runtime benefit. Matches what the NPU recipe already does
    when there's no `from_pretrained` to target.
    """
    out: Dict[str, RewrittenFile] = {}
    for fn, body in source_files.items():
        if fn.endswith(".py"):
            new_body, changes, warnings = _rewrite_python(body)
            out[fn] = RewrittenFile(body=new_body, changes=changes,
                                    warnings=warnings)
        elif fn.endswith(".sh"):
            # Entrypoint scripts often heredoc Python. Apply the same
            # rules to the whole body — false positives are unlikely
            # because rules target torch namespace specifically.
            new_body, changes, warnings = _rewrite_python(body)
            warnings.append(FileWarning(
                severity="info",
                message="Shell file rewritten in-place; if this script "
                        "embeds Python via heredoc the rewrites apply "
                        "to the embedded code.",
            ))
            out[fn] = RewrittenFile(body=new_body, changes=changes,
                                    warnings=warnings)
        else:
            out[fn] = RewrittenFile(body=body)

    # Look across every Python / shell input for any sign of torch.
    # Plain string match is fine — the torch namespace is unique
    # enough that a false positive (e.g. an unrelated `torch` in a
    # docstring) is preferable to skipping a real ML node.
    any_torch_signal = any(
        ("torch" in (body or ""))
        for fn, body in source_files.items()
        if fn.endswith((".py", ".sh"))
    )
    if not any_torch_signal:
        # Workload-level incompatibility: a non-torch workload (e.g.
        # a binary-shell-out photogrammetry script) cannot benefit
        # from IPEX no matter what the operator does. Setting
        # `workload_incompatible=True` tells the planner to suppress
        # the stub Dockerfile — without that suppression, the wizard
        # would show an Intel variant baked with IPEX + the
        # ~5 GB base-intel-xpu image as if the operator could hand-
        # port it, but there's nothing to port to.
        return RecipeOutput(
            files=out,
            workload_incompatible=True,
            incompatible_reason=(
                "No torch / ML code detected in source — IPEX + XPU "
                "have nothing to optimise. Skipping the Intel variant "
                "to avoid building and pushing a ~5 GB image that "
                "would just be a CPU container with extra runtime."
            ),
        )
    return RecipeOutput(files=out, incompatible_reason=None)


# Phase-1 callers still call render(...) — keep that name as an alias.
def render(*, image_name: str = "node", hero_pip_packages: List[str] = (),
           **_: object) -> str:
    return render_dockerfile(image_name=image_name,
                              hero_pip_packages=hero_pip_packages)

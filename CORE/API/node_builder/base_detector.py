"""Detect which curated base image a submission should use.

Input shapes the detector understands:

- **Repo**: a dict of `{path: content}` for a few sentinel files (e.g.
  `requirements.txt`, `package.json`, `go.mod`, `Cargo.toml`) that a caller
  fetched from the user's git repo. We don't clone the repo here — that's
  the LLM-analyzer's job upstream. We take what the caller sniffed.
- **Script**: a declared interpreter + the script body. Interpreter maps
  directly to a base; body is inspected for GPU-framework imports so
  Python scripts that reach for CUDA-backed Torch/TF get upgraded to a
  CUDA base automatically.

The caller passes the **validated allowlist** from node_admission so the
detector's output always lands inside the platform's accepted set —
there's no way for this module to pick something admission will reject.
If the allowlist moves, the detector moves with it.

Output: `DetectedBase(image_ref=platform_image("base-python:3.11"),
reason="Python dependencies detected (requirements.txt)")`
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from utils.registries import platform_image


@dataclass
class DetectedBase:
    image_ref: str
    reason: str
    from_hint: str  # 'manifest-file', 'shebang', 'interpreter', 'dep-sniff', 'default'


# Canonical image refs per language/runtime. Each MUST be a prefix match
# against one of `allowed_from_prefixes` in base_image_allowlist.json so
# the downstream admission re-validation always approves the detected base.
BASE_PYTHON_311 = platform_image("base-python:3.11")
BASE_PYTHON_312 = platform_image("base-python:3.12")
BASE_NODE_20 = platform_image("base-node:20")
BASE_NODE_22 = platform_image("base-node:22")
BASE_GO_122 = platform_image("base-go:1.22")
BASE_RUST_178 = platform_image("base-rust:1.78")
BASE_JAVA_21 = platform_image("base-java:21")
BASE_CUDA_PY311 = platform_image("base-cuda:12.4-python3.11")
BASE_ALPINE = platform_image("base-alpine:3.19")
BASE_DISTROLESS_PY = platform_image("base-distroless-python:3.11")
BASE_DISTROLESS_NODE = platform_image("base-distroless-node:20")


# Markers that a repo's Python code will want a CUDA-enabled base.
# Keep this list short and conservative — false positives cost users
# cold-start time and GPU scheduling pressure; false negatives are cheap
# because users can override on the review step.
_PY_GPU_MARKERS = re.compile(
    r"\b(torch|tensorflow|tensorflow-gpu|cupy|onnxruntime-gpu|jax\[cuda|"
    r"nvidia-|pycuda|tritonclient|deepspeed|xformers|transformer-engine)\b",
    re.IGNORECASE,
)


# Node ≥22 features we should pin to base-node:22 over base-node:20 if seen.
_NODE_22_MARKERS = re.compile(r'"engines"\s*:\s*\{[^}]*"node"\s*:\s*"\^?22', re.IGNORECASE)


def detect_from_repo(manifest_files: dict[str, str]) -> DetectedBase:
    """`manifest_files` is a dict of sentinel-filename → content snippet
    collected by the caller (git sniffer, LLM fetch, etc.). Missing files
    are represented by absent keys or empty strings. Returns the base the
    submission should FROM.
    """
    def has(name: str) -> bool:
        return bool((manifest_files or {}).get(name, "").strip())

    py = manifest_files.get("requirements.txt", "") + "\n" + manifest_files.get("pyproject.toml", "")
    js = manifest_files.get("package.json", "")

    # GPU-framework sniff wins over plain-Python detection — torch in reqs
    # means we want CUDA runtime, not a stock Python base.
    if has("requirements.txt") or has("pyproject.toml"):
        if _PY_GPU_MARKERS.search(py):
            return DetectedBase(
                image_ref=BASE_CUDA_PY311,
                reason="Python + CUDA-backed framework detected (torch/tf/cupy/etc.)",
                from_hint="dep-sniff",
            )
        return DetectedBase(
            image_ref=BASE_PYTHON_311,
            reason="Python dependencies detected (requirements.txt / pyproject.toml)",
            from_hint="manifest-file",
        )

    if has("package.json"):
        if _NODE_22_MARKERS.search(js):
            return DetectedBase(
                image_ref=BASE_NODE_22,
                reason="Node 22 pinned in package.json engines",
                from_hint="manifest-file",
            )
        return DetectedBase(
            image_ref=BASE_NODE_20,
            reason="Node dependencies detected (package.json)",
            from_hint="manifest-file",
        )

    if has("go.mod"):
        return DetectedBase(
            image_ref=BASE_GO_122,
            reason="Go module detected (go.mod)",
            from_hint="manifest-file",
        )

    if has("Cargo.toml"):
        return DetectedBase(
            image_ref=BASE_RUST_178,
            reason="Rust crate detected (Cargo.toml)",
            from_hint="manifest-file",
        )

    if has("pom.xml") or has("build.gradle") or has("build.gradle.kts"):
        return DetectedBase(
            image_ref=BASE_JAVA_21,
            reason="JVM project detected (pom.xml / build.gradle)",
            from_hint="manifest-file",
        )

    # No manifest — fall back to Python since the majority of workflow
    # nodes today are Python-based. Users can override on the review step.
    return DetectedBase(
        image_ref=BASE_PYTHON_311,
        reason="No manifest detected — defaulting to base-python (override available on review step)",
        from_hint="default",
    )


_SHEBANG_RE = re.compile(r"^#!\s*([^\s]+)")


def detect_from_script(interpreter: Optional[str], script_body: str) -> DetectedBase:
    """Script mode — `interpreter` is the user-declared interpreter from
    the wizard (one of python3/bash/node, enforced by admission). If it's
    missing, try a shebang sniff. Python scripts that import GPU frameworks
    are upgraded to CUDA."""
    interp = (interpreter or "").strip().lower()
    if not interp:
        m = _SHEBANG_RE.match(script_body or "")
        if m:
            path = m.group(1)
            if "python" in path:
                interp = "python3"
            elif "bash" in path or path.endswith("/sh"):
                interp = "bash"
            elif "node" in path:
                interp = "node"

    if interp == "python3":
        if _PY_GPU_MARKERS.search(script_body or ""):
            return DetectedBase(
                image_ref=BASE_CUDA_PY311,
                reason="Python + CUDA-backed framework import detected in script body",
                from_hint="dep-sniff",
            )
        return DetectedBase(
            image_ref=BASE_PYTHON_311,
            reason="Python interpreter declared",
            from_hint="interpreter",
        )

    if interp == "node":
        return DetectedBase(
            image_ref=BASE_NODE_20,
            reason="Node interpreter declared",
            from_hint="interpreter",
        )

    if interp == "bash":
        # Minimal environment for shell scripts — distroless too minimal
        # (no /bin/sh), so ship the tiny alpine variant instead.
        return DetectedBase(
            image_ref=BASE_ALPINE,
            reason="Bash interpreter declared — using minimal alpine base",
            from_hint="interpreter",
        )

    # Unknown — upstream admission already restricts to the allowlist, so
    # we shouldn't reach here at steady state. Fall through to python.
    return DetectedBase(
        image_ref=BASE_PYTHON_311,
        reason=f"Unknown interpreter {interpreter!r} — defaulting to base-python",
        from_hint="default",
    )

"""Move E — classify build failures by category, route to specialized
repair prompts.

The repair LLM previously got the SAME generic system prompt for
every failure — whether the build died on a missing compiler
("C++ compiler not found"), a broken version pin ("nerfstudio==1.1.8
does not exist"), or a CUDA-runtime mismatch. Each category has a
known playbook; bundling them into one prompt dilutes signal.

This module:
  - Classifies stderr into one of ~10 categories via deterministic
    regex (fast, no LLM needed — the LLM is for the FIX, not the
    diagnosis).
  - Provides per-category guidance the repair endpoint prepends to
    the LLM user prompt. The guidance is a short playbook that has
    been observed to converge on each category's failure mode.

Used as a hint, not a hard constraint — the LLM is still free to
override if the actual failure doesn't match the classification.
But for the majority of cases where the regex catches the right
category, the LLM saves an iteration of guess-and-check.
"""
from __future__ import annotations

import re
from typing import Optional


# Failure categories — ordered by specificity. classify() returns the
# FIRST matching category, so put narrower patterns earlier.
CATEGORY_COMPILER_MISSING = "compiler_missing"
CATEGORY_VERSION_PIN_INVALID = "version_pin_invalid"
CATEGORY_PACKAGE_NOT_FOUND = "package_not_found"
CATEGORY_IMAGE_NOT_FOUND = "image_not_found"   # FROM-image resolve failure
CATEGORY_CUDA_TOOLCHAIN = "cuda_toolchain"
CATEGORY_DISK_SPACE = "disk_space"
CATEGORY_NETWORK = "network"
CATEGORY_PERMISSION = "permission"
CATEGORY_IMPORT_ERROR = "import_error"
# Item #5 additions — categories the 9-original-categories set missed
# during wfr 42-48 verification:
CATEGORY_WHEEL_BUILD_FAILED = "wheel_build_failed"      # pip wheel build crashed
CATEGORY_APT_PACKAGE_NOT_FOUND = "apt_package_not_found" # E: Unable to locate package X
CATEGORY_TORCH_INDEX_MISMATCH = "torch_index_mismatch"   # torch==X.Y.Z not on cuXYZ index
CATEGORY_USER_DIRECTIVE = "user_directive"               # USER mod failing / useradd conflict
CATEGORY_TLS_CERT = "tls_cert"                           # certificate verify failed
CATEGORY_DEFAULT = "default"


# Each entry: (category, pattern). Order matters — first match wins.
# Patterns are case-insensitive.
_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Version-pin specific — VERY narrow regex catching "X==Y not found",
    # "no matching distribution for X==Y", "no matching distribution
    # found for X==Y". Must come BEFORE generic PACKAGE_NOT_FOUND.
    (CATEGORY_VERSION_PIN_INVALID, re.compile(
        r"(?:no\s+matching\s+distribution\s+(?:found\s+)?for|could\s+not\s+find\s+a\s+version\s+that\s+satisfies)"
        r"\s+[a-z0-9._-]+(?:==|>=|<=|~=)[\w.]+",
        re.IGNORECASE,
    )),

    # Docker base-image FROM resolution failure — must come BEFORE
    # the generic PACKAGE_NOT_FOUND classifier. wfr=42 spent 4
    # outer-repair attempts hallucinating non-existent CUDA tags
    # (nvidia/cuda:12.4.1-base, nvidia/cuda:12.4.0-base, custom
    # harbor variants); a dedicated category routes those to a
    # specialized hint with a curated list of known-existing bases.
    (CATEGORY_IMAGE_NOT_FOUND, re.compile(
        r"failed\s+to\s+resolve\s+source\s+metadata\s+for"
        r"|failed\s+to\s+solve:\s+[A-Za-z0-9/._:-]+:\s+not\s+found"
        r"|manifest\s+(?:for|unknown)\s+[A-Za-z0-9/._:-]+"
        r"|pull\s+access\s+denied\s+for\s+[A-Za-z0-9/._:-]+",
        re.IGNORECASE,
    )),

    # Compiler / build-tool missing.
    (CATEGORY_COMPILER_MISSING, re.compile(
        r"(?:c\+\+\s+compiler|cxx\s+compiler|g\+\+|gcc|cmake)\s*(?:not\s+found|is\s+required|could\s+not\s+(?:find|be\s+found))"
        r"|no\s+such\s+file\s+or\s+directory:\s*['\"]?(?:g\+\+|gcc|cmake|make|ld)"
        r"|cmake_error:?\s*(?:cmake\s+)?(?:c\+\+|cxx)",
        re.IGNORECASE,
    )),

    # CUDA-runtime / toolchain issues.
    (CATEGORY_CUDA_TOOLCHAIN, re.compile(
        r"cuda\s+(?:error|not\s+available|runtime\s+error)"
        r"|no\s+cuda(?:[-\s]+capable)?\s+device(?:s)?\s+(?:is\s+|are\s+)?detected"
        r"|torch\s+was\s+compiled\s+without\s+cuda"
        r"|libcuda(?:\.so)?\s*(?:not\s+found|missing)"
        r"|tinycudann.*(?:not\s+found|failed|cannot)",
        re.IGNORECASE,
    )),

    # PyTorch index URL mismatch — VERY specific signal: "Could not find
    # X==Y" where the URL was a download.pytorch.org/whl/cuXYZ index.
    # Must come BEFORE generic VERSION_PIN_INVALID / PACKAGE_NOT_FOUND.
    (CATEGORY_TORCH_INDEX_MISMATCH, re.compile(
        r"(?:could\s+not\s+find\s+a\s+version|no\s+matching\s+distribution).*"
        r"(?:--?index-url\s+)?https?://download\.pytorch\.org/whl/cu\d+",
        re.IGNORECASE | re.DOTALL,
    )),

    # Wheel build failure — pip downloaded the source dist and failed
    # to compile the wheel. Very different fix family from "package
    # not found": package exists, just won't build with current
    # toolchain. Examples from wfr 42: "Failed to build pyliblzfse
    # fpsample", "failed-wheel-build-for-install".
    (CATEGORY_WHEEL_BUILD_FAILED, re.compile(
        r"failed\s+to\s+build\s+(?:wheel\s+for\s+)?[a-z0-9._-]+"
        r"|failed-wheel-build-for-install"
        r"|building\s+wheel\s+for\s+[a-z0-9._-]+\s*\([^)]*\)\s*\.\.\.\s*error"
        r"|error:\s*command\s+['\"]?[^'\"]*?(?:gcc|g\+\+|cc1|cc1plus)['\"]?\s+failed",
        re.IGNORECASE,
    )),

    # APT package not found — Ubuntu/Debian repo doesn't have the
    # package the LLM added to apt-get install. Different from PyPI
    # package-not-found.
    (CATEGORY_APT_PACKAGE_NOT_FOUND, re.compile(
        r"e:\s*unable\s+to\s+locate\s+package\s+\S+"
        r"|e:\s*package\s+'?[a-z0-9._\-+]+'?\s+has\s+no\s+installation\s+candidate",
        re.IGNORECASE,
    )),

    # USER directive / useradd conflicts. Common when the LLM adds
    # `useradd mod` but the base image already has it, or sets
    # `USER mod` before creating the user.
    (CATEGORY_USER_DIRECTIVE, re.compile(
        r"useradd:\s*user\s+'?\S+'?\s+already\s+exists"
        r"|adduser:\s*the\s+user\s+'?\S+'?\s+already\s+exists"
        r"|setting\s+up\s+user.*chown.*operation\s+not\s+permitted",
        re.IGNORECASE,
    )),

    # TLS / certificate verification. Different from generic NETWORK
    # — fix is "set CA bundle" / "disable verification", not "retry".
    (CATEGORY_TLS_CERT, re.compile(
        r"certificate\s+verify\s+failed"
        r"|ssl(?::\s*|certificate_verify_failed)"
        r"|x509:\s*certificate\s+(?:has\s+expired|is\s+(?:not\s+yet\s+)?valid)",
        re.IGNORECASE,
    )),

    # Generic package-not-found (unpinned). Comes AFTER version-pin so
    # the more-specific pattern wins.
    (CATEGORY_PACKAGE_NOT_FOUND, re.compile(
        r"(?:no\s+matching\s+distribution\s+(?:found\s+)?for|could\s+not\s+find\s+a\s+version\s+that\s+satisfies)"
        r"|enotfound\s*:\s*[a-z0-9._-]+"
        r"|404\s+(?:client\s+)?error\s+for\s+url",
        re.IGNORECASE,
    )),

    # Disk-space failures.
    (CATEGORY_DISK_SPACE, re.compile(
        r"no\s+space\s+left\s+on\s+device"
        r"|disk\s+(?:quota\s+exceeded|full)"
        r"|enospc",
        re.IGNORECASE,
    )),

    # Network / DNS / TLS.
    (CATEGORY_NETWORK, re.compile(
        r"(?:could\s+not\s+resolve\s+host|name\s+(?:or\s+service\s+)?not\s+known|getaddrinfo\s+failed|temporary\s+failure\s+in\s+name\s+resolution)"
        r"|connection\s+(?:timed\s+out|refused)"
        r"|tls\s+handshake\s+(?:timeout|failed)"
        r"|read\s+timed\s+out",
        re.IGNORECASE,
    )),

    # File-system / permission.
    (CATEGORY_PERMISSION, re.compile(
        r"permission\s+denied"
        r"|eacces"
        r"|read-only\s+file\s+system",
        re.IGNORECASE,
    )),

    # Python import error at runtime.
    (CATEGORY_IMPORT_ERROR, re.compile(
        r"modulenotfounderror:?\s+no\s+module\s+named"
        r"|importerror:?\s+cannot\s+import",
        re.IGNORECASE,
    )),
)


def classify(stderr: str) -> str:
    """Classify a build failure's stderr into one of the known
    categories. Returns ``CATEGORY_DEFAULT`` when no pattern matches.
    """
    if not stderr:
        return CATEGORY_DEFAULT
    # Only look at the LAST 4KB — head failures get drowned in
    # earlier build output.
    text = stderr[-4000:]
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(text):
            return category
    return CATEGORY_DEFAULT


# Per-category playbook hints. Each is a short paragraph the repair
# LLM prepends to its user prompt. The hint describes WHAT TO DO
# for this category of failure — distilled from observing many
# previous repair attempts on the same category.
_HINTS: dict[str, str] = {
    CATEGORY_COMPILER_MISSING: (
        "FAILURE CATEGORY — COMPILER MISSING. The build needs a C++ "
        "toolchain. Add this as an early RUN in the Dockerfile:\n"
        "  RUN apt-get update && apt-get install -y \\\n"
        "      build-essential g++ gcc cmake make pkg-config python3-dev \\\n"
        "      && rm -rf /var/lib/apt/lists/*\n"
        "Place this BEFORE any pip install that compiles C extensions "
        "(packages with native code like tinycudann, gsplat, mmcv, "
        "torch from source, etc.). If you already added these and "
        "the error persists, you may also need: libstdc++-dev, "
        "libffi-dev, or libgl1-mesa-glx."
    ),
    CATEGORY_VERSION_PIN_INVALID: (
        "FAILURE CATEGORY — INVALID VERSION PIN. The version pin you "
        "(or a previous attempt) used does not exist on the package "
        "index. Common cause: hallucinated 'X==1.1.8' when 1.1.x maxes "
        "at 1.1.5. Do NOT propose another specific pin without "
        "evidence it exists. Prefer leaving the version unpinned or "
        "using a range like 'X>=2.0,<3.0'. The package index will "
        "pick the latest valid release."
    ),
    CATEGORY_IMAGE_NOT_FOUND: (
        "FAILURE CATEGORY — DOCKER BASE IMAGE NOT FOUND. The FROM "
        "image you (or a previous attempt) referenced does not exist "
        "on docker.io / nvcr.io / our harbor. Common cause: the "
        "LLM hallucinated a tag that LOOKS plausible but isn't real "
        "(e.g. `nvidia/cuda:12.4.1-base`, `nvidia/cuda:12.4.0-base`, "
        "any custom `harbor/.../base-cuda:*-slim` variants).\n\n"
        "DO NOT guess another tag. Pick from this curated list of "
        "bases verified to resolve as of 2026-Q2:\n\n"
        "NVIDIA CUDA (public docker.io/nvidia/cuda):\n"
        "  - nvidia/cuda:12.4.1-runtime-ubuntu22.04         (smaller, no nvcc)\n"
        "  - nvidia/cuda:12.4.1-devel-ubuntu22.04           (HAS nvcc — required to compile tinycudann/gsplat/mmcv-full)\n"
        "  - nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04   (+ cuDNN)\n"
        "  - nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04     (+ cuDNN + nvcc)\n"
        "  - nvidia/cuda:12.1.1-runtime-ubuntu22.04         (older but stable)\n"
        "  - nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04  (for legacy torch 1.x users)\n\n"
        "NVIDIA NGC (nvcr.io — official NVIDIA tuned images):\n"
        "  - nvcr.io/nvidia/pytorch:24.03-py3   (PyTorch + CUDA + TensorRT baked in)\n"
        "  - nvcr.io/nvidia/pytorch:23.10-py3   (older stable)\n\n"
        "PyTorch official (Python + torch baked in):\n"
        "  - pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime\n"
        "  - pytorch/pytorch:2.4.0-cuda12.1-cudnn9-devel\n"
        "  - pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime\n"
        "  - pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime\n\n"
        "AMD ROCm (for AMD GPUs):\n"
        "  - rocm/dev-ubuntu-22.04:6.1                (devel — has hipcc)\n"
        "  - rocm/pytorch:rocm6.1_ubuntu22.04_py3.10_pytorch_2.3.0  (PyTorch + ROCm)\n\n"
        "Intel oneAPI (Intel GPUs + XPU):\n"
        "  - intel/oneapi-basekit:2024.2.0-devel-ubuntu22.04\n\n"
        "CPU bases (no accelerator):\n"
        "  - python:3.11-slim       (Python apps, smaller than full python:3.11)\n"
        "  - python:3.12-slim       (Python 3.12-only syntax)\n"
        "  - node:20-slim           (Node.js apps)\n"
        "  - node:22-slim           (newer Node)\n"
        "  - ubuntu:22.04           (clean Ubuntu base)\n"
        "  - debian:bookworm-slim   (smallest stable Debian)\n"
        "  - alpine:3.19            (smallest of all)\n\n"
        "Rules:\n"
        "  - Compiling CUDA extensions (tinycudann/gsplat/diff_gaussian_rasterization)? You MUST use a `-devel` tag with nvcc, NOT `-runtime`.\n"
        "  - Compiling ROCm extensions? Use `rocm/dev-ubuntu-*`, NOT a runtime-only image.\n"
        "  - Compiling native code (Rust/Go/C++ binaries)? Use `:devel` / `-bullseye` / `ubuntu:*` — NOT `-slim` variants which lack build tools.\n\n"
        "Pick one of the above. Do not invent new tags."
    ),
    CATEGORY_PACKAGE_NOT_FOUND: (
        "FAILURE CATEGORY — PACKAGE NOT FOUND. The package doesn't "
        "exist on PyPI, OR the wrong name is being used (e.g. "
        "'pytorch' instead of 'torch'; 'cv2' is installed via "
        "'opencv-python'). Double-check the canonical PyPI name. "
        "Some packages are install-from-source only — for those, "
        "use `pip install git+https://github.com/owner/repo.git` "
        "instead of `pip install name`."
    ),
    CATEGORY_CUDA_TOOLCHAIN: (
        "FAILURE CATEGORY — CUDA TOOLCHAIN. CUDA runtime is missing "
        "OR the CUDA versions mismatch. Required fixes (in order of "
        "likelihood):\n"
        "  1. Base image MUST be a CUDA base. Use "
        "`nvidia/cuda:12.4-runtime-ubuntu22.04` or "
        "`nvidia/cuda:12.4-devel-ubuntu22.04` (devel needed if "
        "building CUDA extensions like tinycudann).\n"
        "  2. Torch wheel index URL must match the CUDA version: "
        "`pip install torch --index-url "
        "https://download.pytorch.org/whl/cu124` for CUDA 12.4.\n"
        "  3. tinycudann / gsplat-class packages need the devel "
        "image (has nvcc) — runtime image won't compile them."
    ),
    CATEGORY_DISK_SPACE: (
        "FAILURE CATEGORY — DISK SPACE. The build daemon ran out of "
        "disk. Mitigation strategies (combine multiple):\n"
        "  - Combine `RUN pip install` calls into ONE RUN with `&&` "
        "between commands — reduces layer count + intermediate size.\n"
        "  - Add `--no-cache-dir` to every pip install.\n"
        "  - Use a slim base image (`python:3.11-slim` vs `python:3.11`).\n"
        "  - Multi-stage build: install in a builder stage, copy only "
        "the site-packages to the final image."
    ),
    CATEGORY_NETWORK: (
        "FAILURE CATEGORY — NETWORK TRANSIENT. The build couldn't "
        "reach an external resource (PyPI, github, apt mirror, etc.). "
        "This is OFTEN transient — the next attempt may succeed with "
        "no Dockerfile changes. If you change anything, prefer: "
        "pinning a specific mirror URL (e.g. "
        "`pip install --index-url https://pypi.org/simple` to bypass "
        "a misconfigured mirror)."
    ),
    CATEGORY_PERMISSION: (
        "FAILURE CATEGORY — PERMISSION DENIED. A file or directory "
        "operation was rejected. Common cases:\n"
        "  - Writing to a path the runtime user doesn't own. Use "
        "`USER root` for the install steps then `USER <runtime>` at "
        "the end.\n"
        "  - Trying to chmod/chown across a docker-volume mount — "
        "those are root-owned. Use the host bind-mount path instead.\n"
        "  - Read-only filesystem: don't write inside layers that "
        "later get squashed; put runtime state under `/tmp` or a "
        "designated VOLUME."
    ),
    CATEGORY_WHEEL_BUILD_FAILED: (
        "FAILURE CATEGORY — WHEEL BUILD FAILED. The package exists on "
        "PyPI but pip couldn't compile the wheel with the current "
        "toolchain. The fix is to ADD missing build deps, NOT to "
        "disable wheel building.\n\n"
        "DO NOT add `--only-binary=:all:`, `--prefer-binary`, or any "
        "wheel-restriction flag — for packages like nerfstudio, "
        "tinycudann, gsplat, mmcv-full there is NO pre-built wheel on "
        "PyPI. Forcing wheel-only mode guarantees install failure. "
        "Source-build from a properly-equipped base is the correct "
        "path.\n\n"
        "Real fixes (in order of likelihood):\n"
        "  1. Missing C++ toolchain → add build-essential, g++, gcc, "
        "cmake, ninja-build, python3-dev to apt-get install BEFORE "
        "the pip install.\n"
        "  2. Missing system library headers → read the wheel build "
        "error closely; it names a missing header. Install the matching "
        "`-dev` package (libgl1-mesa-dev, libxcb1-dev, libffi-dev, "
        "libfreetype6-dev, etc.).\n"
        "  3. CUDA extension needs nvcc → switch the base image from "
        "`nvidia/cuda:*-runtime-*` to `nvidia/cuda:*-devel-*` (devel "
        "image has nvcc; runtime doesn't).\n"
        "  4. Wheel needs a specific Python version → check the base "
        "image's Python (e.g. nerfstudio 1.1.5 needs Python 3.10+).\n"
        "  5. Build needs more RAM than available → consider a slimmer "
        "set of deps or a larger build machine."
    ),
    CATEGORY_APT_PACKAGE_NOT_FOUND: (
        "FAILURE CATEGORY — APT PACKAGE NOT FOUND. The Ubuntu/Debian "
        "repo doesn't have the package the previous attempt added to "
        "apt-get install. Common causes:\n"
        "  1. Package name differs across distros — e.g. `liblzfse-dev` "
        "doesn't exist in Ubuntu 22.04 (use `liblzfse0` and skip dev "
        "headers, or build from source).\n"
        "  2. Package only available in a specific archive area — try "
        "adding `apt-get update` BEFORE install, and confirm "
        "main/universe/multiverse are enabled.\n"
        "  3. Package was renamed — search apt-cache for the canonical "
        "name: `apt-cache search <fragment>`.\n"
        "  4. The package is not actually needed — many `*-dev` packages "
        "the LLM added speculatively aren't required at runtime. "
        "Remove it and try again; the wheel-build error you previously "
        "saw might have a different root cause.\n"
        "Do NOT add the same package name in another attempt expecting "
        "different results."
    ),
    CATEGORY_TORCH_INDEX_MISMATCH: (
        "FAILURE CATEGORY — TORCH INDEX URL MISMATCH. The pinned "
        "torch version doesn't exist on the CUDA-specific wheel "
        "index URL. Common causes:\n"
        "  1. Pinned to a version that exists on PyPI default but NOT "
        "on the cu124 / cu121 / cu118 channel. The PyTorch download "
        "indexes only carry a subset of versions per CUDA suffix.\n"
        "  2. Version + CUDA tag combo just isn't published yet "
        "(e.g. torch 2.3.1 + cu124 — those versions came out before "
        "the index split).\n\n"
        "Fix: drop the version pin OR adjust the CUDA index URL to "
        "match. Recommended mapping:\n"
        "  - For torch 2.4.x: use --index-url https://download.pytorch.org/whl/cu124\n"
        "  - For torch 2.3.x: use --index-url https://download.pytorch.org/whl/cu121\n"
        "  - For torch 2.1.x: use --index-url https://download.pytorch.org/whl/cu118\n"
        "Or omit the pin entirely and let pip resolve the latest "
        "wheel on the chosen index."
    ),
    CATEGORY_USER_DIRECTIVE: (
        "FAILURE CATEGORY — USER DIRECTIVE / USERADD CONFLICT. The "
        "build is failing on the non-root USER setup. Common patterns:\n"
        "  1. `useradd mod` fails because the user already exists on "
        "the base image. Use the idempotent form:\n"
        "     `RUN id -u mod >/dev/null 2>&1 || useradd -m -u 10001 mod`\n"
        "  2. `USER mod` directive before the user is created. Move "
        "USER to AFTER the useradd block.\n"
        "  3. chown failing because the path lives on a volume that "
        "was already mounted. Move chown into the RUN that creates "
        "the path, not into a separate RUN after WORKDIR.\n"
        "  4. MOD harbor base images already include a `mod` user "
        "with uid 10001. Don't re-create — just `USER mod` directly."
    ),
    CATEGORY_TLS_CERT: (
        "FAILURE CATEGORY — TLS / CERTIFICATE VERIFICATION FAILED. "
        "The build is hitting an upstream HTTPS endpoint with a TLS "
        "issue. Common causes:\n"
        "  1. ca-certificates package not installed in the base. Add "
        "`apt-get install -y ca-certificates && update-ca-certificates` "
        "before any pip install / curl.\n"
        "  2. System clock is wrong inside the container, causing "
        "valid certs to look expired. Rare in build environments.\n"
        "  3. Self-signed harbor / mirror certs that aren't trusted. "
        "Add the cert via `COPY harbor-ca.crt /usr/local/share/ca-"
        "certificates/ && update-ca-certificates`.\n"
        "Do NOT disable TLS verification (--no-check-certificate / "
        "--trusted-host) to make the error go away — that's a security "
        "regression."
    ),
    CATEGORY_IMPORT_ERROR: (
        "FAILURE CATEGORY — IMPORT ERROR. A Python import failed at "
        "runtime. Check the entrypoint script vs. the Dockerfile's "
        "pip-install lines — the package name might differ from the "
        "import name (e.g. `pip install opencv-python` but "
        "`import cv2`; `pip install pillow` but `import PIL`; "
        "`pip install scikit-learn` but `import sklearn`). Or a "
        "transitive dep is missing — re-check requirements."
    ),
    CATEGORY_DEFAULT: "",  # no specialized hint — use the generic system prompt only
}


def hint_for_category(category: str) -> str:
    """Return the specialized prompt hint for ``category``. Empty
    string when no specialized hint applies (caller falls back to the
    generic system prompt alone).
    """
    return _HINTS.get(category, "")

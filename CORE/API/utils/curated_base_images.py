"""Wizard-facing catalog for the curated MOD base-image registry.

There are already three copies of "what base images are curated":
  1. `CORE/DockerContainers/BaseImages/` — the actual Dockerfiles.
  2. `CORE/API/utils/base_image_rebuild.CURATED_VARIANTS` — the
     nightly rebuild list (variant_dir → tag).
  3. `CORE/API/config/base_image_allowlist.json` — the admission
     allowlist (prefixes).

This module is a fourth view of the same set, optimised for the
New Node wizard's dropdown: each entry carries a display name, a
default interpreter, a category, and a short description so the
wizard UI can render it without a second network round-trip.

The underlying identity is the variant directory name (e.g.
`python-3.11`) which must match a directory under
`CORE/DockerContainers/BaseImages/`. If that directory goes away,
the entry should too — `base_image_rebuild.CURATED_VARIANTS`
imports from this module to keep them in lockstep.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional
from utils.registries import platform_image


@dataclass(frozen=True)
class CuratedBaseImage:
    """One row in the wizard-facing base-image catalog."""
    variant_dir: str                  # Directory under BaseImages/
    image_tag: str                    # Tag portion, e.g. "base-python:3.11"
    display_name: str                 # Human-readable label
    category: str                     # "python" | "node" | "gpu" | "system"
    default_interpreter: Optional[str]  # For the Script tab auto-select
    description: str                  # One-line hover description
    process_type_hint: str = "CPU"    # CPU / CUDA / ROCM / INTEL / NPU

    @property
    def image_ref(self) -> str:
        """Full pullable reference assuming the default registry/project.
        Callers that need a different registry should prepend their own
        prefix and keep the tag suffix."""
        return platform_image(self.image_tag)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["image_ref"] = self.image_ref
        return d


# Ordered for the wizard's dropdown. Python first (most common), then
# Node, then system, then GPU. Keep order meaningful — it's what the
# user sees.
CURATED_BASE_IMAGES: list[CuratedBaseImage] = [
    CuratedBaseImage(
        variant_dir="python-3.11",
        image_tag="base-python:3.11",
        display_name="Python 3.11",
        category="python",
        default_interpreter="python3",
        description="Python 3.11 (slim) — default for most Python workloads.",
    ),
    CuratedBaseImage(
        variant_dir="python-3.12",
        image_tag="base-python:3.12",
        display_name="Python 3.12",
        category="python",
        default_interpreter="python3",
        description="Python 3.12 (slim) — newer stdlib + perf improvements.",
    ),
    CuratedBaseImage(
        variant_dir="distroless-python-3.11",
        image_tag="base-distroless-python:3.11",
        display_name="Python 3.11 (distroless)",
        category="python",
        default_interpreter="python3",
        description="Minimal Python 3.11 image — no shell, no apt. "
                    "Smaller attack surface; dependencies must be prebuilt.",
    ),
    CuratedBaseImage(
        variant_dir="node-20",
        image_tag="base-node:20",
        display_name="Node.js 20",
        category="node",
        default_interpreter="node",
        description="Node 20 LTS — long-term support baseline.",
    ),
    CuratedBaseImage(
        variant_dir="node-22",
        image_tag="base-node:22",
        display_name="Node.js 22",
        category="node",
        default_interpreter="node",
        description="Node 22 — current stable. Pick this for new work.",
    ),
    CuratedBaseImage(
        variant_dir="distroless-node-20",
        image_tag="base-distroless-node:20",
        display_name="Node.js 20 (distroless)",
        category="node",
        default_interpreter="node",
        description="Minimal Node 20 image — no shell, no apt. "
                    "Bundle everything your app needs into node_modules.",
    ),
    CuratedBaseImage(
        variant_dir="go-1.22",
        image_tag="base-go:1.22",
        display_name="Go 1.22",
        category="system",
        default_interpreter="go",
        description="Go 1.22 toolchain — for compiled Go binaries.",
    ),
    CuratedBaseImage(
        variant_dir="rust-1.78",
        image_tag="base-rust:1.78",
        display_name="Rust 1.78",
        category="system",
        default_interpreter="rust",
        description="Rust 1.78 toolchain — for compiled Rust binaries.",
    ),
    CuratedBaseImage(
        variant_dir="java-21",
        image_tag="base-java:21",
        display_name="Java 21 (JDK)",
        category="system",
        default_interpreter="java",
        description="OpenJDK 21 — current LTS.",
    ),
    CuratedBaseImage(
        variant_dir="alpine-3.19",
        image_tag="base-alpine:3.19",
        display_name="Alpine 3.19",
        category="system",
        default_interpreter="sh",
        description="Minimal Alpine 3.19 — for shell scripts and small "
                    "statically-linked tools.",
    ),
    CuratedBaseImage(
        variant_dir="cuda-12.4-python3.11",
        image_tag="base-cuda:12.4-python3.11",
        display_name="CUDA 12.4 + Python 3.11",
        category="gpu",
        default_interpreter="python3",
        process_type_hint="CUDA",
        description="NVIDIA CUDA 12.4 runtime with Python 3.11. "
                    "Use for PyTorch / TensorFlow / JAX GPU workloads.",
    ),
    CuratedBaseImage(
        variant_dir="rocm-6.1-python3.11",
        image_tag="base-rocm:6.1-python3.11",
        display_name="ROCm 6.1 + Python 3.11",
        category="gpu",
        default_interpreter="python3",
        process_type_hint="ROCM",
        description="AMD ROCm 6.1 runtime with Python 3.11. "
                    "Use for PyTorch-ROCm / TensorFlow-ROCm workloads.",
    ),
    CuratedBaseImage(
        variant_dir="intel-xpu-2025.0",
        image_tag="base-intel-xpu:2025.0",
        display_name="Intel XPU 2025.0",
        category="gpu",
        default_interpreter="python3",
        process_type_hint="INTEL",
        description="Intel oneAPI / Level Zero runtime for Intel GPUs.",
    ),
    CuratedBaseImage(
        variant_dir="npu-openvino-2025",
        image_tag="base-npu:openvino-2025",
        display_name="OpenVINO NPU 2025",
        category="gpu",
        default_interpreter="python3",
        process_type_hint="NPU",
        description="OpenVINO runtime targeting Intel NPU accelerators.",
    ),
]


def by_variant_dir(variant_dir: str) -> Optional[CuratedBaseImage]:
    """Lookup by directory name (e.g. 'python-3.11'). None when unknown."""
    for b in CURATED_BASE_IMAGES:
        if b.variant_dir == variant_dir:
            return b
    return None


def by_image_tag(image_tag: str) -> Optional[CuratedBaseImage]:
    """Lookup by tag (e.g. 'base-python:3.11')."""
    for b in CURATED_BASE_IMAGES:
        if b.image_tag == image_tag:
            return b
    return None

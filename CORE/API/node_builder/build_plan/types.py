"""Canonical BuildPlan data shapes (wizard-rebuild §3).

A BuildPlan is the unified artifact every wizard mode produces.
The renderer (renderer.py) consumes a plan and emits the Dockerfile;
the repair flow (§7) operates on plans, not Dockerfiles; the
View Build Plan UI (§10) reads plans.

Why "one canonical artifact" matters:

  Before this change, each mode authored Dockerfiles differently —
  Image-mode synthesized via io_adapter, Git-mode dispatched the
  Containerizer LLM, etc. Repair/lint/test logic had to BRANCH on
  which mode produced the Dockerfile. The result was 5 nearly-but-
  not-quite identical flows in 5 places.

  After: every mode's extractor produces a BuildPlan; renderer
  dispatches on `plan.mode` internally. ONE repair flow, ONE lint
  flow, ONE View Plan UI. Mode-specific code lives only in the
  per-mode extractor (`extractors/<mode>.py`).

Schema is fixed at v1. Adding fields requires bumping
`extractor_version` so cached plans get re-extracted with the new
shape (handled by the §3c cache layer).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# Bumped whenever the BuildPlan schema changes in a way that breaks
# cached plans. The §3c cache invalidates entries whose
# `extractor_version` doesn't match this constant.
EXTRACTOR_VERSION = 1


# ---------------------------------------------------------------------------
# Modes — fixed vocabulary
# ---------------------------------------------------------------------------

ModeStr = Literal[
    "image",          # User picked a docker ref; we wrap a thin adapter
    "git",            # User picked a repo URL; docs-driven extraction
    "dockerfile",     # User pasted a Dockerfile; we patch I/O entrypoint
    "script",         # User pasted a script; curated base + glue
    "describe-codegen",  # Describe-cascade Step D fallback; LLM authors
]

# Per Tim 2026-05-21 plan-review pass A.
ALL_MODES: tuple[ModeStr, ...] = (
    "image", "git", "dockerfile", "script", "describe-codegen",
)


# ---------------------------------------------------------------------------
# Supporting shapes
# ---------------------------------------------------------------------------

class IOPort(BaseModel):
    """One input or output port on the resulting Node.

    Multi-port support lets a Node take, e.g., (text, image) → image
    for img2img-with-prompt or (image, image, text) → image for
    ControlNet. The default singleton port shape preserves backward
    compatibility with the existing `input_type` / `output_type`
    fields on the wizard request.
    """
    name: str = "input"
    type: str = "image"
    required: bool = True
    default: Optional[Any] = None
    description: Optional[str] = None


class EnvVar(BaseModel):
    """One environment variable the resulting container expects.

    `doc_line` is the provenance link — "README.md:74" — so the
    View Build Plan UI can deep-link the user to where the env var
    was documented. Set by the git/describe extractors when the
    chunk-tagger surfaces an `env`-labeled chunk.
    """
    name: str
    required: bool = True
    default: Optional[str] = None
    description: Optional[str] = None
    doc_line: Optional[str] = None


class ChunkRef(BaseModel):
    """A reference to a section of a repo doc that influenced the plan.

    Used by the Git-mode extractor to record provenance: which lines
    of README/INSTALL/docs/ drove which BuildPlan fields. The View
    Build Plan UI renders these as deep-link badges next to each field.
    """
    source_file: str            # "README.md", "docs/install.md", etc.
    start_line: int
    end_line: int
    intents: list[str] = Field(default_factory=list)
    # Optional heading for display ("## Installation", "## Requirements").
    heading: Optional[str] = None


class Delta(BaseModel):
    """One field change proposed by the repair LLM (§7).

    Stored on BuildPlan.applied_deltas so the next repair iteration
    can see the full history of fixes attempted, and the View Plan
    UI can render an "evolution" tab.
    """
    field: str           # name of the BuildPlan field changed
    value: Any           # new value (post-validation)
    reason: str          # LLM's one-line explanation
    applied_at: Optional[str] = None  # ISO8601


# ---------------------------------------------------------------------------
# The plan itself
# ---------------------------------------------------------------------------

class BuildPlan(BaseModel):
    """Unified BuildPlan — canonical artifact for every wizard mode.

    Required fields (every plan has them, even at low confidence):
      mode, language, package_manager, workdir, entrypoint

    Optional fields are populated when extractable. Whatever's NOT
    populated falls back to template defaults at render time, and
    the field name is added to `weak_fields` for repair attention.

    Image-mode plans use `from_image_ref` + `from_digest` for the
    user's chosen base; the renderer emits `FROM <ref>@sha256:...`.
    Dockerfile-mode plans carry the user's body in
    `passthrough_dockerfile`; the renderer wraps with I/O glue.
    """

    # ---- required ----
    mode: ModeStr

    # Language runtime. "container" is the special value used by
    # Image mode when we can't introspect the base image's language
    # (we just FROM it and assume the image knows what to do).
    language: str

    # Package manager hint. "none" is valid when there's nothing
    # to install (pre-built images with everything baked in).
    package_manager: str = "pip"

    workdir: str = "/app"
    entrypoint: list[str] = Field(
        default_factory=lambda: ["/entrypoint.sh"],
    )

    # ---- optional (filled when known) ----
    language_version: Optional[str] = None
    cuda_version: Optional[str] = None
    gpu_required: bool = False
    min_vram_gb: Optional[int] = None

    system_deps: list[str] = Field(default_factory=list)
    install_steps: list[str] = Field(default_factory=list)
    env_vars: list[EnvVar] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)

    # ---- I/O port schema ----
    # Backwards-compat defaults: one "input" port + one "output"
    # port using the wizard's declared input_type/output_type. The
    # per-mode extractors populate these from request fields.
    input_ports: list[IOPort] = Field(default_factory=list)
    output_ports: list[IOPort] = Field(default_factory=list)

    # ---- mode-specific carriers ----
    # Image mode: the user's docker ref + resolved sha256 for
    # reproducibility.
    from_image_ref: Optional[str] = None
    from_digest: Optional[str] = None    # "sha256:abc..."

    # Image mode: the runtime user the base image declares (via
    # `Config.User` in its image config). Empty string / None means
    # the base runs as root by default and the rendered adapter
    # doesn't need a trailing USER directive. A non-root value
    # (e.g. "vault", "nginx", "10001") makes the renderer emit
    # `USER root` upfront (so our mkdir/chmod on /data succeeds)
    # and `USER <runtime_user>` at the end (so the container still
    # runs as the base image intended).
    runtime_user: Optional[str] = None

    # Dockerfile mode: carry the user's verbatim Dockerfile body
    # through the canonical pipeline. The renderer wraps it with
    # the I/O entrypoint patch + non-root USER injection.
    passthrough_dockerfile: Optional[str] = None

    # Script mode: the user's pasted body (python/bash/node/etc.)
    # and the on-disk filename the renderer should embed it as.
    # Renderer base64-decodes the body into `<workdir>/<script_filename>`
    # at image-build time so the body survives `docker build`
    # context isolation deterministically.
    script_body: Optional[str] = None
    script_filename: Optional[str] = None

    # ---- repair audit + confidence ----
    confidence: float = 0.0
    extracted_from: list[ChunkRef] = Field(default_factory=list)
    weak_fields: list[str] = Field(default_factory=list)
    applied_deltas: list[Delta] = Field(default_factory=list)

    # Schema version — bumped when the shape changes. Cache layer
    # in §3c invalidates plans whose version doesn't match.
    extractor_version: int = EXTRACTOR_VERSION

    # ---- helpers ----

    def tier(self) -> Literal["high", "medium", "low"]:
        """Confidence tier — drives the §7 repair budget. The
        thresholds match the design.md table:
          high   ≥ 0.75 → outer_repair_max = 3
          medium ≥ 0.4  → outer_repair_max = 8
          low    < 0.4  → outer_repair_max = 15
        """
        if self.confidence >= 0.75:
            return "high"
        if self.confidence >= 0.4:
            return "medium"
        return "low"

    def repair_budget(self) -> int:
        """Outer-repair attempt budget for this plan's confidence
        tier. Mirrors §7c."""
        return {"high": 3, "medium": 8, "low": 15}[self.tier()]


# ---------------------------------------------------------------------------
# Defaults helpers — used by extractors when a field isn't extractable
# ---------------------------------------------------------------------------

def default_ports_for_types(
    input_type: str, output_type: str,
) -> tuple[list[IOPort], list[IOPort]]:
    """Backwards-compat helper: from the legacy `input_type` +
    `output_type` strings, produce the singleton port lists.

    Multi-port plans bypass this and set `input_ports`/`output_ports`
    directly. Backwards-compat plans (everything pre-§3) end up here.
    """
    return (
        [IOPort(name="input", type=input_type or "image", required=True)],
        [IOPort(name="output", type=output_type or "image", required=True)],
    )

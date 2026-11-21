"""δ DAG-wizard B1 — typed BuildSpec contract."""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field


class BuildArtifact(BaseModel):
    filename: str
    content: str


class ContextArtifact(BaseModel):
    """A build CONTEXT delivered as a binary-safe tarball in MinIO (as opposed
    to inline text `source_files`). Content-addressed by sha256; tenant/platform
    scoped by bucket. Streamed to the build-task via the scoped /build-specs/
    {id}/context endpoint, then extracted into the docker build context.

    This is the "bring your own Dockerfile + context" primitive — it lets ANY
    image (a real app with binaries, not just wizard-generated text nodes) build
    through the same gated pipeline."""
    bucket: str
    object_key: str
    sha256: str
    size_bytes: int = 0
    filename: str = "context.tar.gz"


class BuildVariantSpec(BaseModel):
    variant: str
    dockerfile: str
    source_overrides: Optional[List[BuildArtifact]] = None
    adaptations: Optional[Dict[str, Any]] = None


class BuildSampleInput(BaseModel):
    filename: str
    body_b64: str


class BuildSampleOutput(BaseModel):
    """project-agent §1.1 — the output the requester EXPECTS for
    ``sample_input``, i.e. the golden reference the hero variant is
    graded against.

    Same shape as BuildSampleInput and deliberately separate: the
    dispatcher already reads ``spec["sample_output"]["body_b64"]``
    (`node_builder/variant_test/dispatcher.py`) and grades the hero
    against it, but nothing ever populated the field. Without it the
    hero scores 1.0 for any container that exits 0 with non-empty
    output, so MIN_PUBLISH_CONFIDENCE is trivially satisfied and
    every non-hero variant is then faithfully scored against a hero
    whose only proven property is that it did not crash.
    """
    filename: str
    body_b64: str


class BuildSpec(BaseModel):
    """Single source of truth for a wizard build.

    Both the generate-phase inputs (what the user typed/pasted) AND
    the generate-phase outputs (LLM-generated Dockerfile/entrypoint
    /etc.) live here. The generate node populates the output fields
    on task completion; downstream nodes (test-and-repair, build,
    push, register) read them. One row, one workflow_run, one full
    audit trail."""
    image_name: str
    node_name: str
    tenant_id: int

    # Output fields — populated by the generate-phase task. EMPTY at
    # the time a wizard build is submitted; filled in by the
    # orchestrator's downstream-dispatch hook when the generate node
    # completes.
    hero_dockerfile: str = ""
    entrypoint_sh: str = ""
    build_and_push_sh: str = ""

    variants: List[BuildVariantSpec] = Field(default_factory=list)
    source_files: List[BuildArtifact] = Field(default_factory=list)

    # Operator per-variant source OVERRIDES (wizard Preview "Rebuild with my
    # changes"). Keyed {variant: [{filename, content}]}. Distinct from the
    # Containerizer's generated `source_variants` on purpose: this channel is
    # ONLY ever written by the /wizard/rebuild-variant endpoint when a human
    # edited a variant's source in the Preview step, and is applied by
    # build_one_variant OVER the staged source_files for that one variant.
    # Empty for every normal build, so the default build path is unchanged.
    source_overrides_by_variant: Dict[str, List[BuildArtifact]] = Field(default_factory=dict)

    # "Raw context" build (bring-your-own Dockerfile + context). When
    # raw_context is True the build-task extracts `context_artifact` (a MinIO
    # tarball) as the docker build context and builds `hero_dockerfile` against
    # it — SKIPPING the wizard-specific staging (source_files, smoke-guard
    # entrypoint) and the wizard-only DAG stages (generate/ingest/smoke/
    # sample-test/repair). The DAG is just build → push → register.
    raw_context: bool = False
    context_artifact: Optional[ContextArtifact] = None

    # wizard-rebuild §4 chunk 4 — universal variant fan-out.
    # hero_variant is the accelerator key the hero Dockerfile
    # targets ("cpu" / "cuda" / "rocm" / etc.). Used by register_node
    # to map the hero harbor tag onto the right backend slot in
    # NodeDefinition.gpu_variants. Defaults to "cpu" historically;
    # the dispatcher sets it explicitly when fan-out runs.
    hero_variant: Optional[str] = None
    # Variants the C-extension heuristic marked architecturally-
    # incompatible. Each entry: {"variant", "accelerator", "reason"}.
    # Skipped by the generator (no build attempt, no worker time) but
    # surfaced to the Node card so the user knows why ROCm/Intel
    # weren't built for a CUDA-only base image.
    incompatible_variants: List[Dict[str, str]] = Field(default_factory=list)

    sample_input: Optional[BuildSampleInput] = None
    # project-agent §1.1 — golden reference for the hero sample-test.
    # Optional: a build with no expected output still runs, but is
    # recorded as published unanchored (see §1.2) rather than passing.
    sample_output: Optional[BuildSampleOutput] = None
    defer_registration: bool = False

    input_type: str = "image"
    output_type: str = "image"
    process_type: str = "CPU"
    cost_in_credits: int = 1
    # sz-tcp.5b — explicit per-tier pricing (sz-tcp.5b). The WorkflowBuilder
    # node wizard now POSTs /build-and-register-node with `cost_tiers`
    # {small, medium, large} alongside `cost_in_credits` (which stays the
    # medium tier). Optional: absent → the tier columns keep their legacy
    # derived behaviour (utils/node_cost.scaled_tiers + the models.py
    # before_insert listener). Persisted here so it survives into
    # build_specs.spec_json and is readable by the register stage.
    cost_tiers: Optional[Dict[str, Union[int, float]]] = None
    # Pricing-source enum mirroring models.NodeDefinition.cost_basis.
    # 'manual' when the caller sent explicit cost_tiers; None → leave the
    # column at its server default ('estimated').
    cost_basis: Optional[str] = None
    container_timeout: int = 7200
    description: Optional[str] = None
    # WorkflowBuilder palette category. Empty / "Custom" → falls back
    # to "Custom" in the register_node stage (legacy). Filled in by
    # the wizard's smart-suggest path or explicit user override.
    node_type: Optional[str] = None
    container_env: Optional[Dict[str, str]] = None
    allowed_egress: Optional[List[Dict[str, Any]]] = None
    use_sandbox_runtime: bool = False
    skipped_variants: Optional[List[str]] = None
    node_metadata: Optional[Dict[str, Any]] = None

    # node-code-edit-rebuild (node-version-pinning GA.NMF.F8) — when this spec
    # was seeded from a node rebuild (services.node_build_dispatch.
    # seed_spec_from_revision), the rebuilt node's id + the rebuild scope. The
    # single-variant rebuild DAG omits the register/manifest stages that would
    # otherwise set NodeDefinition.image_digest + append a node_versions row,
    # so the push-result handler reads these to record the pushed digest for
    # THIS node. None for every non-rebuild build (wizard, build-farm, …).
    rebuild_node_id: Optional[int] = None
    rebuild_scope: Optional[str] = None

    # Generate-phase INPUTS — what the wizard captured from the user.
    # The generate node reads these from BuildSpec at dispatch time
    # and translates them into Containerizer env vars (REPO_URL,
    # INLINE_SOURCE_BODY, WRAP_BASE_IMAGE, etc.). Empty for builds
    # that bypass the generate phase (legacy /build-and-register-local
    # path that submits pre-generated artifacts).
    source_repo_url: Optional[str] = None      # https://github.com/...
    inline_source_body: Optional[str] = None   # raw script paste
    inline_source_filename: Optional[str] = None
    wrap_base_image: Optional[str] = None      # e.g. python:3.11-slim
    additional_instructions: Optional[str] = None

    # Phase B (#1) — system binaries the describe-synthesize LLM
    # declared its script depends on. The Containerizer's
    # post-emit pass injects these into the hero Dockerfile so the
    # script's `subprocess.run(['ffmpeg', ...])` etc. actually
    # resolve at runtime instead of hitting FileNotFoundError.
    # See describe_synthesize.py's _validate_binaries_declared for
    # the synth-side gate that produces these.
    system_packages: List[str] = Field(default_factory=list)
    dockerfile_extras: str = ""

    # wizard-rebuild §3 — Unified BuildPlan. The canonical artifact
    # for every mode (image / git / dockerfile / script / describe-
    # codegen). Set at extract time; read by §7 plan-aware repair
    # and by the §10 View Build Plan UI. Persisted as a JSON dict
    # rather than the BuildPlan dataclass to avoid a circular import
    # between build_dag and build_plan modules (BuildSpec is a wider
    # contract used by the orchestrator; BuildPlan is a narrower one
    # used only by the wizard pipeline).
    build_plan: Optional[Dict[str, Any]] = None

    # wizard-rebuild §4 chunk 2 — denormalized plan metadata for
    # fast lookups without parsing build_plan. confidence is the
    # raw 0.0-1.0 score; tier is "high" / "medium" / "low" matching
    # plan.tier(). Both drive the §7 plan-aware repair budget table
    # (high → 3, medium → 8, low → 15 outer-repair attempts).
    # repo_docs_extract_id is the §3c cache row that informed a
    # Git-mode extraction — None for any other mode.
    build_plan_confidence: Optional[float] = None
    build_plan_tier: Optional[str] = None
    repo_docs_extract_id: Optional[int] = None

    # Auto-repair loop bookkeeping. The test-and-repair build-task stage
    # mutates these via the API as it iterates:
    #   - inner attempts edit scripts only (cheap rebuilds — docker layer
    #     cache catches everything before the COPY scripts step)
    #   - outer attempts rewrite the Dockerfile (full rebuild)
    # The default budgets cover almost any fixable failure for an on-prem
    # LLM where each call is essentially free; the operator-facing UI
    # streams progress so the user sees what's happening.
    repair_attempts_inner_used: int = 0
    repair_attempts_outer_used: int = 0
    repair_attempts_inner_max: int = 30
    repair_attempts_outer_max: int = 10
    # Audit trail. Each entry: {attempt: int, mode: "inner"|"outer",
    # failure_reason: str, llm_explanation: str, at: iso8601}. Capped at
    # the budget so it can't grow unbounded.

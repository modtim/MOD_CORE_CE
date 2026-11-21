"""δ DAG-wizard B1 — adapter from the wizard's existing
BuildLocalRequest shape to the typed BuildSpec.

Lets the new DAG endpoint accept the same request body the wizard
already sends today (so we can ship the metadata path behind a feature
flag without changing the wizard's request schema), but operate
on the structured BuildSpec internally.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .types import (
    BuildArtifact,
    BuildSampleInput,
    BuildSampleOutput,
    BuildSpec,
    BuildVariantSpec,
)

logger = logging.getLogger(__name__)


def apply_build_plan(
    spec: BuildSpec,
    plan: Any,   # node_builder.build_plan.types.BuildPlan
    *,
    repo_docs_extract_id: Optional[int] = None,
) -> None:
    """Set all four plan-related fields on `spec` atomically
    (wizard-rebuild §4 chunk 2).

    - spec.build_plan        ← plan.model_dump()
    - spec.build_plan_confidence ← plan.confidence
    - spec.build_plan_tier   ← plan.tier()
    - spec.repo_docs_extract_id ← repo_docs_extract_id (Git mode only)

    Decoupled from the per-mode extractor so every dispatcher
    code path (image / git / dockerfile / script / describe-
    codegen) sets the same fields with the same semantics.
    Caller is expected to call this exactly once after extraction;
    re-extraction during plan-aware repair (§7) calls it again
    with the updated plan.

    `plan` is typed as Any to keep this module free of a
    build_plan import (the wider BuildSpec contract must not pull
    in the narrower BuildPlan dependency).
    """
    spec.build_plan = plan.model_dump()
    spec.build_plan_confidence = float(plan.confidence)
    spec.build_plan_tier = plan.tier()
    if repo_docs_extract_id is not None:
        spec.repo_docs_extract_id = int(repo_docs_extract_id)


def _coerce_dockerfile_variants(
    dockerfile_variants: Optional[Dict[str, str]],
    source_variants: Optional[Dict[str, List[Dict[str, Any]]]],
    variant_adaptations: Optional[Dict[str, Any]],
    skipped_variants: Optional[List[str]],
) -> List[BuildVariantSpec]:
    if not dockerfile_variants:
        return []
    skipped = set(skipped_variants or [])
    out: List[BuildVariantSpec] = []
    for variant_key, dockerfile in dockerfile_variants.items():
        if variant_key in skipped:
            continue
        # Safety net for stub Dockerfiles the planner emits when a recipe
        # is incompatible with the user's source (e.g. NPU recipe with no
        # HF model load). The stub carries a `TODO_MODEL_ID` placeholder
        # which makes `optimum-cli export` fail during build. Same for
        # variants explicitly marked incompatible in adaptations. The UI
        # is supposed to auto-skip these in Review, but defending here
        # too so non-UI callers (CLI, retry, programmatic) don't ship
        # known-broken Dockerfiles to the build farm.
        adaptations = None
        if variant_adaptations and variant_key in variant_adaptations:
            adaptations = variant_adaptations[variant_key]
        if isinstance(adaptations, dict) and adaptations.get("incompatible"):
            logger.info(
                "[build_dag.adapter] skipping variant=%s reason=incompatible",
                variant_key,
            )
            continue
        if "TODO_MODEL_ID" in (dockerfile or ""):
            logger.info(
                "[build_dag.adapter] skipping variant=%s reason=todo_model_id",
                variant_key,
            )
            continue
        overrides = None
        if source_variants and variant_key in source_variants:
            overrides = [
                BuildArtifact(filename=f.get("filename", ""), content=f.get("body", ""))
                for f in source_variants[variant_key]
                if isinstance(f, dict) and f.get("filename")
            ]
        out.append(BuildVariantSpec(
            variant=variant_key,
            dockerfile=dockerfile,
            source_overrides=overrides,
            adaptations=adaptations,
        ))
    return out


class _DictAttrAdapter:
    """Wraps a dict so getattr(d, key) works the same as d[key].
    Lets the adapter accept either a Pydantic model or a plain
    dict body without branching."""
    __slots__ = ("_d",)
    def __init__(self, d):
        self._d = d
    def __getattr__(self, name):
        # Raise AttributeError for missing keys so getattr(x, name, default)
        # uses the default. Returning None instead would shortcut the
        # default and produce None values where the adapter expects
        # caller-supplied defaults like "CPU" / 1 / etc.
        if name in self._d:
            return self._d[name]
        raise AttributeError(name)


def _sample_output_from(req: Any) -> Optional[BuildSampleOutput]:
    """project-agent §1.1 — the expected output for the supplied sample
    input, when the requester provided one.

    Mirrors the inline sample_input handling below. Kept as a helper
    rather than duplicated because both request shapes gained the
    field at once, and a build graded against the wrong reference is
    worse than one graded against none.
    """
    filename = getattr(req, "sample_output_filename", None)
    body_b64 = getattr(req, "sample_output_body_b64", None)
    if filename and body_b64:
        return BuildSampleOutput(filename=filename, body_b64=body_b64)
    return None


def from_wizard_inputs_request(req: Any, *, tenant_id: int) -> BuildSpec:
    """Build a BuildSpec from the wizard's INPUT fields — no
    pre-generated artifacts. The generate-phase DAG node populates
    hero_dockerfile / entrypoint_sh / build_and_push_sh / variants
    later by writing back through /node_definitions/wizard/ingest-artifacts.

    Required: at least one of source_repo_url / inline_source_body /
    wrap_base_image. The endpoint rejects payloads with none of those
    set so the LLM has something to work from.
    """
    if isinstance(req, dict):
        req = _DictAttrAdapter(req)

    sample = None
    sample_filename = getattr(req, "sample_input_filename", None)
    sample_b64 = getattr(req, "sample_input_body_b64", None)
    if sample_filename and sample_b64:
        sample = BuildSampleInput(filename=sample_filename, body_b64=sample_b64)

    # Slug the fallback so the harbor image tag is always a valid
    # repo name (lowercase, [a-z0-9-]). Without slugging, a node_name
    # like "PDF to PNG Converter" or "E2E_Script_*" lands in the
    # spec as-is, then `docker build -t <prefix>/<image_name>:latest`
    # fails when the registry refuses uppercase + spaces. The slug
    # function below mirrors the canonical `_node_image_slug` in
    # routers/node_definitions.py — duplicated here to avoid the
    # circular import the adapter module is structured to prevent.
    _raw_image_name = getattr(req, "image_name", "") or ""
    _raw_node_name = req.node_name or ""
    if _raw_image_name.strip():
        _resolved_image_name = _raw_image_name.strip()
    else:
        import re as _re
        _s = _raw_node_name.lower().strip()
        _s = _re.sub(r"[^a-z0-9]+", "-", _s).strip("-")
        _resolved_image_name = _s or "node"

    return BuildSpec(
        image_name=_resolved_image_name,
        node_name=req.node_name,
        tenant_id=tenant_id,
        # Output fields stay empty — the generate node populates them.
        hero_dockerfile="",
        entrypoint_sh="",
        build_and_push_sh="",
        sample_input=sample,
        sample_output=_sample_output_from(req),
        defer_registration=bool(getattr(req, "defer_registration", False)),
        input_type=getattr(req, "input_type", "image"),
        output_type=getattr(req, "output_type", "image"),
        process_type=getattr(req, "process_type", "CPU"),
        cost_in_credits=int(getattr(req, "cost_in_credits", 1)),
        container_timeout=int(getattr(req, "container_timeout", 7200) or 7200),
        description=getattr(req, "description", None),
        node_type=getattr(req, "node_type", None),
        container_env=getattr(req, "container_env", None),
        allowed_egress=getattr(req, "allowed_egress", None),
        use_sandbox_runtime=bool(getattr(req, "use_sandbox_runtime", False)),
        skipped_variants=list(getattr(req, "skipped_variants", []) or []),
        node_metadata=getattr(req, "node_metadata", None),
        # Wizard INPUTS — the generate node reads these and translates
        # them into Containerizer env vars at dispatch time.
        source_repo_url=getattr(req, "source_repo_url", None),
        inline_source_body=getattr(req, "inline_source_body", None),
        inline_source_filename=getattr(req, "inline_source_filename", None),
        wrap_base_image=getattr(req, "wrap_base_image", None),
        additional_instructions=getattr(req, "additional_instructions", None),
        # Phase B — synthesizer's declared system binaries. List
        # defaults to [] / "" so legacy callers (script source w/o
        # describe-synth) keep working unchanged.
        system_packages=list(getattr(req, "system_packages", None) or []),
        dockerfile_extras=str(getattr(req, "dockerfile_extras", "") or ""),
    )


def from_build_local_request(req: Any, *, tenant_id: int) -> BuildSpec:
    """Convert the wizard's ``BuildLocalRequest`` (or any object with
    the same field shape — including a plain dict) into a typed
    ``BuildSpec``.

    The req argument is intentionally untyped (Any) so this module
    doesn't import the request schema from routers/node_definitions
    (avoids a circular import). The fields read here are stable —
    documented as the wizard's payload.
    """
    if isinstance(req, dict):
        req = _DictAttrAdapter(req)
    source_files = []
    raw_source_files = getattr(req, "source_files", None)
    if raw_source_files:
        for entry in raw_source_files:
            if isinstance(entry, dict):
                fn = entry.get("filename")
                body = entry.get("body")
                if fn and body is not None:
                    source_files.append(BuildArtifact(filename=fn, content=body))

    sample = None
    sample_filename = getattr(req, "sample_input_filename", None)
    sample_b64 = getattr(req, "sample_input_body_b64", None)
    if sample_filename and sample_b64:
        sample = BuildSampleInput(filename=sample_filename, body_b64=sample_b64)

    variants = _coerce_dockerfile_variants(
        getattr(req, "dockerfile_variants", None) or {},
        getattr(req, "source_variants", None) or {},
        getattr(req, "variant_adaptations", None) or {},
        getattr(req, "skipped_variants", None) or [],
    )

    return BuildSpec(
        image_name=req.image_name,
        node_name=req.node_name,
        tenant_id=tenant_id,
        hero_dockerfile=req.dockerfile,
        entrypoint_sh=req.entrypoint_sh,
        build_and_push_sh=req.build_and_push_sh,
        variants=variants,
        source_files=source_files,
        sample_input=sample,
        sample_output=_sample_output_from(req),
        defer_registration=bool(getattr(req, "defer_registration", False)),
        input_type=getattr(req, "input_type", "image"),
        output_type=getattr(req, "output_type", "image"),
        process_type=getattr(req, "process_type", "CPU"),
        cost_in_credits=int(getattr(req, "cost_in_credits", 1)),
        container_timeout=int(getattr(req, "container_timeout", 7200) or 7200),
        description=getattr(req, "description", None),
        node_type=getattr(req, "node_type", None),
        container_env=getattr(req, "container_env", None),
        allowed_egress=getattr(req, "allowed_egress", None),
        use_sandbox_runtime=bool(getattr(req, "use_sandbox_runtime", False)),
        skipped_variants=list(getattr(req, "skipped_variants", []) or []),
        node_metadata=getattr(req, "node_metadata", None),
    )

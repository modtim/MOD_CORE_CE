"""Describe-codegen BuildPlan extractor (wizard-rebuild §3d / §8 step D).

The terminal stage of the Describe cascade. When container search
(step A), repo search (step B), and library search (step C) all fall
below the confidence floor, step D asks the Containerizer LLM to
author a wrapper *from the user's intent alone* — there are no docs
to read, so this is the one mode that keeps generative LLM authorship
(design.md, "Describe mode (cascade)").

Division of labour:

  - The GENERATIVE LLM call lives upstream, in
    `node_builder/describe_synthesize.synthesize_wrapper`, invoked by
    `node_builder/describe_cascade/step_d_codegen.py`. That path
    already runs its own retry loop, license/binary validators, and
    executability critics before it hands back a `SynthesisResult`.
  - THIS module is a pure, deterministic assembler: it takes the
    synthesized artifacts (script body + declared requirements +
    base-image hint + system packages) and turns them into a
    canonical `BuildPlan` with `mode="describe-codegen"`. The §3e
    renderer then emits the Dockerfile deterministically.

Keeping the extractor free of the LLM call means:
  - byte-deterministic output (no network, no model wobble) so the
    renderer fixtures pin exact bytes;
  - one clear ownership boundary — synthesis in describe_synthesize,
    plan assembly here, rendering in renderer.py.

Because the synthesized script is Python (the synthesizer's contract:
`script_content` must pass a Python syntax check), describe-codegen
is essentially Script mode with an LLM-authored body. We reuse the
Script extractor's Python import/system-dep helpers rather than
duplicate them, and we embed the body via `script_body` so the
renderer base64-decodes it into the image (no build-context COPY).

Confidence:
  - Medium (0.55) — a base image resolved (explicit override, LLM
    hint, or curated detection) and the plan is renderable.
  - Low (0.30) — no base image could be resolved; §7 plan-aware
    repair gets the larger low-tier budget to iterate.

All LLM-authored fields (requirements, system packages) flow through
the §0a sandbox validators (`utils.llm_input_sandbox`); anything that
fails is dropped and its field name recorded in `weak_fields` so §7
repair knows where to look, and never lands in the rendered Dockerfile.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from utils.llm_input_sandbox import (
    filter_install_steps,
    filter_system_deps,
    validate_env_var_name,
)

from ..types import (
    EXTRACTOR_VERSION,
    BuildPlan,
    EnvVar,
    default_ports_for_types,
)
# Reuse the Script extractor's Python helpers — describe-codegen is
# Script mode with an LLM-authored body, so the interpreter mapping,
# import scan, entrypoint form, and pip→apt inference are identical.
from .script import (
    _entrypoint_for,
    _extract_python_imports,
    _infer_system_deps,
    _interpreter_to_language,
)

logger = logging.getLogger(__name__)


# The synthesizer authors Python wrappers exclusively (its contract:
# `script_content` must parse as Python). We fix the interpreter here
# rather than sniff, so the rendered entrypoint + filename are stable.
_CODEGEN_INTERPRETER = "python3"

# Filename the synthesized body is embedded as. Matches the
# convention step_d_codegen already uses for its Script-mode handoff
# so the cascade and the direct BuildPlan path agree.
_CODEGEN_FILENAME = "main.py"


# A pip distribution name at the start of a requirement token.
_PIP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")

# The one version-pin form that survives the §0a install-step validator:
# an EXACT `==` pin whose version uses only arg-allowlist characters
# (letters/digits/`.`/`_`/`+`/`-`). Range operators (`<`/`>`), extras
# (`[...]`), and `~=` are all rejected downstream because an unquoted
# `pkg>=1.0` in a `RUN pip install` line is a genuine shell-redirect
# hazard — so we strip them to the bare name (still installs, shell-safe)
# rather than let one spec sink the whole install step.
_PIP_EXACT_PIN_RE = re.compile(r"^==[A-Za-z0-9][A-Za-z0-9._+-]*$")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_describe_codegen_plan(
    *,
    script_body: str,
    node_name: str,
    input_type: str = "other",
    output_type: str = "other",
    requirements: Optional[list[str]] = None,
    base_image_hint: Optional[str] = None,
    system_packages: Optional[list[str]] = None,
    wrap_base_image: Optional[str] = None,
    container_env: Optional[dict[str, str]] = None,
    reasoning: Optional[str] = None,
) -> BuildPlan:
    """Assemble a `BuildPlan` for a Describe-cascade step-D submission.

    This is a PURE function — no LLM call, no network call. The
    generative synthesis already happened upstream in
    `describe_synthesize.synthesize_wrapper`; the caller passes its
    result fields in here.

    Args:
      script_body: the LLM-authored Python wrapper
        (`SynthesisResult.script_content`).
      node_name: human-readable Node display name.
      input_type / output_type: the synthesizer's suggested I/O types
        (`suggested_input_type` / `suggested_output_type`).
      requirements: pip requirement strings the synthesizer declared
        (`SynthesisResult.requirements`). Sanitized + validated here;
        an empty / missing list falls back to scanning the body's
        imports (same logic as Script mode).
      base_image_hint: the curated variant_dir the synthesizer chose
        (`SynthesisResult.base_image_hint`, e.g. "python-3.11").
        Resolved to a pullable ref via the curated catalog.
      system_packages: apt package names the synthesizer declared
        (`SynthesisResult.system_packages`). Validated + unioned with
        pip→apt inference.
      wrap_base_image: explicit base-image override; wins over the
        hint and over curated detection.
      container_env: env vars from the wizard's Configure step.
      reasoning: the synthesizer's one-line explanation, carried onto
        the plan's weak-field diagnostics context (not rendered).

    Returns:
      A `BuildPlan` with `mode="describe-codegen"`, `language="python"`,
      `script_body`/`script_filename` set (so the renderer embeds the
      body via base64), install/system deps validated, and I/O ports
      derived from the suggested types. Whatever couldn't be resolved
      lands in `weak_fields` for §7 repair.

    Raises:
      ValueError if `script_body` is empty or whitespace-only.
    """
    if not script_body or not script_body.strip():
        raise ValueError("script_body must be non-empty")

    weak_fields: list[str] = []

    language, ext = _interpreter_to_language(_CODEGEN_INTERPRETER)
    package_manager = "pip"

    # ---- base image -------------------------------------------------
    from_image_ref = _resolve_base_image(
        wrap_base_image=wrap_base_image,
        base_image_hint=base_image_hint,
        script_body=script_body,
    )
    if from_image_ref is None:
        weak_fields.append("from_image_ref")

    # ---- pip deps ---------------------------------------------------
    # Prefer the synthesizer's explicitly-declared requirements; fall
    # back to scanning the authored body's imports when it declared
    # none (the body is the source of truth for what it imports).
    declared = _sanitize_requirements(requirements or [])
    if declared:
        deps = declared
    else:
        deps = _extract_python_imports(script_body)

    install_steps: list[str] = []
    if deps:
        raw_step = "pip install --no-cache-dir " + " ".join(sorted(deps))
        # Final guard: the assembled command must clear the §0a
        # install-step validator (no metachars, known prefix). If a
        # sanitized requirement still yields a rejected command, we
        # drop the whole step and let repair rebuild it.
        kept, rejected = filter_install_steps([raw_step])
        install_steps = kept
        if rejected or not kept:
            weak_fields.append("install_steps")
    else:
        weak_fields.append("install_steps")

    # ---- system deps ------------------------------------------------
    # pip→apt runtime inference (libGL for opencv, libsndfile for
    # soundfile, …) PLUS the synthesizer's explicitly-declared apt
    # packages (a binary the script shells out to — ffmpeg, colmap —
    # is invisible to import scanning).
    system_deps: set[str] = set(_infer_system_deps(deps))
    declared_pkgs = [
        p for p in (system_packages or [])
        if isinstance(p, str) and p.strip()
    ]
    if declared_pkgs:
        kept_pkgs, rejected_pkgs = filter_system_deps(declared_pkgs)
        system_deps.update(kept_pkgs)
        if rejected_pkgs:
            logger.info(
                "describe_codegen: %d system_packages rejected by "
                "validator: %r", len(rejected_pkgs), rejected_pkgs[:5],
            )
            weak_fields.append("system_deps")
    system_deps_sorted = sorted(system_deps)

    # ---- filename + entrypoint -------------------------------------
    workdir = "/app"
    script_filename = _CODEGEN_FILENAME if ext == ".py" else f"main{ext}"
    entrypoint = _entrypoint_for(language, workdir, script_filename)

    # ---- ports + env ------------------------------------------------
    in_ports, out_ports = default_ports_for_types(input_type, output_type)

    env_vars: list[EnvVar] = []
    if container_env:
        for name, default in container_env.items():
            if not name or not validate_env_var_name(name):
                continue
            env_vars.append(EnvVar(
                name=name,
                required=True,
                default=str(default) if default is not None else None,
            ))

    # ---- confidence -------------------------------------------------
    # Codegen is the cascade's last resort — generative, no docs to
    # anchor on. Medium when a base resolved (renderable); low when it
    # didn't, so §7 gets the larger low-tier repair budget.
    confidence = 0.55 if from_image_ref is not None else 0.30

    plan = BuildPlan(
        mode="describe-codegen",
        language=language,
        package_manager=package_manager,
        workdir=workdir,
        entrypoint=entrypoint,

        from_image_ref=from_image_ref,
        system_deps=system_deps_sorted,
        install_steps=install_steps,
        env_vars=env_vars,

        input_ports=in_ports,
        output_ports=out_ports,

        script_body=script_body,
        script_filename=script_filename,

        confidence=confidence,
        weak_fields=sorted(set(weak_fields)),
        extractor_version=EXTRACTOR_VERSION,
    )
    return plan


def plan_from_synthesis(
    synth: Any,
    *,
    node_name: str,
    input_type: Optional[str] = None,
    output_type: Optional[str] = None,
    wrap_base_image: Optional[str] = None,
    container_env: Optional[dict[str, str]] = None,
) -> BuildPlan:
    """Adapter: build a describe-codegen plan straight from a
    `describe_synthesize.SynthesisResult` (or any object/dict with the
    same field names).

    Duck-typed on purpose — this module never imports
    `describe_synthesize`, keeping the plan-assembly boundary free of
    the shared wizard-runtime file. The §8 cascade's step D can call
    this to turn its `SynthesisResult` into a canonical BuildPlan in
    one line.

    Args:
      synth: an object or dict exposing `script_content`,
        `requirements`, `base_image_hint`, `system_packages`,
        `suggested_input_type`, `suggested_output_type`, `reasoning`.
      node_name / wrap_base_image / container_env: as
        `extract_describe_codegen_plan`.
      input_type / output_type: override the synthesizer's suggested
        types when the wizard captured explicit ones; otherwise fall
        back to `suggested_input_type` / `suggested_output_type`.

    Returns:
      A `BuildPlan` with `mode="describe-codegen"`.
    """
    def _get(name: str, default: Any = None) -> Any:
        if isinstance(synth, dict):
            return synth.get(name, default)
        return getattr(synth, name, default)

    return extract_describe_codegen_plan(
        script_body=_get("script_content", "") or "",
        node_name=node_name,
        input_type=input_type or _get("suggested_input_type", "other") or "other",
        output_type=output_type or _get("suggested_output_type", "other") or "other",
        requirements=list(_get("requirements", []) or []),
        base_image_hint=_get("base_image_hint") or None,
        system_packages=list(_get("system_packages", []) or []),
        wrap_base_image=wrap_base_image,
        container_env=container_env,
        reasoning=_get("reasoning") or None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_base_image(
    *,
    wrap_base_image: Optional[str],
    base_image_hint: Optional[str],
    script_body: str,
) -> Optional[str]:
    """Resolve the FROM ref for a codegen plan.

    Order of precedence:
      1. explicit `wrap_base_image` override,
      2. the synthesizer's curated `base_image_hint` (variant_dir →
         pullable ref via the curated catalog),
      3. curated detection from the body (honors the GPU-framework
         upgrade rule — torch/tf imports → CUDA base),
      4. None (recorded in weak_fields by the caller).
    """
    if wrap_base_image and wrap_base_image.strip():
        return wrap_base_image.strip()

    if base_image_hint and base_image_hint.strip():
        try:
            from utils.curated_base_images import by_variant_dir
            curated = by_variant_dir(base_image_hint.strip())
            if curated is not None:
                return curated.image_ref
            logger.info(
                "describe_codegen: base_image_hint %r not in curated "
                "catalog — falling back to body detection",
                base_image_hint,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "describe_codegen: curated-hint lookup failed for %r: "
                "%s (falling back to body detection)",
                base_image_hint, exc,
            )

    try:
        from node_builder.base_detector import detect_from_script
        detected = detect_from_script(_CODEGEN_INTERPRETER, script_body)
        return detected.image_ref
    except Exception as exc:
        logger.warning(
            "describe_codegen: curated-base detection failed: %s "
            "(continuing without from_image_ref)", exc,
        )
        return None


def _sanitize_requirements(requirements: list[str]) -> list[str]:
    """Reduce declared requirements to shell-safe pip tokens.

    The synthesizer runs license validators over its requirements, but
    it doesn't guarantee shell-safety — and an unquoted version range
    (`pkg>=1.0`) in a `RUN pip install` line is a real shell-redirect
    hazard, which the §0a install-step validator rejects. So per token:

      - bare name (`requests`, `package-with-dashes`) → kept verbatim;
      - exact pin (`numpy==1.26.0`) → kept (shell-safe, arg-allowlisted);
      - range / extras / `~=` (`pillow>=10`, `uvicorn[standard]`) →
        stripped to the bare distribution NAME so the dependency still
        installs and the RUN line stays safe; §7 repair can re-pin;
      - URL / VCS / path / metachar forms (`git+https://…`, `$(x)`,
        anything with whitespace or `/`) → dropped entirely.

    The assembled command still flows through `filter_install_steps` in
    the caller as a final guard.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in requirements:
        if not isinstance(raw, str):
            continue
        token = raw.strip()
        if not token:
            continue
        # URL / VCS / path / direct-reference forms are never bare
        # requirements — drop them (whitespace or `/` or `://`).
        if "/" in token or "://" in token or any(c.isspace() for c in token):
            logger.info(
                "describe_codegen: dropping URL/VCS/unsafe requirement %r",
                token,
            )
            continue
        m = _PIP_NAME_RE.match(token)
        if not m:
            logger.info(
                "describe_codegen: dropping non-name requirement %r", token,
            )
            continue
        name = m.group(0)
        rest = token[m.end():]
        if not rest:
            safe = name
        elif _PIP_EXACT_PIN_RE.match(rest):
            safe = name + rest
        else:
            # Range / extras / non-exact spec — keep just the name.
            safe = name
        if safe not in seen:
            out.append(safe)
            seen.add(safe)
    return out

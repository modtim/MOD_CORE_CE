"""Planner — turn (source, enabled-tags whitelist, simple-recipe
output) into stub artefacts for the unsupported gap.

This is the entry point `tools/create_container.py` calls. Returns a
dict the caller can merge into its `variant_dockerfiles`,
`source_variants`, `variant_adaptations` accumulators so the wizard
sees a fully-populated payload for every enabled variant.

Phase 1 implementation: every gap variant gets:

  - A stub Dockerfile from `recipes/`.
  - An `incompatible: True` flag in `variant_adaptations[v]` with a
    user-facing reason from the negative classifier.
  - A copy of the hero source files in `source_variants[v]` (so the
    wizard's diff view has SOMETHING to render — the operator can
    edit it inline before clicking Build).

Phase 2 onwards: backends with a real recipe are passed via
`implemented_recipes`, the classifier softens its blockers, and the
recipe takes over for those backends. The planner shape doesn't
change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .architectures import detect_hf_classes, is_allowed, unsupported_subset
from .negative_classifier import classify, Incompatibility
from .recipes import STUB_RENDERERS, RECIPE_MODULES


@dataclass
class StubArtefacts:
    """Per-variant artefacts the planner produced for a gap variant."""
    dockerfile: str
    source_files: Dict[str, str]
    adaptation: Dict[str, object]   # variant_adaptations[v] shape
    incompatible_reason: str        # also goes into incompatible_reasons[v]


@dataclass
class PlanResult:
    """Aggregate of all stub artefacts to merge into the Containerizer
    output."""
    dockerfiles: Dict[str, str] = field(default_factory=dict)
    source_variants: Dict[str, Dict[str, str]] = field(default_factory=dict)
    variant_adaptations: Dict[str, Dict[str, object]] = field(default_factory=dict)
    incompatible_reasons: Dict[str, str] = field(default_factory=dict)


def plan_unsupported_variants(
    *,
    source_text: str,
    source_files: Dict[str, str],
    source_filenames: Iterable[str],
    enabled_variants: Iterable[str],
    detected_variants: Iterable[str],
    image_name: str = "node",
    implemented_recipes: Iterable[str] = (),
) -> PlanResult:
    """Compute stub artefacts for every variant in the gap.

    The "gap" = enabled_variants - detected_variants. For each gap
    variant we emit Dockerfile + source-files + adaptation + reason.

    `source_files` is the hero source bundle ({filename: body}). It's
    copied verbatim into `source_variants[gap_variant]` so the wizard's
    diff view shows the unaltered hero — the operator edits it inline.

    `source_filenames` is the same set of names for the negative
    classifier's `.cu`-sibling heuristic.

    `enabled_variants` is the platform's whitelist (Settings → GPU
    Compatibility tags). `detected_variants` is what the simple-
    recipe path already produced. The difference is what we fill.
    """
    enabled = {v.strip().lower() for v in enabled_variants if v.strip()}
    detected = {v.strip().lower() for v in detected_variants if v.strip()}
    implemented = {v.strip().lower() for v in implemented_recipes if v.strip()}
    gap = enabled - detected
    if not gap:
        return PlanResult()

    classification = classify(
        source_text,
        source_filenames=source_filenames,
        targets=gap,
        implemented_recipes=implemented,
    )
    architectures = detect_hf_classes(source_text)

    out = PlanResult()
    for variant in sorted(gap):
        renderer = STUB_RENDERERS.get(variant)
        if renderer is None:
            # No stub recipe for this variant (e.g. a future tag the
            # platform admin enabled but we don't ship a recipe for).
            # Surface as incompatible with a generic reason — better
            # than silent.
            out.incompatible_reasons[variant] = (
                f"No Containerizer recipe shipped for variant "
                f"'{variant}'. See MULTIVENDOR_ROADMAP.md for the "
                f"backend support plan."
            )
            out.variant_adaptations[variant] = {
                "compat": "incompatible",
                "incompatible": True,
                "incompatible_reason": out.incompatible_reasons[variant],
                "files": {},
            }
            out.source_variants[variant] = dict(source_files)
            continue

        # Compose the incompatibility reason: classifier (specific to
        # this source) + architecture-allowlist gating (specific to
        # this backend).
        reasons: List[str] = []
        clf = classification.get(variant)
        if clf is not None:
            reasons.append(clf.reason)
        unsupported = unsupported_subset(variant, architectures)
        if unsupported:
            reasons.append(
                f"detected HuggingFace classes not on the {variant} "
                f"allowlist: {', '.join(sorted(unsupported))}"
            )

        # Phase 2+ path: backend has a real recipe AND nothing blocks.
        # Run rewrite_source() FIRST — recipes that need extra context
        # for the Dockerfile (e.g. NPU's optimum-cli export needs the
        # detected HF model_id + task) can stash that on a private
        # recipe-module attribute the renderer reads.
        #
        # Two recipe outcomes to keep separate:
        #   - compat=native: emit per-variant Dockerfile + rewritten
        #     source. The continue path skips the stub block below.
        #   - incompatible: fall through to the stub block. BUT — if
        #     the recipe set `workload_incompatible=True`, signal that
        #     downstream so the stub block suppresses the Dockerfile
        #     entirely (no point shipping a 5 GB IPEX image for a
        #     workload that has no torch in it).
        workload_incompatible = False
        if (
            variant in implemented
            and variant in RECIPE_MODULES
            and not reasons
        ):
            recipe_mod = RECIPE_MODULES[variant]
            try:
                rewrite = recipe_mod.rewrite_source(source_files)
                if rewrite.is_compatible():
                    # Recipe-specific Dockerfile kwargs (set by recipes
                    # that need detection-time context). Optional.
                    extra_kw = getattr(recipe_mod, "_last_render_kwargs", {}) or {}
                    hero_pkgs = list(_parse_hero_pip(image_name=image_name))
                    out.dockerfiles[variant] = recipe_mod.render_dockerfile(
                        hero_pip_packages=hero_pkgs,
                        **extra_kw,
                    )
                    out.source_variants[variant] = {
                        fn: rf.body for fn, rf in rewrite.files.items()
                    }
                    out.variant_adaptations[variant] = (
                        rewrite.to_adaptation(language_for=_guess_language)
                    )
                    continue
                reasons.append(rewrite.incompatible_reason or "")
                workload_incompatible = bool(
                    getattr(rewrite, "workload_incompatible", False)
                )
            except Exception as e:
                reasons.append(
                    f"recipe '{variant}' raised {type(e).__name__}: {e}"
                )

        # Stub path — recipe missing, blocked by classifier, or recipe
        # ran but couldn't safely transform.
        if not reasons:
            reasons.append(
                f"Backend '{variant}' is permitted but the auto-port "
                f"recipe is not yet shipped. Stub Dockerfile provided "
                f"for manual editing."
            )
        joined = " ".join(reasons)
        out.incompatible_reasons[variant] = joined

        # #3 — for workload-level incompatibility, suppress the stub
        # Dockerfile entirely. The wizard's planner contract is
        # "variant_dockerfiles has an entry → there's something to
        # build (or to hand-port from)". Emitting an IPEX or OpenVINO
        # stub for a non-ML workload violates that contract:
        # operators can't hand-port a shell-out-to-binary script into
        # a `from_pretrained` call. Without a Dockerfile in the dict,
        # the build_dag adapter skips the variant entirely — no
        # build attempt, no smoke probe, no wasted Harbor push.
        if workload_incompatible:
            # Keep the source visible so the wizard's diff/inspect
            # view still has SOMETHING to render, but skip the
            # Dockerfile emission. variant_adaptations records the
            # workload_incompatible signal so the operator-facing
            # tile can distinguish this from "operator could
            # hand-port".
            out.variant_adaptations[variant] = {
                "compat": "incompatible",
                "incompatible": True,
                "workload_incompatible": True,
                "incompatible_reason": joined,
                "files": {},
            }
            out.source_variants[variant] = dict(source_files)
            continue

        # Render the stub Dockerfile.
        out.dockerfiles[variant] = renderer(image_name=image_name)
        out.variant_adaptations[variant] = {
            "compat": "incompatible",
            "incompatible": True,
            "incompatible_reason": joined,
            "files": {
                fn: {
                    "changes": [],
                    "warnings": [{
                        "severity": "warn",
                        "message": (
                            f"Variant '{variant}' surfaced as "
                            f"incompatible — operator must edit this "
                            f"file manually if they want to ship the "
                            f"variant."
                        ),
                    }],
                    "language": _guess_language(fn),
                    "incompatible": True,
                    "incompatible_reason": joined,
                    "llm_used": False,
                }
                for fn in source_files
            },
        }
        # Copy the hero source verbatim — the wizard's diff view
        # treats this as "no auto-changes; manually edit".
        out.source_variants[variant] = dict(source_files)

    return out


def _guess_language(filename: str) -> str:
    """Cheap filename → language guess. Matches the rest of the
    Containerizer's heuristic."""
    f = filename.lower()
    if f.endswith(".py"):
        return "python"
    if f.endswith((".sh", ".bash")):
        return "bash"
    if f.endswith((".r", ".rmd")):
        return "r"
    if f.endswith((".js", ".mjs", ".ts")):
        return "javascript"
    return "text"


def _parse_hero_pip(*, image_name: str) -> List[str]:
    """Best-effort hero-pip-package list. The planner's caller in
    create_container.py doesn't pass the hero Dockerfile body in;
    until it does, we return a sensible default for HF inference
    nodes (covers ~90 % of what we see). Phase 4 plumbs the real
    list through.
    """
    return ["transformers", "pillow", "numpy"]

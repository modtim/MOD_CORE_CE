"""Stage 11 — bounded refine loop.

When Stage 8 (static validation) flags errors, this module re-prompts
ONLY the generators whose artifacts produced those errors, feeding the
failure messages back in as context. Up to MAX_ITERATIONS cycles.

Non-error warnings are never sent back — they surface in the UI but
don't block or trigger a refine.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Optional

from tools import artifact_prompts, validate_static
from tools.generate_artifacts_v2 import (
    ArtifactResult, _strip_fences, ARTIFACT_TIMEOUT_SECONDS,
)
from tools.llm_providers import LLMConfig, get_provider


logger = logging.getLogger(__name__)


MAX_ITERATIONS = 2

# Map artifact key → (system prompt, attribute on ArtifactResult)
_ARTIFACT_DISPATCH = {
    "dockerfile": (artifact_prompts.DOCKERFILE_PROMPT, "dockerfile"),
    "entrypoint_sh": (artifact_prompts.ENTRYPOINT_PROMPT, "entrypoint_sh"),
    "build_and_push_sh": (artifact_prompts.BUILD_AND_PUSH_PROMPT, "build_and_push_sh"),
    "node_metadata_py": (artifact_prompts.NODE_METADATA_PROMPT, "node_metadata_py"),
}


async def refine(
    artifacts: ArtifactResult, build_profile: dict, llm: LLMConfig,
    *, max_iterations: int = MAX_ITERATIONS,
) -> tuple[ArtifactResult, validate_static.ValidationReport]:
    """Loop: validate → if errors, re-prompt those artifacts → validate again.

    Returns the final (artifacts, report). The caller surfaces any
    remaining errors to the user rather than hiding them.
    """
    provider = get_provider(llm)
    iteration = 0
    current = artifacts

    while True:
        report = validate_static.validate(current.to_dict(), build_profile)
        if not report.has_errors or iteration >= max_iterations:
            return current, report

        iteration += 1
        logger.info("Refine iteration %d: %d errors across %d artifacts",
                    iteration, sum(1 for i in report.issues if i.severity == "error"),
                    len(report.errors_by_artifact()))

        tasks = []
        labels: list[str] = []
        for art_key, errors in report.errors_by_artifact().items():
            # "cross" errors are fixed by regenerating the artifact
            # most likely at fault. Prefer the metadata when the issue is about
            # env var or egress declaration; otherwise touch dockerfile.
            target_key = _pick_refine_target(art_key, errors)
            if target_key not in _ARTIFACT_DISPATCH:
                continue
            system_prompt, attr = _ARTIFACT_DISPATCH[target_key]
            current_text = getattr(current, attr, "")
            user_prompt = _render_refine_prompt(
                artifact=target_key,
                current_text=current_text,
                issues=errors,
                build_profile=build_profile,
            )
            tasks.append(_call_refine(provider, system_prompt, user_prompt, target_key))
            labels.append(target_key)

        if not tasks:
            break

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for label, res in zip(labels, results):
            if isinstance(res, Exception):
                logger.warning("Refine %s errored: %s", label, res)
                continue
            new_text = res
            setattr(current, label, new_text)
            # If we managed to regenerate, clear that artifact's error
            # bucket in the working copy so validate won't double-count
            # on the next loop iteration.
            current.errors = {k: v for k, v in (current.errors or {}).items() if k != label}

    return current, report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _call_refine(provider, system_prompt: str, user_prompt: str, label: str) -> str:
    try:
        raw = await asyncio.wait_for(
            provider.generate(system_prompt, user_prompt),
            timeout=ARTIFACT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"{label} refine timeout") from e
    return _strip_fences(raw)


def _render_refine_prompt(
    *, artifact: str, current_text: str, issues, build_profile: dict,
) -> str:
    """Build the re-prompt. Lead with the PREVIOUS output, the specific
    errors, and ask for a corrected version of the SAME artifact only.
    """
    issue_lines: list[str] = []
    for i in issues:
        issue_lines.append(f"- [{i.kind}] {i.message}"
                           + (f" (line {i.line})" if i.line else ""))

    deps = build_profile.get("dependencies") or {}
    dep_summary = ", ".join(
        f"{k}: {', '.join(v[:20])}" for k, v in deps.items() if v
    ) or "none"

    return (
        f"The previous {artifact} had validation errors. Produce a "
        f"corrected version. Apply the MOD-node rules from the system "
        f"prompt unchanged; ONLY fix what the errors below identify.\n\n"
        f"Build profile summary:\n"
        f"- node_name: {build_profile.get('node_name')}\n"
        f"- base_image_hint: {build_profile.get('base_image_hint')}\n"
        f"- gpu_required: {build_profile.get('gpu_required')}\n"
        f"- dependencies: {dep_summary}\n"
        f"\nPrevious {artifact}:\n"
        f"--- begin ---\n{current_text}\n--- end ---\n"
        f"\nValidation errors to fix:\n"
        + "\n".join(issue_lines)
        + f"\n\nReturn ONLY the corrected {artifact} text. "
        f"No prose, no fences, no explanation."
    )


def _pick_refine_target(artifact_key: str, errors) -> str:
    """For cross-artifact errors, pick the most-likely artifact at fault."""
    if artifact_key != "cross":
        return artifact_key
    kinds = {e.kind for e in errors}
    if "env_declared_unused" in kinds:
        return "node_metadata_py"    # over-declaration lives in the metadata
    if "egress_missing" in kinds:
        return "node_metadata_py"    # missing entries live in the metadata
    return "node_metadata_py"

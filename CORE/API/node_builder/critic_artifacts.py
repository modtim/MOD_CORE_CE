"""Critic C — Containerizer Artifact Auditor.

Watches the Containerizer-emitted artifact bundle (Dockerfile,
entrypoint_sh, build_and_push_sh, image_name, variants) at the
``/wizard/ingest-artifacts`` API seam — between Containerizer
emit and BuildSpec merge.

Per approach (b) from the design talk: this lives in the API process
(not inside the Containerizer Docker image), so we can use the existing
``call_wizard_llm`` client and share the ``critic_loop`` helper with
critics A and B. The build-task stage that originally generates the
artifact is NOT re-invoked on failure — instead, an in-process LLM
patches the artifact dict inline based on the critic's issues. Avoids
heavyweight container re-dispatch; bounded retries; graceful degrade
on critic exhaustion (per Tim's #4 — ship rather than block).

Failure modes the critic actively catches (all observed in this
session's stage traffic):

  - ``image_name`` is a full ref like ``registry:5000/foo`` instead of
    a clean slug. Causes ``docker build -t <prefix>/<image_name>:latest``
    to produce an invalid tag.
  - ``entrypoint_sh`` doesn't write its output anywhere (no
    OUTPUT_DIR write, no ``/data/output/...`` reference).
  - ``dockerfile`` contains ``TODO_MODEL_ID`` placeholder.
  - ``dockerfile`` missing ``FROM`` or both ``CMD`` / ``ENTRYPOINT``.
  - ``build_and_push_sh`` doesn't run ``docker build`` or doesn't
    push.

Deterministic structural checks (image_name shape, TODO placeholders,
required directives) run BEFORE the LLM critic — they're cheap and
unambiguous. The LLM is only invoked for the semantic checks the
regex layer can't make.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

from services.critic_loop import (
    LoopResult,
    Verdict,
    critic_loop,
    format_issues_for_proposer,
)
from utils.llm_client import LLMClientError, call_wizard_llm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Deterministic pre-checks
# ---------------------------------------------------------------------

_IMAGE_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _deterministic_issues(artifacts: Dict[str, Any]) -> List[str]:
    """Cheap structural checks that don't need an LLM. Each issue is
    phrased so an LLM patcher receiving it can act on it directly."""
    issues: List[str] = []

    img = (artifacts.get("image_name") or "").strip()
    if not img:
        issues.append("image_name is empty. Set it to a clean lowercase slug like 'pdf-to-png'.")
    elif "/" in img or ":" in img:
        issues.append(
            f"image_name='{img}' contains '/' or ':' (looks like a full image ref). "
            "Use only the bare repo segment — lowercase letters, digits, and hyphens, "
            "no registry host, no tag."
        )
    elif not _IMAGE_NAME_OK.match(img):
        issues.append(
            f"image_name='{img}' has invalid characters. Use only lowercase letters, "
            "digits, and hyphens, max 63 chars, starting with a letter or digit."
        )

    dockerfile = (artifacts.get("dockerfile") or "").strip()
    if not dockerfile:
        issues.append("dockerfile is empty.")
    else:
        if "TODO_MODEL_ID" in dockerfile:
            issues.append(
                "dockerfile contains 'TODO_MODEL_ID' placeholder. Either replace it with "
                "the actual model id, or remove the variant that needed it."
            )
        if not re.search(r"(?mi)^\s*FROM\s+\S+", dockerfile):
            issues.append("dockerfile has no FROM directive. Every Dockerfile needs a base image.")
        if not re.search(r"(?mi)^\s*(CMD|ENTRYPOINT)\b", dockerfile):
            issues.append(
                "dockerfile has neither CMD nor ENTRYPOINT. Add one so the container "
                "knows what to run."
            )

    entrypoint = (artifacts.get("entrypoint_sh") or "").strip()
    if not entrypoint:
        issues.append("entrypoint_sh is empty.")
    elif "OUTPUT_DIR" not in entrypoint and "/data/output" not in entrypoint:
        issues.append(
            "entrypoint_sh never references OUTPUT_DIR or /data/output — the node will run "
            "but produce no output for downstream nodes. Write results into "
            "$OUTPUT_DIR (or /data/output) before exit."
        )

    build_push = (artifacts.get("build_and_push_sh") or "").strip()
    if not build_push:
        issues.append("build_and_push_sh is empty.")
    else:
        if "docker build" not in build_push and "buildx build" not in build_push:
            issues.append("build_and_push_sh has no 'docker build' / 'buildx build' command.")
        if "docker push" not in build_push and "buildx" not in build_push:
            issues.append("build_and_push_sh has no 'docker push' command (or buildx push).")

    return issues


# ---------------------------------------------------------------------
# LLM critic for semantic checks
# ---------------------------------------------------------------------

_CRITIC_SYSTEM_PROMPT = """You are reviewing the artifact bundle the \
Containerizer LLM produced for a build wizard. The bundle has these \
fields: image_name, dockerfile, entrypoint_sh, build_and_push_sh, \
plus optional variant data.

Your ONLY job is to check whether the bundle is internally consistent \
and runnable, NOT whether it's beautiful code. Specifically:

1. SOURCE DEPENDENCIES: does the dockerfile install the dependencies \
the entrypoint actually uses? If entrypoint_sh runs `python3 \
inference.py` and inference.py uses torch, the dockerfile must install \
torch (or use a base image that has it).

2. IO CONTRACT: does the entrypoint read from /data/input and write \
to /data/output (or $OUTPUT_DIR)? Containers that read from elsewhere \
or don't write outputs will silently fail downstream nodes.

3. INTERNAL CONSISTENCY: does the image_name in build_and_push_sh \
match the image_name field? Does the dockerfile's WORKDIR / COPY \
paths line up with what entrypoint references?

You are NOT reviewing: code style, comments, docstrings, performance, \
test coverage, or anything that wouldn't prevent the container from \
running successfully.

Output JSON only, no prose, no markdown:
{"ok": true} if the bundle passes all three checks.
{"ok": false, "issues": ["..."]} if any fail. Each issue is one short \
concrete sentence pointing at WHICH field is wrong and WHAT to change. \
Example: "entrypoint_sh imports torch but dockerfile only installs \
numpy. Add 'pip install torch' to the dockerfile."
"""


def _build_critic_user_prompt(artifacts: Dict[str, Any]) -> str:
    def _show(field: str, max_len: int = 4000) -> str:
        v = artifacts.get(field) or ""
        if not isinstance(v, str):
            v = repr(v)
        if len(v) > max_len:
            v = v[:max_len] + f"\n... [truncated, {len(v) - max_len} chars omitted]"
        return v

    return (
        f"image_name: {artifacts.get('image_name', '')}\n"
        "\n"
        "dockerfile:\n"
        f"---\n{_show('dockerfile')}\n---\n"
        "\n"
        "entrypoint_sh:\n"
        f"---\n{_show('entrypoint_sh')}\n---\n"
        "\n"
        "build_and_push_sh:\n"
        f"---\n{_show('build_and_push_sh')}\n---\n"
    )


def _llm_critic(artifacts: Dict[str, Any], llm: Callable[..., Any]) -> Verdict:
    user_prompt = _build_critic_user_prompt(artifacts)
    try:
        parsed = llm(
            system_prompt=_CRITIC_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=20,
            temperature=0.0,
            max_tokens=400,
        )
    except LLMClientError as e:
        logger.info("critic_artifacts LLM unavailable: %s", e)
        return Verdict(ok=True, issues=[], notes={"critic_error": str(e)})

    if not isinstance(parsed, dict):
        return Verdict(ok=True, issues=[], notes={"critic_error": "non-dict response"})
    ok = parsed.get("ok")
    if not isinstance(ok, bool):
        return Verdict(ok=True, issues=[], notes={"critic_error": "missing ok"})
    if ok:
        return Verdict(ok=True, issues=[])

    raw = parsed.get("issues") or []
    if not isinstance(raw, list):
        return Verdict(ok=False, issues=["(reviewer raised an objection but its response was malformed)"])
    issues = [str(i).strip() for i in raw if str(i).strip()][:8]
    if not issues:
        issues = ["(reviewer raised an objection with no concrete reason — try again)"]
    return Verdict(ok=False, issues=issues)


# ---------------------------------------------------------------------
# LLM patcher (the "proposer" for retry iterations)
# ---------------------------------------------------------------------

_PATCHER_SYSTEM_PROMPT = """You are patching an artifact bundle that a \
reviewer just flagged. The bundle has these fields: image_name, \
dockerfile, entrypoint_sh, build_and_push_sh.

Apply the reviewer's issues to the bundle. Make the SMALLEST change \
that fixes each issue — do not rewrite working code. Preserve \
everything the reviewer did NOT object to.

Output JSON only, no prose, no markdown:
{"image_name": "...", "dockerfile": "...", "entrypoint_sh": "...", \
"build_and_push_sh": "..."}

If the reviewer's issue only affects one field, still echo back ALL \
four fields so the bundle stays complete.
"""


def _llm_patch(
    artifacts: Dict[str, Any],
    issues: List[str],
    llm: Callable[..., Any],
) -> Dict[str, Any]:
    issue_block = "\n".join(f"- {i}" for i in issues)
    user_prompt = (
        "Current bundle:\n"
        f"image_name: {artifacts.get('image_name', '')}\n"
        "\ndockerfile:\n"
        f"---\n{artifacts.get('dockerfile', '')}\n---\n"
        "\nentrypoint_sh:\n"
        f"---\n{artifacts.get('entrypoint_sh', '')}\n---\n"
        "\nbuild_and_push_sh:\n"
        f"---\n{artifacts.get('build_and_push_sh', '')}\n---\n"
        "\nReviewer issues to address:\n"
        f"{issue_block}\n"
    )
    try:
        parsed = llm(
            system_prompt=_PATCHER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=30,
            temperature=0.1,
            max_tokens=4000,
        )
    except LLMClientError as e:
        logger.info("critic_artifacts patcher LLM unavailable: %s", e)
        # Patcher unavailable — return artifacts unchanged so the
        # loop exits with the most recent attempt.
        return dict(artifacts)

    if not isinstance(parsed, dict):
        return dict(artifacts)

    # Merge: only update fields the patcher returned, keep anything
    # else (variants, source_variants, etc.) intact.
    out = dict(artifacts)
    for k in ("image_name", "dockerfile", "entrypoint_sh", "build_and_push_sh"):
        v = parsed.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v
    return out


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def audit_artifacts(
    artifacts: Dict[str, Any],
    *,
    llm: Optional[Callable[..., Any]] = None,
    max_iters: int = 5,
) -> Dict[str, Any]:
    """Run the artifact bundle through deterministic + LLM critics,
    patching inline on failure up to ``max_iters`` iterations. Returns
    the (possibly patched) bundle. Always returns a usable dict — on
    critic exhaustion the last attempt is returned so the build can
    still proceed (and downstream test_and_repair can catch what we
    missed).
    """
    if llm is None:
        llm = call_wizard_llm

    def proposer(prev_attempt: Any, prior_issues: Optional[List[str]]) -> Dict[str, Any]:
        if prev_attempt is None:
            # First iteration — return the bundle as-given.
            return dict(artifacts)
        if not prior_issues:
            return prev_attempt
        # Patch the previous attempt with the prior issues.
        return _llm_patch(prev_attempt, prior_issues, llm=llm)

    def critic(output: Dict[str, Any]) -> Verdict:
        # Run deterministic checks first — cheap and unambiguous. If
        # they fail, return their issues directly without burning a
        # critic LLM call.
        det_issues = _deterministic_issues(output)
        if det_issues:
            return Verdict(ok=False, issues=det_issues, notes={"layer": "deterministic"})
        # All deterministic checks pass — defer to the LLM critic for
        # the semantic-consistency checks.
        return _llm_critic(output, llm=llm)

    result: LoopResult = critic_loop(
        proposer,
        critic,
        max_iters=max_iters,
        name="artifacts",
        logger_=logger,
    )
    return result.output

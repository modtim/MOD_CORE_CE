"""LLM prompt assembly for AI-assisted node generation.

Encapsulates the contract the LLM must honor so that admission re-validation
at the end is a formality, not a frequent rejection source:

- The detected base image is a **required** field; the LLM must emit that
  exact `FROM` line and no other.
- Non-root `USER` clause at the final stage.
- No `curl|sh`, `wget|sh`, `ADD <url>`, or `--privileged` references.
- `WORKDIR /app` is canonical; reads from `/data/input/`, writes to
  `/data/output/`.

Retry-with-feedback: when admission rejects the LLM's Dockerfile, the
rejection reason is fed back into the prompt via `build_retry_prompt` so
the next attempt has a specific fix to apply.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .base_detector import DetectedBase


RULES_BLOCK = """\
HARD REQUIREMENTS — the generated Dockerfile must satisfy ALL of these:

1. The FROM line MUST be exactly: {required_from}
   Do not substitute any other base image. Do not add `AS <stage>` unless
   you also add a matching multi-stage FROM <stage> downstream that the
   final image pulls from.
2. The final USER directive must be non-root (numeric UID ≥ 10001, or
   the `mod` user baked into the curated base). Never end with `USER root`
   or `USER 0`.
3. Do NOT pipe downloads to a shell — no `curl ... | sh`, no
   `wget ... | bash`. If you need a remote artifact, `COPY` it in from the
   build context after a verified download, or use an apt/pip package.
4. Do NOT use `ADD <url>`. Use `COPY` for local files, or `RUN <pkg-manager>`
   for packages.
5. Do NOT reference `--privileged` — nodes never run privileged.
6. `WORKDIR /app`.
7. The node reads input from `/data/input/` and writes output to
   `/data/output/`. Do not rely on any other mount point.
8. If the node has Python or JS dependencies, install them with the
   corresponding package manager in a single `RUN` layer and cache-friendly
   order (lockfile + install, then source copy).
"""


@dataclass
class NodeGenPromptInput:
    """Everything the prompt builder needs. Keep small — if a field doesn't
    appear in at least one prompt variant, it shouldn't live here."""

    node_name: str
    detected_base: DetectedBase
    input_type: str = "image"
    output_type: str = "image"
    process_type: str = "CPU"
    user_instructions: Optional[str] = None

    # For script mode
    script_body: Optional[str] = None
    script_interpreter: Optional[str] = None

    # For repo mode — caller provides snippets from sentinel files
    repo_manifest_snippets: dict[str, str] = field(default_factory=dict)


def build_repo_prompt(inp: NodeGenPromptInput) -> str:
    """Prompt for Git Repo mode. The LLM sees the detected base, a few
    manifest snippets, and the user's free-form instructions. The prompt
    is designed to be deterministic-ish: most latitude the LLM has is in
    RUN-command ordering, not in base selection."""
    manifest_block = ""
    for filename, content in (inp.repo_manifest_snippets or {}).items():
        snip = (content or "").strip()
        if snip:
            manifest_block += f"\n### {filename}\n```\n{snip[:2000]}\n```\n"

    return (
        f"You are generating a Dockerfile for a MOD workflow node named "
        f"{inp.node_name!r}. The node processes {inp.input_type} input from "
        f"/data/input/ and writes {inp.output_type} output to /data/output/.\n\n"
        f"{RULES_BLOCK.format(required_from=inp.detected_base.image_ref)}\n"
        f"Detected base reason: {inp.detected_base.reason}\n"
        f"{manifest_block}\n"
        f"User instructions: {inp.user_instructions or '(none)'}\n\n"
        "Output ONLY the Dockerfile text, no markdown fences, no prose.\n"
    )


def build_script_prompt(inp: NodeGenPromptInput) -> str:
    """Prompt for Script mode. The LLM wraps a single script body in a
    Dockerfile using the declared interpreter and the detected base."""
    return (
        f"You are wrapping a user-provided {inp.script_interpreter} script "
        f"into a Dockerfile for MOD workflow node {inp.node_name!r}.\n\n"
        f"{RULES_BLOCK.format(required_from=inp.detected_base.image_ref)}\n"
        f"The script body below MUST be COPY'd into the image and executed "
        "as the ENTRYPOINT. Do not modify the script body. Do not inline it "
        "as a heredoc — always COPY a file.\n\n"
        f"### Script ({inp.script_interpreter})\n"
        f"```\n{(inp.script_body or '').strip()[:8000]}\n```\n\n"
        f"User instructions: {inp.user_instructions or '(none)'}\n\n"
        "Output ONLY the Dockerfile text, no markdown fences, no prose.\n"
    )


def build_retry_prompt(
    original_prompt: str,
    rejected_dockerfile: str,
    admission_rule: str,
    admission_reason: str,
) -> str:
    """After admission rejects an LLM output, feed the specific rule +
    reason back so the model has a concrete fix to apply. Cap the
    rejected Dockerfile excerpt so the retry prompt doesn't balloon past
    the context window."""
    excerpt = (rejected_dockerfile or "").strip()
    if len(excerpt) > 4000:
        excerpt = excerpt[:4000] + "\n... [truncated]"
    return (
        f"{original_prompt}\n\n"
        "---\n\n"
        "Your previous Dockerfile attempt was rejected by admission.\n"
        f"Rule violated: {admission_rule}\n"
        f"Specific reason: {admission_reason}\n\n"
        "Previous attempt (for reference only — do NOT repeat it verbatim):\n"
        f"```\n{excerpt}\n```\n\n"
        "Fix the violation and output ONLY the corrected Dockerfile text.\n"
    )

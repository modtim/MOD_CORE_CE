"""workflow-agentic-execution-loop §wizard-agentic-loop — output
critic for the agentic loop.

The critic inspects what a candidate graph ACTUALLY produced on its
test input and reports whether that output satisfies the user's
stated goal. Three verdicts:

  - SATISFIES_GOAL          — done; surface this workflow
  - NEEDS_FIX               — feed the failure_mode back into the
                              Builder for another iteration
  - NEEDS_USER_CLARIFICATION — output is ambiguous against the goal
                              and the critic asks the user a question

Two evaluation paths, branched on the output type:

  - structured (json / csv / text / tensor):
        deterministic — schema-diff against the candidate graph's
        declared output_schema (when present), surface-area
        comparison otherwise. No LLM call.

  - multimodal (image / video):
        invoke a vision-capable LLM with the output rendered as a
        thumbnail + the user's goal as the prompt. Parses the LLM's
        JSON verdict envelope.

The critic is dependency-injected for testability: the LLM caller
is passed in as `llm_caller`, the output-content loader as
`load_output_content`. Tests stub both; production wires them to
`_call_llm` and a MinIO download helper.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Verdict enum + result dataclass
# ---------------------------------------------------------------------

VERDICT_SATISFIES = "SATISFIES_GOAL"
VERDICT_NEEDS_FIX = "NEEDS_FIX"
VERDICT_NEEDS_USER_CLARIFICATION = "NEEDS_USER_CLARIFICATION"

_ALLOWED_VERDICTS = {VERDICT_SATISFIES, VERDICT_NEEDS_FIX, VERDICT_NEEDS_USER_CLARIFICATION}


@dataclass
class CriticResult:
    verdict: str
    # Populated for NEEDS_FIX — concrete description of what the output
    # is missing / wrong about; fed back to the Builder.
    failure_mode: Optional[str] = None
    # Populated for NEEDS_USER_CLARIFICATION — a direct question to
    # the user (surfaced verbatim in the wizard panel).
    user_question: Optional[str] = None
    # Free-form notes the agent loop can attach to a wizard_test_runs
    # row for later debugging.
    notes: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "failure_mode": self.failure_mode,
            "user_question": self.user_question,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------
# Output-type classification
# ---------------------------------------------------------------------

_STRUCTURED_TYPES = {"json", "csv", "text", "tensor", "embedding"}
_MULTIMODAL_TYPES = {"image", "video"}


def _terminal_output_type(candidate_graph: Dict[str, Any]) -> Optional[str]:
    """Infer the terminal output type by reading the last OUTPUT-
    category node's output_schema. Returns None if no such node
    exists (well-formed graphs always have an output)."""
    nodes = (candidate_graph or {}).get("nodes") or []
    # Last output node wins — multi-output graphs critique the last
    # one (the agent loop encourages single-output workflows).
    for n in reversed(nodes):
        if not isinstance(n, dict):
            continue
        ntype = n.get("type")
        data = n.get("data") or {}
        if ntype == "output" or (data.get("node_kind") or "").endswith("_output"):
            schema = data.get("output_schema") or {}
            t = (schema.get("output_type") or "").strip().lower()
            if t and t not in ("", "none"):
                return t
            kind = (data.get("node_kind") or "").lower()
            if kind.endswith("_output"):
                return kind[: -len("_output")]
    # Fall back: the last non-input node's output_type.
    for n in reversed(nodes):
        if not isinstance(n, dict):
            continue
        data = n.get("data") or {}
        schema = data.get("output_schema") or {}
        t = (schema.get("output_type") or "").strip().lower()
        if t and t not in ("", "none"):
            return t
    return None


# ---------------------------------------------------------------------
# Structured-path critic
# ---------------------------------------------------------------------

def _critique_structured(
    *,
    output_bytes: Optional[bytes],
    output_type: str,
    candidate_graph: Dict[str, Any],
    user_goal: str,
    llm_caller: Optional[Callable[..., str]] = None,
    llm_pin: Optional[str] = None,
) -> CriticResult:
    """Schema-aware critic for structured outputs. Deterministic
    surface checks first (parses cleanly? non-empty? type matches?),
    LLM critique only when surface checks pass — saves a round-trip
    for the most common failure (output is empty / malformed).
    """
    notes: Dict[str, Any] = {"output_type": output_type}
    if output_bytes is None:
        return CriticResult(
            verdict=VERDICT_NEEDS_FIX,
            failure_mode="No terminal output produced — workflow ran "
                         "but emitted no result file.",
            notes=notes,
        )
    if len(output_bytes) == 0:
        return CriticResult(
            verdict=VERDICT_NEEDS_FIX,
            failure_mode="Terminal output is zero-bytes — the graph "
                         "ran but the output file is empty.",
            notes=notes,
        )

    if output_type == "json":
        try:
            parsed = json.loads(output_bytes.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return CriticResult(
                verdict=VERDICT_NEEDS_FIX,
                failure_mode=f"Output declared as JSON but doesn't parse: {e}",
                notes=notes,
            )
        # Non-empty object / array required.
        if parsed == {} or parsed == [] or parsed is None:
            return CriticResult(
                verdict=VERDICT_NEEDS_FIX,
                failure_mode="JSON output is empty (parses but contains "
                             "no fields / items).",
                notes=notes,
            )
        notes["parsed_keys"] = (
            list(parsed.keys()) if isinstance(parsed, dict) else None
        )

    elif output_type == "csv":
        text = output_bytes.decode("utf-8", errors="replace")
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) < 2:
            # Header + at least one data row.
            return CriticResult(
                verdict=VERDICT_NEEDS_FIX,
                failure_mode="CSV output has fewer than 2 non-empty "
                             "lines (header + data row expected).",
                notes=notes,
            )
        notes["csv_rows"] = len(lines)

    elif output_type == "text":
        text = output_bytes.decode("utf-8", errors="replace").strip()
        if not text:
            return CriticResult(
                verdict=VERDICT_NEEDS_FIX,
                failure_mode="Text output is whitespace-only.",
                notes=notes,
            )
        notes["text_length"] = len(text)

    # Surface checks passed. If we have an LLM caller, ask it whether
    # the output ACTUALLY answers the user's goal. Without an LLM,
    # default to SATISFIES — the deterministic checks gave a green
    # light and we'd rather false-positive on agreement than block on
    # missing infrastructure.
    if llm_caller is None:
        return CriticResult(verdict=VERDICT_SATISFIES, notes=notes)

    try:
        verdict = _llm_judge_structured(
            output_bytes=output_bytes,
            output_type=output_type,
            user_goal=user_goal,
            llm_caller=llm_caller,
            llm_pin=llm_pin,
        )
        verdict.notes.update(notes)
        return verdict
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("output_critic: LLM judge failed (%s) — defaulting SATISFIES", e)
        return CriticResult(verdict=VERDICT_SATISFIES, notes=notes)


def _llm_judge_structured(
    *,
    output_bytes: bytes,
    output_type: str,
    user_goal: str,
    llm_caller: Callable[..., str],
    llm_pin: Optional[str],
) -> CriticResult:
    """Ask the LLM whether the output addresses the goal. Trimmed
    payload so we don't blow context: structured outputs cap at 4KB."""
    sample = output_bytes[:4096].decode("utf-8", errors="replace")
    messages = [
        {"role": "system", "content": _STRUCTURED_JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"USER_GOAL:\n{user_goal}\n\n"
            f"OUTPUT_TYPE: {output_type}\n"
            f"OUTPUT_SAMPLE (first 4KB):\n{sample}\n\n"
            "Respond with exactly one JSON object: "
            '{"verdict":"SATISFIES_GOAL"|"NEEDS_FIX"|"NEEDS_USER_CLARIFICATION",'
            ' "failure_mode":"<text|null>", "user_question":"<text|null>"}'
        )},
    ]
    raw = llm_caller(messages, max_tokens_override=300, model_override=llm_pin)
    return _parse_verdict(raw)


# ---------------------------------------------------------------------
# Multimodal-path critic
# ---------------------------------------------------------------------

def _critique_multimodal(
    *,
    output_pointer: str,
    output_bytes: Optional[bytes],
    output_type: str,
    user_goal: str,
    llm_caller: Optional[Callable[..., str]] = None,
    llm_pin: Optional[str] = None,
) -> CriticResult:
    """Vision-LLM critic for image / video outputs. Without a vision-
    capable LLM caller, falls back to a surface check (non-empty
    bytes) and defaults SATISFIES — the agent loop's iteration cap
    bounds the damage if the critic is wrong.

    Production wires `llm_caller` to an OpenAI-compat vision endpoint
    that accepts `messages` with image_url content blocks. Tests pass
    a deterministic stub.
    """
    notes: Dict[str, Any] = {
        "output_type": output_type, "output_pointer": output_pointer,
    }
    if output_bytes is not None and len(output_bytes) == 0:
        return CriticResult(
            verdict=VERDICT_NEEDS_FIX,
            failure_mode="Terminal output is zero-bytes.",
            notes=notes,
        )

    if llm_caller is None:
        # No vision LLM configured — surface check only.
        return CriticResult(verdict=VERDICT_SATISFIES, notes=notes)

    try:
        # Caller decides how to encode the image — we just pass the
        # pointer + goal. In production this becomes a multipart
        # `messages` payload with an image_url content block.
        messages = [
            {"role": "system", "content": _MULTIMODAL_JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"USER_GOAL:\n{user_goal}\n\n"
                f"OUTPUT_TYPE: {output_type}\n"
                f"OUTPUT_POINTER: {output_pointer}\n\n"
                "Respond with exactly one JSON object: "
                '{"verdict":"SATISFIES_GOAL"|"NEEDS_FIX"|"NEEDS_USER_CLARIFICATION",'
                ' "failure_mode":"<text|null>", "user_question":"<text|null>"}'
            )},
        ]
        raw = llm_caller(messages, max_tokens_override=300, model_override=llm_pin)
        verdict = _parse_verdict(raw)
        verdict.notes.update(notes)
        return verdict
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("output_critic (multimodal): LLM call failed (%s)", e)
        return CriticResult(verdict=VERDICT_SATISFIES, notes=notes)


# ---------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------

_VERDICT_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)


def _parse_verdict(raw: str) -> CriticResult:
    """Tolerant parse: accepts the JSON in a fenced block OR bare in
    the response. Falls back to NEEDS_FIX with the raw LLM text as
    failure_mode if the envelope is unrecognisable — keeps the agent
    loop from infinite-looping on malformed critic responses."""
    if not raw or not raw.strip():
        return CriticResult(
            verdict=VERDICT_NEEDS_FIX,
            failure_mode="critic returned empty response",
        )
    text = raw.strip()
    parsed: Optional[Dict[str, Any]] = None
    m = _VERDICT_FENCE_RE.search(text)
    if m:
        try:
            parsed = json.loads(m.group(1))
        except Exception:
            parsed = None
    if parsed is None:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
    if not isinstance(parsed, dict):
        return CriticResult(
            verdict=VERDICT_NEEDS_FIX,
            failure_mode=f"critic emitted unparseable response: {text[:200]}",
        )

    verdict = (parsed.get("verdict") or "").strip().upper()
    if verdict not in _ALLOWED_VERDICTS:
        return CriticResult(
            verdict=VERDICT_NEEDS_FIX,
            failure_mode=f"critic emitted unknown verdict {verdict!r}",
        )

    failure_mode = parsed.get("failure_mode")
    if isinstance(failure_mode, str) and not failure_mode.strip():
        failure_mode = None
    user_question = parsed.get("user_question")
    if isinstance(user_question, str) and not user_question.strip():
        user_question = None

    # NEEDS_USER_CLARIFICATION must have a question. If absent, treat
    # as NEEDS_FIX with a clear failure_mode so the Builder gets a
    # signal to refine.
    if verdict == VERDICT_NEEDS_USER_CLARIFICATION and not user_question:
        return CriticResult(
            verdict=VERDICT_NEEDS_FIX,
            failure_mode="critic asked for clarification but supplied no "
                         "question — treating as NEEDS_FIX",
        )

    return CriticResult(
        verdict=verdict, failure_mode=failure_mode, user_question=user_question,
    )


# ---------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------

_STRUCTURED_JUDGE_SYSTEM_PROMPT = (
    "You are the output-critic of an agentic workflow wizard. Your job "
    "is to judge whether the supplied OUTPUT_SAMPLE addresses the "
    "USER_GOAL. Be strict: an output that runs without producing the "
    "user's actual ask is NEEDS_FIX, not SATISFIES. Use "
    "NEEDS_USER_CLARIFICATION only when the goal itself is "
    "ambiguous — never to dodge a judgement call.\n"
    "\n"
    "Verdicts:\n"
    "  SATISFIES_GOAL — output clearly addresses the user's stated ask.\n"
    "  NEEDS_FIX      — output ran but doesn't match the goal; supply a\n"
    "                   single-sentence failure_mode the Builder can\n"
    "                   act on.\n"
    "  NEEDS_USER_CLARIFICATION — the user_goal itself is ambiguous;\n"
    "                   supply a concrete user_question.\n"
    "\n"
    "Respond with exactly one JSON object — no prose around it."
)


_MULTIMODAL_JUDGE_SYSTEM_PROMPT = (
    "You are the output-critic of an agentic workflow wizard. The "
    "OUTPUT_POINTER references an image (or video frame). Compare it "
    "against the USER_GOAL and decide whether the visual content "
    "matches what the user asked for. Be strict on whether the "
    "specific transformation the user wanted is visible.\n"
    "\n"
    "Verdicts: SATISFIES_GOAL | NEEDS_FIX | NEEDS_USER_CLARIFICATION.\n"
    "Respond with exactly one JSON object."
)


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def critique_output(
    *,
    output_pointer: Optional[str],
    output_bytes: Optional[bytes],
    candidate_graph: Dict[str, Any],
    user_goal: str,
    llm_caller: Optional[Callable[..., str]] = None,
    llm_pin: Optional[str] = None,
    load_output_content: Optional[Callable[[str], Optional[bytes]]] = None,
) -> CriticResult:
    """Inspect the test-run output and return a CriticResult.

    `output_pointer` is required for multimodal paths (the LLM sees a
    URL, not raw bytes). `output_bytes` is required for structured
    paths. When `load_output_content` is supplied, we lazily fetch
    bytes from the pointer.

    Returns NEEDS_FIX with a descriptive failure_mode when the output
    is missing entirely — that's the most common test-run failure
    mode (worker errored, container OOM'd, etc.) and we want to
    iterate on it like any other.
    """
    output_type = _terminal_output_type(candidate_graph) or "text"

    # Lazy-load bytes when only a pointer was supplied.
    if output_bytes is None and output_pointer and load_output_content is not None:
        try:
            output_bytes = load_output_content(output_pointer)
        except Exception as e:
            logger.warning("output_critic: load_output_content failed: %s", e)
            output_bytes = None

    if output_type in _MULTIMODAL_TYPES:
        return _critique_multimodal(
            output_pointer=output_pointer or "",
            output_bytes=output_bytes,
            output_type=output_type,
            user_goal=user_goal,
            llm_caller=llm_caller, llm_pin=llm_pin,
        )

    # Default to structured (handles json/csv/text/tensor + anything
    # unrecognised — structured surface checks always work safely).
    return _critique_structured(
        output_bytes=output_bytes,
        output_type=output_type,
        candidate_graph=candidate_graph,
        user_goal=user_goal,
        llm_caller=llm_caller, llm_pin=llm_pin,
    )


# ---------------------------------------------------------------------
# per-output-type similarity dispatch
# ---------------------------------------------------------------------

def per_output_type_check(
    output_type: Optional[str],
    candidate: Any,
    reference: Any,
    *,
    threshold: Optional[float] = None,
    candidate_embedding: Optional[list] = None,
    reference_embedding: Optional[list] = None,
    embedding_fn: Optional[Callable[[Any], list]] = None,
) -> CriticResult:
    """Compare a candidate output against a reference using the checker that
    matches its output type, and return a CriticResult.

    Dispatch: `image`/`video` -> ImageSimilarityChecker (embedding cosine,
    default threshold 0.8); `text` -> TextSimilarityChecker (BLEU+cosine,
    default 0.75); anything else (json/csv/tensor/embedding/unknown) ->
    StructuredOutputChecker (schema + field match, default 0.9 == 90%).

    Thresholds are parameters, not env vars — the checker's own default
    applies when `threshold` is None. Embeddings are injected by the caller;
    this module never resolves one, so the check stays offline.

    Returns a CriticResult so the agentic loop can feed `failure_mode` back
    to the Builder exactly like `critique_output` does.
    """
    from services.output_similarity import (
        ImageSimilarityChecker,
        StructuredOutputChecker,
        TextSimilarityChecker,
    )

    kind = (output_type or "text").strip().lower()

    if kind in _MULTIMODAL_TYPES:
        outcome = ImageSimilarityChecker().check(
            candidate,
            reference,
            threshold=threshold,
            candidate_embedding=candidate_embedding,
            reference_embedding=reference_embedding,
            embedding_fn=embedding_fn,
        )
    elif kind == "text":
        outcome = TextSimilarityChecker().check(
            candidate, reference, threshold=threshold
        )
    else:
        outcome = StructuredOutputChecker().check(
            candidate, reference, threshold=threshold
        )

    if outcome.passed:
        return CriticResult(
            verdict=VERDICT_SATISFIES,
            notes={
                "similarity_score": outcome.score,
                "similarity_threshold": outcome.threshold,
                "output_type": kind,
            },
        )

    details = outcome.details
    return CriticResult(
        verdict=VERDICT_NEEDS_FIX,
        failure_mode=(
            f"output similarity {outcome.score:.3f} below threshold "
            f"{outcome.threshold:.3f} for output_type={kind}"
            + (f"; missing_fields={details['missing_fields']}"
               if details.get("missing_fields") else "")
            + (f"; mismatched_fields={details['mismatched_fields']}"
               if details.get("mismatched_fields") else "")
        ),
        notes={
            "similarity_score": outcome.score,
            "similarity_threshold": outcome.threshold,
            "output_type": kind,
            "checker_details": details,
        },
    )

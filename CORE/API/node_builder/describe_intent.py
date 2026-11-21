"""δ Phase 4 — describe-source intent classifier.

First step of the describe-and-build pipeline. Takes the user's
free-text description and returns:

- ``intent``: ``"wrap_repo"`` (an existing OSS package likely
  exists) vs. ``"from_scratch"`` (small enough that writing it
  from zero is cleaner).
- ``search_keywords``: 3-5 terms feeding the GitHub search query
  (Phase 6).
- ``interpretation``: one-sentence rephrasing of the user's
  request — surfaced in the wizard so they catch
  misunderstandings early.

Strategy: single LLM call returning JSON. Falls back through the
existing wizard-tool config (vLLM → Anthropic via the multi-
provider chain configured in routers/ai_config). If the LLM
hallucinates / returns garbage, ``classify_intent`` returns a
conservative ``from_scratch`` result with the raw description as
keywords, so the wizard can still proceed (the user can search
manually if they want).

The function is pure and testable: caller injects an ``llm``
callable matching ``call_wizard_llm``'s signature.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from utils.llm_client import LLMClientError, call_wizard_llm

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an engineer triaging build requests.

A user wants a new node-style worker that takes input files in
/data/input/ and writes outputs to /data/output/. Read their
description and decide whether the work is best done by wrapping
an existing open-source library (intent: wrap_repo) or by writing
a small custom script from scratch (intent: from_scratch).

Pick wrap_repo when:
- The task is well-known and high-quality OSS exists (face
  detection, image upscaling, OCR, audio transcription, etc).
- The task needs a non-trivial model or a large dependency.

Pick from_scratch when:
- The task is small (file format conversion, regex extraction,
  arithmetic on a CSV, calling an external HTTP API).
- The user explicitly says they want a custom script.
- The task is too specific to be served by an existing library.

ALSO decide the SURFACE — is this ONE node, or a multi-step WORKFLOW?

A single node does ONE cohesive operation on its input. A workflow is a
PIPELINE of 2+ DISTINCT processing stages, each naturally its own node
(different tool, a hand-off through an intermediate output type).

Pick "workflow" ONLY when the request clearly decomposes into 2+ distinct
stages with a hand-off between them, for example:
- "video to 3D mesh" = extract frames from the VIDEO (ffmpeg) -> photogrammetry
  (frames -> mesh). Workflow ONLY because the input is a VIDEO that must first
  be decoded into images.
- "transcribe a podcast and summarize it" = speech-to-text -> summarize
- "scrape a site, clean the data, and chart it" = fetch -> clean -> plot

Pick "node" for EVERYTHING ELSE — a single operation, even a complex or
model-heavy one: resize, upscale, OCR, a single format conversion, one model
inference, OR photogrammetry that reconstructs a mesh from a SET / SEQUENCE OF
IMAGES (the input is already images -> ONE operation; do NOT add an "extract
frames" stage when there is no video). When in doubt, choose "node". NEVER split
a single operation into artificial stages, and NEVER invent a preceding stage
the stated input does not need.

Always reply with a single JSON object on one line, no prose, no
markdown fences:

{"intent": "wrap_repo" | "from_scratch",
 "search_keywords": ["term1", "term2", ...],
 "interpretation": "one-sentence rephrasing",
 "surface": "node" | "workflow",
 "surface_reason": "one sentence; if workflow, name the stages"}

search_keywords MUST be 2-5 short terms suitable for a GitHub
repo search. Drop quotes, articles, and punctuation. Use
canonical names ("face detection", "image super resolution",
"speech to text") not loose phrasing.
"""


@dataclass
class IntentResult:
    intent: str  # "wrap_repo" | "from_scratch"
    search_keywords: List[str] = field(default_factory=list)
    interpretation: str = ""
    # Whether the request is best served as one node or a multi-step workflow.
    # Conservative default "node" — the wizard only NUDGES toward the Workflow
    # Wizard when this is "workflow"; it never force-redirects.
    surface: str = "node"  # "node" | "workflow"
    surface_reason: str = ""
    raw_response: Optional[Any] = None
    fallback: bool = False

    def as_dict(self) -> dict:
        return {
            "intent": self.intent,
            "search_keywords": list(self.search_keywords),
            "interpretation": self.interpretation,
            "surface": self.surface,
            "surface_reason": self.surface_reason,
            "fallback": self.fallback,
        }


_VALID_INTENTS = {"wrap_repo", "from_scratch"}
_VALID_SURFACES = {"node", "workflow"}


def _parse_surface(parsed: Any) -> tuple[str, str]:
    """Extract (surface, surface_reason) from the LLM JSON, defaulting to
    'node' on anything unexpected — we only ever nudge toward the workflow
    wizard on a clear, explicit 'workflow', never on ambiguity."""
    if not isinstance(parsed, dict):
        return "node", ""
    s = parsed.get("surface")
    if s not in _VALID_SURFACES:
        return "node", ""
    reason = parsed.get("surface_reason")
    reason = reason.strip()[:300] if isinstance(reason, str) else ""
    return s, reason
_KEYWORD_CLEAN = re.compile(r"[^a-z0-9 \-]")


def _clean_keyword(kw: str) -> str:
    if not isinstance(kw, str):
        return ""
    kw = kw.strip().lower()
    kw = _KEYWORD_CLEAN.sub(" ", kw)
    return " ".join(kw.split())[:60]


def _clamp_keywords(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        cleaned = _clean_keyword(item)
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) >= 5:
            break
    return out


def _fallback_for(description: str) -> IntentResult:
    """Conservative default when the LLM returns garbage. We
    treat anything we can't classify as 'from_scratch' so the
    wizard doesn't waste GitHub API calls on a half-baked search.
    """
    keywords = []
    for word in re.split(r"[^a-zA-Z0-9]+", description or ""):
        word = word.strip().lower()
        if len(word) >= 3 and word not in keywords:
            keywords.append(word)
        if len(keywords) >= 5:
            break
    return IntentResult(
        intent="from_scratch",
        search_keywords=keywords[:5],
        interpretation=(description or "").strip()[:200] or "(no description provided)",
        fallback=True,
    )


def classify_intent(
    description: str,
    *,
    llm: Optional[Callable[..., Any]] = None,
) -> IntentResult:
    """Classify the user's free-text description and return an
    ``IntentResult``.

    ``llm`` defaults to ``call_wizard_llm``; tests inject a
    deterministic stub. Any LLM error (timeout, parse, missing
    config) falls back to ``_fallback_for(description)``.
    """
    description = (description or "").strip()
    if not description:
        return IntentResult(
            intent="from_scratch",
            interpretation="",
            fallback=True,
        )
    if llm is None:
        llm = call_wizard_llm
    try:
        parsed = llm(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=description,
            response_format="json",
            timeout_s=30,
        )
    except LLMClientError as e:
        logger.info("describe-intent LLM unavailable, using fallback: %s", e)
        return _fallback_for(description)
    except Exception as e:
        logger.warning("describe-intent LLM raised unexpectedly: %s", e)
        return _fallback_for(description)

    if not isinstance(parsed, dict):
        logger.warning("describe-intent LLM returned non-dict: %r", type(parsed))
        return _fallback_for(description)

    intent = parsed.get("intent")
    if intent not in _VALID_INTENTS:
        logger.warning("describe-intent LLM returned bad intent %r", intent)
        return _fallback_for(description)

    keywords = _clamp_keywords(parsed.get("search_keywords"))
    if not keywords:
        # An empty keyword list makes the GitHub search useless;
        # treat as a fallback so we at least search on the raw
        # description text.
        return _fallback_for(description)

    interpretation = parsed.get("interpretation")
    if not isinstance(interpretation, str) or not interpretation.strip():
        interpretation = description[:200]

    surface, surface_reason = _parse_surface(parsed)

    return IntentResult(
        intent=intent,
        search_keywords=keywords,
        interpretation=interpretation.strip()[:300],
        surface=surface,
        surface_reason=surface_reason,
        raw_response=parsed,
        fallback=False,
    )

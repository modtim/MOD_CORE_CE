"""Phase 8 — LLM-driven HuggingFace architecture auto-discovery.

The intel_npu recipe ships with a static allowlist (`_HF_TO_OV` +
`_OPTIMUM_OPENVINO_SUPPORTED`) covering ~30 model classes. Real-world
HF repos ship new classes faster than we can update the allowlist —
operators hitting an unseen class today get "architecture not on
allowlist" + manual escape hatch.

Phase 8 fills that gap: when the recipe hits an unseen class, this
module calls the configured LLM with a structured prompt containing:

  1. Few-shot examples from the existing `_HF_TO_OV` (show the shape).
  2. The unseen HF class name + task hint (extracted from the source).
  3. Optional model-card text (first ~1KB).

The LLM proposes `{"ov_class": str, "task": str, "confidence": float}`.
We validate the shape against `optimum.intel.openvino`'s known
exporter class names (hardcoded here — Optimum's release cadence is
infrequent so a monthly refresh is fine).

Caching:
  * In-process memoisation (survives one Containerizer run).
  * Redis cache (30-day TTL) keyed on
    `arch_resolver:{hf_class}:{prompt_version}`.

Gating:
  * Disabled by default. Set `MOD_ARCH_RESOLVER_ENABLED=1` to enable.
  * Kill-switch env `MOD_ARCH_RESOLVER_KILLED=1` forces a None
    return even when enabled (incident response).

Failure modes:
  * LLM offline → None.
  * LLM returns prose / malformed JSON → None.
  * LLM proposes an ov_class we don't recognize → None.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


PROMPT_TEMPLATE_VERSION = "v1"
CACHE_TTL_SECONDS = 30 * 24 * 3600

# Optimum-Intel's exporter classes as of 2026-04. Source:
# https://huggingface.co/docs/optimum-intel/openvino_exporter.
# Any ov_class the LLM proposes MUST be in this set — anything outside
# is a hallucination / rename / future class we don't yet support.
_KNOWN_OV_CLASSES: set[str] = {
    "OVModelForSequenceClassification",
    "OVModelForTokenClassification",
    "OVModelForQuestionAnswering",
    "OVModelForMaskedLM",
    "OVModelForFeatureExtraction",
    "OVModelForCausalLM",
    "OVModelForSeq2SeqLM",
    "OVModelForImageClassification",
    "OVModelForVision2Seq",
    "OVModelForCTC",
    "OVModelForSpeechSeq2Seq",
    "OVStableDiffusionPipeline",
    "OVStableDiffusionXLPipeline",
    "OVModelForSemanticSegmentation",
    "OVModelForMultipleChoice",
    "OVModelForZeroShotImageClassification",
    "OVModelForCustomTasks",
    "OVModelForImageToImage",
}

# Tasks Optimum recognizes for `optimum-cli export openvino --task`.
_KNOWN_TASKS: set[str] = {
    "text-classification",
    "token-classification",
    "question-answering",
    "fill-mask",
    "feature-extraction",
    "text-generation",
    "text2text-generation",
    "translation",
    "summarization",
    "image-classification",
    "image-to-text",
    "automatic-speech-recognition",
    "text-to-image",
    "image-to-image",
    "semantic-segmentation",
    "multiple-choice",
    "zero-shot-image-classification",
    "image-captioning",
}


# In-process memoisation. Keys are HF class strings; values are
# (ov_class, task) pairs.
_CACHE: Dict[str, Tuple[str, str]] = {}

_redis = None


def _get_redis():
    global _redis
    if _redis is None:
        try:
            import redis
            _redis = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://redis:6379/2"),
                decode_responses=True,
                socket_connect_timeout=2,
            )
            _redis.ping()
        except Exception as e:  # noqa: BLE001
            logger.debug("arch_resolver: Redis unavailable: %s", e)
            _redis = None
    return _redis


def _reset_for_tests() -> None:
    global _redis
    _CACHE.clear()
    _redis = None


def _cache_key(hf_class: str) -> str:
    return f"arch_resolver:{hf_class}:{PROMPT_TEMPLATE_VERSION}"


def _is_enabled() -> bool:
    if os.getenv("MOD_ARCH_RESOLVER_KILLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return False
    return os.getenv("MOD_ARCH_RESOLVER_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ---------------------------------------------------------------------
# Prompt assembly.
# ---------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an Intel OpenVINO exporter. Given a HuggingFace model "
    "class name (e.g. `BertForSequenceClassification`), propose the "
    "matching Optimum-Intel `OVModelFor*` class and the `optimum-cli "
    "export openvino --task` value.\n\n"
    "Respond with STRICTLY this JSON:\n"
    '{"ov_class": "OVModelFor...", "task": "...", "confidence": <0.0 - 1.0>}\n\n'
    "Rules:\n"
    "  1. `ov_class` MUST be a real class in optimum.intel.openvino.\n"
    "  2. `task` MUST be a real Optimum task string.\n"
    "  3. If you are less than 0.6 confident, return ov_class=\"\" and "
    "explain briefly in a `notes` field (≤120 chars).\n"
    "  4. Do NOT invent new class names. If the architecture requires "
    "a class that doesn't exist in Optimum-Intel yet, refuse with "
    "ov_class=\"\".\n"
    "  5. Do NOT emit markdown fences, prose, or commentary. JUST the "
    "JSON object.\n"
)


def _build_few_shot(seed_map: Dict[str, Tuple[str, str]]) -> str:
    """Flatten a few-shot block from the existing static `_HF_TO_OV`
    mapping. Cap at 12 examples — enough shape signal, cheap prompt."""
    picks: List[Tuple[str, str, str]] = []
    for hf, (ov, task) in seed_map.items():
        picks.append((hf, ov, task))
        if len(picks) >= 12:
            break
    if not picks:
        return ""
    lines = ["Known mappings (few-shot):"]
    for hf, ov, task in picks:
        lines.append(f'  - "{hf}" -> {{"ov_class": "{ov}", "task": "{task}"}}')
    return "\n".join(lines)


def _build_user_prompt(
    hf_class: str,
    seed_map: Dict[str, Tuple[str, str]],
) -> str:
    parts = [
        _build_few_shot(seed_map),
        "",
        f"Resolve: {hf_class}",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------
# LLM call.
# ---------------------------------------------------------------------

async def _call_llm(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Invoke the Containerizer's LLM provider. Returns the raw
    response string, or None when the provider is unavailable."""
    try:
        from tools.llm_providers import get_provider
    except Exception:
        return None
    try:
        provider = get_provider()
    except Exception as e:  # noqa: BLE001
        logger.info("arch_resolver: get_provider failed: %s", e)
        return None
    try:
        response = await provider.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format="json",
        )
    except Exception:
        logger.exception("arch_resolver: provider.generate raised")
        return None
    return response


def _parse_response(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    # Tolerate leading/trailing whitespace + markdown fences even though
    # the system prompt forbids them (LLMs are LLMs).
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.MULTILINE)
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _validate(proposal: Optional[Dict[str, Any]]) -> Optional[Tuple[str, str, float]]:
    if not isinstance(proposal, dict):
        return None
    ov_class = str(proposal.get("ov_class") or "").strip()
    task = str(proposal.get("task") or "").strip()
    try:
        confidence = float(proposal.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    if not ov_class or not task:
        return None
    if ov_class not in _KNOWN_OV_CLASSES:
        logger.info("arch_resolver: LLM proposed unknown ov_class=%r", ov_class)
        return None
    if task not in _KNOWN_TASKS:
        logger.info("arch_resolver: LLM proposed unknown task=%r", task)
        return None
    return (ov_class, task, confidence)


# ---------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------

def resolve_arch(
    hf_class: str,
    *,
    seed_map: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Optional[Tuple[str, str]]:
    """Resolve an HF class to `(ov_class, task)`. Returns None when:
      - the feature is disabled,
      - the kill switch is active,
      - the LLM is unreachable,
      - the LLM response doesn't parse / validate.

    Synchronous wrapper around the async LLM call so callers inside
    `rewrite_source` (which runs inside a sync Python code path) don't
    have to manage an event loop themselves.
    """
    if not hf_class:
        return None

    # In-process cache first — a Containerizer run that ports 3 variants
    # hits this 3x per unseen class.
    cached = _CACHE.get(hf_class)
    if cached is not None:
        return (cached[0], cached[1])

    if not _is_enabled():
        return None

    # Redis cache.
    r = _get_redis()
    if r is not None:
        try:
            cache_line = r.get(_cache_key(hf_class))
            if cache_line:
                parsed = json.loads(cache_line)
                ov = str(parsed.get("ov_class") or "")
                task = str(parsed.get("task") or "")
                if ov and task and ov in _KNOWN_OV_CLASSES and task in _KNOWN_TASKS:
                    _CACHE[hf_class] = (ov, task)
                    return (ov, task)
        except Exception:
            logger.debug("arch_resolver: cache read failed", exc_info=True)

    seed = seed_map or {}
    system_prompt = _SYSTEM_PROMPT
    user_prompt = _build_user_prompt(hf_class, seed)

    try:
        raw = asyncio.run(_call_llm(system_prompt, user_prompt))
    except RuntimeError:
        # Nested loop — run in a thread-exec helper.
        import concurrent.futures
        def _runner():
            return asyncio.run(_call_llm(system_prompt, user_prompt))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            raw = ex.submit(_runner).result(timeout=60)
    except Exception:
        logger.exception("arch_resolver: LLM call fell through")
        return None

    validated = _validate(_parse_response(raw))
    if validated is None:
        return None
    ov_class, task, confidence = validated
    if confidence < 0.6:
        logger.info(
            "arch_resolver: LLM proposal below confidence floor: %s → %s (%.2f)",
            hf_class, ov_class, confidence,
        )
        return None

    # Write-through cache.
    _CACHE[hf_class] = (ov_class, task)
    if r is not None:
        try:
            r.setex(
                _cache_key(hf_class),
                CACHE_TTL_SECONDS,
                json.dumps({
                    "ov_class": ov_class,
                    "task": task,
                    "confidence": confidence,
                }),
            )
        except Exception:
            logger.debug("arch_resolver: cache write failed", exc_info=True)
    return (ov_class, task)


def as_arch_mapping_entry(
    hf_class: str,
    ov_class: str,
    task: str,
    confidence: float = 1.0,
    source: str = "llm",
) -> Dict[str, Any]:
    """Shape for PortOutcome.diagnostics['arch_mapping'] rows. Kept
    tiny; Phase 10's nightly aggregator reads these across tenants to
    promote high-observation mappings into the static allowlist."""
    return {
        "hf_class": hf_class,
        "ov_class": ov_class,
        "task": task,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "source": source,
    }


__all__ = [
    "resolve_arch",
    "as_arch_mapping_entry",
    "PROMPT_TEMPLATE_VERSION",
    "_KNOWN_OV_CLASSES",
    "_KNOWN_TASKS",
]

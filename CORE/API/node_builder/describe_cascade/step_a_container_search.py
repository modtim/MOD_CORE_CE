"""wizard-rebuild §8 step A — container search.

Probes 3 container registries for pre-built images that match the
user's description:

  - Docker Hub  — `hub.docker.com/v2/search/repositories`
  - HF Spaces   — `huggingface.co/api/spaces`
  - NGC catalog — `ngc.nvidia.com/v2/catalog/containers/search`

Each registry is independently rate-limited via
`describe_cascade.rate_limits`. Cache hits don't consume window. A
backend that's rate-limited or 5xx-errors is skipped without
failing the whole step — the other backends + later cascade steps
still run.

The LLM scores each candidate against the user's description so
the ranker can pick across registries. We DO NOT just trust the
registry's relevance ranking — those are tuned for general search
and badly match the "specific tool that does X" wizard query
shape.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

import requests

from ._types import Candidate, StepResult
from . import rate_limits
from .relevance import gate as _relevance_gate

logger = logging.getLogger(__name__)


_DOCKER_HUB_SEARCH = "https://hub.docker.com/v2/search/repositories"
_HF_SPACES_SEARCH = "https://huggingface.co/api/spaces"
_NGC_SEARCH = "https://api.ngc.nvidia.com/v2/search/catalog/resources/CONTAINER"

_REQUEST_TIMEOUT = 10  # seconds — registries vary; 10s is generous


def _docker_hub_search(query: str) -> List[Dict[str, Any]]:
    """Return the top Docker Hub hits for `query` as a list of
    dicts. Empty list on any error."""
    try:
        r = requests.get(
            _DOCKER_HUB_SEARCH,
            params={"query": query, "page_size": 10},
            timeout=_REQUEST_TIMEOUT,
        )
        if not r.ok:
            return []
        data = r.json()
        results = data.get("results") or []
        return [
            {
                "name": item.get("repo_name") or item.get("name") or "",
                "description": item.get("short_description") or "",
                "stars": int(item.get("star_count") or 0),
                "pull_count": int(item.get("pull_count") or 0),
                "is_official": bool(item.get("is_official")),
            }
            for item in results
            if item.get("repo_name") or item.get("name")
        ]
    except Exception as exc:
        logger.warning("step_a docker_hub_search failed: %s", exc)
        return []


def _hf_spaces_search(query: str) -> List[Dict[str, Any]]:
    """Top HF Spaces matching `query`. Spaces are demos/apps — useful
    when the user described an interactive tool ("a UI for stable
    diffusion")."""
    try:
        r = requests.get(
            _HF_SPACES_SEARCH,
            params={"search": query, "limit": 10},
            timeout=_REQUEST_TIMEOUT,
        )
        if not r.ok:
            return []
        # HF returns a JSON array.
        items = r.json() if r.text.strip().startswith("[") else []
        return [
            {
                "name": s.get("id") or "",
                "description": (s.get("cardData") or {}).get("title") or "",
                "likes": int(s.get("likes") or 0),
                "sdk": s.get("sdk") or "",
            }
            for s in items
            if s.get("id")
        ]
    except Exception as exc:
        logger.warning("step_a hf_spaces_search failed: %s", exc)
        return []


def _ngc_search(query: str) -> List[Dict[str, Any]]:
    """NGC catalog containers — Nvidia-curated. Tends to skew toward
    AI/ML workloads. Free to query without auth; rate-limited at
    the network level."""
    try:
        r = requests.get(
            _NGC_SEARCH,
            params={"q": query, "page": 0, "pageSize": 10},
            timeout=_REQUEST_TIMEOUT,
        )
        if not r.ok:
            return []
        data = r.json() or {}
        # NGC's response wraps under `results` → `resources`.
        results = ((data.get("results") or [{}])[0] or {}).get("resources") or []
        return [
            {
                "name": item.get("name") or "",
                "description": item.get("description") or "",
                "publisher": item.get("publisher") or "",
            }
            for item in results
            if item.get("name")
        ]
    except Exception as exc:
        logger.warning("step_a ngc_search failed: %s", exc)
        return []


def _score_against_intent(
    description: str,
    candidates: List[Dict[str, Any]],
    backend: str,
) -> List[Candidate]:
    """Score each candidate's fit for the user's description. PRIMARY signal is
    embedding similarity (local arctic-embed); falls back to the LLM ranker, then
    to popularity, only when embeddings are unavailable."""
    if not candidates:
        return []

    # PRIMARY: embedding rerank (no new model, no hallucinated scores). The
    # relevance gate still applies as a belt-and-suspenders guard.
    try:
        from node_builder.rerank import rerank_relevance
        _emb = rerank_relevance(
            description,
            [f"{c.get('name') or ''}: {c.get('description') or ''}" for c in candidates],
        )
    except Exception:  # noqa: BLE001
        _emb = None
    if _emb is not None:
        return [
            Candidate(
                kind="container",
                label=c.get("name") or "",
                submission={"wrap_base_image": c.get("name") or ""},
                confidence=_relevance_gate(
                    _emb[i] / 100.0, description, c.get("name"), c.get("description")
                ),
                backend=backend,
                eta_seconds=180,
                reason=c.get("description") or "",
            )
            for i, c in enumerate(candidates)
        ]

    try:
        from utils.llm_client import call_wizard_llm
        prompt = (
            f"User wants a Docker container that does: {description!r}\n\n"
            f"Candidate containers from {backend}:\n"
            + "\n".join(
                f"  {i+1}. {c.get('name')} — {c.get('description') or '(no description)'}"
                for i, c in enumerate(candidates)
            )
            + "\n\nFor each candidate, return a confidence score in "
              "[0.0, 1.0] indicating how well it matches the user's intent. "
              "0.0 = irrelevant, 1.0 = perfect match. Return JSON only: "
              '{"scores": [<float>, <float>, ...]}'
        )
        resp = call_wizard_llm(
            system_prompt="You are a search-result ranker. Output only JSON.",
            user_prompt=prompt,
            response_format="json",
            timeout_s=30,
            _label="cascade-rank",  # was purpose= (unsupported kwarg → ranker silently disabled)
        )
        scores = resp.get("scores") or []
        if not isinstance(scores, list) or len(scores) != len(candidates):
            raise ValueError("scores list length mismatch")
        scored = [float(min(1.0, max(0.0, s))) for s in scores]
    except Exception as exc:
        logger.info("step_a LLM scoring failed (%s); falling back to popularity", exc)
        # Popularity-based fallback — already a useful signal even
        # without an LLM round-trip.
        scored = []
        for c in candidates:
            if backend == "docker_hub":
                pulls = int(c.get("pull_count") or 0)
                stars = int(c.get("stars") or 0)
                # Crude but monotonic: official + many stars + many pulls.
                s = min(1.0, (stars / 1000.0) * 0.4
                              + (1.0 if c.get("is_official") else 0.0) * 0.3
                              + min(1.0, pulls / 1_000_000.0) * 0.3)
            elif backend == "hf_spaces":
                likes = int(c.get("likes") or 0)
                s = min(1.0, likes / 500.0)
            else:
                s = 0.5  # NGC: opaque metadata, neutral score
            scored.append(s)

    # Relevance gate — cap any candidate that shares no meaningful term with the
    # description below the short-circuit floor, so a popular-but-wrong image
    # (e.g. `tomcat` for "text to image") can't hijack the cascade before the
    # GitHub/library steps run. Applied to BOTH the LLM and popularity paths.
    return [
        Candidate(
            kind="container",
            label=c.get("name") or "",
            submission={"wrap_base_image": c.get("name") or ""},
            confidence=_relevance_gate(
                scored[i], description, c.get("name"), c.get("description")
            ),
            backend=backend,
            eta_seconds=180,  # image-mode wrapper builds fast
            reason=c.get("description") or "",
        )
        for i, c in enumerate(candidates)
    ]


def run_step_a(description: str, tenant_id: int) -> StepResult:
    """Run all 3 container-registry searches in parallel (sequential
    in v1 — easy to parallelize later) and return a unified ranked
    list."""
    result = StepResult(step="A")
    backends = [
        ("docker_hub", _docker_hub_search),
        ("hf_spaces",  _hf_spaces_search),
        ("ngc",        _ngc_search),
    ]
    for backend_name, fn in backends:
        rate = rate_limits.check_and_inc(backend_name, tenant_id)
        if not rate["allowed"]:
            result.rate_limited[backend_name] = rate
            continue
        try:
            raw = fn(description)
        except Exception as exc:
            result.errors[backend_name] = str(exc)[:200]
            # The increment already fired; we treat an error as
            # consumed (less surprising than rolling back, and
            # protects against retry storms).
            continue
        if not raw:
            # No hits — refund the increment (effectively a cache
            # miss with no work product).
            rate_limits.record_cache_hit(backend_name, tenant_id)
            continue
        scored = _score_against_intent(description, raw, backend_name)
        result.candidates.extend(scored)
    # Sort by confidence (descending), keep top 10 across all
    # backends to keep the response payload bounded.
    result.candidates.sort(key=lambda c: -c.confidence)
    result.candidates = result.candidates[:10]
    return result

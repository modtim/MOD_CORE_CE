"""wizard-rebuild §8 step B — repo search.

Wraps the existing `/describe-search` GitHub-search backend. The
endpoint already handles ranking, cache, rate-limit. We just call
it via the same code path and convert hits to cascade `Candidate`
rows. Picked candidate flows into docs-driven Git mode (§3 chunk
7 extractor + §4 variant fan-out).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ._types import Candidate, StepResult
from . import rate_limits
from .relevance import gate as _relevance_gate

logger = logging.getLogger(__name__)


def _call_describe_search(db, tenant_id: int, description: str) -> List[Dict[str, Any]]:
    """Reuse the existing /describe-search code path. The router
    function does the GitHub call + LLM ranking + cache; we just
    want its ranked candidate list."""
    try:
        # Import lazily — routers depend on FastAPI runtime modules
        # that we don't want to pull in at startup.
        from routers.node_definitions import (
            _describe_search_internal,  # may not exist; fall through
        )
        return _describe_search_internal(db, tenant_id, description) or []
    except ImportError:
        pass
    # Fallback path: use the canonical github_search helper with the SHARED
    # platform token resolver. This unifies the token source (the `github_token`
    # secret → GITHUB_PLATFORM_TOKEN/GITHUB_TOKEN env, via utils.github_token)
    # with the /describe-search endpoint, and — critically — STOPS silently
    # swallowing failures: the old code did `if not r.ok: return []`, so a
    # rate-limit (unauthenticated search = 10/min) or a missing token looked
    # identical to "no results found". Now each failure mode is logged distinctly.
    try:
        from utils.github_search import (
            search_repos, RateLimited, NotConfigured, GitHubError,
        )
        from utils.github_token import get_platform_github_token
        token = get_platform_github_token(db)
        results = search_repos(description, token=token, max_n=10)
        return [
            {
                "name": c.full_name or "",
                "url": c.html_url or "",
                "description": c.description or "",
                "stars": int(c.stars or 0),
                "language": c.primary_language or "",
            }
            for c in results
            if c.full_name
        ]
    except NotConfigured:
        logger.warning(
            "describe repo search: no platform GitHub token configured — GitHub "
            "search skipped. Set `github_token` (Settings → Secrets) or the "
            "GITHUB_PLATFORM_TOKEN env. Unauthenticated search is only 10/min, "
            "so it rate-limits almost immediately."
        )
        return []
    except RateLimited:
        logger.warning(
            "describe repo search: GitHub RATE-LIMITED. Configure a platform "
            "`github_token` to raise the limit (authenticated search = 30/min "
            "vs 10/min unauthenticated). No candidates returned this attempt."
        )
        return []
    except GitHubError as exc:
        logger.warning("describe repo search: GitHub error: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001 — never let the cascade die here
        logger.warning("describe repo search failed: %s", exc)
        return []


def _score_against_intent(
    description: str, candidates: List[Dict[str, Any]],
) -> List[Candidate]:
    if not candidates:
        return []

    # PRIMARY: embedding rerank (local arctic-embed) — a real semantic signal,
    # no hallucinated LLM score, no star-count-as-relevance. Falls through to the
    # LLM/star path below only when embeddings are unavailable. The relevance
    # gate still applies as a guard.
    try:
        from node_builder.rerank import rerank_relevance
        _emb = rerank_relevance(
            description,
            [f"{c.get('name') or ''}: {c.get('description') or ''} ({c.get('language') or ''})"
             for c in candidates],
        )
    except Exception:  # noqa: BLE001
        _emb = None
    if _emb is not None:
        return [
            Candidate(
                kind="repo",
                label=c.get("name") or "",
                submission={"source_repo_url": c.get("url") or ""},
                confidence=_relevance_gate(
                    _emb[i] / 100.0, description,
                    c.get("name"), c.get("description"), c.get("language"),
                ),
                backend="github",
                eta_seconds=420,
                reason=c.get("description") or "",
            )
            for i, c in enumerate(candidates)
        ]

    # Same LLM scoring shape as step A; on failure, star-count
    # fallback.
    try:
        from utils.llm_client import call_wizard_llm
        prompt = (
            f"User wants a GitHub repo that does: {description!r}\n\n"
            "Candidates:\n"
            + "\n".join(
                f"  {i+1}. {c.get('name')} ({c.get('language')}, "
                f"{c.get('stars')}★) — {c.get('description') or '(no desc)'}"
                for i, c in enumerate(candidates)
            )
            + "\n\nReturn JSON only with relevance scores in [0,1]: "
              '{"scores": [<float>, ...]}'
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
            raise ValueError("score length mismatch")
        scored = [float(min(1.0, max(0.0, s))) for s in scores]
    except Exception:
        scored = [
            min(1.0, int(c.get("stars") or 0) / 5000.0)
            for c in candidates
        ]
    # Same relevance gate as step A — a popular-but-off-topic repo can't clear
    # the floor on stars or a hallucinated LLM score. GitHub search already
    # returns topically-relevant repos, so this mostly guards the star-count
    # fallback; matches on name, description, or primary language.
    return [
        Candidate(
            kind="repo",
            label=c.get("name") or "",
            submission={"source_repo_url": c.get("url") or ""},
            confidence=_relevance_gate(
                scored[i], description,
                c.get("name"), c.get("description"), c.get("language"),
            ),
            backend="github",
            eta_seconds=420,  # git-mode builds clone + image-build
            reason=c.get("description") or "",
        )
        for i, c in enumerate(candidates)
    ]


def run_step_b(db, description: str, tenant_id: int) -> StepResult:
    """GitHub repo search. Single backend; rate-limit gate +
    LLM scoring."""
    result = StepResult(step="B")
    rate = rate_limits.check_and_inc("github", tenant_id)
    if not rate["allowed"]:
        result.rate_limited["github"] = rate
        return result
    try:
        raw = _call_describe_search(db, tenant_id, description)
    except Exception as exc:
        result.errors["github"] = str(exc)[:200]
        return result
    if not raw:
        rate_limits.record_cache_hit("github", tenant_id)
        return result
    result.candidates = _score_against_intent(description, raw)
    result.candidates.sort(key=lambda c: -c.confidence)
    result.candidates = result.candidates[:10]
    return result

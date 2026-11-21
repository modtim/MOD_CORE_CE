"""δ Phase 5 — score GitHub candidates against the user's
description.

Phase 2's ``search_repos`` returns up to ten plausibly-relevant
repos by stars-desc. That ordering is a poor proxy for "which one
is best for the user's actual task" (a 50k-star general-purpose
library may rank above a 200-star purpose-built solution).

This module asks the LLM to assign each candidate a relevance
score 0-100 and a one-sentence ``fit_reason``, then layers
deterministic risk flags on top: ``stale`` (last pushed > 2y ago),
``tiny`` (<5 stars), and a passthrough of the safety warnings
that ``repo_safety`` already produced.

Output: input candidates with three new fields. Caller (Phase 6)
sorts on ``relevance`` desc and renders the top N.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from utils.llm_client import LLMClientError, call_wizard_llm

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You score GitHub repositories for fit against a
user's task description.

You'll receive a JSON object with `description` (the user's
request) and `candidates` (a list of repos with full_name,
description, stars, primary_language).

For EACH candidate, return:
- `relevance`: integer 0-100 (100 = perfect fit, 0 = irrelevant)
- `fit_reason`: ONE concise sentence explaining the score

CRITICAL — direction + domain check. Before scoring relevance,
verify that the repo:
  1. Takes the SAME KIND OF INPUT the user described (PDF, image,
     video, mesh, JSON, text, etc.).
  2. Produces the SAME KIND OF OUTPUT the user described.
  3. Operates in the SAME DOMAIN (e.g. "PDF documents" vs "CAD
     drawings" are NOT the same — both are documents but the
     content semantics are completely different).

If the direction is REVERSED (the user wants A→B but the repo does
B→A), or if the domain is wrong (user wants PDF→PNG, repo does
CAD→PDF), the score MUST be below 30 regardless of keyword overlap
with the description. Mention the mismatch explicitly in fit_reason.

After the direction + domain check passes, score on actual fit:
- A 5k-star repo that mentions similar keywords but solves a
  different problem → below 50.
- A purpose-built 200-star repo that exactly matches the task →
  above 80.

Output a single JSON array, one object per candidate, in the same
order as the input list:

[{"full_name": "owner/repo", "relevance": 85, "fit_reason": "..."}]

No prose, no markdown fences.
"""


@dataclass
class ScoredCandidate:
    full_name: str
    html_url: str
    description: str
    license_spdx: Optional[str]
    stars: int
    last_pushed_iso: str
    primary_language: Optional[str]
    relevance: int
    fit_reason: str
    risk_flags: List[str] = field(default_factory=list)
    safety_warnings: List[str] = field(default_factory=list)
    safety_ok: bool = True
    # Composite of LLM relevance + log-scaled GitHub stars. Used as
    # the sort key; surfaced so the UI can show "why is this ranked
    # higher than that one." 0-100 like relevance itself.
    composite_score: int = 0
    # True for the single best match when its composite is meaningfully
    # ahead of #2 AND has decent absolute quality. The wizard renders
    # this candidate as a prominent "best match" card.
    is_recommended: bool = False
    # How the synthesize step should install this repo into a Docker
    # image. One of "pip" / "requirements" / "dockerfile" / "clone".
    # Set by github_search.detect_install_strategy in the route handler
    # before scoring. Empty string when unset (e.g. tests that bypass
    # the route).
    install_strategy: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "full_name": self.full_name,
            "html_url": self.html_url,
            "description": self.description,
            "license_spdx": self.license_spdx,
            "stars": self.stars,
            "last_pushed_iso": self.last_pushed_iso,
            "primary_language": self.primary_language,
            "relevance": self.relevance,
            "fit_reason": self.fit_reason,
            "risk_flags": list(self.risk_flags),
            "safety_warnings": list(self.safety_warnings),
            "safety_ok": self.safety_ok,
            "composite_score": self.composite_score,
            "is_recommended": self.is_recommended,
            "install_strategy": self.install_strategy,
        }


def _deterministic_flags(c: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    if (c.get("stars") or 0) < 5:
        flags.append("tiny")
    last_pushed = c.get("last_pushed_iso") or ""
    if last_pushed and not (
        last_pushed.startswith("2024")
        or last_pushed.startswith("2025")
        or last_pushed.startswith("2026")
    ):
        flags.append("stale")
    return flags


def _zero_score_for(c: Dict[str, Any], reason: str) -> ScoredCandidate:
    return ScoredCandidate(
        full_name=c.get("full_name") or "",
        html_url=c.get("html_url") or "",
        description=c.get("description") or "",
        license_spdx=c.get("license_spdx"),
        stars=int(c.get("stars") or 0),
        last_pushed_iso=c.get("last_pushed_iso") or "",
        primary_language=c.get("primary_language"),
        relevance=0,
        fit_reason=reason,
        risk_flags=_deterministic_flags(c),
        safety_warnings=list(c.get("safety_warnings") or []),
        safety_ok=bool(c.get("safety_ok", True)),
        install_strategy=str(c.get("install_strategy") or ""),
    )


def score_candidates(
    description: str,
    candidates: List[Dict[str, Any]],
    *,
    llm: Optional[Callable[..., Any]] = None,
) -> List[ScoredCandidate]:
    """Return scored candidates in the input order.

    Each candidate dict should carry ``full_name``, ``html_url``,
    ``description``, ``license_spdx``, ``stars``,
    ``last_pushed_iso``, ``primary_language``, plus optional
    ``safety_ok`` / ``safety_warnings`` from repo_safety.

    ``llm`` defaults to ``call_wizard_llm``. On any failure, every
    candidate gets relevance=0 with an explanatory fit_reason so
    the wizard still has something to render — it just won't be
    able to rank them.
    """
    if not candidates:
        return []

    # PRIMARY ranker — embedding similarity (the wizard's local arctic-embed-l,
    # no new model). Replaces the LLM relevance guess, which hallucinated (a
    # popular-but-wrong repo scored ~1.0) and conflated fame with fit. Only when
    # no explicit `llm` is injected (i.e. production; tests inject a stub ranker
    # to exercise the LLM path) and only when embeddings are actually available;
    # otherwise fall through to the LLM scorer below. The composite/gates
    # downstream operate on `relevance` unchanged, so nothing else moves.
    if llm is None:
        try:
            from node_builder.rerank import rerank_relevance
            docs = [
                f"{c.get('full_name') or ''}: {(c.get('description') or '')[:300]}"
                for c in candidates
            ]
            emb = rerank_relevance(description, docs)
        except Exception:  # noqa: BLE001
            emb = None
        if emb is not None:
            logger.info("describe-score: ranked %d candidate(s) by embedding "
                        "similarity (arctic-embed)", len(candidates))
            return [
                ScoredCandidate(
                    full_name=c.get("full_name") or "",
                    html_url=c.get("html_url") or "",
                    description=c.get("description") or "",
                    license_spdx=c.get("license_spdx"),
                    stars=int(c.get("stars") or 0),
                    last_pushed_iso=c.get("last_pushed_iso") or "",
                    primary_language=c.get("primary_language"),
                    relevance=rel,
                    fit_reason=f"Semantic match to your description ({rel}%).",
                    risk_flags=_deterministic_flags(c),
                    safety_warnings=list(c.get("safety_warnings") or []),
                    safety_ok=bool(c.get("safety_ok", True)),
                    install_strategy=str(c.get("install_strategy") or ""),
                )
                for c, rel in zip(candidates, emb)
            ]

    if llm is None:
        llm = call_wizard_llm

    # devstral-small-2 generates at ~30-40 tok/s on the stage A6000;
    # scoring 24 candidates with a fit_reason each requires ~700-900
    # output tokens, which routinely tipped over the prior 30s
    # timeout. Cap LLM-scoring to the top-N by stars (proxy for
    # relevance prior) — the remaining candidates still appear in
    # the results, just unranked at the bottom. Combined with the
    # 60s timeout below this keeps the request well within budget
    # AND avoids burning tokens on long-tail candidates that the
    # composite-with-stars step would have ranked low anyway.
    SCORED_TOP_N = 12
    candidates_for_llm = candidates[:SCORED_TOP_N]
    unranked_tail = candidates[SCORED_TOP_N:]

    user_prompt = json.dumps({
        "description": description,
        "candidates": [
            {
                "full_name": c.get("full_name"),
                "description": (c.get("description") or "")[:300],
                "stars": c.get("stars") or 0,
                "primary_language": c.get("primary_language"),
            }
            for c in candidates_for_llm
        ],
    }, separators=(",", ":"))

    try:
        parsed = llm(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=60,
        )
    except LLMClientError as e:
        logger.info("describe-score LLM unavailable, zeroing scores: %s", e)
        return [
            _zero_score_for(c, "Scoring service unavailable; pick the best candidate by hand.")
            for c in candidates
        ]
    except Exception as e:
        logger.warning("describe-score LLM raised: %s", e)
        return [
            _zero_score_for(c, "Scoring service raised an error.")
            for c in candidates
        ]

    # vLLM/Anthropic in JSON-object mode tend to wrap a list inside
    # an object key. Be liberal in what we accept.
    if isinstance(parsed, dict):
        for v in parsed.values():
            if isinstance(v, list):
                parsed = v
                break
    if not isinstance(parsed, list):
        logger.warning("describe-score LLM returned non-list: %r", type(parsed))
        return [
            _zero_score_for(c, "Scoring service returned unexpected shape.")
            for c in candidates
        ]

    # Index by full_name so we tolerate the LLM reordering / dropping.
    by_name: Dict[str, Dict[str, Any]] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        name = entry.get("full_name")
        if isinstance(name, str):
            by_name[name] = entry

    out: List[ScoredCandidate] = []
    for c in candidates:
        name = c.get("full_name") or ""
        scored = by_name.get(name) or {}
        relevance_raw = scored.get("relevance")
        try:
            relevance = max(0, min(100, int(relevance_raw)))
        except (TypeError, ValueError):
            relevance = 0
        fit_reason = scored.get("fit_reason") or ""
        if not isinstance(fit_reason, str):
            fit_reason = ""
        fit_reason = fit_reason.strip()[:300]
        # Distinguish "LLM scored this and produced no rationale" from
        # "LLM never saw this candidate" (the unranked tail beyond
        # SCORED_TOP_N). The latter gets a clearer message so the user
        # knows it's intentionally unranked, not a service failure.
        if not fit_reason:
            if name in by_name:
                fit_reason = "(no rationale provided)"
            else:
                fit_reason = "Lower-priority match; not auto-scored. Browse below if the top results don't fit."
        out.append(ScoredCandidate(
            full_name=name,
            html_url=c.get("html_url") or "",
            description=c.get("description") or "",
            license_spdx=c.get("license_spdx"),
            stars=int(c.get("stars") or 0),
            last_pushed_iso=c.get("last_pushed_iso") or "",
            primary_language=c.get("primary_language"),
            relevance=relevance,
            fit_reason=fit_reason,
            risk_flags=_deterministic_flags(c),
            safety_warnings=list(c.get("safety_warnings") or []),
            safety_ok=bool(c.get("safety_ok", True)),
            install_strategy=str(c.get("install_strategy") or ""),
        ))
    return out


# ───────────────────────────────────────────────────────────────────
# Composite scoring + recommendation flag.
# ───────────────────────────────────────────────────────────────────

# Weights for the composite score. Relevance (the arctic-embed reranker) is the
# PRIMARY signal; stars are only a light tiebreaker among similarly-relevant
# repos — never a way for FAME to beat FIT. At 0.7/0.3 the log-scaled star term
# contributed up to 30 points, enough to float a 0%-relevance mega-repo
# (huggingface/diffusers, 34k★) above a real match (repro 2026-09-03: "grayscale"
# ranked diffusers first). At 0.9/0.1 stars can only shift a repo by <=10 points,
# so any candidate with a ~12+ relevance lead wins on fit regardless of stars.
# Sum is 1.0.
_RELEVANCE_WEIGHT = 0.9
_STARS_WEIGHT     = 0.1

# Primary languages that make a repo a poor base for a containerised compute
# node (web-frontend / template / server-page ecosystems). Their composite is
# halved so a well-described PHP/web lib can't outrank a Python/C/C++/CUDA fit.
_POOR_NODE_LANGUAGES = {
    "php", "html", "css", "scss", "less", "vue", "svelte", "blade", "twig",
    "hack", "asp", "coldfusion",
}

# Thresholds for flagging a single candidate as RECOMMENDED. We only
# mark #1 as recommended when:
#   - composite is meaningfully ahead of #2 (Δ ≥ this many points), AND
#   - absolute relevance is at least this much (we don't recommend
#     "least bad of a bunch of bad")
_RECOMMEND_LEAD_THRESHOLD     = 8
_RECOMMEND_MIN_RELEVANCE      = 60
_RECOMMEND_MIN_COMPOSITE      = 55

# Below this best-candidate relevance, NONE of the results are worth
# showing — the user described a task (e.g. "convert pdf to png") that's
# a few-line script, and the search only surfaced big repos that share
# keywords but don't actually do it. The wizard collapses to "build
# fresh" instead of rendering a wall of repos it has already judged to
# be non-matches. Distinct from _RECOMMEND_MIN_RELEVANCE (60), which
# only governs the prominent "best match" card; between 40 and 60 we
# still show the cards (a borderline-useful repo the user may want),
# we just don't crown one.
_WORTH_SHOWING_RELEVANCE      = 40

# Relevance floor applied by describe-search to a repo named by the curated
# crossover table, a learned crossover, or the user's own tool answer — the
# human-vetted canonical tool for a task family, which a sparse GitHub
# description can otherwise let the embedding ranker score below a generic
# keyword hit. Kept below a genuine strong cosine match (~80+) so a real
# top hit still leads, but comfortably above the worth-showing floor.
_CROSSOVER_RELEVANCE_FLOOR    = 72


def _stars_score(stars: int) -> int:
    """Map raw GitHub star count to a 0-100 score. Log-scaled because
    star distributions span 4+ orders of magnitude across the GitHub
    universe — a linear scale would make every popular repo tie at 100
    and never let a fresh-but-perfect-fit repo win on relevance.

      0 stars    →  0
      10 stars   →  ~25
      100 stars  →  ~50
      1000 stars →  ~75
      10000+     →  100 (clamped)
    """
    if stars <= 0:
        return 0
    return min(100, int(round(math.log10(stars + 1) * 25)))


def apply_composite_and_recommendation(
    scored: List[ScoredCandidate],
) -> List[ScoredCandidate]:
    """In-place compute composite_score on each candidate, sort the
    list by it (desc), then flag the top candidate as is_recommended
    when it has a meaningful lead AND decent absolute quality.

    The wizard's chat shows the recommended candidate in a prominent
    "best match" card above the regular candidate cards. If no row
    qualifies, no recommendation is shown — better silent than
    "least bad."
    """
    for c in scored:
        # Both 0-100. Weighted blend rounded to int.
        c.composite_score = int(round(
            _RELEVANCE_WEIGHT * c.relevance
            + _STARS_WEIGHT * _stars_score(c.stars)
        ))
        # Ecosystem demotion — a node is a containerised wrapper (Python entry,
        # any CLI/binary underneath), so a web-template/PHP repo is a poor base
        # for a compute node however well its description matches. Halve its
        # composite so it can't lead a Python/C/C++/CUDA fit (repro 2026-09-03:
        # "grayscale" floated Intervention/image, a PHP lib, to #1). Keeps it in
        # the list — the user can still pick it — just not crowned.
        _lang = (c.primary_language or "").strip().lower()
        if _lang in _POOR_NODE_LANGUAGES:
            c.composite_score = int(round(c.composite_score * 0.5))

    scored.sort(key=lambda s: s.composite_score, reverse=True)

    if not scored:
        return scored

    top = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None
    lead = top.composite_score - (runner_up.composite_score if runner_up else 0)

    qualifies = (
        top.composite_score >= _RECOMMEND_MIN_COMPOSITE
        and top.relevance >= _RECOMMEND_MIN_RELEVANCE
        and (runner_up is None or lead >= _RECOMMEND_LEAD_THRESHOLD)
        and top.safety_ok
    )
    top.is_recommended = bool(qualifies)
    return scored


def should_recommend_build_fresh(scored: List[ScoredCandidate]) -> bool:
    """True when the search produced candidates but none are worth
    showing (best relevance < _WORTH_SHOWING_RELEVANCE) — i.e. the task
    is trivial / standard-library-tier and the repo results are just
    keyword noise. The wizard then skips the candidate wall and goes
    straight to "build fresh".

    Returns False for an empty list — that case (from_scratch intent,
    not-configured, rate-limited) is already handled upstream and has
    its own build-fresh path.
    """
    if not scored:
        return False
    return max(c.relevance for c in scored) < _WORTH_SHOWING_RELEVANCE

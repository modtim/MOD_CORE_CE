"""Embedding reranker — semantic relevance for wizard search candidates.

Replaces the weak-LLM 0-100 relevance guess (which hallucinated — "tomcat" 1.0
for "text to image") and the popularity fallback (fame, not fit) with cosine
similarity from the wizard's OWN local `snowflake-arctic-embed-l` model (no new
model, runs in-process on CPU, already loaded for exemplar retrieval).

The user's description and each candidate's text are embedded and ranked by
cosine. MEASURED, not assumed: arctic-embed's asymmetric "query" prompt was
tested and it HURT here (it collapsed canonical repos to ~= junk), so BOTH sides
are embedded plain — see the _COS_LO/_COS_HI note. A real semantic signal that
works for ANY prompt, not just curated ones, and does not hallucinate.

Honest limits (see the wizard-discovery memory):
  * it RERANKS the pool the keyword search fetched — it can't promote a repo that
    was never fetched;
  * arctic-embed runs "hot" (fits cluster ~0.75-0.93, noise ~0.63-0.68), so the
    absolute margins are thin — a good RANK signal, a coarse absolute one;
  * relevance != quality/direction/buildability — pair with structural filters.
"""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# Cosine → 0-100 mapping window, calibrated by MEASUREMENT (not theory) against
# the discovery batch's real candidates. Finding: arctic-embed-l's asymmetric
# "query" prompt HURT here — it collapsed a canonical repo's score to ~= junk
# (whisper 0.334 vs junk 0.307). PLAIN embedding on BOTH sides discriminated
# correctly (whisper 0.765 > junk 0.663; colmap 0.867 > numpy 0.810; argos 0.771
# > crypto 0.679). So the reranker embeds both sides plain. Plain cosines cluster
# HIGH (a real fit ~0.75-0.93, keyword-adjacent noise ~0.63-0.68), so the window
# is set high: [0.62, 0.90] → [0,100], clamped, so most noise lands below the
# describe_score gate (_WORTH_SHOWING=40) and a genuine fit clears it. Margins
# are thin (arctic-embed runs hot), so this is a good RANK signal but a coarse
# absolute one — re-tune if the model pin changes.
_COS_LO = 0.62
_COS_HI = 0.90


def _cosine(a: List[float], b: List[float]) -> float:
    # Both vectors are unit-normalised by the embedding client, so the dot
    # product IS the cosine. Guard against a length/None mismatch.
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


def _to_100(cos: float) -> int:
    span = _COS_HI - _COS_LO
    if span <= 0:
        return 0
    return max(0, min(100, int(round((cos - _COS_LO) / span * 100))))


def rerank_relevance(description: str, docs: List[str]) -> Optional[List[int]]:
    """Return a 0-100 semantic-relevance score for each `docs[i]` vs
    `description`, or None if embedding is unavailable (caller keeps its own
    scorer). `docs[i]` should be the candidate's most descriptive text
    (name + description works well). Never raises."""
    if not description or not docs:
        return None
    try:
        from services import wizard_pro_client as _pro
        qvec = _pro.embed(description)  # plain both sides — measured better than the query prompt
        dvecs = _pro.embed_batch([d or "" for d in docs])
    except Exception as e:  # noqa: BLE001 — reranker must never break ranking
        logger.info("embedding rerank unavailable (%s); caller keeps its score", e)
        return None
    if qvec is None or dvecs is None or len(dvecs) != len(docs):
        return None
    return [_to_100(_cosine(qvec, d)) for d in dvecs]


def rerank_scores_unit(description: str, docs: List[str]) -> Optional[List[float]]:
    """Like `rerank_relevance` but returns the raw cosine in ~[0,1] for callers
    (e.g. the cascade) that rank on a 0-1 confidence rather than 0-100."""
    if not description or not docs:
        return None
    try:
        from services import wizard_pro_client as _pro
        qvec = _pro.embed(description)  # plain both sides — measured better than the query prompt
        dvecs = _pro.embed_batch([d or "" for d in docs])
    except Exception as e:  # noqa: BLE001
        logger.info("embedding rerank unavailable (%s); caller keeps its score", e)
        return None
    if qvec is None or dvecs is None or len(dvecs) != len(docs):
        return None
    return [round(max(0.0, _cosine(qvec, d)), 4) for d in dvecs]

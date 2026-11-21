"""wizard-rebuild §8 — lexical relevance gate for cascade candidate scoring.

The container (step A) and repo (step B) search steps rank hits two ways, and
BOTH can score an irrelevant result near 1.0:

  * the wizard LLM (a small local model) is asked to rate relevance and
    hallucinates high scores, and
  * the popularity fallback (Docker Hub pulls/stars, GitHub stars) measures
    FAME, not fit.

So "text to image" returned Docker Hub's `tomcat` (a Java servlet server) at
confidence 1.0 — official image, billions of pulls — which tripped the cascade's
0.7 short-circuit at step A and skipped the GitHub + library steps entirely. The
wizard never reached GitHub because a popular-but-wrong container looked perfect.

This gate caps the confidence of a candidate that shares NO meaningful term with
the user's description, so neither popularity nor a hallucinated LLM score can
push junk above the short-circuit floor. It only ever DEMOTES a zero-overlap
candidate — it never promotes — so a genuinely relevant hit keeps its earned
score. Lexical overlap misses pure synonyms (a "stable-diffusion" repo has no
"text"/"image" token), which is why it is paired with the cascade change that
always runs the GitHub step regardless: the gate stops junk from winning, and
always-run-GitHub guarantees the real source is still consulted.
"""
from __future__ import annotations

import re
from typing import Optional

# Confidence ceiling for a candidate with no lexical overlap with the query.
# MUST sit below the cascade's short-circuit floor (DEFAULT_CONFIDENCE_FLOOR =
# 0.7) so an irrelevant hit can neither short-circuit the cascade nor outrank a
# relevant candidate in the final merge.
IRRELEVANT_CAP = 0.4

_WORD_RE = re.compile(r"[a-z0-9]+")

# ONLY grammatical function words + first-person/imperative filler. Kept
# deliberately minimal: the danger direction is stripping a query's real term
# (which would wrongly DEMOTE a relevant candidate), so domain nouns and verbs
# (image, text, pdf, video, audio, mesh, resize, convert, render, …) are all
# KEPT. Being permissive is safe — the gate only ever caps ZERO-overlap hits, so
# an over-broad match just means we trust the upstream score, as we do today.
_STOPWORDS = {
    "a", "an", "the", "to", "of", "for", "and", "or", "with", "from", "into",
    "in", "on", "onto", "that", "this", "these", "those", "it", "its", "is",
    "are", "be", "am", "was", "were", "as", "by", "at", "i", "we", "you", "my",
    "our", "your", "want", "wants", "wanted", "need", "needs", "make", "makes",
    "build", "create", "please", "would", "like", "some", "any", "then",
}


def _terms(text: Optional[str]) -> set:
    """Meaningful lowercase tokens: alphanumerics of length >= 2 that are not
    grammatical/imperative stopwords. Domain nouns and verbs are kept."""
    return {
        t for t in _WORD_RE.findall((text or "").lower())
        if len(t) >= 2 and t not in _STOPWORDS
    }


def is_relevant(description: str, *candidate_texts: Optional[str]) -> bool:
    """True if the candidate shares at least one meaningful term with the query.

    Fail-open: when the description yields no meaningful terms at all (e.g. a
    one-word prompt that is entirely boilerplate), returns True — there is
    nothing to gate on, and demoting everything would be worse than trusting the
    upstream score."""
    q = _terms(description)
    if not q:
        return True
    cand: set = set()
    for t in candidate_texts:
        cand |= _terms(t)
    return bool(q & cand)


def gate(confidence: float, description: str, *candidate_texts: Optional[str]) -> float:
    """Return `confidence` unchanged when the candidate is lexically relevant to
    the description, else cap it at ``IRRELEVANT_CAP`` (below the short-circuit
    floor). Never raises; never increases a score."""
    try:
        if is_relevant(description, *candidate_texts):
            return confidence
        return min(confidence, IRRELEVANT_CAP)
    except Exception:  # noqa: BLE001 — a gate fault must not break ranking
        return confidence

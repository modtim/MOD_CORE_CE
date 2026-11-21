"""per-output-type
similarity checkers.

Three self-contained, offline checkers that answer "is this candidate output
close enough to the reference output?" for the three output families the
wizard/agentic loop can produce:

  - TextSimilarityChecker      — BLEU (1..4-gram, brevity-penalised) blended
                                 with word-bag cosine; threshold default 0.75
  - ImageSimilarityChecker     — cosine over embedding vectors; threshold
                                 default 0.80
  - StructuredOutputChecker    — schema (key-set) match + per-field value
                                 match; threshold default 0.90 (i.e. 90%)

Every checker is a pure function/class over the inputs you hand it: NO
network, NO model calls, NO DB. Where an embedding is needed, the caller
supplies a precomputed vector, or injects an `embedding_fn` callable — the
module never resolves one itself, so it has no hard runtime dependency and
is unit-testable offline.

Thresholds are function parameters with the defaults above; making them
tunable at runtime is a follow-up (platform-settings registry), not a new
env var.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

DEFAULT_TEXT_THRESHOLD = 0.75
DEFAULT_IMAGE_THRESHOLD = 0.8
DEFAULT_STRUCTURED_MATCH_THRESHOLD = 0.9

__all__ = [
    "SimilarityResult",
    "TextSimilarityChecker",
    "ImageSimilarityChecker",
    "StructuredOutputChecker",
    "DEFAULT_TEXT_THRESHOLD",
    "DEFAULT_IMAGE_THRESHOLD",
    "DEFAULT_STRUCTURED_MATCH_THRESHOLD",
]


@dataclass
class SimilarityResult:
    """Uniform verdict envelope shared by all three checkers.

    `score` is always normalised to 0..1 so it is directly comparable to
    `threshold` (the structured threshold is expressed as a fraction too:
    0.9 == 90% of fields matched).
    """
    score: float
    threshold: float
    passed: bool
    details: Dict[str, Any]


def _clamp01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 when degenerate."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return _clamp01(dot / (math.sqrt(norm_a) * math.sqrt(norm_b)))


def _tokenize(text: str) -> List[str]:
    """Lowercased word tokens — deterministic, no NLTK, no corpora."""
    out: List[str] = []
    buf: List[str] = []
    for ch in text:
        if ch.isalnum():
            buf.append(ch)
            continue
        if buf:
            out.append("".join(buf).lower())
            buf = []
    if buf:
        out.append("".join(buf).lower())
    return out


def _ngrams(tokens: Sequence[str], n: int) -> List[str]:
    if len(tokens) < n:
        return []
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def _bleu_precision(candidate: str, reference: str, max_n: int = 4) -> float:
    """Clipped n-gram precision averaged over orders 1..max_n (BLEU core).

    Order-0 fallback: when the candidate is shorter than every n we still
    want a signal, so we fall back to unigram overlap.
    """
    cand_tokens = _tokenize(candidate)
    ref_tokens = _tokenize(reference)
    if not cand_tokens or not ref_tokens:
        return 0.0
    precisions: List[float] = []
    for n in range(1, max_n + 1):
        cand_grams = _ngrams(cand_tokens, n)
        if not cand_grams:
            continue
        ref_counts: Dict[str, int] = {}
        for g in _ngrams(ref_tokens, n):
            ref_counts[g] = ref_counts.get(g, 0) + 1
        clipped = 0
        for g in cand_grams:
            avail = ref_counts.get(g, 0)
            if avail > 0:
                clipped += 1
                ref_counts[g] = avail - 1
        precisions.append(clipped / len(cand_grams))
    if not precisions:
        return 0.0
    return sum(precisions) / len(precisions)


class TextSimilarityChecker:
    """BLEU + word-bag cosine over two strings.

    `score` = 0.6 * BLEU-style precision (with brevity penalty) + 0.4 *
    token-bag cosine. Both are deterministic and offline. Threshold default
    0.75.
    """
    THRESHOLD = DEFAULT_TEXT_THRESHOLD

    def check(
        self,
        candidate: Any,
        reference: Any,
        *,
        threshold: Optional[float] = None,
    ) -> SimilarityResult:
        thr = self.THRESHOLD if threshold is None else float(threshold)
        cand_text = "" if candidate is None else str(candidate)
        ref_text = "" if reference is None else str(reference)

        cand_tokens = _tokenize(cand_text)
        ref_tokens = _tokenize(ref_text)
        if not cand_tokens or not ref_tokens:
            exact = bool(cand_text.strip() == ref_text.strip()) and bool(ref_text.strip())
            score = 1.0 if exact else 0.0
        else:
            precision = _bleu_precision(cand_text, ref_text)
            # Brevity penalty: short candidates lose credit.
            bp = 1.0
            if len(cand_tokens) < len(ref_tokens):
                bp = math.exp(1.0 - len(ref_tokens) / len(cand_tokens))
            bleu = bp * precision
            bag = _cosine(
                [float(cand_tokens.count(t)) for t in sorted(set(cand_tokens) | set(ref_tokens))],
                [float(ref_tokens.count(t)) for t in sorted(set(cand_tokens) | set(ref_tokens))],
            )
            score = _clamp01(0.6 * bleu + 0.4 * bag)

        return SimilarityResult(
            score=score,
            threshold=thr,
            passed=score >= thr,
            details={
                "candidate_tokens": len(cand_tokens),
                "reference_tokens": len(ref_tokens),
                "method": "bleu+cosine",
            },
        )


class ImageSimilarityChecker:
    """Cosine similarity over image embedding vectors.

    The embedding is NEVER computed here — the caller either passes
    precomputed vectors (`candidate_embedding` / `reference_embedding`) or
    injects an `embedding_fn` that maps an image payload to a vector. With
    no `embedding_fn` and no vectors, the check fails (score 0.0) rather
    than reaching for a model. Threshold default 0.8.
    """
    THRESHOLD = DEFAULT_IMAGE_THRESHOLD

    def check(
        self,
        candidate: Any,
        reference: Any,
        *,
        threshold: Optional[float] = None,
        candidate_embedding: Optional[Sequence[float]] = None,
        reference_embedding: Optional[Sequence[float]] = None,
        embedding_fn: Optional[Callable[[Any], Sequence[float]]] = None,
    ) -> SimilarityResult:
        thr = self.THRESHOLD if threshold is None else float(threshold)
        cand_vec = candidate_embedding
        ref_vec = reference_embedding
        embedded = False
        if embedding_fn is not None:
            if cand_vec is None:
                cand_vec = embedding_fn(candidate)
                embedded = True
            if ref_vec is None:
                ref_vec = embedding_fn(reference)
                embedded = True

        if cand_vec is None or ref_vec is None:
            return SimilarityResult(
                score=0.0,
                threshold=thr,
                passed=False,
                details={
                    "reason": "no embeddings supplied and no embedding_fn injected",
                    "embedded": embedded,
                },
            )

        score = _cosine(_as_floats(cand_vec), _as_floats(ref_vec))
        return SimilarityResult(
            score=score,
            threshold=thr,
            passed=score >= thr,
            details={
                "method": "embedding-cosine",
                "dimensions": len(_as_floats(cand_vec)),
                "embedded": embedded,
            },
        )


def _as_floats(vec: Any) -> List[float]:
    out: List[float] = []
    if vec is None:
        return out
    try:
        items = list(vec)
    except TypeError:
        return out
    for x in items:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


_MAX_FLATTEN_ENTRIES = 4096


def _flatten(
    obj: Any,
    prefix: str = "",
    depth: int = 0,
    _budget: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Flatten nested dict/list structures to leaf paths -> scalar values.

    Lists contribute both their length (under `<path>#len`) and their
    indexed leaves, so a shape mismatch shows up as field mismatches.

    Bounded on two axes so a hostile/huge payload can't blow up memory or
    recursion: `depth` caps nesting at 12 (also terminates cyclic/self
    -referencing structures, since depth still increases every recursive
    step even when the same object reappears), and `_budget` caps the
    total number of flattened entries at `_MAX_FLATTEN_ENTRIES` — once
    exhausted, traversal simply stops early rather than keep allocating.
    """
    if _budget is None:
        _budget = [_MAX_FLATTEN_ENTRIES]
    out: Dict[str, Any] = {}
    if depth > 12 or _budget[0] <= 0:
        out[prefix or "<root>"] = obj
        _budget[0] -= 1
        return out
    if isinstance(obj, dict):
        if not obj and prefix:
            out[prefix] = {}
            _budget[0] -= 1
            return out
        for k, v in obj.items():
            if _budget[0] <= 0:
                break
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, key, depth + 1, _budget))
        return out
    if isinstance(obj, (list, tuple)):
        if prefix:
            out[f"{prefix}#len"] = len(obj)
            _budget[0] -= 1
        for i, v in enumerate(obj):
            if _budget[0] <= 0:
                break
            out.update(_flatten(v, f"{prefix or '<root>'}[{i}]", depth + 1, _budget))
        return out
    out[prefix or "<root>"] = obj
    _budget[0] -= 1
    return out


class StructuredOutputChecker:
    """Schema (key-set) match + per-field value match for structured output.

    `score` = 0.5 * schema_overlap + 0.5 * field_value_match, where
    schema_overlap is the Jaccard overlap of the flattened key sets and
    field_value_match is the fraction of shared keys whose values agree.
    Threshold default 0.9 (== 90% match).
    """
    THRESHOLD = DEFAULT_STRUCTURED_MATCH_THRESHOLD

    def check(
        self,
        candidate: Any,
        reference: Any,
        *,
        threshold: Optional[float] = None,
        schema_only: Optional[Dict[str, Any]] = None,
    ) -> SimilarityResult:
        thr = self.THRESHOLD if threshold is None else float(threshold)
        cand_flat = _flatten(candidate)
        ref_flat = _flatten(reference) if schema_only is None else _flatten(schema_only)

        cand_keys = set(cand_flat)
        ref_keys = set(ref_flat)
        union = cand_keys | ref_keys
        schema_overlap = (
            len(cand_keys & ref_keys) / len(union) if union else 0.0
        )

        shared = cand_keys & ref_keys
        matched = 0
        mismatched: List[str] = []
        schema_mode = schema_only is not None
        for k in shared:
            if _value_equal(cand_flat[k], ref_flat[k], schema_mode=schema_mode):
                matched += 1
            else:
                mismatched.append(k)
        field_match = matched / len(shared) if shared else 0.0

        score = _clamp01(0.5 * schema_overlap + 0.5 * field_match)
        return SimilarityResult(
            score=score,
            threshold=thr,
            passed=score >= thr,
            details={
                "schema_overlap": schema_overlap,
                "field_match": field_match,
                "shared_fields": len(shared),
                "missing_fields": sorted(ref_keys - cand_keys)[:20],
                "extra_fields": sorted(cand_keys - ref_keys)[:20],
                "mismatched_fields": sorted(mismatched)[:20],
            },
        )


def _value_equal(a: Any, b: Any, *, schema_mode: bool = False) -> bool:
    """Tolerant leaf comparison: numbers within 1e-6, strings case-folded.

    In `schema_mode` (a `schema_only` reference was supplied) a null
    reference leaf declares shape only — any candidate value matches.
    """
    if schema_mode and b is None:
        return True
    if a is None or b is None:
        return a is b or (not a and not b)
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-6
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    if type(a) is not type(b):
        return str(a) == str(b)
    return a == b

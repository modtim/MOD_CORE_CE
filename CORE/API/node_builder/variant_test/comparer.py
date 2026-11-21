"""§5 / GO.4 — output-similarity comparer per design.md threshold table.

One ``compare(output_type, ref_bytes, candidate_bytes) → score`` entry
point; dispatch internal to the module so callers don't need to know
which method backs each output_type.

Per design.md:

  | image     | SSIM (RGB)                            | ≥ 0.85 |
  | video     | per-frame SSIM/PSNR (true, container)  | SSIM ≥ 0.98 mean / PSNR ≥ 35dB |
  | text      | rouge-L OR cosine of embedding        | ≥ 0.85 |
  | embedding | cosine similarity                     | ≥ 0.98 |
  | 3d_mesh   | vertex/face count + Chamfer (container)| counts equal, Chamfer ≤ 1e-3×bbox diag |
  | audio     | log-mel spectrogram correlation (container) | ≥ 0.98 |
  | point_cloud/splat | Chamfer distance (container)   | ≤ 1e-3×bbox diag |
  | json      | schema match + per-field fuzzy        | schema 100%, fields ≥ 0.95 |
  | binary    | byte-identical OR drift-warning       | exact |
  | accelerator_scores/numeric_json | numeric JSON walk, per-leaf rel tol NUMERIC_REL_TOL | mean per-leaf score |

GO.4 — video/audio/3d_mesh/point_cloud/splat get TRUE comparers (ffmpeg
decode + scikit-image SSIM/PSNR, a manual log-mel + correlation, trimesh
+ scipy KDTree Chamfer distance). Those comparers are heavy (ffmpeg,
scikit-image, trimesh, scipy) and run ONLY inside the
``mod/variant-comparer`` task container — see
``CORE/DockerContainers/VariantComparer/comparers.py`` for the actual
implementations and ``CORE/DockerContainers/VariantComparer/compare.py``
for its CLI entrypoint. This module (the API side) MUST stay importable
with no new heavy deps: it only ever builds the container task's
command/spec (``build_comparer_task_spec`` / ``dispatch_compare``) —
it never imports skimage/trimesh/scipy itself.

Everything else (image / text / json / embedding / binary, plus any
output_type this module doesn't recognize) is scored inline with the
existing cheap comparers below — those stay on the API path since
they're genuinely light (Pillow + numpy, already a wizard dependency).

All public ``compare*`` functions return floats in [0, 1] where 1.0 =
identical.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GO.4 — container-routed output types. These get TRUE comparers (decode +
# perceptual metrics), which need heavy libs this module deliberately does
# not import. A caller with stored artifacts (MinIO refs / local paths) for
# both sides should route through `dispatch_compare` / `build_comparer_task_spec`
# instead of `compare()` for these types; `compare()` itself still answers
# for them (falls through to the byte-chunk approximation below) so it never
# raises for a caller that only has bytes in hand.
CONTAINER_COMPARE_TYPES = frozenset({
    "video", "audio", "3d_mesh", "mesh", "point_cloud", "pointcloud", "splat",
})

# The comparer task container image. Overridable so a dev/stage stack can
# point at a locally-built tag (e.g. mod/variant-comparer:dev) without a
# code change.
COMPARER_IMAGE_ENV = "VARIANT_COMPARER_IMAGE"
DEFAULT_COMPARER_IMAGE = "mod/variant-comparer:latest"

# Thresholds — module constants, overridable per call via the `thresholds`
# kwarg on `build_comparer_task_spec` / `dispatch_compare`. Kept in sync by
# hand with CORE/DockerContainers/VariantComparer/comparers.py's
# DEFAULT_THRESHOLDS (that module can't import this one, and vice versa —
# neither side may pull the other's dependency surface in). If you change
# one, change both.
CONTAINER_DEFAULT_THRESHOLDS: Dict[str, Dict[str, Any]] = {
    "video": {"ssim_min": 0.98, "psnr_min_db": 35.0},
    "audio": {"mel_corr_min": 0.98},
    "3d_mesh": {
        "chamfer_max_frac_bbox": 1e-3,
        "vertex_count_equal": True,
        "face_count_equal": True,
    },
    "point_cloud": {"chamfer_max_frac_bbox": 1e-3},
}

# Aliases the container's --kind accepts; used to normalize before spec build
# and threshold lookup.
_KIND_ALIASES = {
    "mesh": "3d_mesh",
    "pointcloud": "point_cloud",
    "splat": "point_cloud",
}


def _normalize_container_kind(output_type: str) -> str:
    k = (output_type or "").strip().lower()
    return _KIND_ALIASES.get(k, k)


def needs_container_compare(output_type: str) -> bool:
    """True iff `output_type` should be scored by the true (heavy)
    container comparer rather than the inline byte/image/etc comparers."""
    return (output_type or "").strip().lower() in CONTAINER_COMPARE_TYPES


def build_comparer_task_spec(
    output_type: str,
    ref_uri: str,
    candidate_uri: str,
    *,
    thresholds: Optional[Dict[str, Any]] = None,
    image: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure function: build the ``mod/variant-comparer`` task's spec for
    comparing two stored outputs. Does NOT submit anything — no Docker /
    ApiTaskQueue / HTTP call here, so this is unit-testable without a
    running cluster. The caller (a follow-on harness — GO.5 — or the
    variant-test dispatcher) is responsible for actually enqueueing this
    spec as an ``ApiTaskQueue`` container task and reading back the
    container's stdout JSON verdict.

    ``ref_uri`` / ``candidate_uri`` are whatever locator the eventual
    worker resolves to local files before invoking the container (e.g.
    ``s3://bucket/key`` — the same shape ``variant_test/dispatcher.py``
    already stores sample-test outputs under). This function does not
    interpret them; it only threads them through as CLI args / env so the
    container-launch code has one place to build that shape.
    """
    kind = _normalize_container_kind(output_type)
    th = dict(CONTAINER_DEFAULT_THRESHOLDS.get(kind, {}))
    if thresholds:
        th.update(thresholds)
    resolved_image = image or os.environ.get(COMPARER_IMAGE_ENV, DEFAULT_COMPARER_IMAGE)
    return {
        "image": resolved_image,
        "command": [
            "python", "compare.py",
            "--kind", kind,
            "--ref", ref_uri,
            "--candidate", candidate_uri,
            "--thresholds-json", json.dumps(th),
        ],
        "kind": kind,
        "thresholds": th,
        "ref_uri": ref_uri,
        "candidate_uri": candidate_uri,
    }


def dispatch_compare(
    output_type: str,
    ref: bytes,
    candidate: bytes,
    *,
    ref_uri: Optional[str] = None,
    candidate_uri: Optional[str] = None,
    thresholds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The single API-side routing function: decide whether a comparison
    goes to the comparer TASK CONTAINER (true metrics; video / audio /
    3d_mesh / point_cloud / splat, when both sides have a stored-artifact
    URI to hand the container) or is scored INLINE right here (every other
    output_type, plus a heavy type whose caller only has raw bytes and no
    stored ref to point the container at).

    Returns one of:
      ``{"mode": "container", "kind": ..., "task_spec": {...}}`` — caller
        must submit `task_spec` as a container task and parse its stdout
        JSON for the real verdict; no score is computed here.
      ``{"mode": "inline", "score": float}`` — score is final.

    This is the ONE function callers should use instead of calling
    `compare()` directly when they might have stored artifacts for a
    heavy type — keeps "does this need the container" logic in one place
    rather than duplicated at each call site.
    """
    kind = (output_type or "").strip().lower()
    if kind in CONTAINER_COMPARE_TYPES and ref_uri and candidate_uri:
        return {
            "mode": "container",
            "kind": _normalize_container_kind(kind),
            "task_spec": build_comparer_task_spec(
                kind, ref_uri, candidate_uri, thresholds=thresholds,
            ),
        }
    return {"mode": "inline", "score": compare(output_type, ref, candidate)}


def compare(output_type: str, ref: bytes, candidate: bytes) -> float:
    """Return a similarity score in [0, 1] between ``ref`` and
    ``candidate``. Unknown output_types fall back to byte-equality.

    Resilient to empty / malformed inputs — never raises.
    """
    if not isinstance(ref, (bytes, bytearray)):
        ref = bytes(ref or b"")
    if not isinstance(candidate, (bytes, bytearray)):
        candidate = bytes(candidate or b"")
    if not ref and not candidate:
        return 1.0
    if not ref or not candidate:
        return 0.0
    output_type = (output_type or "").strip().lower()
    try:
        if output_type == "binary":
            return _compare_binary(ref, candidate)
        if output_type == "text":
            return _compare_text(ref, candidate)
        if output_type == "json":
            return _compare_json(ref, candidate)
        if output_type in ("accelerator_scores", "numeric_json"):
            return _compare_numeric_json(ref, candidate)
        if output_type == "image":
            return _compare_image(ref, candidate)
        if output_type == "embedding":
            return _compare_embedding(ref, candidate)
        if output_type == "video":
            # GO.4 — the TRUE comparer (ffmpeg decode + per-frame
            # SSIM/PSNR) runs in the comparer task container and is
            # reached via `dispatch_compare`/`build_comparer_task_spec`,
            # which needs stored-artifact URIs for both sides. This
            # bytes-only `compare()` path is what's left for a caller
            # that has no URI to hand the container (or the container
            # path failed) — chunked byte-similarity approximation.
            return _compare_video_fallback(ref, candidate)
        if output_type == "audio":
            return _compare_audio_fallback(ref, candidate)
        if output_type in ("3d_mesh", "mesh"):
            return _compare_mesh_fallback(ref, candidate)
        if output_type in ("point_cloud", "pointcloud", "splat"):
            return _compare_mesh_fallback(ref, candidate)
    except Exception as exc:
        logger.warning(
            "comparer: output_type=%r raised %s during compare; "
            "falling back to byte-equality", output_type, exc,
        )
    return _compare_binary(ref, candidate)


def _compare_binary(ref: bytes, candidate: bytes) -> float:
    return 1.0 if ref == candidate else 0.0


def _compare_text(ref: bytes, candidate: bytes) -> float:
    """rouge-L approximation: 2 * LCS / (len(ref) + len(cand)).

    Quadratic in length, capped at 4 KB on each side to keep latency
    bounded. Real rouge-L uses tokenized LCS; our character-level
    version is close enough to keep the threshold (0.85) meaningful
    for sample-test outputs which are typically short.
    """
    r = ref[:4096].decode("utf-8", errors="replace")
    c = candidate[:4096].decode("utf-8", errors="replace")
    if r == c:
        return 1.0
    lcs = _lcs_length(r, c)
    denom = len(r) + len(c)
    if denom == 0:
        return 0.0
    return min(1.0, 2.0 * lcs / denom)


def _lcs_length(a: str, b: str) -> int:
    """O(n*m) longest-common-subsequence length. Capped inputs by
    caller — see _compare_text."""
    if not a or not b:
        return 0
    n, m = len(a), len(b)
    prev = [0] * (m + 1)
    for i in range(1, n + 1):
        curr = [0] * (m + 1)
        ai = a[i - 1]
        for j in range(1, m + 1):
            if ai == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev = curr
    return prev[m]


def _compare_json(ref: bytes, candidate: bytes) -> float:
    """Schema match + per-field fuzzy.

    Score breakdown (multiplicative):
      - 50% schema match: top-level keys + nested type tree align
      - 50% value similarity: each leaf field gets a similarity score,
        averaged.

    Returns 0.0 if either side isn't parseable as JSON.
    """
    try:
        r = json.loads(ref)
        c = json.loads(candidate)
    except Exception:
        return 0.0
    schema_match = _json_schema_similarity(r, c)
    value_match = _json_value_similarity(r, c)
    # Schema is load-bearing — if the shape differs, the consuming
    # workflow node breaks. Weight schema heavier than per-field.
    return 0.6 * schema_match + 0.4 * value_match


def _json_schema_similarity(a: Any, b: Any) -> float:
    if type(a) is not type(b):
        return 0.0
    if isinstance(a, dict):
        if not a and not b:
            return 1.0
        keys = set(a.keys()) | set(b.keys())
        if not keys:
            return 1.0
        matches = sum(
            1 for k in keys
            if k in a and k in b
            and _json_schema_similarity(a[k], b[k]) >= 0.99
        )
        return matches / len(keys)
    if isinstance(a, list):
        if not a and not b:
            return 1.0
        # Schema for lists: element types match. Use first elem as
        # exemplar; matches what most JSON outputs look like.
        if not a or not b:
            return 0.0
        return _json_schema_similarity(a[0], b[0])
    # Scalars match by type only at the schema layer.
    return 1.0


def _json_value_similarity(a: Any, b: Any) -> float:
    if type(a) is not type(b):
        return 0.0
    if isinstance(a, dict):
        keys = set(a.keys()) & set(b.keys())
        if not keys:
            return 1.0 if not a and not b else 0.0
        scores = [_json_value_similarity(a[k], b[k]) for k in keys]
        return sum(scores) / len(scores)
    if isinstance(a, list):
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        n = min(len(a), len(b))
        scores = [_json_value_similarity(a[i], b[i]) for i in range(n)]
        if len(a) != len(b):
            # Length mismatch penalty proportional to the gap.
            scores.append(min(len(a), len(b)) / max(len(a), len(b)))
        return sum(scores) / len(scores)
    if isinstance(a, str):
        if a == b:
            return 1.0
        denom = len(a) + len(b)
        if denom == 0:
            return 1.0
        return min(1.0, 2.0 * _lcs_length(a[:512], b[:512]) / denom)
    if isinstance(a, (int, float)):
        if a == b:
            return 1.0
        # Relative-error similarity for numerics.
        denom = max(abs(a), abs(b))
        if denom == 0:
            return 1.0
        return max(0.0, 1.0 - abs(a - b) / denom)
    if isinstance(a, bool):
        return 1.0 if a == b else 0.0
    if a is None:
        return 1.0 if b is None else 0.0
    return 1.0 if a == b else 0.0


# Perf numbers (TFLOPS / GB/s / copy speed) are noisy across accelerators,
# so numeric-JSON leaves are given this relative tolerance. Module constant
# on purpose — not a behavioural tunable, so it stays out of platform
# settings / env.
NUMERIC_REL_TOL = 0.25


def _is_numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _numeric_leaf_score(a: float, b: float, rel_tol: float) -> float:
    diff = abs(a - b)
    scale = max(abs(a), abs(b), 1e-9)
    if diff <= rel_tol * scale:
        return 1.0
    denom = max(abs(a), abs(b))
    if denom == 0:
        return 1.0
    return max(0.0, 1.0 - diff / denom)


def _walk_numeric_json(a: Any, b: Any, rel_tol: float, scores: list[float]) -> None:
    """Parallel walk over REF's paths; appends one score per ref leaf.
    A missing key/path or structural type mismatch in candidate scores 0
    for that leaf. Extra keys in candidate are ignored."""
    if isinstance(a, dict):
        if isinstance(b, dict):
            for k, v in a.items():
                if k in b:
                    _walk_numeric_json(v, b[k], rel_tol, scores)
                else:
                    scores.append(0.0)
        else:
            scores.append(0.0)
        return
    if isinstance(a, list):
        if isinstance(b, list):
            for i, v in enumerate(a):
                if i < len(b):
                    _walk_numeric_json(v, b[i], rel_tol, scores)
                else:
                    scores.append(0.0)
        else:
            scores.append(0.0)
        return
    if _is_numeric(a):
        if not _is_numeric(b):
            scores.append(0.0)
        else:
            scores.append(_numeric_leaf_score(a, b, rel_tol))
        return
    scores.append(1.0 if a == b else 0.0)


def _compare_numeric_json(
    ref: bytes, candidate: bytes, rel_tol: float = NUMERIC_REL_TOL
) -> float:
    """Numeric-tolerance comparer for accelerator_benchmark output
    (``accelerator_scores``) and any other noisy per-run JSON scores
    (``numeric_json``).

    Walks both parsed JSON trees in parallel over ref's paths:
      - numeric leaf in ref present in candidate → 1.0 if within
        rel_tol, else max(0, 1 - |a-b|/max(|a|,|b|));
      - non-numeric leaf → exact equality (1/0);
      - key/path missing in candidate → 0 for that leaf;
    overall score = mean of leaf scores. Unparseable JSON → 0.0.
    """
    try:
        r = json.loads(ref)
        c = json.loads(candidate)
    except Exception:
        return 0.0
    scores: list[float] = []
    _walk_numeric_json(r, c, rel_tol, scores)
    if not scores:
        # No leaves (empty structures / bare scalars compared as above) —
        # decide on whole-document equality.
        return 1.0 if r == c else 0.0
    return sum(scores) / len(scores)


def _compare_image(ref: bytes, candidate: bytes) -> float:
    """SSIM-ish similarity on the raw image data. Uses Pillow + numpy
    (already in the API's dep set for ML / image work).

    Approximation: 1 - normalized RMSE in RGB space, resized to a
    common 64x64 so big-vs-small differences in image dimensions
    don't dominate the score. Not true SSIM but tracks it well for
    the band we care about (>= 0.85 verified, < 0.85 drift).
    """
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        # Pillow / numpy missing — degrade to byte-equality.
        return _compare_binary(ref, candidate)
    try:
        a = Image.open(io.BytesIO(ref)).convert("RGB").resize((64, 64))
        b = Image.open(io.BytesIO(candidate)).convert("RGB").resize((64, 64))
    except Exception:
        return 0.0
    av = np.array(a, dtype=np.float32)
    bv = np.array(b, dtype=np.float32)
    diff = av - bv
    rmse = float((diff * diff).mean()) ** 0.5
    # Channel range is 0..255; rmse in [0, 255]. Map to [0, 1]
    # similarity with a slight curve to give the upper band more
    # discriminative power than a flat linear map.
    sim = max(0.0, 1.0 - rmse / 255.0)
    # Boost slight differences toward 1: identical → 1, ~20 RMSE
    # (typical re-encode JPEG noise) → ~0.92.
    return min(1.0, sim ** 0.8)


def _compare_embedding(ref: bytes, candidate: bytes) -> float:
    """Cosine similarity. Accepts either JSON list-of-floats or
    newline-separated floats."""
    try:
        a = _parse_floats(ref)
        b = _parse_floats(candidate)
    except Exception:
        return 0.0
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    cos = dot / (na * nb)
    # Cosine ranges in [-1, 1]; map to [0, 1]. Negative correlation
    # is just as much "drift" as orthogonal.
    return max(0.0, (cos + 1.0) / 2.0)


def _parse_floats(raw: bytes) -> list[float]:
    s = raw.decode("utf-8", errors="replace").strip()
    if not s:
        return []
    # Try JSON list first.
    if s.startswith("["):
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [float(x) for x in v]
        except Exception:
            pass
    # Whitespace / newline / comma separated.
    out: list[float] = []
    for tok in s.replace(",", " ").split():
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def _compare_video_fallback(ref: bytes, candidate: bytes) -> float:
    """Byte-level fallback for when the comparer container path isn't
    available (no stored-artifact URI on one/both sides). Splits each
    blob into ~16 chunks (mimics frame sampling), compares chunks at
    equivalent offsets, averages. GO.4's real per-frame SSIM/PSNR lives
    in CORE/DockerContainers/VariantComparer/comparers.py:compare_video —
    see `dispatch_compare` for the routing."""
    return _chunked_byte_similarity(ref, candidate, chunks=16)


def _compare_audio_fallback(ref: bytes, candidate: bytes) -> float:
    """Byte-level fallback (see _compare_video_fallback). GO.4's real
    log-mel spectrogram correlation lives in
    CORE/DockerContainers/VariantComparer/comparers.py:compare_audio."""
    return _chunked_byte_similarity(ref, candidate, chunks=24)


def _compare_mesh_fallback(ref: bytes, candidate: bytes) -> float:
    """Byte-level fallback (see _compare_video_fallback) for 3d_mesh /
    point_cloud / splat. GO.4's real trimesh vertex/face-count + Chamfer
    distance comparer lives in
    CORE/DockerContainers/VariantComparer/comparers.py:compare_mesh /
    compare_point_cloud."""
    return _chunked_byte_similarity(ref, candidate, chunks=12)


def _chunked_byte_similarity(ref: bytes, candidate: bytes, *, chunks: int) -> float:
    """Compare two byte blobs by splitting each into N equal-ish
    chunks, computing the byte-equality ratio per chunk, and
    averaging. Penalizes length mismatch."""
    if not ref or not candidate:
        return 0.0
    if ref == candidate:
        return 1.0
    n1, n2 = len(ref), len(candidate)
    len_pen = min(n1, n2) / max(n1, n2)
    step1 = max(1, n1 // chunks)
    step2 = max(1, n2 // chunks)
    matches = 0
    for i in range(chunks):
        a = ref[i * step1 : (i + 1) * step1]
        b = candidate[i * step2 : (i + 1) * step2]
        if not a or not b:
            continue
        # Byte-overlap ratio for this pair of chunks.
        common = min(len(a), len(b))
        equal = sum(1 for x, y in zip(a[:common], b[:common]) if x == y)
        matches += equal / common
    base = matches / chunks
    return max(0.0, min(1.0, base * len_pen))

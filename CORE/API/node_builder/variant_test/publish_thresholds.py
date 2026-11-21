"""§5 — per-output-type publish + drift thresholds.

Two threshold concepts:

  - ``MIN_PUBLISH_CONFIDENCE``: the hero variant must score AT or
    ABOVE this on its sample-test for the Node to be published. Hero
    below threshold → the build's register step fails the whole
    workflow_run; no NodeDefinition row is created. This is the
    "load-bearing" gate that prevents silently broken nodes from
    appearing in the library.

  - ``DRIFT_THRESHOLD``: non-hero variants score against the
    reference (defaults to hero, user-selectable per §5c). Above
    threshold → ``verified``. Below threshold but the sample-test
    container exited 0 with output → ``drift`` (the variant is
    still usable, just produces output that diverges from the
    reference enough to warrant a warning chip in the UI). Below
    AND the container failed → ``incompatible``.

  - ``MARGINAL_BAND``: a hero score that's AT or ABOVE the publish
    threshold but within this distance of it earns a
    ``"marginal"`` verdict (still published — security: never
    quietly downgrade), with a warning that sample-test review is
    worthwhile. Catches the "barely-clears" case where a typo or
    near-broken pipe step might still squeeze past.

Tables come from `the internal design notes` (§ Output-
similarity thresholds). Numbers are slightly more conservative on the
publish side than the drift side — drift is "good enough" but publish
is "users will run this against real input."
"""
from __future__ import annotations

from typing import Iterable


# Hero must reach this score to publish. Below → workflow FAILED.
MIN_PUBLISH_CONFIDENCE = {
    "image": 0.90,
    "video": 0.90,
    "text": 0.90,
    "embedding": 0.99,
    "3d_mesh": 0.95,
    "audio": 0.92,
    "json": 0.98,
    "binary": 1.00,
}

# Non-hero variants score above this against the reference variant
# stay "verified"; below = "drift". Hero never uses this (hero is
# the reference by default).
DRIFT_THRESHOLD = {
    "image": 0.85,
    "video": 0.85,
    "text": 0.85,
    "embedding": 0.98,
    "3d_mesh": 0.99,
    "audio": 0.90,
    "json": 0.95,
    "binary": 1.00,
}

# Hero scores within MARGINAL_BAND ABOVE the publish threshold publish
# with a "marginal — consider sample-test review" warning. Below the
# threshold is failure; above (threshold + band) is verified-clean.
MARGINAL_BAND = 0.05

# When the wizard couldn't infer output_type with confidence, we use
# this fallback. Picked to be neither the strictest (binary) nor the
# laxest — keeps the gate meaningful for unknown content shapes.
_DEFAULT_PUBLISH = 0.90
_DEFAULT_DRIFT = 0.85


def known_output_types() -> Iterable[str]:
    """Return the canonical set of output_type keys the thresholds
    apply to. Useful for UI dropdowns + test coverage assertions."""
    return tuple(MIN_PUBLISH_CONFIDENCE.keys())


def publish_threshold(output_type: str) -> float:
    """Hero's required sample-test score for this output_type."""
    return MIN_PUBLISH_CONFIDENCE.get(output_type or "", _DEFAULT_PUBLISH)


def drift_threshold(output_type: str) -> float:
    """Non-hero variants must clear this against the reference variant
    to stay ``verified``; below counts as ``drift``."""
    return DRIFT_THRESHOLD.get(output_type or "", _DEFAULT_DRIFT)


def verdict_for_score(
    output_type: str,
    score: float,
    *,
    is_hero: bool,
    sample_test_ok: bool = True,
) -> str:
    """Decide the verdict for a variant given its similarity score.

    Returns one of:
      - ``"verified"`` — score is comfortably above the relevant
        threshold; node card shows clean chip.
      - ``"marginal"`` — hero-only verdict; hero passes publish gate
        but within `MARGINAL_BAND` of the threshold. Node is still
        published with a "sample-test review recommended" warning.
      - ``"drift"`` — non-hero only; sample-test ran successfully
        but the output diverges from the reference beyond drift
        tolerance. Variant remains usable with a drift-warning chip.
      - ``"failed"`` — hero score < publish threshold OR hero
        sample-test container failed. The build's register step
        treats this as a publish-gate failure.
      - ``"incompatible"`` — non-hero sample-test container failed
        or produced no output. Variant is removed from the Node's
        gpu_variants map; scheduler won't route real workloads.

    `sample_test_ok=False` short-circuits to ``failed`` (hero) /
    ``incompatible`` (non-hero) regardless of score.
    """
    if not sample_test_ok:
        return "failed" if is_hero else "incompatible"
    if is_hero:
        pub_t = publish_threshold(output_type)
        if score < pub_t:
            return "failed"
        # Perfect match always wins regardless of the band — a 1.0
        # against a binary publish threshold of 1.0 is verified, not
        # marginal. (Without this carve-out, exact-match thresholds
        # like binary's 1.00 collapse `verified` into a never-reachable
        # band above 1.05.)
        if score >= 1.0:
            return "verified"
        if score < pub_t + MARGINAL_BAND:
            return "marginal"
        return "verified"
    # Non-hero
    drift_t = drift_threshold(output_type)
    if score < drift_t:
        return "drift"
    return "verified"


def is_marginal(output_type: str, score: float) -> bool:
    """True iff this hero score lands in the marginal-warning band.
    Convenience for UI / log messaging — the verdict already
    encodes this distinction. Perfect 1.0 matches never count as
    marginal (see `verdict_for_score`)."""
    if score >= 1.0:
        return False
    pub_t = publish_threshold(output_type)
    return pub_t <= score < pub_t + MARGINAL_BAND

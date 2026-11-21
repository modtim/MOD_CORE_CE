"""§5 — per-variant sample-test + drift-aware verification.

The ``variant_test`` package owns the wizard-rebuild §5 functionality:

  - ``publish_thresholds``: per-output-type thresholds for the
    publish gate (hero must clear) and drift tolerance (non-hero
    variants stay "verified" above, drop to "drift" below).
  - ``comparer``: output-similarity scoring per design.md threshold
    table — image SSIM, text rouge-L approximation, embedding
    cosine, json schema-match, binary byte-identical.
  - ``dispatcher``: server-side glue called from the
    ``/wizard/variant-sample-test-result`` endpoint that ingests an
    output, compares it to the spec's current reference variant
    (defaults to hero), and writes a ``node_variant_verification``
    row with score + verdict.
"""

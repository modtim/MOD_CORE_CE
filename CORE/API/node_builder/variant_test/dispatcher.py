"""§5 — sample-test result dispatcher.

Server-side glue called from the
``POST /node_definitions/wizard/variant-sample-test-result`` endpoint.
Takes the build-task's per-variant sample-test result (exit code,
output bytes, output_type), persists the output to MinIO so the §5c
reference-variant swap can re-score later, runs the comparer against
the current reference, and writes a ``node_variant_verification``
row with score + verdict.

Hero scoring rules:
  - If the user supplied ``sample_output`` on the BuildSpec, hero
    score = similarity(actual_output, sample_output) using the
    output_type's comparer. This is the case where the wizard has a
    "golden reference" to grade against, and ``MIN_PUBLISH_CONFIDENCE``
    is meaningful.
  - Otherwise, hero score = 1.0 when the sample-test container
    exited 0 with non-empty output, else 0.0. The publish threshold
    trivially passes; the gate becomes "did the build produce ANY
    output on synth input" which is still strictly stronger than
    the old smoke probe. project-agent §1.2: such a pass records
    ``scored_against_variant = "unanchored"`` so it is not read as a
    real 1.0. This matters beyond the hero — every non-hero variant
    is graded against the hero, so an unanchored hero silently
    defines "correct" for the whole node.

Non-hero scoring:
  - Compare against the spec's current reference variant's stored
    output (MinIO). If the reference hasn't been scored yet (e.g.
    this variant's sample-test landed before hero's), the row is
    written as ``untested`` and `__bb__needs_recompute__bb__` so the
    register step can resolve it. (Hero ALWAYS runs first per the
    §4 DAG gate; this is a defensive fallback only.)
"""
from __future__ import annotations

import base64
import io
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


_BUCKET = "wizard-sample-tests"

# project-agent §1.2 — sentinel written to
# ``NodeVariantVerification.scored_against_variant`` when a hero passed
# with no reference to grade against. Follows the existing
# "user-skipped" sentinel convention in routers/node_definitions.py.
# NOT None: None already means "not scored yet" for the untested case,
# and conflating "we had nothing to compare to" with "we have not
# compared yet" is how an unanchored publish reads as a pass.
UNANCHORED = "unanchored"

# project-agent §1.4 — tenant setting key deciding what happens to a
# non-hero variant that scored below DRIFT_THRESHOLD. "ask" leaves the
# ruling NULL, and §1.5 will not select an unruled drifted variant to
# serve work. Default is "ask" precisely because the previous behaviour
# — publish it and show a chip — assumed a human was looking.
DRIFT_DEFAULT_SETTING = "node_build.drift_default"
DRIFT_RULINGS = ("publish", "drop", "keep_fixing")


def tenant_drift_default(db, tenant_id: int) -> Optional[str]:
    """The tenant's standing ruling for drifted variants, or None to ask.

    Returns one of DRIFT_RULINGS, or None. An unrecognised stored value
    is treated as None rather than trusted — a typo in a setting must not
    silently put divergent builds into service.
    """
    try:
        import models
        row = (
            db.query(models.TenantSetting)
            .filter(models.TenantSetting.tenant_id == tenant_id,
                    models.TenantSetting.key == DRIFT_DEFAULT_SETTING)
            .first()
        )
    except Exception as exc:
        logger.warning("variant_test.dispatcher: drift default lookup "
                       "failed for tenant=%s: %s", tenant_id, exc)
        return None
    if row is None:
        return None
    value = row.value
    if isinstance(value, dict):
        value = value.get("value")
    return value if value in DRIFT_RULINGS else None


def _get_minio_client():
    """Backend-agnostic storage client (Vault service account) — see utils.storage."""
    from utils.storage import get_service_client
    return get_service_client()


def _ensure_bucket(client, bucket: str) -> None:
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def _put_output(tenant_id: int, build_spec_id: int, variant: str,
                data: bytes, attempt: int = 1) -> Optional[str]:
    """Upload the variant's sample-test output bytes to MinIO. Returns
    an ``s3://bucket/key`` ref, or None on failure.

    project-agent §1.3 — the key carries the attempt number. Without it
    a second attempt overwrote the first's bytes, so keeping the best
    SCORE would have left the row pointing at the worse OUTPUT. Attempt 1
    keeps the historical key so refs already stored still resolve.
    """
    if not data:
        return None
    try:
        client = _get_minio_client()
        _ensure_bucket(client, _BUCKET)
        suffix = "" if attempt <= 1 else f"-attempt{int(attempt)}"
        key = (
            f"tenant-{int(tenant_id)}/spec-{int(build_spec_id)}/"
            f"{variant}{suffix}.bin"
        )
        client.put_object(_BUCKET, key, io.BytesIO(data), len(data))
        return f"s3://{_BUCKET}/{key}"
    except Exception as exc:
        logger.warning(
            "variant_test.dispatcher: MinIO upload failed for "
            "spec=%d variant=%s: %s",
            build_spec_id, variant, exc,
        )
        return None


def _fetch_output(ref: Optional[str]) -> bytes:
    """Pull the bytes previously stored at ``ref`` back from MinIO.
    Returns empty bytes on any failure — comparer treats that as 0.0
    similarity."""
    if not ref or not isinstance(ref, str) or not ref.startswith("s3://"):
        return b""
    rest = ref[5:]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        return b""
    try:
        client = _get_minio_client()
        resp = client.get_object(bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()
    except Exception as exc:
        logger.warning(
            "variant_test.dispatcher: MinIO fetch failed for %r: %s",
            ref, exc,
        )
        return b""


def record_variant_test(
    db,
    *,
    build_spec_id: int,
    variant: str,
    exit_code: int,
    produced_output: bool,
    output_bytes: bytes,
    output_type: str,
    image_digest: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a per-variant sample-test result and return the verdict.

    Args:
      db: SQLAlchemy session.
      build_spec_id: the in-flight BuildSpecRecord id.
      variant: hero / cuda / rocm / intel / npu / cpu.
      exit_code: the docker run exit code (0 == success).
      produced_output: True iff the container wrote at least one
        non-empty file under /data/output.
      output_bytes: the raw bytes of the produced output (for
        scoring + storage). Empty when produced_output=False.
      output_type: declared output port type (image / text / video /
        embedding / 3d_mesh / audio / json / binary).
      image_digest: pushed manifest digest for THIS attempt. Optional
        so existing callers keep working, but without it a regression
        can be detected and not reverted — moving :latest leaves the
        earlier manifest in place, and the digest is the only handle
        on it.

    Returns:
      ``{"verdict", "score", "publish_ok", "scored_against",
         "is_marginal", "attempt", "best_score", "regressed",
         "published_digest"}``. ``publish_ok`` is True iff this
      variant's verdict permits the parent workflow_run to advance to
      register (always True for non-hero; hero needs verified or
      marginal).

    project-agent §1.3 — the persisted row describes the BEST attempt,
    not the most recent one. A repair scoring lower than its predecessor
    is recorded in ``attempt_history`` and marked ``regressed``, but does
    not replace the published score, output ref or digest.
    """
    # Local imports — keep startup-time import surface lean.
    import models
    from node_builder.variant_test import publish_thresholds, comparer

    spec_row = (
        db.query(models.BuildSpecRecord)
        .filter(models.BuildSpecRecord.id == build_spec_id)
        .first()
    )
    if spec_row is None:
        raise ValueError(f"build_spec {build_spec_id} not found")
    spec = spec_row.spec_json or {}
    tenant_id = spec_row.tenant_id
    # 23.1a — "fix my code" re-verify. A single-variant rebuild (POST /nodes/
    # {id}/rebuild scope=single_variant) has NO register stage, so finalize-
    # verification never runs for it and its row would keep node_definition_id
    # NULL — invisible to GET /nodes/{id}/variant-verification, which is
    # exactly the document the UI re-reads after the re-verify. seed_spec_from_
    # revision stamps rebuild_node_id on every client rebuild spec (wizard /
    # corpus / backfill specs never set it), so link the row to that node here.
    # Only THIS (spec, variant) row is touched — other variants' verdicts stay.
    rebuild_node_id = spec.get("rebuild_node_id")

    is_hero = variant == "hero"
    sample_test_ok = (
        exit_code == 0 and produced_output and len(output_bytes) > 0
    )

    # Existing row (if any) decides which attempt this is, and what
    # score the new one has to beat.
    row = (
        db.query(models.NodeVariantVerification)
        .filter(
            models.NodeVariantVerification.build_spec_id == build_spec_id,
            models.NodeVariantVerification.variant == variant,
        )
        .first()
    )
    # The link is conditional, and the production unique index says why:
    # migrations/20260523_variant_verification.py ships
    # `CREATE UNIQUE INDEX uq_nvv_node_def_variant ON node_variant_verification
    # (node_definition_id, variant) WHERE node_definition_id IS NOT NULL`
    # (replayed at bootstrap by db_bootstrap_guards). The WHERE clause is what
    # makes many spec-scoped rows per variant legal and node-linked rows
    # unique: exactly ONE row per (node, variant) is the node's published
    # verdict. So stamp the link only when the node has no node-linked row for
    # this variant yet; a second re-verify under a NEW BuildSpecRecord keeps
    # its own spec-scoped row (its attempt_history still records the attempt)
    # and the GET collapses to the incumbent. Stamping unconditionally is the
    # bug: the second insert raises UniqueViolation, the endpoint 500s, and the
    # operator's second "fix my code" click records nothing at all.
    rebuild_node_id = int(rebuild_node_id) if rebuild_node_id else None
    if rebuild_node_id:
        node_linked = (
            db.query(models.NodeVariantVerification)
            .filter(
                models.NodeVariantVerification.node_definition_id
                == rebuild_node_id,
                models.NodeVariantVerification.variant == variant,
            )
            .first()
        )
        if node_linked is not None:
            rebuild_node_id = None

    attempt = (int(row.attempt_count or 0) + 1) if row is not None else 1

    # Persist the output to MinIO so a later reference-variant swap
    # (§5c) can re-score in place, and so a superseded attempt's bytes
    # survive for comparison.
    output_ref = _put_output(tenant_id, build_spec_id, variant,
                             output_bytes, attempt=attempt)

    # Score
    score = 0.0
    scored_against: Optional[str] = None
    if is_hero:
        sample_out = spec.get("sample_output") or {}
        body_b64 = sample_out.get("body_b64")
        if body_b64 and sample_test_ok:
            try:
                ref_bytes = base64.b64decode(body_b64)
                score = comparer.compare(output_type, ref_bytes, output_bytes)
                scored_against = "user_sample"
            except Exception as exc:
                logger.warning(
                    "variant_test.dispatcher: hero sample_output "
                    "decode failed for spec=%d: %s — degrading to "
                    "trivial 1.0", build_spec_id, exc,
                )
                score = 1.0 if sample_test_ok else 0.0
        else:
            # No golden — trivially clear publish gate on success.
            #
            # project-agent §1.2 — mark this rather than leaving
            # scored_against unset. A hero that scored 1.0 for merely
            # exiting 0 with output is indistinguishable, in the row
            # and in every surface reading it, from one that scored
            # 1.0 against a real reference. Every non-hero variant is
            # then graded against that hero, so an unanchored hero
            # silently defines "correct" for the whole node.
            score = 1.0 if sample_test_ok else 0.0
            if sample_test_ok:
                scored_against = UNANCHORED
    else:
        # Non-hero: compare against the reference variant's stored
        # output. Reference defaults to hero; for the in-flight build
        # the BuildSpec doesn't carry a reference yet (it lives on
        # the NodeDefinition row that hasn't been created), so hero
        # is the implicit reference here. Once registered, §5c lets
        # users swap.
        hero_row = (
            db.query(models.NodeVariantVerification)
            .filter(
                models.NodeVariantVerification.build_spec_id == build_spec_id,
                models.NodeVariantVerification.variant == "hero",
            )
            .first()
        )
        if hero_row and hero_row.raw_output_ref and sample_test_ok:
            hero_bytes = _fetch_output(hero_row.raw_output_ref)
            if hero_bytes:
                score = comparer.compare(output_type, hero_bytes, output_bytes)
                scored_against = "hero"
            else:
                score = 0.0
        else:
            score = 0.0

    verdict = publish_thresholds.verdict_for_score(
        output_type, score,
        is_hero=is_hero, sample_test_ok=sample_test_ok,
    )

    # project-agent §1.3 — the row holds the BEST attempt. A repair that
    # scored worse is recorded and marked, but does not take over the
    # published score, output ref or digest: publishing a regression
    # because it happened to run last is how an unattended repair loop
    # hill-climbs downhill.
    prev_best = None if row is None else row.best_score
    wins = prev_best is None or float(score) > float(prev_best)
    regressed = not wins

    # §1.4 — a drifted non-hero variant needs a ruling before it serves
    # work. The tenant may have a standing one; absent that it waits.
    drift_ruling = None
    if verdict == "drift" and not is_hero:
        drift_ruling = tenant_drift_default(db, tenant_id)

    entry = {
        "attempt": attempt,
        "score": float(score),
        "verdict": verdict,
        "scored_against": scored_against,
        "raw_output_ref": output_ref,
        "image_digest": image_digest,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "regressed": regressed,
        "drift_ruling": drift_ruling,
    }

    if row is None:
        row = models.NodeVariantVerification(
            build_spec_id=build_spec_id,
            node_definition_id=(int(rebuild_node_id) if rebuild_node_id else None),
            variant=variant,
            score=float(score),
            verdict=verdict,
            scored_against_variant=scored_against,
            output_type_at_test=output_type,
            raw_output_ref=output_ref,
            image_digest=image_digest,
            best_score=float(score),
            attempt_count=1,
            attempt_history=[entry],
            drift_ruling=drift_ruling,
        )
        db.add(row)
    else:
        row.attempt_count = attempt
        row.attempt_history = list(row.attempt_history or []) + [entry]
        row.output_type_at_test = output_type
        if wins:
            row.score = float(score)
            row.best_score = float(score)
            row.verdict = verdict
            row.scored_against_variant = scored_against
            row.scored_at = datetime.now(timezone.utc)
            if output_ref:
                row.raw_output_ref = output_ref
            if image_digest:
                row.image_digest = image_digest
            # A new published build is a new thing to rule on. Carrying an
            # old 'publish' forward would let a rebuild inherit approval it
            # never earned; clearing a ruling on a variant that no longer
            # drifts is equally correct.
            row.drift_ruling = drift_ruling
            row.drift_ruled_at = None
            row.drift_ruled_by_user_id = None
        else:
            logger.info(
                "variant_test.dispatcher: spec=%d variant=%s attempt %d "
                "scored %.4f, below best %.4f — keeping the earlier build",
                build_spec_id, variant, attempt, float(score),
                float(prev_best),
            )
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(row, "attempt_history")
    db.commit()

    # What the caller is told about is the PUBLISHED attempt, so a
    # regression does not fail a run whose incumbent build is fine.
    published_score = float(row.score if row.score is not None else score)
    published_verdict = row.verdict
    published_against = row.scored_against_variant

    publish_ok = True
    if is_hero:
        publish_ok = published_verdict in ("verified", "marginal")

    return {
        "verdict": published_verdict,
        "score": published_score,
        "publish_ok": publish_ok,
        "scored_against": published_against,
        "attempt": attempt,
        "best_score": float(row.best_score) if row.best_score is not None else None,
        "regressed": regressed,
        "published_digest": row.image_digest,
        "drift_ruling": row.drift_ruling,
        "awaiting_drift_ruling": (
            row.verdict == "drift" and row.drift_ruling is None
        ),
        "is_marginal": (
            is_hero
            and publish_thresholds.is_marginal(output_type, published_score)
        ),
    }


def rescore_against_new_reference(
    db,
    *,
    node_definition_id: int,
    new_reference_variant: str,
) -> Dict[str, Any]:
    """§5c — recompute drift scores for every non-reference variant
    against the new reference, in place. No rebuilds.

    Reads each variant's stored output from MinIO (via
    ``raw_output_ref``), compares to the new reference's stored
    output, updates score + verdict + scored_against_variant.

    Returns ``{"updated": [...], "skipped": [...]}`` so the endpoint
    can surface what happened.
    """
    import models
    from node_builder.variant_test import publish_thresholds, comparer

    rows = (
        db.query(models.NodeVariantVerification)
        .filter(
            models.NodeVariantVerification.node_definition_id == node_definition_id,
        )
        .all()
    )
    if not rows:
        return {"updated": [], "skipped": [], "reason": "no rows"}

    ref_row = next(
        (r for r in rows if r.variant == new_reference_variant),
        None,
    )
    if ref_row is None:
        raise ValueError(
            f"new reference variant {new_reference_variant!r} has no "
            f"verification row for node_definition_id={node_definition_id}"
        )
    if ref_row.verdict in ("failed", "incompatible"):
        raise ValueError(
            f"reference variant {new_reference_variant!r} has verdict "
            f"{ref_row.verdict!r}; pick a verified variant instead"
        )

    ref_bytes = _fetch_output(ref_row.raw_output_ref)
    if not ref_bytes:
        raise ValueError(
            f"reference variant {new_reference_variant!r} has no stored "
            f"output to compare against"
        )

    updated, skipped = [], []
    for r in rows:
        if r.id == ref_row.id:
            # The reference itself: score = 1.0 vs itself.
            r.score = 1.0
            r.verdict = "verified"
            r.scored_against_variant = new_reference_variant
            r.scored_at = datetime.now(timezone.utc)
            updated.append({
                "variant": r.variant,
                "score": 1.0,
                "verdict": "verified",
            })
            continue
        cand_bytes = _fetch_output(r.raw_output_ref)
        if not cand_bytes:
            skipped.append({
                "variant": r.variant,
                "reason": "no stored output to compare",
            })
            continue
        score = comparer.compare(
            r.output_type_at_test or "binary",
            ref_bytes, cand_bytes,
        )
        # Non-hero verdict regardless of whether this variant is the
        # erstwhile "hero" — it's a peer of the new reference now.
        verdict = publish_thresholds.verdict_for_score(
            r.output_type_at_test or "binary", score,
            is_hero=False, sample_test_ok=True,
        )
        r.score = float(score)
        r.verdict = verdict
        r.scored_against_variant = new_reference_variant
        r.scored_at = datetime.now(timezone.utc)
        updated.append({
            "variant": r.variant,
            "score": float(score),
            "verdict": verdict,
        })
    db.commit()
    return {"updated": updated, "skipped": skipped}

"""scheduler-promote-gate — trainer-side MAE gate for new ONNX models.

When the trainer (`ml-retrain` container) finishes a run, it computes a
new model's MAE and compares against the current active model's MAE.
If the new model is not worse than the current by more than a
configurable tolerance (default 5%), it uploads as
`scheduler-vN.onnx` and hot-reload picks it up on the next 60s tick.
Otherwise it uploads as `scheduler-vN-shadow.onnx` — hot-reload's
filename parser silently skips that suffix, leaving the active model
untouched until an operator manually promotes (renames the file) or
ignores.

Pure functions only. No DB, no FastAPI, no SQLAlchemy. The trainer
container imports this as a flat file copied via Dockerfile so the
gate is available without dragging the full API package into the
trainer image.

Both the API + the trainer container run the same gate code; tests
live in `CORE/API/tests/test_ml_promote_gate.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE_PCT = 5.0
TOLERANCE_ENV_VAR = "SCHEDULER_PROMOTE_MAE_TOLERANCE_PCT"

OUTCOME_PROMOTED_ACTIVE = "promoted_active"
OUTCOME_UPLOADED_AS_SHADOW = "uploaded_as_shadow"
OUTCOME_NO_BASELINE = "no_baseline_for_comparison"

MODEL_PROMOTE_GATED_EVENT = "MODEL_PROMOTE_GATED"


def _safe_get_mae(meta: Optional[dict]) -> Optional[float]:
    """Pull `metrics.duration_mae_seconds` out of a model JSON dict.

    Returns None if any layer is missing or non-numeric.
    """
    if not isinstance(meta, dict):
        return None
    metrics = meta.get("metrics")
    if not isinstance(metrics, dict):
        return None
    raw = metrics.get("duration_mae_seconds")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def resolve_tolerance_pct(env: Optional[dict] = None) -> float:
    """Return the tolerance percent. Reads SCHEDULER_PROMOTE_MAE_TOLERANCE_PCT
    from the supplied env mapping (defaults to os.environ).

    Negative or non-parseable values fall back to the default; warns.
    """
    if env is None:
        env = os.environ
    raw = env.get(TOLERANCE_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_TOLERANCE_PCT
    try:
        v = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r could not be parsed; using default %.1f",
            TOLERANCE_ENV_VAR, raw, DEFAULT_TOLERANCE_PCT,
        )
        return DEFAULT_TOLERANCE_PCT
    if v < 0:
        logger.warning(
            "%s=%s is negative; using default %.1f",
            TOLERANCE_ENV_VAR, v, DEFAULT_TOLERANCE_PCT,
        )
        return DEFAULT_TOLERANCE_PCT
    return v


def evaluate(
    current_meta: Optional[dict],
    new_meta: dict,
    tolerance_pct: Optional[float] = None,
) -> Tuple[str, dict]:
    """Decide whether a new model can be uploaded as the active version.

    Returns `(outcome, audit_details)` where outcome is one of
    `promoted_active`, `uploaded_as_shadow`, or
    `no_baseline_for_comparison`.

    `audit_details` always contains `outcome`, `current_version`,
    `new_version`, `current_mae`, `new_mae`, `mae_delta_pct`,
    `tolerance_pct`. The caller adds `trigger_source` and (if shadow)
    `shadow_filename` before emitting.

    Decision tree:
    - `current_meta` is None or missing MAE → `no_baseline_for_comparison`
      (upload as active; first model in the lineage).
    - new is missing MAE → `no_baseline_for_comparison` defensively
      (we can't gate something we can't measure).
    - mae_delta_pct = (new - current) / current * 100. If
      `mae_delta_pct <= tolerance_pct` → `promoted_active`. Else →
      `uploaded_as_shadow`.
    """
    if tolerance_pct is None:
        tolerance_pct = resolve_tolerance_pct()

    current_mae = _safe_get_mae(current_meta)
    new_mae = _safe_get_mae(new_meta)
    new_version = new_meta.get("version") if isinstance(new_meta, dict) else None
    current_version = (
        current_meta.get("version") if isinstance(current_meta, dict) else None
    )

    base_details: dict[str, Any] = {
        "current_version": current_version,
        "new_version": new_version,
        "current_mae": current_mae,
        "new_mae": new_mae,
        "tolerance_pct": float(tolerance_pct),
        "mae_delta_pct": None,
    }

    # No baseline to compare against → first-model case, upload active.
    if current_mae is None or new_mae is None:
        return (
            OUTCOME_NO_BASELINE,
            {**base_details, "outcome": OUTCOME_NO_BASELINE},
        )

    # Avoid div-by-zero on a degenerate current_mae=0 (means a perfect
    # prior model — anything new is technically "infinitely worse" by
    # relative %; treat as promote-fail to err on the safe side).
    if current_mae == 0:
        return (
            OUTCOME_UPLOADED_AS_SHADOW,
            {
                **base_details,
                "outcome": OUTCOME_UPLOADED_AS_SHADOW,
                "mae_delta_pct": float("inf") if new_mae > 0 else 0.0,
            },
        )

    mae_delta_pct = (new_mae - current_mae) / current_mae * 100.0
    base_details["mae_delta_pct"] = round(mae_delta_pct, 2)

    if mae_delta_pct <= tolerance_pct:
        return (
            OUTCOME_PROMOTED_ACTIVE,
            {**base_details, "outcome": OUTCOME_PROMOTED_ACTIVE},
        )
    else:
        return (
            OUTCOME_UPLOADED_AS_SHADOW,
            {**base_details, "outcome": OUTCOME_UPLOADED_AS_SHADOW},
        )


def filename_suffix_for_outcome(outcome: str) -> str:
    """Helper: trainer uses this to pick the suffix for the three files
    it uploads. Returns `''` for active uploads and `'-shadow'` for
    gated uploads.
    """
    return "-shadow" if outcome == OUTCOME_UPLOADED_AS_SHADOW else ""


def fetch_current_active_metadata(minio_client, bucket: str) -> Optional[dict]:
    """Fetch metadata for the highest-versioned scheduler model in the
    given MinIO bucket. Returns the parsed dict, or None if no model
    exists (fresh deploy) or the GET fails.

    Skips any `-shadow` filenames (they are not eligible to be the
    "current active").
    """
    import json

    try:
        if not minio_client.bucket_exists(bucket):
            return None
    except Exception as e:  # noqa: BLE001
        logger.warning("ml_promote_gate: bucket existence check failed: %s", e)
        return None

    versions: list[int] = []
    try:
        for obj in minio_client.list_objects(bucket, prefix="scheduler-v"):
            name = obj.object_name
            # Only consider active filenames (no -shadow suffix).
            if not name.endswith(".onnx") or "-shadow" in name:
                continue
            stem = name[len("scheduler-v"):-len(".onnx")]
            try:
                versions.append(int(stem))
            except ValueError:
                continue
    except Exception as e:  # noqa: BLE001
        logger.warning("ml_promote_gate: list_objects failed: %s", e)
        return None

    if not versions:
        return None

    latest = max(versions)
    meta_key = f"scheduler-v{latest}.json"
    try:
        resp = minio_client.get_object(bucket, meta_key)
        try:
            data = resp.read()
        finally:
            try:
                resp.close()
                resp.release_conn()
            except Exception:
                pass
        return json.loads(data.decode())
    except Exception as e:  # noqa: BLE001
        logger.warning("ml_promote_gate: could not read %s: %s", meta_key, e)
        return None

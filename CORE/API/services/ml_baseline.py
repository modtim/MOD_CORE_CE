"""First-boot seed of the baked-in scheduler ONNX baseline into MinIO.

The API container ships with `/app/ml-baseline/scheduler-v<N>.onnx` (+
companion json + normalization). On startup, if MinIO `platform-models/`
has no `scheduler-v*.onnx` artifacts, we copy the baked-in bundle into
MinIO so the existing hot-reload thread (`main.py:_hot_reload_loop`)
picks it up on its next 60s tick.

Idempotent: if any `scheduler-v*.onnx` already exists in MinIO the
function is a no-op, so a redeploy of an already-running customer
never overwrites their local-tuned model.

Failures (MinIO unreachable, baseline files missing or corrupt) log an
error + emit an audit event and return without raising — startup
continues. Operators can fix the baseline + restart later.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

DEFAULT_BASELINE_DIR = Path("/app/ml-baseline")
PLATFORM_MODELS_BUCKET = "platform-models"

MODEL_BASELINE_SEEDED_EVENT = "MODEL_BASELINE_SEEDED"
MODEL_BASELINE_SEED_FAILED_EVENT = "MODEL_BASELINE_SEED_FAILED"
MODEL_BASELINE_REPAIRED_EVENT = "MODEL_BASELINE_REPAIRED"

_SCHEDULER_ONNX_RE = re.compile(r"^scheduler-v(\d+)\.onnx$")


def _detect_baseline_version(baseline_dir: Path) -> Optional[int]:
    """Return the version `N` whose three files are all present in
    `baseline_dir`, or None if no complete bundle is found."""
    if not baseline_dir.is_dir():
        return None
    for entry in sorted(baseline_dir.iterdir()):
        m = _SCHEDULER_ONNX_RE.match(entry.name)
        if not m:
            continue
        version = int(m.group(1))
        json_path = baseline_dir / f"scheduler-v{version}.json"
        norm_path = baseline_dir / f"scheduler-v{version}-normalization.json"
        if json_path.exists() and norm_path.exists():
            return version
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _emit_audit(db: Optional[Session], event_name: str, details: dict) -> None:
    """Best-effort audit emission. Never raises."""
    if db is None:
        logger.debug("ml_baseline: no db session — skipping audit %s", event_name)
        return
    try:
        db.add(models.AuditEvent(
            tenant_id=None,
            user_id=None,
            event_name=event_name,
            details=details,
        ))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("ml_baseline: audit emit failed for %s: %s", event_name, e)


def _list_existing_versions(client) -> list[int]:
    versions: list[int] = []
    if not client.bucket_exists(PLATFORM_MODELS_BUCKET):
        return versions
    for obj in client.list_objects(PLATFORM_MODELS_BUCKET, prefix="scheduler-v"):
        m = _SCHEDULER_ONNX_RE.match(obj.object_name)
        if m:
            versions.append(int(m.group(1)))
    return versions


def seed_baseline_if_empty(
    db: Optional[Session] = None,
    *,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    minio_client_factory=None,
) -> dict:
    """Seed the baked-in baseline into MinIO if no scheduler models exist.

    Returns a dict with at least `outcome` and (on success) `version` +
    `sha256`. Outcomes:
      - `seeded`: baseline uploaded; hot-reload will pick it up.
      - `skipped_minio_has_models`: at least one scheduler-v*.onnx
        already exists; no-op.
      - `skipped_no_baseline_files`: nothing under `baseline_dir` —
        likely an image built without bake-in (shouldn't happen given
        the Dockerfile RUN check, but treated defensively).
      - `failed`: any unexpected error. Audit-event emitted.

    `minio_client_factory` is injectable for tests; default constructs
    a real client from env.
    """
    baseline_dir = Path(baseline_dir)

    version = _detect_baseline_version(baseline_dir)
    if version is None:
        logger.warning("ml_baseline: no complete baseline bundle in %s — skipping seed", baseline_dir)
        return {"outcome": "skipped_no_baseline_files", "baseline_dir": str(baseline_dir)}

    onnx_path = baseline_dir / f"scheduler-v{version}.onnx"
    json_path = baseline_dir / f"scheduler-v{version}.json"
    norm_path = baseline_dir / f"scheduler-v{version}-normalization.json"

    try:
        if minio_client_factory is None:
            from utils.storage import get_service_client
            client = get_service_client()
        else:
            client = minio_client_factory()

        existing = _list_existing_versions(client)
        if existing:
            logger.info(
                "ml_baseline: seed skipped — MinIO already has scheduler models v%s",
                ",".join(str(v) for v in sorted(existing)),
            )
            return {"outcome": "skipped_minio_has_models", "existing_versions": sorted(existing)}

        if not client.bucket_exists(PLATFORM_MODELS_BUCKET):
            client.make_bucket(PLATFORM_MODELS_BUCKET)

        sha = _sha256(onnx_path)

        for path in (onnx_path, json_path, norm_path):
            with path.open("rb") as f:
                data = f.read()
            client.put_object(
                PLATFORM_MODELS_BUCKET,
                path.name,
                __import__("io").BytesIO(data),
                len(data),
            )

        logger.info(
            "ml_baseline: seeded scheduler-v%d into MinIO (%d bytes, sha256=%s…)",
            version, onnx_path.stat().st_size, sha[:12],
        )
        _emit_audit(
            db,
            MODEL_BASELINE_SEEDED_EVENT,
            {
                "version": version,
                "sha256": sha,
                "trigger_source": "first_boot_seed",
            },
        )
        return {"outcome": "seeded", "version": version, "sha256": sha}

    except Exception as e:  # noqa: BLE001 — seed must never crash startup
        logger.exception("ml_baseline: seed failed: %s", e)
        _emit_audit(
            db,
            MODEL_BASELINE_SEED_FAILED_EVENT,
            {
                "version_attempted": version,
                "reason": str(e)[:512],
            },
        )
        return {"outcome": "failed", "version_attempted": version, "reason": str(e)}


def repair_baseline(
    db: Optional[Session] = None,
    *,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
) -> dict:
    """Self-heal storage so it always serves a LOADABLE scheduler model.

    `seed_baseline_if_empty` only acts on an EMPTY bucket, so it cannot recover
    the failure mode that stranded the scheduler: the bucket is non-empty but
    every `scheduler-v*.onnx` is a truncated/corrupt stub (4–64 bytes), which
    the loader still picks by version number and fails to parse forever.

    This validates what storage actually serves (via
    `model_store.get_latest_valid_model_version`, which loads each candidate)
    and, when NO version loads, restores the baked-in good baseline over the
    corrupt bytes. `model_store.upload_model` validates the source and writes
    atomically, so a bad blob can never be (re)introduced. Idempotent: a no-op
    when a loadable model already exists. Designed to run periodically (a
    watchdog) as well as at startup, so recovery is automatic and permanent.

    Outcomes: `healthy` | `repaired` | `skipped_no_baseline_files` | `failed`.
    """
    baseline_dir = Path(baseline_dir)
    version = _detect_baseline_version(baseline_dir)
    if version is None:
        logger.warning("ml_baseline: no baked baseline bundle in %s — cannot repair", baseline_dir)
        return {"outcome": "skipped_no_baseline_files", "baseline_dir": str(baseline_dir)}

    onnx_path = baseline_dir / f"scheduler-v{version}.onnx"
    json_path = baseline_dir / f"scheduler-v{version}.json"
    norm_path = baseline_dir / f"scheduler-v{version}-normalization.json"

    try:
        from utils import model_store

        valid = model_store.get_latest_valid_model_version()
        if valid:
            return {"outcome": "healthy", "valid_version": valid}

        # No loadable model anywhere in storage — restore the baked baseline.
        import json as _json
        try:
            metadata = _json.loads(json_path.read_text())
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            metadata = {}
        metadata.setdefault("source", "baked_baseline_repair")

        model_store.upload_model(
            str(onnx_path), version,
            metadata=metadata, normalization_path=str(norm_path),
        )
        sha = _sha256(onnx_path)
        logger.warning(
            "ml_baseline: REPAIRED — storage served no loadable model; restored "
            "baked scheduler-v%d (%d bytes, sha256=%s…). Hot-reload will pick it up.",
            version, onnx_path.stat().st_size, sha[:12],
        )
        _emit_audit(
            db,
            MODEL_BASELINE_REPAIRED_EVENT,
            {"version": version, "sha256": sha, "trigger_source": "watchdog_repair"},
        )
        return {"outcome": "repaired", "version": version, "sha256": sha}

    except Exception as e:  # noqa: BLE001 — repair must never crash the watchdog
        logger.exception("ml_baseline: repair failed: %s", e)
        _emit_audit(
            db,
            MODEL_BASELINE_SEED_FAILED_EVENT,
            {"version_attempted": version, "reason": str(e)[:512], "phase": "repair"},
        )
        return {"outcome": "failed", "version_attempted": version, "reason": str(e)}

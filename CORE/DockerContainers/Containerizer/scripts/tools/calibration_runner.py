"""Phase 9 — compose `optimum-cli export openvino` CLI args from a
calibration config.

The API stashes a `calibration_inputs` dict on
`NodeDefinition.custom_metadata` (see `routers/variant_calibration.py`).
The wizard's build-and-register-local path serialises that dict into
the `INLINE_CALIBRATION_CONFIG` env var on the image_builder task.

This module converts the config dict back into a fragment of shell
flags that the intel_npu recipe splices into its
`optimum-cli export openvino` RUN line before rendering the
Dockerfile.

Input shape (identical to the API endpoint's persistence):

    {
        "source":       "hf_dataset" | "minio_refs",
        "dataset_id":   str | None,
        "minio_refs":   [str, ...],
        "split":        str | None,
        "sample_count": int,
        "content_hash": str,
    }

Output shape:

    build_calibration_flags(config) ->
        {
            "cli_args":        str,   # ready to splice into the RUN line
            "docker_prelude":  str,   # RUN step that materialises minio
                                      # refs (when source=minio_refs)
            "skip":            bool,  # True when config is None/empty
        }

The `docker_prelude` runs BEFORE `optimum-cli export openvino`:
- source=hf_dataset  → no prelude; optimum-cli fetches the dataset.
- source=minio_refs  → a RUN step that `mc cp`s refs into
                        `/opt/calibration_set/` + points
                        `--dataset` at that directory.

The Dockerfile template gets the flags + prelude; the ImageBuilder
doesn't need to know calibration was in play.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CALIB_DIR = "/opt/calibration_set"
ENV_VAR_NAME = "INLINE_CALIBRATION_CONFIG"


def load_config_from_env() -> Optional[Dict[str, Any]]:
    """Parse the INLINE_CALIBRATION_CONFIG env var. Returns None when
    unset / unparseable / shape-invalid. Callers treat None as "use
    Optimum's default dataset" — same semantics as pre-Phase-9."""
    raw = os.environ.get(ENV_VAR_NAME, "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("calibration_runner: INLINE_CALIBRATION_CONFIG not valid JSON")
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("source") not in ("hf_dataset", "minio_refs"):
        return None
    return obj


def _shell_quote(s: str) -> str:
    if not s:
        return "''"
    if all(c.isalnum() or c in "._-=<>!~+@/:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _format_sample_count(n: Any) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return 100
    return max(1, min(1024, v))


def build_calibration_flags(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the CLI-args + Dockerfile-prelude pair to splice into
    the intel_npu recipe's Dockerfile template.

    Idempotent: passing None or an invalid shape yields a skip-signal
    and empty strings — callers fall through to Optimum's default.
    """
    if not isinstance(config, dict):
        return {"cli_args": "", "docker_prelude": "", "skip": True}

    source = config.get("source")
    sample_count = _format_sample_count(config.get("sample_count"))

    if source == "hf_dataset":
        dataset_id = (config.get("dataset_id") or "").strip()
        if not dataset_id:
            return {"cli_args": "", "docker_prelude": "", "skip": True}
        cli_args = (
            f"--dataset {_shell_quote(dataset_id)} "
            f"--num-samples {sample_count}"
        )
        split = (config.get("split") or "").strip()
        if split:
            cli_args += f" --dataset-split {_shell_quote(split)}"
        return {
            "cli_args": cli_args,
            "docker_prelude": "",
            "skip": False,
        }

    if source == "minio_refs":
        refs = config.get("minio_refs") or []
        refs = [r for r in refs if isinstance(r, str) and r.strip()]
        if not refs:
            return {"cli_args": "", "docker_prelude": "", "skip": True}
        # Prelude: fetch each ref into CALIB_DIR. We rely on `mc` being
        # available in the base image (it ships with the tenant-harbor
        # stack's image and the NPU base); emit `aws s3 cp` as an
        # alternative when MINIO_ENDPOINT env carries "s3://" style.
        prelude_lines = [
            f"RUN mkdir -p {CALIB_DIR}",
        ]
        # Use the tenant's minio endpoint via env var the worker forwards.
        for idx, ref in enumerate(refs[:1024]):
            # `mc cp` expects alias/bucket/key form; expect the ref to
            # already include tenant bucket prefix.
            prelude_lines.append(
                f"RUN --mount=type=secret,id=minio_env "
                f". /run/secrets/minio_env && "
                f"mc cp \"$MC_ALIAS/{_shell_quote(ref)}\" "
                f"{CALIB_DIR}/sample_{idx:04d}.bin"
            )
        cli_args = (
            f"--dataset {CALIB_DIR} "
            f"--num-samples {sample_count}"
        )
        return {
            "cli_args": cli_args,
            "docker_prelude": "\n".join(prelude_lines),
            "skip": False,
        }

    return {"cli_args": "", "docker_prelude": "", "skip": True}


def apply_to_optimum_cli(
    base_cli_line: str,
    flags: Dict[str, Any],
) -> str:
    """Splice the calibration flags into the optimum-cli export line.
    The existing recipe's line ends with `{ov_path}` — we inject the
    flags immediately before that final arg so the output path
    remains the last positional.

    When `flags.skip` is True, returns the line unchanged.
    """
    if flags.get("skip") or not flags.get("cli_args"):
        return base_cli_line
    cli_args = flags["cli_args"]
    # Look for the continuation before `{ov_path}` — a backslash
    # followed by whitespace then the token. This is the shape the
    # intel_npu recipe template already uses; if the caller deviates,
    # we fall back to appending at the end.
    marker = "{ov_path}"
    if marker in base_cli_line:
        return base_cli_line.replace(
            marker,
            f"{cli_args} \\\n        {marker}",
        )
    return base_cli_line + f" {cli_args}"


__all__ = [
    "ENV_VAR_NAME",
    "CALIB_DIR",
    "load_config_from_env",
    "build_calibration_flags",
    "apply_to_optimum_cli",
]

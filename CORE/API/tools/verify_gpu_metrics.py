#!/usr/bin/env python3
"""18.5 (GI.5) — one-command operator check of the k8s per-task GPU metrics read path.

Usage:
    python3 -m tools.verify_gpu_metrics \
        --prom-url http://prom:9090 \
        --namespace tenant-1 --pod mod-task-42-abc \
        --window-s 300 [--sidecar-file /var/log/mod-gpu-samples.csv] [--json]

PASS/FAIL on stdout/stderr, exit 0 pass / 1 no metrics / 2 usage or validation.
Imports only services.k8s_task_metrics functions (read-only): no DB, no tenant
data, no API call, no new env var.  Never prints the Prometheus URL, the query
text, or a response body — only the source label and the two numbers.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from utils.paid_import import optional_paid

_K8S_NAME_RE = re.compile(r"\A[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?\Z")
MAX_SIDECAR_BYTES = 1048576  # 1 MiB


def _ktm():
    """Call-time fetch of the entitlement read-path module (None on CE).

    ``services/k8s_task_metrics.py`` is denied in open-core/manifest.yaml, so the
    CE carve strips it; a module-level import here would make the operator CLI
    die with an ImportError traceback at startup. Absent == this edition has no
    GPU-metrics read path to check: the caller prints one usage line and exits 2.
    """
    return optional_paid("services.k8s_task_metrics")


def _default_get(url: str, params: Dict[str, Any]) -> Any:
    """Prometheus transport that NEVER follows redirects (the shared getter
    defaults to ``allow_redirects=True``, which would let a 3xx hop move the
    query to another host)."""
    import requests  # lazy: tests inject a fixture

    ktm = _ktm()
    if ktm is None:
        raise ValueError("k8s_task_metrics: not in this edition")
    return requests.get(
        url,
        params=params,
        timeout=ktm.PROMETHEUS_TIMEOUT,
        allow_redirects=False,
    )


def _validate_url(raw: Any) -> str:
    """Cleaned URL or ValueError('bad_url: ...'); the value is never echoed."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("bad_url: empty")
    value = raw.strip()
    if "://" not in value:
        raise ValueError("bad_url: missing scheme")
    scheme, rest = value.split("://", 1)
    if scheme not in ("http", "https"):
        raise ValueError("bad_url: scheme not http/https")
    if "@" in rest:
        raise ValueError("bad_url: embedded credentials")
    if "#" in rest:
        raise ValueError("bad_url: fragment present")
    host = rest.split("/")[0].split(":")[0]
    if not host:
        raise ValueError("bad_url: no host")
    return value


def _validate_name(raw: Any, label: str) -> str:
    """k8s-name charset gate BEFORE the value reaches PromQL label matchers."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"bad_{label}: empty")
    if not _K8S_NAME_RE.match(raw):
        raise ValueError(f"bad_{label}: charset")
    return raw


def _read_sidecar(path: Any) -> str:
    """Sidecar file text: regular file only, at most 1 MiB, UTF-8/replace."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("unreadable_sidecar_file: empty path")
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_file():
            raise ValueError("unreadable_sidecar_file: not a regular file")
        if p.stat().st_size > MAX_SIDECAR_BYTES:
            raise ValueError("unreadable_sidecar_file: exceeds 1 MiB")
        return p.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("unreadable_sidecar_file: os error") from exc


def run_check(
    prom_url: str,
    namespace: str,
    pod: str,
    window_s: float,
    sidecar_text: Optional[str] = None,
    *,
    http_get: Optional[Callable] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Core testable check: ``{"peak_vram_mb","gpu_util_pct","source"}`` or
    ``None`` when no source produced data.  Raises ``ValueError`` (a class
    label only, never a value) on unusable inputs so the caller can exit 2.
    Window math always uses the injected ``now`` (default: wall clock)."""
    url = _validate_url(prom_url)
    ns = _validate_name(namespace, "namespace")
    pod_name = _validate_name(pod, "pod")
    window = float(window_s)
    if not math.isfinite(window) or window <= 0:
        raise ValueError("bad_window: not a positive number of seconds")
    end = float(now) if now is not None else time.time()
    start = end - window

    ktm = _ktm()
    if ktm is None:
        raise ValueError("k8s_task_metrics is not in this edition")

    metrics = ktm.query_peak_vram(
        url, ns, pod_name, start, end, http_get=http_get or _default_get
    )
    if metrics is not None:
        return metrics
    if sidecar_text is not None:
        return ktm.parse_sidecar_samples(sidecar_text)
    return None


def _human_output(metrics: Dict[str, Any]) -> str:
    return (
        f"PASS source={metrics['source']} "
        f"peak_vram_mb={metrics['peak_vram_mb']} "
        f"gpu_util_pct={metrics['gpu_util_pct']}"
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Check the k8s per-task GPU metrics read path for one pod."
    )
    p.add_argument("--prom-url", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--pod", required=True)
    p.add_argument("--window-s", type=float, default=300.0)
    p.add_argument("--sidecar-file", default=None)
    p.add_argument("--json", action="store_true", dest="as_json")
    args = p.parse_args(argv)

    try:
        url = _validate_url(args.prom_url)
        ns = _validate_name(args.namespace, "namespace")
        pod_name = _validate_name(args.pod, "pod")
    except ValueError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2
    if not math.isfinite(args.window_s) or args.window_s <= 0:
        print("FAIL bad_window: not a positive number of seconds", file=sys.stderr)
        return 2

    sidecar_text = None
    if args.sidecar_file:
        try:
            sidecar_text = _read_sidecar(args.sidecar_file)
        except ValueError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1

    try:
        metrics = run_check(url, ns, pod_name, args.window_s, sidecar_text)
    except ValueError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — fail closed, class label only
        print(f"FAIL check_raised: {type(exc).__name__}", file=sys.stderr)
        return 1

    if metrics is None:
        print("FAIL no_series: no Prometheus or sidecar data found", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(metrics, sort_keys=True))
    else:
        print(_human_output(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

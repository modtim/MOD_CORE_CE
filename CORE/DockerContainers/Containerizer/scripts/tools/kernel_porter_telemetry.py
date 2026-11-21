"""Phase 6 — structured stderr telemetry for kernel_porter outcomes.

The Containerizer runs inside a worker task; its stderr is captured into
`log_tail` and flows back to the API through the ApiTaskQueue result
payload. We piggyback on that existing channel to emit structured port
outcomes without a new network dependency.

Each event is a single stderr line:

    [port_telemetry] {"event": "port_outcome.recorded", ...}

The API's result parser can grep for `[port_telemetry]` and materialise
rows on the admin port-coverage dashboard without touching the
hot-path log.

Event schema (v1):
  event            constant "port_outcome.recorded"
  variant          e.g. "rocm", "intel", "npu"
  tool             e.g. "hipify-clang", "torch.hipify (auto)", "dpct",
                   "openvino", "preflight-only", "noop"
  compatibility    "compatible" | "not_recommended" | "incompatible"
  wall_ms          integer port-pass wall-clock for this variant
  unsupported_count int — len(port_report.unsupported_apis)
  files_translated int — len(port_report.files_translated)
  reason           short string; may be null for `compatible`
  porter_version   str — kernel_porter PORTER_VERSION + tool fingerprint
  hipify_warning_count int | null
                   — populated only when _hipify_subprocess ran; lets
                     the API cluster repos by HIPIFY warning density.
  emitted_at       iso8601 UTC

Kept deliberately small: one line per variant per port. Big-payload
diagnostics (unsupported API lists, manual cleanup hints) stay on the
PortOutcome row where they already live."""
from __future__ import annotations

import datetime as _dt
import json as _json
import sys as _sys
from typing import Any, Dict, Optional

_EVENT_VERSION = 1
_PREFIX = "[port_telemetry]"


def emit_port_outcome(
    *,
    variant: str,
    tool: str,
    compatibility: str,
    wall_ms: int,
    unsupported_count: int,
    files_translated: int,
    reason: Optional[str],
    porter_version: str,
    hipify_warning_count: Optional[int] = None,
) -> None:
    """Write one `[port_telemetry] {...}` line to stderr.

    Never raises — telemetry must not break the port pass. Any encoding
    failure is swallowed; the caller still gets the port outcome through
    the normal Containerizer result JSON.
    """
    payload: Dict[str, Any] = {
        "v": _EVENT_VERSION,
        "event": "port_outcome.recorded",
        "variant": variant,
        "tool": tool,
        "compatibility": compatibility,
        "wall_ms": int(wall_ms),
        "unsupported_count": int(unsupported_count),
        "files_translated": int(files_translated),
        "reason": reason,
        "porter_version": porter_version,
        "hipify_warning_count": hipify_warning_count,
        "emitted_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    try:
        line = f"{_PREFIX} {_json.dumps(payload, sort_keys=True)}"
    except Exception:  # noqa: BLE001
        return
    try:
        print(line, file=_sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        return


def parse_telemetry_lines(log_tail: str) -> list[Dict[str, Any]]:
    """Complement to `emit_port_outcome`: extract structured events from a
    worker log_tail blob. The API uses this to aggregate port outcomes on
    the coverage dashboard; tests call it to assert emissions happened."""
    out: list[Dict[str, Any]] = []
    if not log_tail:
        return out
    for line in log_tail.splitlines():
        idx = line.find(_PREFIX)
        if idx < 0:
            continue
        payload_str = line[idx + len(_PREFIX):].strip()
        if not payload_str:
            continue
        try:
            obj = _json.loads(payload_str)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict) and obj.get("event") == "port_outcome.recorded":
            out.append(obj)
    return out


__all__ = ["emit_port_outcome", "parse_telemetry_lines"]

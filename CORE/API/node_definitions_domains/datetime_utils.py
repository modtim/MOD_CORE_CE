"""node_definitions_domains/datetime_utils — DateTime domain-primitive NodeDefinitions.

launch-completion-megaplan sz-6.15.1 (Phase 6: Domain primitives). Registers five
system node definitions backed by the datetime-utils container
(`CORE/DockerContainers/datetime-utils`): parse, format, convert timezone,
add/subtract duration, and diff between two datetimes.

Posture notes
-------------
- Specs are plain dicts (same shape as `seed_converters.py`) so the module is
  importable + testable without a DB; `register_datetime_utils()` is the only
  DB-touching entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog already speaks:
  the conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility checks,
  AND named ports (`data`, `text`) so the UI can label the ports this domain
  actually exposes. `text` is in `node_builder/analyze_prompt.py`'s
  VALID_INPUT_TYPES.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing system
  row rather than duplicating it (system-node names are globally unique).
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1), exactly as `seed_converters.py` does — existing rows are never deleted.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTAINER_REPO_TAG = "datetime-utils:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/datetime-utils/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# The entrypoint selects its operation with os.getenv("DATETIME_OP", "parse"),
# so every spec must pin that variable in its configuration_schema or the node
# silently runs the container default ("parse"). Unlike crypto-utils, the
# datetime entrypoint does NOT strip a name prefix, so the value the container
# accepts is the short op name — hence this explicit map.
CONTAINER_OP_NAMES = {
    "datetime_parse": "parse",
    "datetime_format": "format",
    "datetime_convert_timezone": "convert_timezone",
    "datetime_add_subtract": "add_subtract_duration",
    "datetime_diff": "diff",
}

DATETIME_OP_DESCRIPTION = (
    "Fixed for this node: selects the datetime-utils operation. "
    "Leave it as-is unless you have a reason to change it."
)

# ---------------------------------------------------------------------------
# Entrypoint source — byte-identical mirror of the Dockerfile's quoted
# heredoc above (CORE/DockerContainers/datetime-utils/Dockerfile). The Dockerfile
# stays the source of truth; this constant exists so the op functions are
# unit-testable without building the image (tests exec this string). Keep the two
# in sync — any entrypoint change lands in BOTH files.
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r'''#!/usr/bin/env python3
"""datetime-utils entrypoint. See the header comment in the Dockerfile."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_SIZE = 10 * 1024 * 1024  # 10 MB input cap
MAX_FORMAT_LEN = 64  # bound on user-supplied strftime/strptime format strings

# Vet step 5 (fail-closed): every failure path ends in fail(), which prints
# exactly one JSON line on stdout and exits non-zero.
CODE_NO_INPUT = "NO_INPUT"
CODE_BAD_PARAM = "BAD_PARAM"
CODE_PROCESSING = "PROCESSING_FAILURE"
CODE_OUTPUT_NOT_WRITTEN = "OUTPUT_NOT_WRITTEN"
CODE_INTERNAL = "INTERNAL"


class NodeError(ValueError):
    """Bad-input failure carrying its contract code.

    Subclasses ValueError so the per-op unit tests (pytest.raises(ValueError,
    match=...)) keep working while main() maps each raise to its code.
    """

    def __init__(self, msg, code=CODE_BAD_PARAM):
        super().__init__(msg)
        self.code = code


def _write_error(msg):
    # Best-effort sidecar; the stdout JSON from fail() is the contract.
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "error.json").write_text(json.dumps({"error": str(msg)}), encoding="utf-8")
    except OSError:
        pass


def fail(msg, code=CODE_INTERNAL):
    # Vet step 5: one JSON line on stdout, then exit non-zero.
    sys.stderr.write(f"ERROR: {msg}\n")
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    _write_error(msg)
    sys.exit(1)


def _read_all_text(name):
    p = IN / name
    if not p.is_file():
        return None
    if p.stat().st_size > MAX_SIZE:
        raise NodeError(f"Input {name} exceeds {MAX_SIZE} byte limit", CODE_NO_INPUT)
    return p.read_text(encoding="utf-8")


def _write_output(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        (OUT / name).write_text(value, encoding="utf-8")
    except OSError as exc:
        raise NodeError(f"could not write {name}: {exc}", CODE_OUTPUT_NOT_WRITTEN)


# Pre-compute the whitelist once at module load; bounded to ~400 zones.
_TIMEZONE_WHITELIST = frozenset(available_timezones() or set())


def op_parse(fields):
    """Parse a date string into an ISO-8601 datetime string."""
    raw = fields.get("date_string", "").strip()
    if not raw:
        raise NodeError("Empty date_string", CODE_NO_INPUT)
    fmt = fields.get("strftime_format")
    if fmt:
        if len(fmt) > MAX_FORMAT_LEN:
            raise NodeError(f"strftime_format exceeds {MAX_FORMAT_LEN} char limit", CODE_BAD_PARAM)
        dt = datetime.strptime(raw, fmt)
    else:
        dt = datetime.fromisoformat(raw)
    _write_output("parsed.txt", dt.isoformat())


def op_format(fields):
    """Format a datetime into an arbitrary strftime string."""
    raw = fields.get("date_string", "").strip()
    fmt = fields.get("strftime_format", "").strip()
    if not raw:
        raise NodeError("Empty date_string", CODE_NO_INPUT)
    if not fmt:
        raise NodeError("Empty strftime_format", CODE_NO_INPUT)
    if len(fmt) > MAX_FORMAT_LEN:
        raise ValueError(f"strftime_format exceeds {MAX_FORMAT_LEN} char limit")
    dt = datetime.fromisoformat(raw)
    _write_output("formatted.txt", dt.strftime(fmt))


def op_convert_timezone(fields):
    """Convert a datetime to a target IANA timezone."""
    raw = fields.get("date_string", "").strip()
    target_tz_name = fields.get("target_timezone", "").strip()
    if not raw:
        raise NodeError("Empty date_string", CODE_NO_INPUT)
    if not target_tz_name:
        raise NodeError("Empty target_timezone", CODE_NO_INPUT)
    if target_tz_name not in _TIMEZONE_WHITELIST:
        raise NodeError(f"Timezone {target_tz_name!r} not in whitelist", CODE_BAD_PARAM)
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    converted = dt.astimezone(ZoneInfo(target_tz_name))
    _write_output("converted.txt", converted.isoformat())


def op_add_subtract_duration(fields):
    """Add or subtract an ISO-8601 duration or structured duration object."""
    raw = fields.get("date_string", "").strip()
    duration_val = fields.get("duration")
    sign = fields.get("sign", "+")
    if not raw:
        raise NodeError("Empty date_string", CODE_NO_INPUT)
    if duration_val is None:
        raise NodeError("Missing duration", CODE_BAD_PARAM)

    if isinstance(duration_val, str):
        dt = datetime.fromisoformat(raw)
        parts = duration_val.lstrip("-")
        d = timedelta()
        i = 0
        while i < len(parts):
            ch = parts[i]
            if ch in ("P", "T"):
                i += 1
                continue
            if ch in "DHMS":
                # Extract the numeric portion right before the unit letter
                num_str = parts[0:i].replace("P", "").replace("T", "")
                if num_str:
                    if ch == "D":
                        d += timedelta(days=int(num_str))
                    elif ch == "H":
                        d += timedelta(hours=int(num_str))
                    elif ch == "M":
                        d += timedelta(minutes=int(num_str))
                    elif ch == "S":
                        d += timedelta(seconds=int(num_str))
                # Move past the unit letter, and clear the part before
                # for the next iteration (next number starts fresh)
                i += 1
                parts = parts[i:]
                i = 0
            else:
                i += 1
        if sign == "-":
            d = -d
        dt = dt + d
    elif isinstance(duration_val, dict):
        days = duration_val.get("days", 0)
        hours = duration_val.get("hours", 0)
        minutes = duration_val.get("minutes", 0)
        seconds = duration_val.get("seconds", 0)
        td = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        if sign == "-":
            td = -td
        dt = datetime.fromisoformat(raw) + td
    else:
        raise NodeError(f"Unsupported duration type: {type(duration_val)}", CODE_BAD_PARAM)

    _write_output("result.txt", dt.isoformat())


def op_diff(fields):
    """Compute the difference between two datetimes."""
    raw1 = fields.get("datetime_a", "").strip()
    raw2 = fields.get("datetime_b", "").strip()
    if not raw1 or not raw2:
        raise NodeError("Missing datetime_a or datetime_b", CODE_NO_INPUT)
    dt_a = datetime.fromisoformat(raw1)
    dt_b = datetime.fromisoformat(raw2)
    delta = dt_b - dt_a
    abs_delta = abs(delta)
    total_secs = int(abs_delta.total_seconds())
    sign = -1 if delta.total_seconds() < 0 else 1
    days = abs_delta.days
    remainder = abs_delta - timedelta(days=days)
    result = {
        "total_seconds": sign * total_secs,
        "days": sign * delta.days,
        "breakdown": {
            "days": days * sign,
            "hours": remainder.seconds // 3600 * sign,
            "minutes": (remainder.seconds // 60) % 60 * sign,
            "seconds": remainder.seconds % 60 * sign,
        },
    }

    _write_output("diff.json", json.dumps(result))


OPS = {
    "parse": op_parse,
    "format": op_format,
    "convert_timezone": op_convert_timezone,
    "add_subtract_duration": op_add_subtract_duration,
    "diff": op_diff,
}


def main():
    try:
        op = os.getenv("DATETIME_OP", "parse").strip().lower()
        if op not in OPS:
            fail(f"unknown DATETIME_OP {op!r}", CODE_BAD_PARAM)

        # Read fields from fields.json on input
        fields_path = IN / "fields.json"
        if not fields_path.is_file():
            fail("fields.json required on input port", CODE_NO_INPUT)
        fields_bytes = fields_path.read_bytes()
        if len(fields_bytes) > MAX_SIZE:
            fail(f"Input exceeds {MAX_SIZE} byte limit", CODE_NO_INPUT)
        try:
            fields = json.loads(fields_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            fail(f"Invalid fields.json: {exc}", CODE_BAD_PARAM)
        if not isinstance(fields, dict):
            fail("fields.json must be a JSON object", CODE_BAD_PARAM)

        OPS[op](fields)
    except SystemExit:
        raise
    except NodeError as exc:
        fail(str(exc), exc.code)
    except Exception as exc:  # noqa: BLE001 - untrusted input, always report cleanly
        fail(f"{type(exc).__name__}: {exc}", CODE_INTERNAL)


if __name__ == "__main__":
    main()'''

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts — one per operation.
# ---------------------------------------------------------------------------

DATETIME_PARSE_SPEC = {
    "name": "Datetime Parse",
    "description": (
        "Parse a date string into an ISO-8601 datetime. Accepts a `date_string` "
        "field and optionally a `strftime_format` field (Python strptime format). "
        "If no format is given, the string is parsed as ISO-8601. "
        "Outputs `parsed.txt` with the ISO-8601 result."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "datetime_parse",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "date_string": {"type": "text", "description": "The date string to parse."},
    },
    "output_schema": {
        "output_type": "text",
        "parsed": {"type": "text", "description": "ISO-8601 parsed datetime."},
    },
    "configuration_schema": {
        "DATETIME_OP": {
            "type": "string",
            "default": CONTAINER_OP_NAMES["datetime_parse"],
            "description": DATETIME_OP_DESCRIPTION,
        },
        "extra_variables": {
            "strftime_format": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional Python strptime format, e.g. '%Y-%m-%d'. "
                    "Blank = auto-detect ISO-8601."
                ),
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

DATETIME_FORMAT_SPEC = {
    "name": "Datetime Format",
    "description": (
        "Format an ISO-8601 datetime into a custom strftime string. Requires "
        "`date_string` and `strftime_format` fields. "
        "Outputs `formatted.txt` with the formatted result."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "datetime_format",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "date_string": {"type": "text", "description": "The datetime string to format."},
    },
    "output_schema": {
        "output_type": "text",
        "formatted": {"type": "text", "description": "Formatted datetime string."},
    },
    "configuration_schema": {
        "DATETIME_OP": {
            "type": "string",
            "default": CONTAINER_OP_NAMES["datetime_format"],
            "description": DATETIME_OP_DESCRIPTION,
        },
        "extra_variables": {
            "strftime_format": {
                "type": "string",
                "default": "%Y-%m-%d %H:%M:%S",
                "description": "Python strftime format for output.",
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

DATETIME_CONVERT_TZ_SPEC = {
    "name": "Datetime Convert Timezone",
    "description": (
        "Convert a datetime to a target IANA timezone (e.g. 'America/New_York'). "
        "Requires `date_string` and `target_timezone` fields. "
        "Timezone is validated against zoneinfo.available_timezones() whitelist. "
        "Outputs `converted.txt` with the result in the target timezone."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "datetime_convert_timezone",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "date_string": {"type": "text", "description": "The datetime to convert."},
    },
    "output_schema": {
        "output_type": "text",
        "converted": {
            "type": "text",
            "description": "Datetime converted to target timezone.",
        },
    },
    "configuration_schema": {
        "DATETIME_OP": {
            "type": "string",
            "default": CONTAINER_OP_NAMES["datetime_convert_timezone"],
            "description": DATETIME_OP_DESCRIPTION,
        },
        "extra_variables": {
            "target_timezone": {
                "type": "string",
                "default": "UTC",
                "description": "IANA timezone name (e.g. America/New_York).",
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

DATETIME_ADD_SUBTRACT_SPEC = {
    "name": "Datetime Add/Subtract Duration",
    "description": (
        "Add or subtract an ISO-8601 duration (e.g. P3DT12H) or a structured "
        "object {days, hours, minutes, seconds} from a datetime. "
        "Requires `date_string` and `duration` fields. "
        "An optional `sign` field (default '+') controls add vs subtract. "
        "Outputs `result.txt` with the resulting datetime."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "datetime_add_subtract",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "date_string": {"type": "text", "description": "The base datetime."},
    },
    "output_schema": {
        "output_type": "text",
        "result": {"type": "text", "description": "Resulting datetime."},
    },
    "configuration_schema": {
        "DATETIME_OP": {
            "type": "string",
            "default": CONTAINER_OP_NAMES["datetime_add_subtract"],
            "description": DATETIME_OP_DESCRIPTION,
        },
        "extra_variables": {
            "duration": {
                "type": "string",
                "default": "P0D",
                "description": "ISO-8601 duration (P3DT12H) or JSON duration object.",
            },
            "sign": {
                "type": "string",
                "default": "+",
                "description": "'+' to add, '-' to subtract.",
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

DATETIME_DIFF_SPEC = {
    "name": "Datetime Diff",
    "description": (
        "Compute the difference between two datetimes. Requires `datetime_a` "
        "and `datetime_b` fields. Returns total_seconds, days, and a breakdown "
        "into days/hours/minutes/seconds. Outputs `diff.json` with the result."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "datetime_diff",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "datetime_a": {"type": "text", "description": "First datetime."},
        "datetime_b": {"type": "text", "description": "Second datetime."},
    },
    "output_schema": {
        "output_type": "text",
        "diff": {
            "type": "text",
            "description": "JSON with total_seconds, days, and breakdown.",
        },
    },
    "configuration_schema": {
        "DATETIME_OP": {
            "type": "string",
            "default": CONTAINER_OP_NAMES["datetime_diff"],
            "description": DATETIME_OP_DESCRIPTION,
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

# ---------------------------------------------------------------------------
# Registry lists
# ---------------------------------------------------------------------------

NODE_DEFINITIONS = [
    DATETIME_PARSE_SPEC,
    DATETIME_FORMAT_SPEC,
    DATETIME_CONVERT_TZ_SPEC,
    DATETIME_ADD_SUBTRACT_SPEC,
    DATETIME_DIFF_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "datetime_parse": DATETIME_PARSE_SPEC,
    "datetime_format": DATETIME_FORMAT_SPEC,
    "datetime_convert_timezone": DATETIME_CONVERT_TZ_SPEC,
    "datetime_add_subtract": DATETIME_ADD_SUBTRACT_SPEC,
    "datetime_diff": DATETIME_DIFF_SPEC,
}

RECONCILED_FIELDS = (
    "description",
    "executable_path",
    "input_schema",
    "output_schema",
    "configuration_schema",
    "accepted_formats",
    "cost_in_credits",
)


def register_datetime_utils(db: Session) -> int:
    """Register the 5 datetime operations as system node definitions.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping datetime-utils seed"
        )
        return 0

    created = 0
    for spec in NODE_DEFINITIONS:
        existing = db.query(NodeDefinition).filter(
            NodeDefinition.name == spec["name"],
            NodeDefinition.tenant_id.is_(None),
        ).first()

        if existing:
            for field in RECONCILED_FIELDS:
                setattr(existing, field, spec[field])
            continue

        db.add(
            NodeDefinition(
                name=spec["name"],
                tenant_id=None,  # system node — available to all tenants
                description=spec["description"],
                type=spec["type"],
                executable_path=spec["executable_path"],
                process_type=spec["process_type"],
                node_kind=spec["node_kind"],
                batch_mode=spec["batch_mode"],
                input_schema=spec["input_schema"],
                output_schema=spec["output_schema"],
                configuration_schema=spec["configuration_schema"],
                accepted_formats=spec["accepted_formats"],
                cost_in_credits=spec["cost_in_credits"],
            )
        )
        created += 1

    if created > 0:
        db.commit()

    return created

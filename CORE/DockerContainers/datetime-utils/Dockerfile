# datetime-utils — MOD node container for date/time domain primitives
# (launch-completion-megaplan sz-6.15.1). One container, five operations:
#
#   DATETIME_OP=parse            date string  -> parsed.txt
#   DATETIME_OP=format           datetime + fmt -> formatted.txt
#   DATETIME_OP=convert_timezone datetime -> tgt_tz -> converted.txt
#   DATETIME_OP=add_subtract_duration datetime + dur -> result.txt
#   DATETIME_OP=diff             two datetimes -> diff.json
#
# Same I/O contract as every other MOD node container: the worker stages the
# node's input files into /data/input and collects /data/output.

FROM python:3.11-slim

RUN mkdir -p /data/input /data/output /app

# The entrypoint is written here rather than COPYed so the container is
# self-contained (one file, no sidecar to keep in sync).
RUN cat > /app/datetime_process.py <<'PYEOF'
#!/usr/bin/env python3
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
    main()
PYEOF

WORKDIR /data
ENTRYPOINT ["python3", "/app/datetime_process.py"]

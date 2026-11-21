#!/usr/bin/env python3
"""GY.X4 — offline verifier for the sealed-mode 24h soak evidence file.

Usage:
    python3 tools/verify_sealed_soak.py <soak-evidence.json>
        [--min-hours 24] [--max-gap-seconds 900]

Exit codes: 0 = the capture proves a sealed soak with zero outbound
attempts, 1 = anything else (fail closed: unreadable file, invalid JSON,
missing key, wrong type, non-UTC timestamp, hole in the samples, reset
counter, any accepted external packet, any dropped packet delta).

Capture format (the contract the lead's HW soak run follows; the file is
produced with nft counters on the soak host that applies the deny ruleset
of CORE/Worker/internal/lockdown/rules.go):

    {"schema":"mod.sealed-soak/1",
     "started_at":"<UTC ISO8601 Z>","ended_at":"<UTC ISO8601 Z>",
     "sealed_mode":true,
     "ruleset_default_policy":"drop",
     "ruleset_sha256":"<64 lowercase hex>",
     "samples":[{"t":"<UTC ISO8601 Z>",
                 "output_drop_packets":<int>,
                 "output_accept_external_packets":<int>}, ...]}

Rules (each failure is one reason string; all must hold):
  * schema equals mod.sealed-soak/1;
  * sealed_mode is exactly the boolean true (the egress.sealed_mode
    setting was on for the whole soak);
  * ruleset_default_policy == "drop";
  * ruleset_sha256 is 64 lowercase hex;
  * ended_at - started_at >= --min-hours;
  * at least 2 samples; first sample within --max-gap-seconds of
    started_at, last within --max-gap-seconds of ended_at, and every
    consecutive gap <= --max-gap-seconds (a soak with holes is not a
    soak);
  * both counters are non-negative ints (bools never count as ints) and
    non-decreasing across samples (a decreasing counter = reset/tamper);
  * output_accept_external_packets is 0 in EVERY sample (nothing ever
    left the host);
  * last output_drop_packets minus first is 0 (zero outbound attempts).

Stdlib only; no network module is imported and no socket is opened.
Reasons name fields and counts only — never sample contents.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime

SCHEMA = "mod.sealed-soak/1"
SHA_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_ts(value):
    """Strict UTC-ISO8601-Z parser; returns None when not parseable."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


def verify(doc: dict, min_hours: float = 24.0,
           max_gap_seconds: int = 900) -> list[str]:
    reasons = []
    if not isinstance(doc, dict):
        return ["document is not a JSON object"]
    if not isinstance(min_hours, (int, float)) or (
            not math.isfinite(min_hours) and not math.isinf(min_hours)):
        reasons.append("min_hours is not a finite number")
    if not isinstance(max_gap_seconds, (int, float)) or (
            not math.isfinite(max_gap_seconds) and
            not math.isinf(max_gap_seconds)):
        reasons.append("max_gap_seconds is not a finite number")
    if reasons:
        return reasons
    if doc.get("schema") != SCHEMA:
        reasons.append("schema mismatch (expected %s)" % SCHEMA)
    if doc.get("sealed_mode") is not True:
        reasons.append("sealed_mode is not exactly true")
    if doc.get("ruleset_default_policy") != "drop":
        reasons.append("ruleset_default_policy is not drop")
    sha = doc.get("ruleset_sha256")
    if not (isinstance(sha, str) and SHA_RE.match(sha)):
        reasons.append("ruleset_sha256 is not 64 lowercase hex")
    started = _parse_ts(doc.get("started_at"))
    ended = _parse_ts(doc.get("ended_at"))
    if started is None:
        reasons.append("started_at is not a UTC ISO8601 Z timestamp")
    if ended is None:
        reasons.append("ended_at is not a UTC ISO8601 Z timestamp")
    if started is not None and ended is not None:
        duration = (ended - started).total_seconds()
        if duration < 0:
            reasons.append("ended_at is before started_at")
        elif duration < min_hours * 3600:
            reasons.append("soak duration %.3f hours is under %.3f hours"
                           % (duration / 3600.0, min_hours))
    samples = doc.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        reasons.append("samples missing, not a list, or fewer than 2")
        return reasons
    parsed = []
    for idx, sample in enumerate(samples):
        if not isinstance(sample, dict):
            reasons.append("sample %d is not an object" % idx)
            continue
        stamp = _parse_ts(sample.get("t"))
        if stamp is None:
            reasons.append("sample %d has a non-UTC-Z timestamp" % idx)
            continue
        drop = sample.get("output_drop_packets")
        accept = sample.get("output_accept_external_packets")
        if not _is_int(drop) or drop < 0:
            reasons.append(
                "sample %d output_drop_packets is not a non-negative int"
                % idx)
            continue
        if not _is_int(accept) or accept < 0:
            reasons.append(
                "sample %d output_accept_external_packets is not a "
                "non-negative int" % idx)
            continue
        parsed.append((stamp, drop, accept))
    if len(parsed) < 2:
        return reasons
    for idx, (stamp, drop, accept) in enumerate(parsed):
        if accept != 0:
            reasons.append(
                "sample %d output_accept_external_packets is not 0 "
                "(%d external packets accepted)" % (idx, accept))
    for idx in range(1, len(parsed)):
        prev_stamp, prev_drop, prev_accept = parsed[idx - 1]
        stamp, drop, accept = parsed[idx]
        gap = (stamp - prev_stamp).total_seconds()
        if gap < 0:
            reasons.append("sample %d timestamp is out of order" % idx)
        elif gap > max_gap_seconds:
            reasons.append(
                "gap of %d seconds between sample %d and sample %d "
                "exceeds %d" % (int(gap), idx - 1, idx, max_gap_seconds))
        if drop < prev_drop:
            reasons.append(
                "sample %d output_drop_packets decreased (counter reset)"
                % idx)
        if accept < prev_accept:
            reasons.append(
                "sample %d output_accept_external_packets decreased "
                "(counter reset)" % idx)
    first_gap = abs((parsed[0][0] - started).total_seconds()) \
        if started is not None else None
    if first_gap is not None and first_gap > max_gap_seconds:
        reasons.append(
            "first sample is %d seconds from started_at (max %d)"
            % (int(first_gap), max_gap_seconds))
    last_gap = abs((ended - parsed[-1][0]).total_seconds()) \
        if ended is not None else None
    if last_gap is not None and last_gap > max_gap_seconds:
        reasons.append(
            "last sample is %d seconds from ended_at (max %d)"
            % (int(last_gap), max_gap_seconds))
    delta = parsed[-1][1] - parsed[0][1]
    if delta != 0:
        reasons.append(
            "output_drop_packets delta across the soak is %d "
            "(outbound attempts detected)" % delta)
    return reasons


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a sealed-mode soak evidence capture offline")
    parser.add_argument("evidence", help="path to the soak evidence json")
    parser.add_argument("--min-hours", dest="min_hours", type=float,
                        default=24.0,
                        help="minimum soak duration in hours")
    parser.add_argument("--max-gap-seconds", dest="max_gap_seconds",
                        type=int, default=900,
                        help="maximum gap between samples and window ends")
    args = parser.parse_args(argv)
    try:
        try:
            with open(args.evidence) as handle:
                doc = json.load(handle)
        except Exception as exc:
            print("FAIL: cannot read evidence file: %s" % exc,
                  file=sys.stderr)
            return 1
        reasons = verify(doc, args.min_hours, args.max_gap_seconds)
    except Exception as exc:
        print("FAIL: verifier error: %s" % exc, file=sys.stderr)
        return 1
    if reasons:
        for reason in reasons:
            print("FAIL: %s" % reason, file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        sys.exit(1)

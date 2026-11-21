"""Tests for the wizard build-task's remediation cache client + the
loop wiring that calls it.

The single most important property: the error-signature algorithm MUST
match ImageBuilder's, or the two build paths won't share cache entries.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

# stages/ on the path for remediation_client + test_and_repair_one_variant
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stages"))
# ImageBuilder/ on the path for the reference implementation
sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "ImageBuilder")
)

import remediation_client as rc  # noqa: E402


# ---------------------------------------------------------------------------
# Signature parity with ImageBuilder — the cache-sharing contract.
# ---------------------------------------------------------------------------

SAMPLE_ERRORS = [
    "ERROR: Unable to get page count. Is poppler installed and in PATH?",
    "#8 1.9 ERROR: No matching distribution found for pdf2image==0.3.9",
    "externally-managed-environment\nhint: See PEP 668 for the detailed specification.",
    "/tmp/pip-build-abc123/setup.py error at 2026-05-28T13:00:00Z pid=4567 line 12345",
]


def test_error_signature_matches_imagebuilder():
    import llm_remediation as ib  # ImageBuilder reference impl
    for err in SAMPLE_ERRORS:
        assert rc.error_signature(err) == ib.error_signature(err), (
            "build-task and ImageBuilder must compute identical "
            "signatures so they share cache entries"
        )


def test_diff_hash_matches_imagebuilder():
    import llm_remediation as ib
    for diff in ("FROM x\nRUN y", "", "FROM debian:bookworm-slim"):
        assert rc.diff_hash(diff) == ib.diff_hash(diff)


def test_normalise_strips_per_build_noise():
    a = rc.normalise_error("failed at 2026-05-28T13:00:00Z pid=4567 line 99999")
    b = rc.normalise_error("failed at 2026-01-01T00:00:00Z pid=11 line 12345")
    assert a == b  # timestamps / pids / long digit-runs normalised away


# ---------------------------------------------------------------------------
# known_bad_excerpts parsing
# ---------------------------------------------------------------------------

def test_known_bad_excerpts_extracts_strings():
    resp = {"known_bad": [
        {"diff_hash": "h1", "excerpt": "FROM debian\nRUN bad"},
        {"diff_hash": "h2", "excerpt": "FROM ubuntu\nRUN worse"},
        {"diff_hash": "h3", "excerpt": ""},  # empty skipped
    ]}
    out = rc.known_bad_excerpts(resp)
    assert out == ["FROM debian\nRUN bad", "FROM ubuntu\nRUN worse"]


def test_known_bad_excerpts_handles_none_and_empty():
    assert rc.known_bad_excerpts(None) == []
    assert rc.known_bad_excerpts({}) == []
    assert rc.known_bad_excerpts({"known_bad": []}) == []


# ---------------------------------------------------------------------------
# Loop wiring (source-level, matching test_test_and_repair_url.py style)
# ---------------------------------------------------------------------------

def test_repair_endpoint_forwards_cache_fields():
    import test_and_repair_one_variant as tar
    src = inspect.getsource(tar._call_repair_endpoint)
    for field in ("cache_hint", "known_bad", "promoted_diff"):
        assert field in src, f"_call_repair_endpoint must forward {field}"


def test_loop_records_success_and_failed_attempts():
    import test_and_repair_one_variant as tar
    src = inspect.getsource(tar.main)
    assert "record_success" in src, "loop must record converged fixes"
    assert "record_failed_attempt" in src or "_record_failed_if_repaired" in src, (
        "loop must record failed fixes (negative cache)"
    )
    assert "_cache_first_failure" in src, "loop must look up the cache on first failure"

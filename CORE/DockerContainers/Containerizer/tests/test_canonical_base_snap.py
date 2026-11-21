"""Tests for canonical_base_snap.

Keeps the wizard's hero FROM aligned with curated Harbor tags even
when the LLM emits a plausible-but-wrong ref (e.g. base-cuda:12.4
instead of base-cuda:12.4-python3.11 — a real production failure
observed during AMD/ROCm wizard runs).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts"),
)

import pytest  # noqa: E402

from tools.canonical_base_snap import snap_to_canonical  # noqa: E402


CANONICAL = [
    "ghcr.io/modtim/base-cuda:12.4-python3.11",
    "ghcr.io/modtim/base-rocm:6.1-python3.11",
    "ghcr.io/modtim/base-python:3.11",
    "ghcr.io/modtim/base-python:3.12",
    "ghcr.io/modtim/base-intel-xpu:2025.0",
]


def test_snaps_when_repo_has_single_canonical_tag():
    df = (
        "FROM ghcr.io/modtim/base-cuda:12.4\n"
        "WORKDIR /app\n"
        "RUN echo hi\n"
    )
    out, snapped = snap_to_canonical(df, CANONICAL)
    assert snapped is True
    assert "FROM ghcr.io/modtim/base-cuda:12.4-python3.11" in out
    assert "WORKDIR /app" in out
    assert "RUN echo hi" in out


def test_no_change_when_already_canonical():
    df = "FROM ghcr.io/modtim/base-rocm:6.1-python3.11\nWORKDIR /app\n"
    out, snapped = snap_to_canonical(df, CANONICAL)
    assert snapped is False
    assert out == df


def test_no_snap_when_repo_has_multiple_canonical_tags():
    df = "FROM ghcr.io/modtim/base-python:3.10\n"
    out, snapped = snap_to_canonical(df, CANONICAL)
    assert snapped is False
    assert out == df


def test_no_snap_for_unknown_repo():
    df = "FROM nvcr.io/nvidia/pytorch:24.01-py3\n"
    out, snapped = snap_to_canonical(df, CANONICAL)
    assert snapped is False
    assert out == df


def test_no_change_when_no_canonical_refs():
    df = "FROM ghcr.io/modtim/base-cuda:bogus\n"
    out, snapped = snap_to_canonical(df, [])
    assert snapped is False
    assert out == df


def test_handles_dockerfile_without_from():
    df = "# just a comment\nWORKDIR /app\n"
    out, snapped = snap_to_canonical(df, CANONICAL)
    assert snapped is False
    assert out == df


def test_only_first_from_is_rewritten():
    df = (
        "FROM ghcr.io/modtim/base-cuda:12.4 AS builder\n"
        "RUN make\n"
        "FROM ghcr.io/modtim/base-cuda:also-bogus\n"
    )
    out, snapped = snap_to_canonical(df, CANONICAL)
    assert snapped is True
    assert "FROM ghcr.io/modtim/base-cuda:12.4-python3.11 AS builder" in out
    assert "FROM ghcr.io/modtim/base-cuda:also-bogus" in out


def test_loads_from_env_var(monkeypatch):
    monkeypatch.setenv("CURATED_BASE_IMAGES_JSON", "[\"reg/repo:good\"]")
    out, snapped = snap_to_canonical("FROM reg/repo:bad\n")
    assert snapped is True
    assert "FROM reg/repo:good" in out


def test_malformed_env_var_is_safe(monkeypatch):
    monkeypatch.setenv("CURATED_BASE_IMAGES_JSON", "{not json")
    df = "FROM reg/repo:any\n"
    out, snapped = snap_to_canonical(df)
    assert snapped is False
    assert out == df


def test_preserves_image_digest_when_repo_unknown():
    df = "FROM mystery.io/foo@sha256:abc123\n"
    out, snapped = snap_to_canonical(df, CANONICAL)
    assert snapped is False
    assert out == df

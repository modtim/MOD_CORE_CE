"""Tests for ingest.py's clone-timeout handling (GSEC R-100).

The build path honours `CLONE_TIMEOUT_S` (DB platform setting
`wizard.clone_timeout_s`, passed by the generate stage via
build_dag/generate_env.py); the repo-docs/codebase ingest path must
honour the same env var with the same 30-3600s clamp that
git_operations.clone_repo_tool uses, falling back to the historic
600s default.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts"),
)

import pytest  # noqa: E402

from tools import ingest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CLONE_TIMEOUT_S", raising=False)


def test_fallback_when_unset(monkeypatch):
    assert ingest._clone_timeout_seconds() == 600


def test_honours_env(monkeypatch):
    monkeypatch.setenv("CLONE_TIMEOUT_S", "1200")
    assert ingest._clone_timeout_seconds() == 1200


def test_clamps_low_to_30(monkeypatch):
    monkeypatch.setenv("CLONE_TIMEOUT_S", "5")
    assert ingest._clone_timeout_seconds() == 30


def test_clamps_high_to_3600(monkeypatch):
    monkeypatch.setenv("CLONE_TIMEOUT_S", "999999")
    assert ingest._clone_timeout_seconds() == 3600


def test_unparsable_falls_back(monkeypatch):
    monkeypatch.setenv("CLONE_TIMEOUT_S", "not-a-number")
    assert ingest._clone_timeout_seconds() == 600


def test_blank_falls_back(monkeypatch):
    monkeypatch.setenv("CLONE_TIMEOUT_S", "  ")
    assert ingest._clone_timeout_seconds() == 600


def test_ingest_git_passes_clamped_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("CLONE_TIMEOUT_S", "4200")  # clamps to 3600
    seen = {}

    def _fake_run(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        return mock.MagicMock()

    monkeypatch.setattr(ingest.subprocess, "run", _fake_run)
    monkeypatch.setattr(ingest, "_git_head_sha", lambda p: "abc1234")
    monkeypatch.setattr(ingest, "_tree_size", lambda p: (10, 1))
    ingest._ingest_git(tmp_path, "https://example.com/org/repo.git")
    assert seen["timeout"] == 3600

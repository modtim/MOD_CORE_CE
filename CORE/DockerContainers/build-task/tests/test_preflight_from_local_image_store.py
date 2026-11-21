"""The FROM pre-flight must treat an image present in the local docker store
as resolved, without asking the registry (air-gapped CE installs; bases that
are private until published answer ``denied`` on a pull although the tag is
loaded in the daemon -- NB-PROOF-B F-1)."""
from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stages"))

import test_and_repair_one_variant as tar  # noqa: E402


def _fake_run(local_refs):
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        ref = argv[-1]
        rc = 0
        if argv[:3] == ["docker", "image", "inspect"]:
            rc = 0 if ref in local_refs else 1
        elif argv[:2] == ["docker", "pull"]:
            rc = 1  # registry says denied for everything
        return subprocess.CompletedProcess(argv, rc, "", "denied")

    return run, calls


def test_local_image_is_present(monkeypatch):
    run, calls = _fake_run({"ghcr.io/modtim/base-python:3.11"})
    monkeypatch.setattr(tar.subprocess, "run", run)
    assert tar._image_present_locally("ghcr.io/modtim/base-python:3.11") is True
    assert tar._image_present_locally("ghcr.io/modtim/other:1") is False
    assert all(c[:2] != ["docker", "pull"] for c in calls)


def test_missing_docker_cli_answers_false(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(tar.subprocess, "run", boom)
    assert tar._image_present_locally("x:1") is False


def test_preflight_checks_local_store_before_pull():
    src = inspect.getsource(tar.main)
    assert src.index("_image_present_locally(_ref)") < src.index('["docker", "pull", _ref]')

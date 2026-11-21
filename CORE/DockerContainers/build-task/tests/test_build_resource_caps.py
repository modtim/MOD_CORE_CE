"""The one build-command seam: resource caps, timeout, builder endpoint.

Both build call sites (build_one_variant.py and the hero build in
test_and_repair_one_variant.py) assemble their ``docker buildx build`` argv via
``_common.build_command``, so a different builder (e.g. a rootless BuildKit
sidecar) is a change in ONE function. Defaults are sized for a homelab; every
cap is env-tunable and "0"/"off" disables it.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stages"))

import _common  # noqa: E402
import build_one_variant as bov  # noqa: E402

_TUNABLES = ("BUILD_MEMORY_LIMIT", "BUILD_CPUS", "BUILD_TIMEOUT_S", "BUILD_BUILDER",
             "BUILD_NETWORK", "BUILD_MIN_FREE_GB", "BUILD_INACTIVITY_TIMEOUT_S",
            "CE_BUILD_CAPS")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in _TUNABLES:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(_common, "_BUILDX_RESOURCE_SUPPORT", True)
    monkeypatch.setenv("CE_BUILD_CAPS", "1")  # CE posture; paid posture tested below


def _resources(cmd):
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "--resource"]


# ------------------------------------------------------------------ command

def test_default_caps_are_applied_and_swap_is_pinned_to_memory():
    cmd = _common.build_command("img:latest", "/ctx")
    res = _resources(cmd)
    assert "memory=6g" in res
    assert "memory-swap=6g" in res          # no swap: otherwise the cap is not enforced
    assert "cpu-period=100000" in res
    assert "cpu-quota=400000" in res        # 4 CPUs


def test_command_shape_is_unchanged_apart_from_the_caps():
    cmd = _common.build_command("img:latest", "/ctx")
    assert cmd[:3] == ["docker", "buildx", "build"]
    for flag in ("--provenance=false", "--sbom=false", "--output=type=docker"):
        assert flag in cmd
    assert cmd[-3:] == ["-t", "img:latest", "/ctx"]
    assert "--progress=plain" not in cmd


def test_progress_plain_for_the_repair_loop():
    assert "--progress=plain" in _common.build_command("t", "/c", progress="plain")


def test_caps_are_env_tunable(monkeypatch):
    monkeypatch.setenv("BUILD_MEMORY_LIMIT", "12g")
    monkeypatch.setenv("BUILD_CPUS", "2.5")
    res = _resources(_common.build_command("t", "/c"))
    assert "memory=12g" in res and "memory-swap=12g" in res
    assert "cpu-quota=250000" in res


@pytest.mark.parametrize("off", ["0", "off", "none", ""])
def test_each_cap_can_be_disabled(monkeypatch, off):
    monkeypatch.setenv("BUILD_MEMORY_LIMIT", off)
    monkeypatch.setenv("BUILD_CPUS", off)
    assert _resources(_common.build_command("t", "/c")) == []


def test_memory_only_cap(monkeypatch):
    monkeypatch.setenv("BUILD_CPUS", "0")
    res = _resources(_common.build_command("t", "/c"))
    assert "memory=6g" in res and not any(r.startswith("cpu-") for r in res)


def test_bad_cpu_value_is_ignored_not_fatal(monkeypatch):
    monkeypatch.setenv("BUILD_CPUS", "lots")
    res = _resources(_common.build_command("t", "/c"))
    assert "memory=6g" in res and not any(r.startswith("cpu-") for r in res)


def test_old_buildx_without_resource_flag_still_builds(monkeypatch):
    """An older buildx rejects unknown flags; the caps are skipped, the build
    is not broken."""
    monkeypatch.setattr(_common, "_BUILDX_RESOURCE_SUPPORT", None)
    monkeypatch.setattr(
        _common.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="Usage: docker buildx build\n  --provenance", stderr=""),
    )
    cmd = _common.build_command("t", "/c")
    assert "--resource" not in cmd
    assert cmd[-3:] == ["-t", "t", "/c"]


def test_buildx_probe_detects_resource_flag_once(monkeypatch):
    calls = []
    monkeypatch.setattr(_common, "_BUILDX_RESOURCE_SUPPORT", None)

    def fake(cmd, **k):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="      --resource stringArray   Resource limits", stderr="")
    monkeypatch.setattr(_common.subprocess, "run", fake)
    assert _common.build_command("t", "/c").count("--resource") == 4
    _common.build_command("t", "/c")
    assert len(calls) == 1


def test_builder_endpoint_is_swappable_through_env(monkeypatch):
    monkeypatch.setenv("BUILD_BUILDER", "rootless-bk")
    monkeypatch.setenv("BUILD_NETWORK", "host")
    cmd = _common.build_command("t", "/c")
    assert cmd[cmd.index("--builder") + 1] == "rootless-bk"
    assert cmd[cmd.index("--network") + 1] == "host"


def test_builder_env_passes_the_credential_filter(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://buildkit:2375")
    monkeypatch.setenv("BUILDX_BUILDER", "bk")
    monkeypatch.setenv("API_KEY", "secret")
    env = _common.build_subprocess_env()
    assert env["DOCKER_HOST"] == "tcp://buildkit:2375"
    assert env["BUILDX_BUILDER"] == "bk"
    assert env["DOCKER_BUILDKIT"] == "1"
    assert "API_KEY" not in env


# ------------------------------------------------------------ tunables

def test_timeout_default_and_override(monkeypatch):
    assert _common.build_timeout_s() == 3600
    monkeypatch.setenv("BUILD_TIMEOUT_S", "900")
    assert _common.build_timeout_s() == 900
    monkeypatch.setenv("BUILD_TIMEOUT_S", "0")
    assert _common.build_timeout_s() is None
    monkeypatch.setenv("BUILD_TIMEOUT_S", "soon")
    assert _common.build_timeout_s() == 3600


def test_min_free_disk_default_30_and_tunable(monkeypatch):
    assert _common.min_free_gb_default() == 30
    monkeypatch.setenv("BUILD_MIN_FREE_GB", "12")
    assert _common.min_free_gb_default() == 12
    monkeypatch.setenv("BUILD_MIN_FREE_GB", "x")
    assert _common.min_free_gb_default() == 30


# ----------------------------------------------- build_one_variant wiring

SPEC = {"image_name": "node", "tenant_id": 3, "hero_dockerfile": "FROM x\n",
        "entrypoint_sh": "", "source_files": [], "variants": []}


@pytest.fixture
def bv(monkeypatch):
    reports, runs = [], []
    for k, v in {"API_URL": "http://api", "API_KEY": "t", "BUILD_SPEC_ID": "9",
                 "VARIANT": "hero"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(bov, "check_docker_disk_space", lambda min_gb: None)
    monkeypatch.setattr(bov, "fetch_build_spec", lambda *a, **k: dict(SPEC))
    monkeypatch.setattr(bov, "report_back", lambda *a, **k: reports.append(k))
    monkeypatch.setattr(bov, "log", lambda m: None)
    return SimpleNamespace(reports=reports, runs=runs, mp=monkeypatch)


def test_build_one_variant_uses_the_shared_command_and_timeout(bv):
    def fake_run(cmd, **kw):
        bv.runs.append((list(cmd), kw))
        if cmd[:3] == ["docker", "buildx", "build"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="sha256:" + "c" * 64, stderr="")
    bv.mp.setattr(bov.subprocess, "run", fake_run)
    bv.mp.setenv("BUILD_TIMEOUT_S", "1200")
    assert bov.main() == 0
    build = next((c, k) for c, k in bv.runs if c[:3] == ["docker", "buildx", "build"])
    assert build[0] == _common.build_command("node:latest", build[0][-1])
    assert build[1]["timeout"] == 1200
    assert bv.reports[-1]["success"] is True


def test_build_one_variant_timeout_is_reported_and_fails(bv):
    def fake_run(cmd, **kw):
        if cmd[:3] == ["docker", "buildx", "build"]:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"], stderr="#5 RUN pip install\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    bv.mp.setattr(bov.subprocess, "run", fake_run)
    bv.mp.setenv("BUILD_TIMEOUT_S", "5")
    assert bov.main() == 124
    rep = bv.reports[-1]
    assert rep["success"] is False
    assert rep["payload"]["stage_failed"] == "build_timeout"
    assert "BUILD_TIMEOUT_S=5" in rep["payload"]["message"]


def test_build_one_variant_preflight_floor_is_tunable(bv):
    seen = []
    bv.mp.setattr(bov, "check_docker_disk_space", lambda min_gb: seen.append(min_gb) or "low")
    bv.mp.setenv("BUILD_MIN_FREE_GB", "15")
    assert bov.main() == 1
    assert seen == [15]


# ------------------------------- hero build (test_and_repair) wall-clock cap

def test_hero_build_wall_clock_cap_kills_a_chatty_never_ending_build(monkeypatch):
    tar = pytest.importorskip("test_and_repair_one_variant")
    state = {"t": 1000.0, "killed": False, "argv": None}

    class FakeProc:
        stdout = iter(())
        returncode = -9

        def poll(self):
            return -9 if state["killed"] else None

        def kill(self):
            state["killed"] = True

    def fake_popen(cmd, **kw):
        state["argv"] = list(cmd)
        return FakeProc()

    def fake_time():
        state["t"] += 30.0
        return state["t"]

    monkeypatch.setattr(tar.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(time, "time", fake_time)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setenv("BUILD_TIMEOUT_S", "60")
    monkeypatch.setenv("BUILD_INACTIVITY_TIMEOUT_S", "999999")
    ok, tail = tar._build_image("/ctx", "node:latest")
    assert ok is False
    assert "BUILD_TIMEOUT_S=60" in tail
    assert state["killed"] is True
    assert state["argv"] == _common.build_command("node:latest", "/ctx", progress="plain")


# ------------------------------------------------- CE-only default caps (Tim D)

def test_flag_absent_paid_has_no_default_caps(monkeypatch):
    monkeypatch.delenv("CE_BUILD_CAPS", raising=False)
    assert _resources(_common.build_command("img:latest", "/ctx")) == []
    assert _common.build_timeout_s() is None


def test_flag_absent_explicit_operator_caps_still_honoured(monkeypatch):
    monkeypatch.delenv("CE_BUILD_CAPS", raising=False)
    monkeypatch.setenv("BUILD_MEMORY_LIMIT", "2g")
    monkeypatch.setenv("BUILD_TIMEOUT_S", "900")
    assert "memory=2g" in _resources(_common.build_command("img:latest", "/ctx"))
    assert _common.build_timeout_s() == 900


def test_flag_on_applies_default_caps(monkeypatch):
    monkeypatch.setenv("CE_BUILD_CAPS", "1")
    assert "memory=6g" in _resources(_common.build_command("img:latest", "/ctx"))
    assert _common.build_timeout_s() == 3600

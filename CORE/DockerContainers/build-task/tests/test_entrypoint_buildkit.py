"""entrypoint.sh: build stages go through the rootless BuildKit sidecar when
BUILDKIT_HOST is set, and refuse the host daemon when isolation is required."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"


def _fake_bin(tmp_path: Path, *, inspect_rc: int = 0) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        f'if [ "$1 $2" = "buildx inspect" ]; then exit {inspect_rc}; fi\n'
        "exit 0\n"
    )
    py = bindir / "python3"
    py.write_text('#!/bin/sh\necho "builder=${BUILD_BUILDER:-}"\n')
    for f in (docker, py):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return bindir


def _run(tmp_path, env_extra, *, inspect_rc=0):
    bindir = _fake_bin(tmp_path, inspect_rc=inspect_rc)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", **env_extra}
    r = subprocess.run(["bash", str(ENTRYPOINT)], env=env, capture_output=True, text=True)
    log = (tmp_path / "docker.log").read_text() if (tmp_path / "docker.log").exists() else ""
    return r, log


def test_build_stage_uses_remote_builder(tmp_path):
    r, log = _run(tmp_path, {"STAGE": "build_variant", "BUILDKIT_HOST": "tcp://buildkitd:1234"})
    assert r.returncode == 0, r.stderr
    assert "builder=mod-buildkit" in r.stdout
    assert "buildx create --name mod-buildkit --driver remote tcp://buildkitd:1234" in log


def test_unreachable_sidecar_fails_fast(tmp_path):
    r, _ = _run(tmp_path, {"STAGE": "test_and_repair_variant", "BUILDKIT_HOST": "tcp://buildkitd:1234"}, inspect_rc=1)
    assert r.returncode == 1
    assert "BUILDER_UNAVAILABLE" in r.stdout


def test_isolation_required_without_host_refuses(tmp_path):
    r, log = _run(tmp_path, {"STAGE": "build_variant", "MOD_REQUIRE_ISOLATED_BUILD": "1"})
    assert r.returncode == 1
    assert "BUILDER_UNAVAILABLE" in r.stdout
    assert log == ""


def test_legacy_path_unchanged_when_not_required(tmp_path):
    r, log = _run(tmp_path, {"STAGE": "build_variant"})
    assert r.returncode == 0
    assert "builder=" in r.stdout and "mod-buildkit" not in r.stdout
    assert log == ""


def test_non_build_stage_ignores_buildkit(tmp_path):
    r, log = _run(tmp_path, {"STAGE": "smoke_variant", "BUILDKIT_HOST": "tcp://buildkitd:1234",
                             "MOD_REQUIRE_ISOLATED_BUILD": "1"})
    assert r.returncode == 0
    assert log == ""

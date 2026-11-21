"""WS6.8d — the nested smoke probe must never run a node against an EMPTY
/data/input.

Root cause this pins (reproduced 09-29 on the Docker-Desktop/WSL2 host):
the probe dirs live on the task's /data NAMED VOLUME, and the nested
`docker run -v {host_path}/probe-…/input:/data/input` used the volume's
on-host `Mountpoint` (`/var/lib/docker/volumes/<vol>/_data`). On a
Docker-Desktop/WSL2 host the docker daemon lives in a VM, so that path
does NOT exist in the daemon's filesystem — the daemon auto-creates an
empty root-owned dir at it and the node's input loop runs 0 times
(hflip/blur s2_golden: identical script PASSed in one run, failed with
`for src in IN.rglob('*')` 0-iterating in the next).

Fix (test_and_repair_one_variant._smoke_probe): when /data is a named
volume, mount the SAME named volume (`docker run -v {vol}:/data`) instead
of a host-path bind, and refuse to run if the staged input isn't visible
(`_assert_smoke_input_visible` → "smoke input staging empty").

These are behavioural tests (mock the one nested `docker run`), not
source greps, so a refactor that re-introduces the host-path bind — or
silently runs on empty staging — fails here first.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "stages"),
)

import test_and_repair_one_variant as tar  # noqa: E402
from _common import resolve_data_volume_name  # noqa: E402


# ---------------------------------------------------------------------------
# _assert_smoke_input_visible — the pre-run loud-failure contract
# ---------------------------------------------------------------------------

def test_assert_raises_when_staged_input_missing(tmp_path):
    """The staged synth file is absent → fail loudly, don't run on nothing."""
    with pytest.raises(RuntimeError, match="smoke input staging empty"):
        tar._assert_smoke_input_visible(str(tmp_path), "input.png", size=123)


def test_assert_raises_when_staged_input_empty(tmp_path):
    """A 0-byte staged file counts as empty — the node would 0-loop on it."""
    f = tmp_path / "input.png"
    f.write_bytes(b"")
    with pytest.raises(RuntimeError, match="smoke input staging empty"):
        tar._assert_smoke_input_visible(str(tmp_path), "input.png", size=1)


def test_assert_passes_when_staged_input_present(tmp_path):
    """A non-empty staged file is fine — the guard must not raise."""
    f = tmp_path / "input.png"
    f.write_bytes(b"\x89PNG-not-really")
    # No exception means success.
    tar._assert_smoke_input_visible(str(tmp_path), "input.png", size=f.stat().st_size)


# ---------------------------------------------------------------------------
# _smoke_probe in named-volume mode — the actual mount strategy
# ---------------------------------------------------------------------------

def _fake_docker_run(captured, out_dir):
    """Stand in for the nested `docker run`: record its argv, and (like a
    healthy node) drop one non-empty file into the probe's output dir so
    the gate can evaluate output."""
    class _Res:
        returncode = 0
        stdout = "produced output\n"
        stderr = ""

    def run(args, **kwargs):
        captured.append(list(args))
        out_file = os.path.join(out_dir, "out.png")
        with open(out_file, "wb") as fh:
            fh.write(b"\x89PNG-output-bytes")
        return _Res()

    return run


def test_volume_mode_mounts_named_volume_at_data_not_host_bind(tmp_path):
    """The fix: with a named-volume /data, the nested probe mounts the SAME
    named volume at /data and must NOT fall back to a host-path bind of
    the (VM-invisible) volume Mountpoint — that was the empty-input bug."""
    captured = []
    out_dir = str(tmp_path / "output")
    # Pre-plant stale content the probe must wipe (a leftover could be
    # mistaken for the node's output / a foreign input).
    stale_in = tmp_path / "input"
    stale_in.mkdir()
    (stale_in / "stale.txt").write_bytes(b"leftover")
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "stale.png").write_bytes(b"leftover-out")

    with patch.object(tar.subprocess, "run",
                      side_effect=_fake_docker_run(captured, out_dir)):
        res = tar._smoke_probe(
            "tenant-1/wizard-x:latest", "image",
            data_host_path="/var/lib/docker/volumes/modcore-task-x/_data",
            expected_output_type=None,
            variant="cpu",
            probe_net="none",
            data_volume_name="modcore-task-x",
            data_root=str(tmp_path),
        )

    assert captured, "the nested docker run was never issued"
    argv = " ".join(captured[0])
    # The named volume is mounted at /data ...
    assert "modcore-task-x:/data" in argv, (
        "volume mode must mount the named volume at /data, got: " + argv
    )
    # ... and NOT via the old host-path bind (the bug).
    assert ":/data/input:ro" not in argv, (
        "volume mode must not bind-mount a host path at /data/input "
        "(that path is VM-invisible on Docker-Desktop/WSL2): " + argv
    )
    assert "/_data/probe-" not in argv, "no host-path probe subdir in volume mode"

    # The staged input is reported back and the probe actually produced
    # output (i.e. it read what it was given).
    assert res["input_seed"]["filename"] == "input.png"
    assert res["produced_output"] is True

    # Stale content was wiped: the stale input is gone, and after the run
    # the probe's own input file was cleaned up (input dir empty).
    in_names = set(os.listdir(str(tmp_path / "input")))
    assert in_names == set(), (
        f"input dir must be empty after probe cleanup, got: {in_names}"
    )
    # The output dir holds only the node's product, not the stale file.
    out_names = set(os.listdir(str(tmp_path / "output")))
    assert "stale.png" not in out_names
    assert "out.png" in out_names


def test_volume_mode_fails_loudly_when_staging_invisible(tmp_path, monkeypatch):
    """If the staged input vanishes before the probe runs (the original
    symptom), _smoke_probe must raise 'smoke input staging empty' rather
    than let the node 0-loop on an empty /data/input."""
    # Make synth_input_for write the file to a location that won't survive
    # — simulate the daemon/handoff dropping the staged bytes. The probe
    # stages into in_dir_container, so point the real synth at a valid dir
    # but then delete it before the visibility assert runs.
    real_synth = tar.synth_input_for
    def vanishing_synth(input_type, dest_dir):
        fname, size = real_synth(input_type, dest_dir)
        # Emulate the mount/daemon dropping the staged file.
        os.remove(os.path.join(dest_dir, fname))
        return fname, size

    monkeypatch.setattr(tar, "synth_input_for", vanishing_synth)
    captured = []
    with patch.object(tar.subprocess, "run",
                      side_effect=_fake_docker_run(captured, str(tmp_path / "output"))):
        with pytest.raises(RuntimeError, match="smoke input staging empty"):
            tar._smoke_probe(
                "tenant-1/wizard-x:latest", "image",
                data_host_path=None,
                data_volume_name="modcore-task-x",
                data_root=str(tmp_path),
            )
    # The probe must NOT have launched the node against an empty input.
    assert captured == [], "node must not run when staging is empty"


def test_host_path_mode_still_binds_host_dirs(tmp_path, monkeypatch):
    """Regression guard: without a named volume (bare-metal host where the
    daemon shares the filesystem) the probe keeps its host-path bind
    mounts of /data/probe-<id>/{input,output} — the fix must not break
    that layout. The /data paths are stubbed out (unit env can't create
    dirs under the container's real /data); only the -v args matter here.
    """
    host = "/var/lib/docker/volumes/modcore-task-x/_data"
    captured = []
    staging = tmp_path / "fake-data"
    monkeypatch.setattr(tar.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(tar.os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr(tar, "synth_input_for", lambda t, d: ("input.png", 8))
    monkeypatch.setattr(
        tar, "_assert_smoke_input_visible", lambda d, f, s: None)
    monkeypatch.setattr(
        tar, "collect_outputs",
        lambda out_dir: [{"name": "out.png", "size": 10}])

    class _Res:
        returncode = 0
        stdout = "produced output\n"
        stderr = ""

    with patch.object(tar.subprocess, "run",
                      return_value=_Res()) as run_mock:
        run_mock.side_effect = lambda args, **k: (
            captured.append(list(args)), _Res())[1]
        tar._smoke_probe(
            "tenant-1/wizard-x:latest", "image",
            data_host_path=host,
            data_volume_name=None,
        )

    assert captured, "nested docker run was never issued"
    argv = " ".join(captured[0])
    assert f"{host}/probe-" in argv and ":/data/input:ro" in argv, (
        "host-path mode must still bind the host probe dirs, got: " + argv
    )
    assert ":/data/output:rw" in argv


# ---------------------------------------------------------------------------
# resolve_data_volume_name
# ---------------------------------------------------------------------------

def test_resolve_volume_returns_name_when_data_is_a_volume():
    fake = MagicMock(returncode=0, stdout="modcore-task-abc\n", stderr="")
    with patch("_common.subprocess.run", return_value=fake):
        assert resolve_data_volume_name() == "modcore-task-abc"


def test_resolve_volume_none_when_data_is_a_bind_mount():
    """A bind-mounted /data reports no Name (we render 'BIND') → None, so
    callers fall back to resolve_data_host_path."""
    fake = MagicMock(returncode=0, stdout="BIND\n", stderr="")
    with patch("_common.subprocess.run", return_value=fake):
        assert resolve_data_volume_name() is None


def test_resolve_volume_none_when_inspect_fails():
    fake = MagicMock(returncode=1, stdout="", stderr="No such container")
    with patch("_common.subprocess.run", return_value=fake):
        assert resolve_data_volume_name() is None


def test_resolve_volume_none_when_inspect_empty():
    fake = MagicMock(returncode=0, stdout="\n", stderr="")
    with patch("_common.subprocess.run", return_value=fake):
        assert resolve_data_volume_name() is None


def test_resolve_volume_strips_whitespace():
    fake = MagicMock(returncode=0, stdout="modcore-task-abc   \n", stderr="")
    with patch("_common.subprocess.run", return_value=fake):
        assert resolve_data_volume_name() == "modcore-task-abc"


# ---------------------------------------------------------------------------
# main() must thread the volume name through and fail loudly on staging
# ---------------------------------------------------------------------------

def test_main_threads_volume_into_probe_and_fails_loudly():
    """A regression that stops passing data_volume_name to the probe — or
    that swallows the staging RuntimeError — would silently revive the
    empty-input bug. Pin both wirings in main()."""
    body = _function_source(tar.main)
    assert "data_volume_name=data_volume_name" in body, (
        "main() must pass the resolved named volume to _smoke_probe"
    )
    assert "smoke_input_staging_empty" in body, (
        "main() must fail loudly (report_back + return 1) when the probe "
        "raises 'smoke input staging empty' instead of letting the node "
        "run on nothing"
    )
    assert "resolve_data_volume_name()" in body, (
        "main() must resolve the named volume backing /data"
    )


def _function_source(fn):
    import inspect
    return inspect.getsource(fn)

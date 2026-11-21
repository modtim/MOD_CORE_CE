"""resolve_data_host_path — finds the host path backing /data inside
the build-task container.

Stage repro: TRELLIS.2 smoke ran with:
    + mkdir -p /data/output
    mkdir: cannot create directory '/data/output': Permission denied
…repeating forever. Root cause: nested `docker run -v {path}:/data/output`
mounted `{path}` from the host filesystem, but `{path}` was a tempfile
dir that only existed INSIDE the build-task container. The docker
daemon auto-created an empty root-owned dir at that path on the host,
mounted it into the wizard image, and the wizard's non-root USER mod
couldn't write to it.

Fix: probe dirs go under /data (which the worker bind-mounted from a
docker-managed volume), and the nested `docker run -v` gets the
corresponding HOST path. The daemon resolves that path correctly
because it owns the volume.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "stages"),
)

from _common import resolve_data_host_path  # noqa: E402


def test_returns_source_path_when_docker_inspect_succeeds():
    """Happy path: /data is mounted as a docker volume; the Source
    path is the on-host filesystem location we need to pass to nested
    `docker run -v`."""
    expected = "/var/lib/docker/volumes/system_test_and_repair_abc/_data"
    fake_proc = MagicMock(returncode=0, stdout=expected + "\n", stderr="")
    with patch("_common.subprocess.run", return_value=fake_proc):
        assert resolve_data_host_path() == expected


def test_returns_none_when_docker_inspect_returns_empty():
    """Container has no /data mount (or it's not a docker-managed mount).
    Callers fall back to tempfile.mkdtemp with the documented caveat
    that nested mounts will be empty."""
    fake_proc = MagicMock(returncode=0, stdout="\n", stderr="")
    with patch("_common.subprocess.run", return_value=fake_proc):
        assert resolve_data_host_path() is None


def test_returns_none_when_docker_inspect_fails():
    fake_proc = MagicMock(returncode=1, stdout="", stderr="No such container")
    with patch("_common.subprocess.run", return_value=fake_proc):
        assert resolve_data_host_path() is None


def test_returns_none_when_docker_socket_missing():
    """Defensive: in a dev/test environment the docker socket might not
    be reachable. Never raise; the caller's fallback path is correct."""
    with patch("_common.subprocess.run", side_effect=FileNotFoundError("docker")):
        assert resolve_data_host_path() is None


def test_strips_trailing_whitespace():
    """`docker inspect --format` ends its output with a newline. Strip
    it so the path can be joined cleanly with subdirectories."""
    expected = "/host/path"
    fake_proc = MagicMock(returncode=0, stdout=expected + "  \n", stderr="")
    with patch("_common.subprocess.run", return_value=fake_proc):
        assert resolve_data_host_path() == expected


def test_uses_self_hostname_to_identify_container():
    """The build-task container ID resolution starts with the hostname
    (cheap), falls back to /proc/1/cpuset and /proc/self/cgroup.
    Inspect the helper rather than resolve_data_host_path itself."""
    from _common import _self_container_id
    src = inspect.getsource(_self_container_id)
    assert "socket.gethostname()" in src
    # The cgroup-based fallbacks must be present so a container with a
    # custom --hostname still resolves correctly.
    assert "/proc/1/cpuset" in src
    assert "/proc/self/cgroup" in src


def test_resolve_uses_docker_inspect_with_data_mount_filter():
    """The destination filter must be /data — that's what the worker
    bind-mounts the per-task volume at."""
    src = inspect.getsource(resolve_data_host_path)
    assert "docker" in src and "inspect" in src
    assert '"/data"' in src


def test_resolve_falls_back_to_mountinfo_when_docker_inspect_fails():
    """If docker inspect returns nothing, parse /proc/self/mountinfo
    instead. This covers running outside docker entirely and the case
    where docker socket isn't reachable from inside the build-task."""
    src = inspect.getsource(resolve_data_host_path)
    assert "_parse_mountinfo_for_data" in src

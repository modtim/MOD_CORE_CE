"""check_docker_disk_space — pre-flight gate for wizard builds.

Stage repro: spec 12 TRELLIS.2 ran 5 outer repair attempts thrashing
on bogus "docker socket" explanations. The real issue was 92% disk
usage on the docker root; BuildKit cancelled mid-build with an error
that happened to contain the word "docker", and the LLM kept pattern-
matching that as a Docker-in-Docker issue.

The check should:
  1. Refuse to proceed when free space is below the threshold
  2. Allow the build through when there's enough space
  3. Fail open if it can't measure (better to attempt than block on
     a faulty check)
  4. Name the recovery action in the error message (the operator
     needs to know `docker system prune` is the fix)
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

from _common import check_docker_disk_space  # noqa: E402


def _mock_statvfs(free_gb: float):
    """Build a posix_statvfs_result-like object with the given free GB."""
    fake = MagicMock()
    fake.f_bavail = int(free_gb * (1024 ** 3) // 4096)
    fake.f_frsize = 4096
    return fake


def test_returns_none_when_enough_space():
    with patch("_common.subprocess.run", return_value=MagicMock(stdout="/var/lib/docker\n")):
        with patch("os.statvfs", return_value=_mock_statvfs(50)):
            assert check_docker_disk_space(min_gb=30) is None


def test_returns_warning_when_below_threshold():
    with patch("_common.subprocess.run", return_value=MagicMock(stdout="/var/lib/docker\n")):
        with patch("os.statvfs", return_value=_mock_statvfs(5)):
            warning = check_docker_disk_space(min_gb=30)
    assert warning is not None
    assert "only 5.0 GB" in warning
    assert "30 GB" in warning
    # Must name the recovery action so operators know what to do.
    assert "docker system prune" in warning


def test_fail_open_when_statvfs_unavailable():
    """If statvfs can't be read (rare but possible — read-only proc),
    don't block the build. We'd rather attempt and surface a real
    failure than block on a faulty check."""
    with patch("_common.subprocess.run", return_value=MagicMock(stdout="/var/lib/docker\n")):
        with patch("os.statvfs", side_effect=OSError("permission denied")):
            assert check_docker_disk_space(min_gb=30) is None


def test_fail_open_when_docker_info_fails():
    """docker info call can fail (socket down). statvfs falls back to
    /var/lib/docker, which still gives a useful answer."""
    with patch("_common.subprocess.run", side_effect=FileNotFoundError("docker")):
        with patch("os.statvfs", return_value=_mock_statvfs(50)):
            assert check_docker_disk_space(min_gb=30) is None


def test_warning_message_explains_the_thrashing_failure_mode():
    """Operators reading this message need to understand WHY this is
    pre-flight blocked vs just letting the build fail. Otherwise they
    bypass the check and waste cycles."""
    with patch("_common.subprocess.run", return_value=MagicMock(stdout="/var/lib/docker\n")):
        with patch("os.statvfs", return_value=_mock_statvfs(5)):
            warning = check_docker_disk_space(min_gb=30)
    assert warning is not None
    assert "misleading" in warning.lower() or "mis-interpret" in warning.lower()


def test_threshold_is_caller_controlled():
    """A tiny smoke node doesn't need 30 GB; the caller picks the
    appropriate floor."""
    with patch("_common.subprocess.run", return_value=MagicMock(stdout="/var/lib/docker\n")):
        with patch("os.statvfs", return_value=_mock_statvfs(8)):
            assert check_docker_disk_space(min_gb=5) is None
            warning = check_docker_disk_space(min_gb=10)
            assert warning is not None and "only 8.0 GB" in warning


def test_function_signature_is_stable():
    """The wizard pre-flight endpoint on the API side imports this
    function; pin the signature so a renaming refactor surfaces here."""
    sig = inspect.signature(check_docker_disk_space)
    assert "min_gb" in sig.parameters
    # Default must be conservative — TRELLIS-class needs ~15-20 GB
    # working; 30 GB leaves headroom for parallel builds + cache.
    assert sig.parameters["min_gb"].default == 30

"""Unit tests for --help grounding candidate extraction + trigger.

Covers the pure (no-docker) helpers of piece 1 of the wizard
grounding/learning program: `_looks_like_missing_command`,
`_candidate_binaries`. The actual `_probe_binary_reality` runs a
container and is exercised end-to-end, not here.

Key invariants:
  - the OpenMVS hallucinated-CLI class is detected + the fake binary
    is extracted as a probe candidate,
  - candidate names are sanitised (shell-injection guard) — anything
    with shell metacharacters is dropped, since candidates are
    interpolated into a `sh -c` string,
  - ubiquitous coreutils and user-script paths are not probed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stages"))

import test_and_repair_one_variant as tar  # noqa: E402


# --------------------------------------------------------------------
# _looks_like_missing_command
# --------------------------------------------------------------------

def test_trigger_on_command_not_found():
    assert tar._looks_like_missing_command("openMVS_main: command not found")


def test_trigger_on_executable_not_found():
    assert tar._looks_like_missing_command(
        'exec: "colmap": executable file not found in $PATH'
    )


def test_trigger_on_absolute_system_path_missing():
    assert tar._looks_like_missing_command(
        "No such file or directory: '/usr/local/bin/DensifyPointCloud'"
    )


def test_no_trigger_on_missing_input_file():
    # A missing INPUT file under /data is not a missing command — don't
    # burn a docker run probing for it.
    assert not tar._looks_like_missing_command(
        "No such file or directory: '/data/input/foo.jpg'"
    )


def test_no_trigger_on_generic_error():
    assert not tar._looks_like_missing_command("ValueError: bad shape")


# --------------------------------------------------------------------
# _candidate_binaries
# --------------------------------------------------------------------

def test_extracts_missing_binary_from_stderr():
    cands = tar._candidate_binaries(
        "openMVS_main: command not found", {},
    )
    assert "openMVS_main" in cands


def test_extracts_binaries_from_script_subprocess_calls():
    script = (
        'import subprocess\n'
        'subprocess.run(["openMVG_main_ComputeMatches", "-i", x])\n'
        'subprocess.run(["DensifyPointCloud", scene])\n'
    )
    cands = tar._candidate_binaries("", {"run.py": script})
    assert "openMVG_main_ComputeMatches" in cands
    assert "DensifyPointCloud" in cands


def test_shell_string_form_takes_first_token():
    script = 'os.system("colmap feature_extractor --database x")\n'
    cands = tar._candidate_binaries("", {"run.py": script})
    assert "colmap" in cands
    assert "feature_extractor" not in cands  # only the binary, not its arg


def test_ubiquitous_commands_are_skipped():
    script = (
        'subprocess.run(["cp", a, b])\n'
        'subprocess.run(["python3", "x.py"])\n'
        'subprocess.run(["ffmpeg", "-i", v])\n'
    )
    cands = tar._candidate_binaries("", {"run.py": script})
    assert "cp" not in cands
    assert "python3" not in cands
    assert "ffmpeg" in cands  # domain tool kept


def test_user_script_paths_are_skipped():
    script = 'subprocess.run(["/app/scripts/run.sh"])\n'
    cands = tar._candidate_binaries("", {"run.py": script})
    assert cands == []


def test_absolute_tool_path_reduces_to_basename():
    script = 'subprocess.run(["/opt/openMVG/bin/openMVG_main_SfMInit_ImageListing"])\n'
    cands = tar._candidate_binaries("", {"run.py": script})
    assert "openMVG_main_SfMInit_ImageListing" in cands


def test_injection_guard_drops_shell_metacharacters():
    # A candidate carrying shell metacharacters must never survive — it
    # would break out of the probe's `sh -c`. Every returned name must
    # match the strict command-name shape.
    script = 'subprocess.run(["foo; rm -rf /", "bar$(whoami)", "a`id`"])\n'
    cands = tar._candidate_binaries("evil: command not found\n$(touch /pwned): command not found", {"run.py": script})
    for c in cands:
        assert tar._CMD_NAME_RE.match(c), f"unsanitised candidate leaked: {c!r}"


def test_no_candidates_returns_empty():
    assert tar._candidate_binaries("", {}) == []


def test_candidate_limit_respected():
    script = "\n".join(
        f'subprocess.run(["tool_{i}", "x"])' for i in range(40)
    )
    cands = tar._candidate_binaries("", {"run.py": script}, limit=12)
    assert len(cands) <= 12

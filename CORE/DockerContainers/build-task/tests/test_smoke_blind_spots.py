"""WS6 finding #2/#3: documents the 1x1-synthetic-input blind spot in the
build-DAG smoke/sample gates.

`output_matches_declared_type` (see `_synth_input.py`) only checks a
produced file's extension and, for known binary types, its magic bytes.
It never checks image dimensions. Combined with the smoke probe's 1x1
RGBA synthetic input (`_synth_input.py:33-52`), a node whose entrypoint
silently resizes to the wrong dimensions (e.g. asked for 32x32, produces
64x64 -- WS6 breadth-set task p1) still satisfies the gate: it produced a
non-empty file with the right extension and valid magic bytes. That is a
false success (FS) the wizard currently cannot catch without a golden
`sample_output` to compare against (WS6 finding #3).

This module has two tests:

  (a) `test_output_matches_declared_type_blind_to_wrong_dimensions` --
      documents the blind spot. It is `xfail(strict=True)` because it
      asserts the DESIRED behaviour (the gate should reject a wrong-dims
      output), which is NOT what the code does today. See the note in
      the test body for the exact xfail semantics.

  (b) `test_typeerror_crash_is_rejected_by_looks_like_runtime_failure` --
      a normal (non-xfail) passing test showing the crash-class blind
      spot is NOT a gap: a script that raises TypeError writes a Python
      traceback to stderr, and `looks_like_runtime_failure` does catch
      that via the "traceback (most recent call last)" pattern.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "stages"),
)

from _synth_input import (  # noqa: E402
    collect_outputs,
    looks_like_runtime_failure,
    output_matches_declared_type,
)


@pytest.mark.xfail(strict=True, reason="needs golden sample_output")
def test_output_matches_declared_type_blind_to_wrong_dimensions(tmp_path):
    """Blind spot: a script declared/asked to produce a 32x32 image but
    actually resizes to 64x64 (a wrong-dims semantic bug, not a crash).
    `output_matches_declared_type` only checks extension + magic bytes,
    so it accepts this output as-is today.

    XFAIL SEMANTICS -- read before "fixing" this test: with
    `strict=True`, the test SUITE only passes if this test's body
    FAILS (an unexpected pass -- XPASS -- would itself fail the run).
    So the assertion below intentionally encodes the DESIRED behaviour
    (the gate rejects the wrong-dims output), not today's actual
    behaviour. That assertion is false today because there is no
    dimension check at all, so the test fails now (reported as
    "xfailed", which counts as a pass for the suite). Once a golden
    `sample_output` dimension check is added to
    `output_matches_declared_type`, this assertion will start passing,
    turn into an XPASS, and the strict marker will force a hard failure
    -- at that point remove the xfail marker, don't just relax it.
    """
    declared_size = (32, 32)

    # The "script" produced a real, valid PNG -- just the wrong size.
    img = Image.new("RGB", (64, 64), color=(10, 20, 30))
    out_path = tmp_path / "out.png"
    img.save(out_path)

    files = collect_outputs(str(tmp_path))
    ok, reason = output_matches_declared_type(str(tmp_path), files, "image")
    # Sanity check on today's actual behavior: extension + magic bytes
    # both check out for a real PNG, so the permissive gate passes this
    # regardless of dimensions. This is the false success (FS) the WS6
    # findings describe -- confirmed here, not assumed.
    assert ok is True, (
        f"expected today's extension+magic-byte gate to pass a wrong-dims "
        f"PNG, got ok={ok} ({reason}) -- if this now fails, the blind spot "
        f"described above may already be closed"
    )

    with Image.open(out_path) as produced:
        actual_size = produced.size

    # DESIRED behaviour (not implemented today): the gate should compare
    # against a golden sample_output/declared size and reject a mismatch.
    # This is the assertion that is expected to fail right now.
    assert actual_size == declared_size, (
        f"output_matches_declared_type has no dimension check: accepted a "
        f"{actual_size} PNG (ok={ok}, reason={reason!r}) when {declared_size} "
        f"was declared/expected -- needs a golden sample_output to catch "
        f"wrong-dims semantic bugs like WS6 breadth-set task p1 (resize)"
    )


def test_typeerror_crash_is_rejected_by_looks_like_runtime_failure():
    """A script that crashes with TypeError (e.g. mixing tuple + int, or
    string * int misuse -- WS6 breadth-set task p7) writes a Python
    traceback to stderr. `looks_like_runtime_failure` must flag that as
    a runtime failure so the crash class (unlike the wrong-dims class
    above) is NOT a blind spot."""
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "process.py", line 14, in <module>\n'
        "    result = process(img, border)\n"
        '  File "process.py", line 9, in process\n'
        "    offset = (width, height) + border\n"
        "TypeError: can only concatenate tuple (not \"int\") to tuple\n"
    )
    reason = looks_like_runtime_failure("", stderr)
    assert reason is not None, (
        "a TypeError crash traceback must be caught by "
        "looks_like_runtime_failure, not treated as a clean run"
    )

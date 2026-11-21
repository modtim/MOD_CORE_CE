"""Source-pinning tests for the new "real output required" gates on
smoke and sample-test stages.

These are intentionally source-presence checks (not end-to-end docker
runs) because:
  * docker isn't available in the unit-test image
  * we already validate the synthesis + collection helpers separately
  * the new logic is small enough that "code path is wired" is the
    high-leverage assertion — a regression that re-introduces the
    exit-code-only gate would land in source first

Stage repro the gates close out:
  - wizard-built `ffmpeg --help` node passed both gates (exit 0)
  - now smoke + sample require non-empty /data/output AND exit==0
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "stages"),
)


def test_sample_test_gates_on_produced_output():
    """`ok` must be (exit==0) AND has_real_output — not just exit==0."""
    import sample_one_test
    src = inspect.getsource(sample_one_test.main)
    # The OLD code: `ok = res.returncode == 0` (and only that).
    # The NEW code combines exit-code AND produced_output.
    assert "produced_output = has_real_output(out_files)" in src, \
        "sample_one_test must compute produced_output via has_real_output"
    # Whitespace-insensitive: the gate is now a multi-line `ok = (...)` that
    # also ANDs in runtime_failure/type_ok; exit==0 AND output must lead it.
    assert "ok = ( res.returncode == 0 and produced_output" in " ".join(src.split()), \
        "sample_one_test must require BOTH exit==0 AND non-empty output"
    # No silent skip — synth_input falls in when the wizard didn't
    # provide a sample. The OLD code returned early with success on
    # missing sample.
    assert "no sample input — skipping" not in src, \
        "sample_one_test must NOT silently skip when sample_input is absent"


def test_sample_test_synthesizes_input_when_wizard_sample_missing():
    """When the wizard didn't carry a sample, fall back to synth_input —
    don't silently mark the stage successful. That silent skip is how
    the ffmpeg-help-only node sailed through last time."""
    import sample_one_test
    src = inspect.getsource(sample_one_test)
    assert "synth_input_for" in src
    # The seed helper documents which path we took (wizard_sample vs
    # synth_stub) so operators can read it back in the report payload.
    assert '"source": "wizard_sample"' in src
    assert '"source": "synth_stub"' in src


def test_smoke_variant_does_real_io_probe_not_env_probe():
    """The old probe was `docker run -e MOD_VARIANT_SMOKE=1 <image>`
    and relied on an in-image probe handler. Wizard images don't have
    one, so the gate degenerated to "exit code 0". The new probe
    mounts a real input + output volume and checks for output."""
    import smoke_one_variant
    src = inspect.getsource(smoke_one_variant.main)
    assert "MOD_VARIANT_SMOKE" not in src, \
        "smoke_one_variant must NOT rely on the in-image env probe anymore"
    # Real I/O probe — mount input + output dirs.
    assert "/data/input:ro" in src
    assert "/data/output:rw" in src
    # Same output gate as sample_test.
    assert "produced_output = has_real_output(out_files)" in src
    assert "ok = ( res.returncode == 0 and produced_output" in " ".join(src.split())


def test_smoke_variant_reports_failure_reason_when_exit0_but_no_output():
    """The clear-error message that operators need when a non-functional
    entrypoint registers: don't say "exit 0, success", say "container
    exited 0 but produced no output…"."""
    import smoke_one_variant
    src = inspect.getsource(smoke_one_variant.main)
    assert 'exited 0 but produced no output' in src, \
        "operator-facing failure reason must distinguish exit-0-no-output from exit-N"


def test_sample_test_reports_failure_reason_when_exit0_but_no_output():
    import sample_one_test
    src = inspect.getsource(sample_one_test.main)
    assert 'exited 0 but produced no output' in src
    # And the hint that points at the entrypoint, not at the inputs.
    assert "entrypoint" in src.lower()

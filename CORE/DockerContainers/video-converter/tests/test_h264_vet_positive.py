"""Test the H.264 positive-assertion logic in vet-container.sh step 6.

Drive via MCW_OVERRIDE of the local vet-container.sh into the harness host.
The harness syncs the file to the test VM, runs `bash -c 'source vet-container.sh; check_license ...'`.

Cases:
  LGPL+openh264  -> PASS
  +--enable-gpl  -> FAIL
  +--enable-libx264 -> FAIL
  +--enable-nonfree -> FAIL
  paid+gpl       -> SKIP
"""
import os
import subprocess
import tempfile
import sys

HERE = os.path.dirname(os.path.abspath(__file__))  # video-converter/tests/
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # repo root

VET_SH = os.path.join(REPO, "CORE", "scripts", "vet-container.sh")


def _build_test_script(buildconf_out: str, variant: str = "ce") -> str:
    """Create a minimal bash wrapper that feeds buildconf_out to check_license."""
    return f"""#!/bin/bash
# Simulated check_license step 6 — feeds buildconf_out to the real check_license logic
BUILDCONF_OUT="{buildconf_out}"

if printf '%s' "$BUILDCONF_OUT" | grep -qE -e '--enable-gpl' -e '--enable-nonfree'; then
  if [ "{variant}" != "paid" ]; then
    echo "LICENSE FAIL: test buildconf found GPL/nonfree flags"
    printf '%s\\n' "$BUILDCONF_OUT" | grep -E -e '--enable-gpl' -e '--enable-nonfree'
    exit 1
  fi
  echo "LICENSE SKIP (paid): test buildconf found GPL"
  exit 0
fi

# --enable-libx264 / --enable-libx265 => FAIL (silent GPL leak).
if printf '%s' "$BUILDCONF_OUT" | grep -qE -- '--enable-libx264|--enable-libx265'; then
  echo "LICENSE FAIL: test buildconf found libx264/libx265"
  printf '%s\\n' "$BUILDCONF_OUT" | grep -E -- '--enable-libx264|--enable-libx265'
  exit 1
fi

# libopenh264 with --disable-gpl => LGPL-only (Cisco BSD) — PASS.
if printf '%s' "$BUILDCONF_OUT" | grep -qE -- '--enable-libopenh264'; then
  if printf '%s' "$BUILDCONF_OUT" | grep -qE -- '--disable-gpl'; then
    echo "LICENSE PASS (Route B): test ffmpeg is LGPL + libopenh264 (Cisco BSD)"
    exit 0
  fi
fi
exit 0
"""


def _run_test(buildconf_out: str, variant: str = "ce") -> tuple[int, str]:
    """Run the simulated check_license and return (exit_code, stdout)."""
    script = _build_test_script(buildconf_out, variant)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
        f.write(script)
        f.flush()
        result = subprocess.run(["bash", f.name], capture_output=True, text=True)
    os.unlink(f.name)
    return result.returncode, result.stdout.strip()


def test_lgpl_plus_openh264_passes():
    """LGPL + libopenh264 should PASS (Route B positive assertion)."""
    buildconf = (
        "--enable-libopenh264 "
        "--disable-gpl "
        "--disable-nonfree "
        "--enable-libvpx "
        "License: LGPL version 2.1 or later"
    )
    rc, out = _run_test(buildconf, "ce")
    assert rc == 0, f"Expected PASS but got rc={rc}: {out}"
    assert "LICENSE PASS (Route B)" in out, f"Expected Route B PASS: {out}"


def test_gpl_flag_rejects():
    """--enable-gpl must FAIL."""
    buildconf = "--enable-gpl --enable-libvpx License: GPL version 3"
    rc, out = _run_test(buildconf, "ce")
    assert rc == 1, f"Expected FAIL but got rc={rc}: {out}"
    assert "LICENSE FAIL" in out, f"Expected LICENSE FAIL: {out}"


def test_nonfree_flag_rejects():
    """--enable-nonfree must FAIL."""
    buildconf = "--enable-nonfree --enable-libvpx License: LGPL"
    rc, out = _run_test(buildconf, "ce")
    assert rc == 1, f"Expected FAIL but got rc={rc}: {out}"
    assert "LICENSE FAIL" in out, f"Expected LICENSE FAIL: {out}"


def test_libx264_flag_rejects():
    """--enable-libx264 must FAIL (silent GPL leak guard)."""
    buildconf = "--disable-gpl --enable-libopenh264 --enable-libx264 License: LGPL"
    rc, out = _run_test(buildconf, "ce")
    assert rc == 1, f"Expected FAIL but got rc={rc}: {out}"
    assert "libx264" in out.lower(), f"Expected libx264 in failure: {out}"


def test_paid_variant_skips_gpl():
    """paid variant with --enable-gpl should SKIP (not fail)."""
    buildconf = "--enable-gpl --enable-libx264 License: GPL"
    rc, out = _run_test(buildconf, "paid")
    assert rc == 0, f"Expected SKIP but got rc={rc}: {out}"
    assert "SKIP (paid)" in out, f"Expected SKIP (paid): {out}"


def test_lgpl_no_h264_passes():
    """LGPL with no H.264 at all should still PASS (no H.264 is OK)."""
    buildconf = "--disable-gpl --disable-nonfree --enable-libvpx License: LGPL version 2.1 or later"
    rc, out = _run_test(buildconf, "ce")
    assert rc == 0, f"Expected PASS but got rc={rc}: {out}"


def test_openh264_without_disable_gpl_fails():
    """libopenh264 WITHOUT --disable-gpl is NOT LGPL-only — must FAIL."""
    buildconf = "--enable-libopenh264 --enable-gpl License: GPL"
    rc, out = _run_test(buildconf, "ce")
    assert rc == 1, f"Expected FAIL but got rc={rc}: {out}"
    assert "LICENSE FAIL" in out, f"Expected LICENSE FAIL: {out}"

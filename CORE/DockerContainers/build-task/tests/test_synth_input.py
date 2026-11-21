"""Validate the minimum-input stubs the smoke + sample-test stages mount.

Stage repro: an `exec ffmpeg` entrypoint passed both smoke and sample
gates because they only checked exit_code == 0. The fix gates on
"non-empty output produced", which only makes sense if we feed the
container a real input — that's what `_synth_input.synth_input_for`
does. These tests pin:

  1. Every declared input_type produces a non-empty file.
  2. PNG bytes parse as a valid PNG (signature + IHDR + IEND).
  3. PDF starts with %PDF and ends with %%EOF (smallest probe a real
     PDF decoder runs).
  4. WAV/MP4/STL have their respective magic bytes.
  5. has_real_output returns True iff at least one file has size > 0.
  6. collect_outputs ignores subdirectories and missing dirs.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "stages"),
)

from _synth_input import (  # noqa: E402
    collect_outputs,
    has_real_output,
    looks_like_runtime_failure,
    output_matches_declared_type,
    synth_input_for,
)


KNOWN_TYPES = ["image", "video", "audio", "pdf", "text", "csv", "json", "mesh", "cad"]


@pytest.mark.parametrize("input_type", KNOWN_TYPES)
def test_synth_input_for_known_types_writes_non_empty_file(input_type, tmp_path):
    fname, size = synth_input_for(input_type, str(tmp_path))
    assert size > 0, f"{input_type} stub was empty"
    full = tmp_path / fname
    assert full.exists()
    assert full.stat().st_size == size


def test_synth_input_unknown_type_falls_back_to_bin(tmp_path):
    fname, size = synth_input_for("totally-made-up-type", str(tmp_path))
    assert fname == "input.bin"
    assert size == 1024
    assert (tmp_path / fname).exists()


def test_synth_input_empty_input_type_falls_back_to_bin(tmp_path):
    fname, size = synth_input_for("", str(tmp_path))
    assert fname == "input.bin"


def test_synth_image_is_valid_png(tmp_path):
    """PNG signature + IHDR + IEND. A real PNG decoder will accept this
    1x1 transparent image. Hardcoded the right way: we lift the parser
    just enough to know the file isn't garbage."""
    synth_input_for("image", str(tmp_path))
    body = (tmp_path / "input.png").read_bytes()
    assert body.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in body
    assert body.endswith(b"IEND\xaeB`\x82")


def test_synth_pdf_is_valid_pdf(tmp_path):
    synth_input_for("pdf", str(tmp_path))
    body = (tmp_path / "input.pdf").read_bytes()
    assert body.startswith(b"%PDF-1.")
    assert b"%%EOF" in body
    assert b"/Catalog" in body


def test_synth_audio_is_riff_wav(tmp_path):
    synth_input_for("audio", str(tmp_path))
    body = (tmp_path / "input.wav").read_bytes()
    assert body.startswith(b"RIFF")
    assert body[8:12] == b"WAVE"
    assert b"fmt " in body
    assert b"data" in body


def test_synth_video_is_mp4_ftyp_box(tmp_path):
    synth_input_for("video", str(tmp_path))
    body = (tmp_path / "input.mp4").read_bytes()
    # mp4 ftyp box: 4 bytes size + "ftyp" + brand
    assert body[4:8] == b"ftyp"


def test_synth_mesh_and_cad_are_binary_stl(tmp_path):
    """Both 'mesh' and 'cad' route to a minimal binary STL — header
    is 80 bytes + uint32 triangle count = 0."""
    for tname in ("mesh", "cad"):
        d = tmp_path / tname
        d.mkdir()
        fname, _ = synth_input_for(tname, str(d))
        body = (d / fname).read_bytes()
        assert len(body) == 84
        # Last 4 bytes = triangle count (uint32 LE) = 0
        assert body[80:] == b"\x00\x00\x00\x00"


def test_synth_text_csv_json_are_human_readable(tmp_path):
    synth_input_for("text", str(tmp_path))
    assert (tmp_path / "input.txt").read_bytes() == b"hello\n"
    synth_input_for("csv", str(tmp_path))
    assert b"," in (tmp_path / "input.csv").read_bytes()
    synth_input_for("json", str(tmp_path))
    body = (tmp_path / "input.json").read_bytes()
    import json as _json
    parsed = _json.loads(body)
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------
# collect_outputs / has_real_output
# ---------------------------------------------------------------------


def test_has_real_output_false_on_empty_list():
    assert has_real_output([]) is False


def test_has_real_output_false_when_all_files_zero_bytes():
    """Crucial case for the smoke gate: an entrypoint that `touch`es an
    output file but doesn't write anything is NOT considered a real
    output — that's still a sign the entrypoint isn't doing work."""
    assert has_real_output([{"name": "a", "size": 0}, {"name": "b", "size": 0}]) is False


def test_has_real_output_true_when_any_file_non_empty():
    assert has_real_output([{"name": "a", "size": 0}, {"name": "b", "size": 1}]) is True


def test_collect_outputs_lists_files(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "b.txt").write_bytes(b"")
    files = collect_outputs(str(tmp_path))
    assert {f["name"] for f in files} == {"a.txt", "b.txt"}
    by_name = {f["name"]: f["size"] for f in files}
    assert by_name["a.txt"] == 5
    assert by_name["b.txt"] == 0


def test_collect_outputs_ignores_subdirs(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_bytes(b"nested")
    (tmp_path / "top.txt").write_bytes(b"top")
    files = collect_outputs(str(tmp_path))
    assert {f["name"] for f in files} == {"top.txt"}


def test_collect_outputs_returns_empty_for_missing_dir():
    assert collect_outputs("/path/that/definitely/does/not/exist/here") == []


# ---------------------------------------------------------------------
# looks_like_runtime_failure — catches scripts that swallow a runtime
# error and exit 0, writing an error log instead of real output.
# ---------------------------------------------------------------------

class TestLooksLikeRuntimeFailure:
    def test_clean_run_returns_none(self):
        assert looks_like_runtime_failure("PNG written\n", "") is None
        assert looks_like_runtime_failure("", "") is None

    def test_catches_traceback(self):
        stderr = (
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 1\n"
            "ZeroDivisionError: division by zero\n"
        )
        assert looks_like_runtime_failure("", stderr) is not None

    def test_catches_missing_poppler(self):
        # Real failure mode of a pdf2image node missing poppler-utils.
        stdout = "Converting input.pdf...\nError converting input.pdf: Unable to get page count. Is poppler installed and in PATH?\n"
        assert looks_like_runtime_failure(stdout, "") == "error converting"

    def test_catches_modulenotfound(self):
        assert looks_like_runtime_failure("", "ModuleNotFoundError: No module named 'fitz'") is not None

    def test_catches_command_not_found(self):
        assert looks_like_runtime_failure("ffmpeg: command not found\n", "") is not None

    def test_is_case_insensitive(self):
        assert looks_like_runtime_failure("FATAL ERROR: something broke", "") is not None


# ---------------------------------------------------------------------
# output_matches_declared_type — extension + magic-byte gate
# ---------------------------------------------------------------------

class TestOutputMatchesDeclaredType:
    def test_no_outputs_fails(self, tmp_path):
        ok, _ = output_matches_declared_type(str(tmp_path), [], "image")
        assert ok is False

    def test_open_output_type_is_permissive(self, tmp_path):
        # Unmapped / open types fall back to has_real_output
        (tmp_path / "weird.frob").write_bytes(b"some bytes")
        files = collect_outputs(str(tmp_path))
        for ot in ("", "other", "any", "none", "quantum-frob"):
            ok, _ = output_matches_declared_type(str(tmp_path), files, ot)
            assert ok is True, ot

    def test_image_output_rejects_error_log(self, tmp_path):
        # The exact bug we're guarding against: pdf2image catches the
        # missing-poppler error and writes an error.json. has_real_output
        # alone would pass — the strict gate must reject (wrong extension
        # for an image output).
        (tmp_path / "error.json").write_bytes(b'{"status":"failed"}')
        files = collect_outputs(str(tmp_path))
        ok, reason = output_matches_declared_type(str(tmp_path), files, "image")
        assert ok is False
        assert "extension" in reason.lower()

    def test_image_output_accepts_real_png(self, tmp_path):
        # Real PNG signature
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        (tmp_path / "out.png").write_bytes(png)
        files = collect_outputs(str(tmp_path))
        ok, reason = output_matches_declared_type(str(tmp_path), files, "image")
        assert ok is True, reason

    def test_image_output_rejects_renamed_text(self, tmp_path):
        # Right extension, wrong content — defence against "wrote an error
        # log and renamed it to result.png".
        (tmp_path / "result.png").write_bytes(b"this is not a png, just text\n" * 5)
        files = collect_outputs(str(tmp_path))
        ok, reason = output_matches_declared_type(str(tmp_path), files, "image")
        assert ok is False
        assert "magic" in reason.lower()

    def test_pdf_output_accepts_real_pdf(self, tmp_path):
        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        files = collect_outputs(str(tmp_path))
        ok, _ = output_matches_declared_type(str(tmp_path), files, "pdf")
        assert ok is True

    def test_pdf_output_rejects_text_with_pdf_extension(self, tmp_path):
        (tmp_path / "fake.pdf").write_bytes(b"plain text, not a pdf\n")
        files = collect_outputs(str(tmp_path))
        ok, _ = output_matches_declared_type(str(tmp_path), files, "pdf")
        assert ok is False

    def test_text_output_extension_only(self, tmp_path):
        # Text type has no magic-byte rule — extension match is enough.
        (tmp_path / "out.txt").write_bytes(b"hello\n")
        files = collect_outputs(str(tmp_path))
        ok, _ = output_matches_declared_type(str(tmp_path), files, "text")
        assert ok is True


# ---------------------------------------------------------------------
# docker_device_args_for_variant + accelerator presence helpers
# ---------------------------------------------------------------------

from _synth_input import (  # noqa: E402
    accelerator_family_for_variant,
    docker_device_args_for_variant,
    looks_like_missing_accelerator,
)


class TestDeviceArgs:
    def test_hero_and_cpu_get_no_device_args(self):
        assert docker_device_args_for_variant("hero") == ()
        assert docker_device_args_for_variant("cpu") == ()
        assert docker_device_args_for_variant("") == ()
        assert docker_device_args_for_variant(None) == ()

    def test_cuda_variants_get_gpus_flag(self):
        for v in ("cuda12.4", "cuda12.6", "cuda", "CUDA12.4"):
            assert docker_device_args_for_variant(v) == ("--gpus", "all"), v

    def test_rocm_gets_kfd_dri_video(self):
        flags = docker_device_args_for_variant("rocm6")
        assert "--device=/dev/kfd" in flags
        assert "--device=/dev/dri" in flags
        assert "--group-add=video" in flags

    def test_intel_xpu_gets_dri(self):
        assert docker_device_args_for_variant("intel-xpu") == ("--device=/dev/dri",)

    def test_unknown_variant_falls_back_to_empty(self):
        assert docker_device_args_for_variant("some-future-accelerator") == ()


class TestAcceleratorFamily:
    def test_families(self):
        assert accelerator_family_for_variant("cuda12.4") == "cuda"
        assert accelerator_family_for_variant("rocm6") == "rocm"
        assert accelerator_family_for_variant("intel-xpu") == "intel-xpu"
        assert accelerator_family_for_variant("npu-openvino") == "npu"

    def test_no_family_for_cpu_paths(self):
        assert accelerator_family_for_variant("hero") == ""
        assert accelerator_family_for_variant("cpu") == ""
        assert accelerator_family_for_variant("") == ""
        assert accelerator_family_for_variant(None) == ""


class TestMissingAccelerator:
    def test_hero_never_flags(self):
        # Even "no CUDA-capable device" in the log for hero is fine —
        # hero isn't expected to use CUDA.
        assert looks_like_missing_accelerator(
            "no CUDA-capable device is detected\n", "", "hero",
        ) is None

    def test_cuda_variant_catches_fallback(self):
        msg = looks_like_missing_accelerator(
            "running...\nCUDA is not available — falling back to CPU\n",
            "", "cuda12.4",
        )
        assert msg is not None
        assert "falling back to cpu" in msg or "cuda is not available" in msg

    def test_cuda_variant_catches_real_driver_error(self):
        assert looks_like_missing_accelerator(
            "", "RuntimeError: No CUDA-capable device is detected", "cuda12.6",
        ) is not None

    def test_rocm_variant_catches_hip_error(self):
        assert looks_like_missing_accelerator(
            "hip error: no hip-capable device found\n", "", "rocm6",
        ) is not None

    def test_clean_log_for_cuda_variant_passes(self):
        assert looks_like_missing_accelerator(
            "Using device: cuda:0\nPNG written to /data/output/out.png\n",
            "", "cuda12.4",
        ) is None

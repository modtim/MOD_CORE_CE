"""Minimal valid-input stubs for wizard smoke + sample-test stages.

Stage repro: the wizard built a node whose entrypoint was effectively
`exec ffmpeg` (no positional args, prints help, exits 0). Today's smoke
gate only checks `exit_code == 0`, so the broken entrypoint registered
into the palette. The user ran a workflow on it and got no output —
silently, because the orchestrator can't tell a "ran fine, produced
nothing" container from a "ran fine, work is downstream" container.

The fix: smoke and sample-test stages now mount a /data/input dir with
a real file in it and require at least one non-empty file in /data/output
after the run. This module produces the minimum-viable input file for
each declared input_type.

We keep these intentionally TINY (bytes, not kilobytes) so the smoke
stage stays fast — the only requirement is that the file passes a
"valid header" check on the consuming side. A real PNG decoder will
accept these; a real PDF decoder will accept these. Anything more
elaborate (correct color space, full audio frame, etc.) is overkill
for a "did the entrypoint actually do work" probe.

If a node's entrypoint genuinely rejects these and produces no output,
that's a different failure: the smoke gate fires and the user gets a
clear "no output produced on synthetic input" message instead of a
silent passing build.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple


# Minimum-valid 1x1 transparent PNG. 67 bytes, IHDR + zero-length IDAT
# (well, near-zero — the deflate envelope is required). Built via
# struct + zlib so we don't have to commit a binary blob to git.
def _make_minimal_png() -> bytes:
    import struct
    import zlib

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    # IHDR: width=1, height=1, bit_depth=8, colortype=6 (RGBA),
    # compression=0, filter=0, interlace=0
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    # 1px transparent RGBA: filter byte (0) + 4 zero bytes
    raw = b"\x00\x00\x00\x00\x00"
    idat = zlib.compress(raw)
    iend = b""
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", iend)


# Minimum-valid PDF. ~330 bytes. PDF 1.4 with one empty page.
_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]/Contents 4 0 R>>endobj\n"
    b"4 0 obj<</Length 8>>stream\nBT ET\nendstream\nendobj\n"
    b"xref\n0 5\n"
    b"0000000000 65535 f\n"
    b"0000000009 00000 n\n"
    b"0000000056 00000 n\n"
    b"0000000111 00000 n\n"
    b"0000000189 00000 n\n"
    b"trailer<</Size 5/Root 1 0 R>>\n"
    b"startxref\n246\n"
    b"%%EOF\n"
)


# Minimum-valid WAV. 44-byte RIFF header + 0 data bytes is rejected by
# strict readers (data size 0), so we ship 1 sample = 2 bytes mono 16-bit.
_MINIMAL_WAV = (
    b"RIFF"
    b"\x26\x00\x00\x00"   # file size - 8 = 38
    b"WAVE"
    b"fmt "
    b"\x10\x00\x00\x00"   # subchunk size = 16
    b"\x01\x00"           # audio format = PCM
    b"\x01\x00"           # channels = 1
    b"\x44\xAC\x00\x00"   # sample rate = 44100
    b"\x88\x58\x01\x00"   # byte rate
    b"\x02\x00"           # block align
    b"\x10\x00"           # bits per sample = 16
    b"data"
    b"\x02\x00\x00\x00"   # data size = 2
    b"\x00\x00"           # one zero sample
)


# Tiny mp4 — generated via ffmpeg in CI would be cleaner but we can't
# depend on ffmpeg here. Instead, we ship a 32-byte ftyp box that mp4
# parsers accept the start of (real demux will fail mid-stream — the
# point is the entrypoint had to LOOK at it).
_MINIMAL_MP4 = (
    b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2mp41"
)


# Minimal valid STL (binary, header + 0 triangles)
_MINIMAL_STL = b"\x00" * 80 + b"\x00\x00\x00\x00"

_INPUT_TYPE_DEFAULTS = {
    # File-extension hints come from describe-chat.ts (_FILE_TYPE_VOCAB),
    # so the wizard-side type strings line up with what we make here.
    "image":  ("input.png", _make_minimal_png),
    "video":  ("input.mp4", lambda: _MINIMAL_MP4),
    "audio":  ("input.wav", lambda: _MINIMAL_WAV),
    "pdf":    ("input.pdf", lambda: _MINIMAL_PDF),
    "text":   ("input.txt", lambda: b"hello\n"),
    "csv":    ("input.csv", lambda: b"a,b\n1,2\n"),
    "json":   ("input.json", lambda: b'{"k":1}'),
    "mesh":   ("input.stl", lambda: _MINIMAL_STL),
    "cad":    ("input.stl", lambda: _MINIMAL_STL),
}


def synth_input_for(input_type: str, dest_dir: str) -> Tuple[str, int]:
    """Write a minimum-valid input file for `input_type` into dest_dir.

    Returns (filename, size_bytes). Unknown input_types fall back to
    a 1KB random binary file with a generic name — the entrypoint may
    reject it, in which case smoke fails with a clear "no output
    produced" message and the operator knows the node needs a
    type-specific sample.
    """
    norm = (input_type or "").strip().lower()
    if norm in _INPUT_TYPE_DEFAULTS:
        fname, factory = _INPUT_TYPE_DEFAULTS[norm]
        body = factory()
    else:
        fname = "input.bin"
        body = os.urandom(1024)
    path = os.path.join(dest_dir, fname)
    with open(path, "wb") as f:
        f.write(body)
    return fname, len(body)


def collect_outputs(out_dir: str) -> list[dict]:
    """Return a sorted list of {name, size} for every file in out_dir."""
    out_files: list[dict] = []
    if not os.path.isdir(out_dir):
        return out_files
    for fname in sorted(os.listdir(out_dir)):
        fpath = os.path.join(out_dir, fname)
        if os.path.isfile(fpath):
            out_files.append({"name": fname, "size": os.path.getsize(fpath)})
    return out_files


def has_real_output(out_files: list[dict]) -> bool:
    """True iff the container produced at least one non-empty file.

    Smoke + sample-test gate the build on this — an entrypoint that
    exits 0 without writing anything has not earned a NodeDefinition
    row in the palette.

    NOTE: this is the legacy permissive gate (any non-empty file). It
    accepts an error-log file as "real output", so on its own it lets
    broken nodes through — e.g. a pdf2image node missing poppler that
    catches the exception and writes an error.json. Prefer the stricter
    helpers below in new code:

      - ``looks_like_runtime_failure(stdout, stderr)`` — catch the
        case where the script caught a runtime error and exited 0.
      - ``output_matches_declared_type(out_dir, out_files, output_type)``
        — require at least one produced file to look like the declared
        output type (extension + magic bytes for binary formats).
    """
    return any(f["size"] > 0 for f in out_files)


# ---------------------------------------------------------------------
# Strict-gate helpers — the γ test layers these on top of has_real_output
# so an error-log file can't masquerade as a real result.
# ---------------------------------------------------------------------

# Patterns that strongly suggest the entrypoint caught a runtime error
# and exited 0 anyway (so has_real_output sees the error log it wrote
# and passes the build). Matched case-insensitively over stdout+stderr.
# A hit fails the γ test even when the exit code is 0.
_RUNTIME_FAILURE_PATTERNS: tuple[str, ...] = (
    "traceback (most recent call last)",
    "modulenotfounderror",
    "importerror",
    "no module named",
    "command not found",
    "is not installed",
    "is not in path",
    "is not recognized",
    "error converting",
    "error processing",
    "failed to load",
    "fatal error",
)


# ---------------------------------------------------------------------
# Hardware-variant docker-run helpers — used by smoke + sample-test
# probes to expose the right accelerator devices to GPU variants AND to
# detect "the variant silently fell back to CPU" so the per-variant
# verified badge doesn't lie.
# ---------------------------------------------------------------------

# Map a variant key prefix → (docker run device args, accelerator family).
# variant keys observed in this repo: "hero", "cpu", "cuda12.4", "cuda12.6",
# "rocm6", "intel-xpu", "npu-openvino"… so we match by prefix.
_VARIANT_DEVICE_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    # (prefix-or-key,                    docker run flags,                       family-tag)
    ("cuda",      ("--gpus", "all"),                                                   "cuda"),
    ("nvidia",    ("--gpus", "all"),                                                   "cuda"),
    ("rocm",      ("--device=/dev/kfd", "--device=/dev/dri", "--group-add=video"),     "rocm"),
    ("intel-xpu", ("--device=/dev/dri",),                                              "intel-xpu"),
    ("intel",     ("--device=/dev/dri",),                                              "intel-xpu"),
    ("npu",       (),                                                                  "npu"),
    # "hero" + "cpu" + everything else → no device args, no family.
)


def docker_device_args_for_variant(variant: Optional[str]) -> tuple[str, ...]:
    """Return the docker-run flags that expose the right accelerator to a
    variant's container. CPU / hero / unknown variants get no flags.

    A worker that doesn't have the requested device will fail with a
    daemon error at `docker run` time — that surfaces as a normal
    docker-run failure in the smoke probe and feeds the repair loop, so
    callers don't need a separate "host has GPU?" check."""
    if not variant:
        return ()
    key = variant.strip().lower()
    if key in ("", "hero", "cpu"):
        return ()
    for prefix, flags, _family in _VARIANT_DEVICE_RULES:
        if key.startswith(prefix):
            return flags
    return ()


def accelerator_family_for_variant(variant: Optional[str]) -> str:
    """Return the family tag (cuda / rocm / intel-xpu / npu) for a variant,
    or "" for CPU / hero / unknown. Used by the GPU-presence verifier so it
    knows which markers count as a positive detection signal."""
    if not variant:
        return ""
    key = variant.strip().lower()
    if key in ("", "hero", "cpu"):
        return ""
    for prefix, _flags, family in _VARIANT_DEVICE_RULES:
        if key.startswith(prefix):
            return family
    return ""


# Patterns that mean "the variant ran but its accelerator was missing /
# silently fell back to CPU". For a non-hero variant we want this to fail
# the strict gate — a CUDA variant that produces a valid PNG by running
# the same CPU codepath as hero isn't a "verified CUDA variant".
_GPU_ABSENCE_PATTERNS: dict[str, tuple[str, ...]] = {
    "cuda": (
        "no cuda-capable device",
        "no cuda gpus are available",
        "cuda runtime error: no cuda-capable",
        "cuda is not available",
        "torch.cuda.is_available() returned false",
        "could not load cuda runtime",
        "cuda_visible_devices is empty",
        "falling back to cpu",
    ),
    "rocm": (
        "no rocm devices",
        "hip error: no hip-capable device",
        "rocm is not available",
        "torch.cuda.is_available() returned false",  # rocm reuses torch.cuda
        "falling back to cpu",
    ),
    "intel-xpu": (
        "no xpu devices",
        "xpu is not available",
        "intel extension for pytorch is not installed",
        "torch.xpu.is_available() returned false",
        "falling back to cpu",
    ),
    "npu": (
        "no npu device",
        "openvino device not found",
        "falling back to cpu",
    ),
}


def looks_like_missing_accelerator(
    stdout: str, stderr: str, variant: Optional[str],
) -> Optional[str]:
    """Return the first absence marker for ``variant``'s expected accelerator
    family, or None when no marker is found. CPU / hero variants always
    return None (they have no accelerator to be missing)."""
    family = accelerator_family_for_variant(variant)
    if not family:
        return None
    combined = ((stdout or "") + "\n" + (stderr or "")).lower()
    for pat in _GPU_ABSENCE_PATTERNS.get(family, ()):
        if pat in combined:
            return pat
    return None


def looks_like_runtime_failure(stdout: str, stderr: str) -> Optional[str]:
    """Return the first matching error pattern in stdout+stderr (lower-cased),
    or None when no failure marker is present. Used by the γ test to reject
    builds whose script caught a runtime error and wrote an error log instead
    of real output — the exact failure mode of a pdf2image node missing the
    poppler-utils system binary."""
    combined = ((stdout or "") + "\n" + (stderr or "")).lower()
    for pat in _RUNTIME_FAILURE_PATTERNS:
        if pat in combined:
            return pat
    return None


# Declared output_type → set of acceptable file extensions for the γ test.
# Aligned with the wizard's catalog vocabulary and the synth input map.
# Unknown / open types ("other", "any", "") fall back to the permissive gate.
_OUTPUT_TYPE_EXTENSIONS: dict[str, set[str]] = {
    "image":  {"png", "jpg", "jpeg", "tif", "tiff", "webp", "bmp", "gif"},
    "video":  {"mp4", "mov", "avi", "mkv", "webm"},
    "audio":  {"mp3", "wav", "flac", "m4a", "ogg", "aac"},
    "mesh":   {"obj", "fbx", "gltf", "glb", "usd", "usda", "usdc", "stl", "ply"},
    "pdf":    {"pdf"},
    "text":   {"txt", "md", "log"},
    "json":   {"json"},
    "csv":    {"csv"},
}

# Magic-byte signatures for binary output types. Cheap (no PIL/ffprobe);
# enough to catch "wrote a fake foo.png that's actually an error log".
_MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    "pdf":   (b"%PDF-",),
    "image": (
        b"\x89PNG\r\n\x1a\n",   # PNG
        b"\xff\xd8\xff",         # JPEG
        b"GIF87a", b"GIF89a",    # GIF
        b"BM",                   # BMP
        b"II*\x00", b"MM\x00*",  # TIFF
        b"RIFF",                 # WEBP (RIFF\x..\x..\x..WEBP, prefix only)
    ),
    "video": (
        b"\x00\x00\x00\x18ftyp",
        b"\x00\x00\x00\x1cftyp",
        b"\x00\x00\x00 ftyp",
        b"OggS",
        b"\x1aE\xdf\xa3",
    ),
    "audio": (
        b"ID3",
        b"\xff\xfb", b"\xff\xf3", b"\xff\xf2",
        b"RIFF",
        b"OggS",
        b"fLaC",
    ),
}


def output_matches_declared_type(
    out_dir: str,
    out_files: list[dict],
    expected_output_type: Optional[str],
) -> Tuple[bool, str]:
    """Strict γ-test gate: produced files must look like the declared
    output_type. Returns (ok, reason).

    Two layers, increasing in rigor:
      1. EXTENSION — at least one non-empty file's extension is in the set
         we recognise for ``expected_output_type``. Catches "wrote an
         error.json when output_type was image".
      2. MAGIC BYTES — for known binary types (image / video / audio /
         pdf), require at least one extension-matching file's first 16
         bytes to look like that type. Catches "renamed an error log to
         result.png".

    Unknown / open types (empty / "other" / "any" / "none" / unmapped) fall
    back to ``has_real_output`` — be permissive so a one-off output_type
    like 'quantum-frob' doesn't break the build.
    """
    if not out_files:
        return False, "no output files produced"

    norm = (expected_output_type or "").strip().lower()
    if norm in ("", "other", "any", "none"):
        return has_real_output(out_files), f"permissive gate (output_type={norm or 'unset'})"

    allowed_exts = _OUTPUT_TYPE_EXTENSIONS.get(norm)
    if allowed_exts is None:
        return has_real_output(out_files), f"permissive gate (unmapped output_type={norm})"

    # Layer 1: extension match
    ext_candidates: list[dict] = []
    for f in out_files:
        name = f.get("name") or ""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in allowed_exts and (f.get("size") or 0) > 0:
            ext_candidates.append(f)
    if not ext_candidates:
        return False, (
            f"no produced file has an extension in {sorted(allowed_exts)} for "
            f"output_type={norm!r} — got {[f['name'] for f in out_files]}"
        )

    # Layer 2: magic bytes (only when we have signatures for this type)
    magic_prefixes = _MAGIC_PREFIXES.get(norm)
    if not magic_prefixes:
        return True, f"{len(ext_candidates)} file(s) match output_type={norm} by extension"

    for f in ext_candidates:
        fpath = os.path.join(out_dir, f["name"])
        try:
            with open(fpath, "rb") as fh:
                head = fh.read(16)
        except OSError:
            continue
        if any(head.startswith(prefix) for prefix in magic_prefixes):
            return True, f"{f['name']} magic-byte match for output_type={norm}"

    return False, (
        f"file(s) {[f['name'] for f in ext_candidates]} have the right extension for "
        f"output_type={norm!r} but none have valid magic bytes — likely an error log "
        f"renamed with the result extension"
    )

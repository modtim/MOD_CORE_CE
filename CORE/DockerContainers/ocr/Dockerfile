# ocr — MOD node container for the image-OCR (Tesseract) domain primitives
# (launch-completion-megaplan sz-6.6.1). One container, three operations:
#
#   OCR_OP=ocr_text   image -> text.txt          (plain text, reading order)
#   OCR_OP=ocr_hocr   image -> hocr.html         (word boxes as hOCR markup)
#   OCR_OP=ocr_words  image -> words.json/.tsv   (word boxes + confidence)
#
# Same I/O contract as every other MOD node container: the worker stages the
# node's input files into /data/input and collects /data/output.
#
# Posture: no network access at runtime, no eval/exec, no shell=True (tesseract
# is invoked as a fixed argv list), inputs capped at 10 MB and 100 MP (Pillow
# MAX_IMAGE_PIXELS), languages restricted to the tesseract-ocr-* packs installed
# in the image, --psm restricted to 0-13. Bad input exits non-zero with a JSON
# error on stdout (the vet fail-closed contract) plus /data/output/error.json.
#
# Cloud-vision OCR is out of scope by design: it needs external API keys and
# network access, both of which this container forbids. Scanned-PDF OCR already
# lives in the pdf-processor container (PDF OCR) — this one covers still images.
#
# Authored only in sz-6.6.1 — build/push is a lead deploy step (no disk space
# for a build in the agent lane).

# 1716: rebased off python:3.11-slim (Debian trixie). Trixie carries 10 CRITICAL
# grype findings (libcurl4t64 x8, libtiff6 CVE-2026-52490, libxml2 CVE-2026-6653)
# that Debian marks "wont fix", and tesseract links all three at runtime, so
# apt-get upgrade is a no-op and python:3.12-slim still shows 10. Alpine's
# packaging has fixes for them; pinned to the multi-arch index digest below
# (not a per-platform manifest digest) so arm64 worker fleets (GX10) resolve
# the same tag instead of failing to pull.
FROM python:3.11.17-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929

# tesseract-ocr* language packs so OCR can read Western text without a runtime
# download (the container must work 100% offline). Keep this list in lockstep
# with ALLOWED_LANGS in the entrypoint below. Alpine spells the data packs
# tesseract-ocr-data-<lang>; apk upgrade first so the linked libs (curl/tiff/
# xml2) come from the fixed -security releases.
RUN apk upgrade --no-cache \
    && apk add --no-cache \
        tesseract-ocr \
        tesseract-ocr-data-eng \
        tesseract-ocr-data-deu \
        tesseract-ocr-data-fra \
        tesseract-ocr-data-spa

# Pillow only: image decode/normalize + the MAX_IMAGE_PIXELS guard. Tesseract is
# the installed binary above, invoked as argv — no pytesseract pip layer needed.
# Pinned (with sha256 hashes) to the same version the API image already resolves:
# requirements.lock.txt is the pillow==12.3.0 entry copied from
# CORE/API/requirements.lock.txt. Pillow has no runtime dependencies.
COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.lock.txt \
    && rm -f /tmp/requirements.lock.txt

RUN mkdir -p /data/input /data/output /app

# The entrypoint is written here rather than COPYed so the container is
# self-contained (one file, no sidecar to keep in sync). This block is a
# verbatim mirror of ENTRYPOINT_SOURCE in
# CORE/API/node_definitions_domains/ocr.py.
RUN cat > /app/ocr_process.py <<'PYEOF'
#!/usr/bin/env python3
# ocr — MOD node container entrypoint (launch-completion-megaplan sz-6.6.1).
# One operation per run, selected by OCR_OP:
#
#   OCR_OP=ocr_text   image -> text.txt          (plain text, reading order)
#   OCR_OP=ocr_hocr   image -> hocr.html         (word boxes as hOCR)
#   OCR_OP=ocr_words  image -> words.json/.tsv   (word boxes + confidence)
#
# Same I/O contract as every other MOD node container: the worker stages the
# node's input files into /data/input and collects /data/output.
#
# Posture: no network access at runtime, no eval/exec, no shell=True (tesseract
# is invoked as a fixed argv list), inputs capped at 10 MB and 100 MP (Pillow
# MAX_IMAGE_PIXELS), languages restricted to the tesseract-ocr-* packs installed
# in the image, --psm restricted to 0-13. Bad input exits non-zero with a JSON
# error on stdout (the vet fail-closed contract) plus /data/output/error.json.
#
# Cloud-vision OCR is out of scope by design: it needs external API keys and
# network access, both of which this container forbids.

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from PIL import Image

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_SIZE = 10 * 1024 * 1024  # 10 MB input cap
MAX_PIXELS = 100_000_000  # Pillow decompression-bomb guard (100 MP)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}

# Must match the tesseract-ocr-* language packs installed above — any other
# value is rejected rather than handed to the tesseract binary.
ALLOWED_LANGS = {"eng", "deu", "fra", "spa"}
ALLOWED_PSM = {str(i) for i in range(14)}

# Failures we recognise as "the operation itself failed" (input cap, image
# decode, tesseract argv) as opposed to an unexpected crash.
EXPECTED_ERRORS = (ValueError, OSError, subprocess.TimeoutExpired)

# Scratch space for the normalized PNG handed to tesseract (created on demand so
# importing this entrypoint never leaves a directory behind).
_TMP = None
_TMP_SEQ = 0


def _scratch():
    global _TMP, _TMP_SEQ
    if _TMP is None:
        _TMP = Path(tempfile.mkdtemp(prefix="mod-ocr-"))
    _TMP_SEQ += 1
    return _TMP / f"{_TMP_SEQ}.png"


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _write_text(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(value, encoding="utf-8")


def _write_json(name, obj):
    _write_text(name, json.dumps(obj, ensure_ascii=False))


def _write_error(msg, code="INTERNAL"):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(
        json.dumps({"error": str(msg), "code": code}, sort_keys=True), encoding="utf-8"
    )


def fail(msg, code="INTERNAL"):
    # every failure path ends here: exactly one line of error JSON on stdout
    # (the vet fail-closed contract), the same payload mirrored into
    # /data/output/error.json for the worker, then a non-zero exit.
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    _write_error(msg, code)
    _cleanup()
    sys.exit(1)


def _inputs():
    if not IN.is_dir():
        return []
    return [
        f
        for f in sorted(IN.iterdir())
        if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in IMAGE_SUFFIXES
    ]


def resolve_lang():
    raw = os.getenv("LANGUAGE", "eng").strip().lower()
    parts = [p for p in raw.split("+") if p]
    if not parts or any(p not in ALLOWED_LANGS for p in parts):
        _log(f"WARN: LANGUAGE {raw!r} not in {sorted(ALLOWED_LANGS)} — falling back to eng")
        return "eng"
    return "+".join(parts)


def resolve_psm():
    raw = os.getenv("PSM", "3").strip()
    if raw not in ALLOWED_PSM:
        _log(f"WARN: PSM {raw!r} not in 0-13 — falling back to 3")
        return "3"
    return raw


Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def load_image(path):
    # Validate + normalize one input image; return the scratch PNG path.
    if path.stat().st_size > MAX_SIZE:
        raise ValueError(f"{path.name} is {path.stat().st_size} bytes > {MAX_SIZE} cap")
    with Image.open(path) as im:
        im.load()  # forces the decode so a decompression bomb fails here
        if im.width * im.height > MAX_PIXELS:
            raise ValueError(f"{path.name} is {im.width}x{im.height} px > {MAX_PIXELS} cap")
        rgb = im.convert("RGB")
    staged = _scratch()
    rgb.save(staged, format="PNG")
    return staged


def _tesseract(png, fmt, lang, psm):
    # Run the tesseract binary on a normalized PNG: a fixed argv list, never a shell.
    proc = subprocess.run(
        ["tesseract", str(png), "stdout", "-l", lang, "--psm", psm, fmt],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "tesseract failed").strip().splitlines()[-1]
        raise ValueError(f"tesseract {fmt} failed: {detail}")
    return proc.stdout


def op_ocr_text(paths):
    lang, psm = resolve_lang(), resolve_psm()
    pages = [_tesseract(load_image(p), "txt", lang, psm).strip() for p in paths]
    _write_text("text.txt", "\n\n".join(pages))
    if not any(pages):
        _log("WARN: no text recognized — the image may be blank or too low-res")


def op_hocr(paths):
    lang, psm = resolve_lang(), resolve_psm()
    pages = [_tesseract(load_image(p), "hocr", lang, psm) for p in paths]
    _write_text("hocr.html", "\n".join(pages))


def op_words(paths):
    lang, psm = resolve_lang(), resolve_psm()
    tables = [_tesseract(load_image(p), "tsv", lang, psm) for p in paths]
    merged = _merge_tsv(tables)
    _write_text("words.tsv", merged)
    _write_json("words.json", parse_words(merged))


def _merge_tsv(tables):
    # Concatenate per-image TSV tables, keeping one header and re-numbering pages.
    header, rows, page = None, [], 0
    for tsv in tables:
        lines = [ln for ln in tsv.splitlines() if ln.strip()]
        if not lines:
            continue
        if lines[0].startswith("level\t"):
            header = header or lines[0]
            body = lines[1:]
        else:
            body = lines
        for ln in body:
            parts = ln.split("\t")
            if len(parts) >= 2 and parts[1].isdigit():
                parts[1] = str(int(parts[1]) + page)
                ln = "\t".join(parts)
            rows.append(ln)
        page += 1
    return "\n".join(([header] if header else []) + rows) + "\n"


def parse_words(tsv):
    # TSV -> list of word dicts (box + text + confidence).
    lines = [ln for ln in tsv.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("level\t"):
        return []
    words = []
    for ln in lines[1:]:
        parts = ln.split("\t")
        if len(parts) < 11:
            continue
        level, page, block, par, line, word = parts[:6]
        left, top, width, height = (int(x) for x in parts[6:10])
        text, conf = parts[10], parts[-1]
        if level.strip() != "5":
            continue
        try:
            confidence = int(conf)
        except ValueError:
            confidence = -1
        words.append(
            {
                "page": int(page),
                "block": int(block),
                "par": int(par),
                "line": int(line),
                "word": int(word),
                "left": left,
                "top": top,
                "right": left + width,
                "bottom": top + height,
                "text": text,
                "conf": confidence,
            }
        )
    return words


OPS = {"ocr_text": op_ocr_text, "ocr_hocr": op_hocr, "ocr_words": op_words}


def main():
    op = os.getenv("OCR_OP", "ocr_text").strip().lower()
    if op not in OPS:
        _log(f"ERROR: unknown OCR_OP {op!r}")
        fail(f"unknown OCR_OP {op!r}", "BAD_PARAM")
    try:
        paths = _inputs()
        if not paths:
            fail("no image (png/jpg/jpeg/webp/tif/tiff) found in /data/input", "NO_INPUT")
        _log(f"=== ocr: {op} ({len(paths)} image) ===")
        OPS[op](paths)
    except SystemExit:  # fail() already printed the JSON line - pass the exit through
        raise
    except Exception as exc:  # noqa: BLE001 — report, never crash with a traceback
        _log(f"ERROR: {exc}")
        code = "PROCESSING_FAILURE" if isinstance(exc, EXPECTED_ERRORS) else "INTERNAL"
        fail(str(exc) or exc.__class__.__name__, code)
    _cleanup()


def _cleanup():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:  # fail() already printed the JSON line - pass the exit through
        raise
    except Exception as exc:  # noqa: BLE001 — one JSON line, never a traceback
        fail(f"{type(exc).__name__}: {exc}", "INTERNAL")
PYEOF

WORKDIR /data
ENTRYPOINT ["python3", "/app/ocr_process.py"]

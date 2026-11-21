"""node_definitions_domains/ocr — Image OCR (Tesseract) domain-primitive NodeDefinitions.

launch-completion-megaplan sz-6.6.1 (Phase 6: domain primitives). Registers
three system node definitions backed by the ocr container
(`CORE/DockerContainers/ocr`): plain-text OCR of a still image, hOCR word boxes,
and TSV word boxes with per-word confidence.

Posture notes
-------------
- Specs are plain dicts (same shape as `seed_converters.py`) so the module is
  importable + testable without a DB; `register_ocr()` is the only DB-touching
  entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog already speaks:
  the conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility checks,
  AND named ports (`file` -> `text` / `hocr` / `words`) so the UI can label the
  ports this domain actually exposes. `image`, `text` and `json` are all in
  `node_builder/analyze_prompt.py`'s VALID_INPUT_TYPES.
- Every spec pins its own operation with a FLAT `OCR_OP` default equal to its
  `NODE_DEFINITIONS_BY_OP` key — without it every node would silently run the
  container's default op (the bug the sibling domain modules shipped).
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing system
  row rather than duplicating it (system-node names are globally unique).
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1), exactly as `seed_converters.py` does — existing rows are never deleted.
- Scanned-PDF OCR already exists (`PDF OCR`, see pdf_processing.py); this domain
  covers still images (png/jpeg/tiff/webp) and does not duplicate it.
- Cloud-vision OCR (Google/AWS/etc.) is deliberately OUT of scope: it needs
  external API keys and runtime network access, which the ocr container forbids
  — it runs 100% offline against the tesseract language packs baked into it.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

# Bare repo:tag of the container behind all three operations. The fully-qualified
# ref (registry + project) is resolved at seed time by platform_image() so a
# private-Harbor / on-prem deployment points at its own registry.
CONTAINER_REPO_TAG = "ocr:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/ocr/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# The whitelist the container can actually speak — must stay in lockstep with the
# `tesseract-ocr-*` apt packs installed in CORE/DockerContainers/ocr/Dockerfile
# and with ALLOWED_LANGS inside the entrypoint.
ALLOWED_LANGS = ("eng", "deu", "fra", "spa")
ALLOWED_PSM = tuple(str(i) for i in range(14))
ACCEPTED_IMAGE_FORMATS = ["png", "jpg", "jpeg", "webp", "tif", "tiff"]

# ---------------------------------------------------------------------------
# Container entrypoint — byte-identical mirror of the heredoc in the Dockerfile
# (kept here so the unit tests can exec the real op functions without building
# an image). Heavy deps: Pillow only; tesseract is the installed binary, called
# as an argv list (never a shell string).
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
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
"""

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts — one per operation.
# ---------------------------------------------------------------------------

OCR_TEXT_SPEC = {
    "name": "Image OCR",
    "description": (
        "Read the text printed in a still image (png/jpeg/tiff/webp) and emit "
        "`text` — the recognized words in reading order, one block per line. "
        "Runs Tesseract fully offline against the language packs baked into the "
        "ocr container: English (default), German, French, Spanish, or any "
        "combination (eng+deu). Pick PSM to match the layout: 3 = plain text "
        "block (default), 6 = a single line, 13 = raw line-free text. For a "
        "scanned PDF use PDF OCR instead; for word-level coordinates use Image "
        "OCR Word Boxes. Cloud-vision OCR is not offered: it needs external API "
        "keys and network access, which this container forbids."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "ocr_text",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "image",
        "file": {"type": "image", "description": "Image file(s) to read text from."},
    },
    "output_schema": {
        "output_type": "text",
        "text": {"type": "text", "description": "Recognized text, reading order."},
    },
    "configuration_schema": {
        "OCR_OP": {
            "type": "string",
            "default": "ocr_text",
            "description": "Fixed for this node: ocr-container operation to run.",
        },
        "extra_variables": {
            "LANGUAGE": {
                "type": "string",
                "default": "eng",
                "description": (
                    "Tesseract language(s): eng, deu, fra, spa, or a '+' combo "
                    "such as eng+fra. Anything else falls back to eng."
                ),
            },
            "PSM": {
                "type": "string",
                "default": "3",
                "description": (
                    "Tesseract page-segmentation mode, 0-13. 3 = plain text "
                    "block (default), 6 = single uniform line, 13 = raw line-free text."
                ),
            },
        },
    },
    "accepted_formats": ACCEPTED_IMAGE_FORMATS,
    "cost_in_credits": 1,
}

OCR_HOCR_SPEC = {
    "name": "Image OCR hOCR",
    "description": (
        "OCR a still image and emit `hocr` — the recognized text as hOCR: HTML "
        "with every word/line/block carrying its pixel bounding box, so a "
        "downstream node can highlight or cut out what the image says. Same "
        "offline Tesseract engine, languages and PSM modes as Image OCR. For "
        "machine-readable boxes without HTML use Image OCR Word Boxes instead."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "ocr_hocr",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "image",
        "file": {"type": "image", "description": "Image file(s) to OCR as hOCR."},
    },
    "output_schema": {
        "output_type": "text",
        "hocr": {"type": "text", "description": "hOCR markup with word bounding boxes."},
    },
    "configuration_schema": {
        "OCR_OP": {
            "type": "string",
            "default": "ocr_hocr",
            "description": "Fixed for this node: ocr-container operation to run.",
        },
        "extra_variables": {
            "LANGUAGE": {
                "type": "string",
                "default": "eng",
                "description": (
                    "Tesseract language(s): eng, deu, fra, spa, or a '+' combo "
                    "such as eng+deu. Anything else falls back to eng."
                ),
            },
            "PSM": {
                "type": "string",
                "default": "3",
                "description": (
                    "Tesseract page-segmentation mode, 0-13. 3 = plain text "
                    "block (default), 6 = single uniform line, 13 = raw line-free text."
                ),
            },
        },
    },
    "accepted_formats": ACCEPTED_IMAGE_FORMATS,
    "cost_in_credits": 1,
}

OCR_WORDS_SPEC = {
    "name": "Image OCR Word Boxes",
    "description": (
        "OCR a still image and emit `words` — one entry per recognized word: its "
        "pixel bounding box (left/top/right/bottom), its text, its confidence "
        "(0-100, -1 when Tesseract did not score it) and its page/block/line "
        "coordinates. Multiple input images are merged into one list with pages "
        "re-numbered 0..N. Also written as TSV (`words.tsv`) for shell-friendly "
        "downstream nodes. Use this, not Image OCR, when you need to know WHERE "
        "in the image each word is (masking, redaction, captioning, click maps)."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "ocr_words",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "image",
        "file": {"type": "image", "description": "Image file(s) to OCR word-by-word."},
    },
    "output_schema": {
        "output_type": "json",
        "words": {"type": "json", "description": "Word boxes + text + confidence."},
    },
    "configuration_schema": {
        "OCR_OP": {
            "type": "string",
            "default": "ocr_words",
            "description": "Fixed for this node: ocr-container operation to run.",
        },
        "extra_variables": {
            "LANGUAGE": {
                "type": "string",
                "default": "eng",
                "description": (
                    "Tesseract language(s): eng, deu, fra, spa, or a '+' combo "
                    "such as eng+spa. Anything else falls back to eng."
                ),
            },
            "PSM": {
                "type": "string",
                "default": "3",
                "description": (
                    "Tesseract page-segmentation mode, 0-13. 3 = plain text "
                    "block (default), 6 = single uniform line, 13 = raw line-free text."
                ),
            },
        },
    },
    "accepted_formats": ACCEPTED_IMAGE_FORMATS,
    "cost_in_credits": 1,
}

# Registry entries, keyed by the operation id the rest of the catalog uses.
NODE_DEFINITIONS = [
    OCR_TEXT_SPEC,
    OCR_HOCR_SPEC,
    OCR_WORDS_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "ocr_text": OCR_TEXT_SPEC,
    "ocr_hocr": OCR_HOCR_SPEC,
    "ocr_words": OCR_WORDS_SPEC,
}

RECONCILED_FIELDS = (
    "description",
    "executable_path",
    "input_schema",
    "output_schema",
    "configuration_schema",
    "accepted_formats",
    "cost_in_credits",
)


def register_ocr(db: Session) -> int:
    """Register the 3 image-OCR operations as system node definitions.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping image-OCR domain-primitive seed"
        )
        return 0

    created = 0
    for spec in NODE_DEFINITIONS:
        existing = db.query(NodeDefinition).filter(
            NodeDefinition.name == spec["name"],
            NodeDefinition.tenant_id.is_(None),
        ).first()

        if existing:
            for field in RECONCILED_FIELDS:
                setattr(existing, field, spec[field])
            continue

        db.add(
            NodeDefinition(
                name=spec["name"],
                tenant_id=None,  # system node — available to all tenants
                description=spec["description"],
                type=spec["type"],
                executable_path=spec["executable_path"],
                process_type=spec["process_type"],
                node_kind=spec["node_kind"],
                batch_mode=spec["batch_mode"],
                input_schema=spec["input_schema"],
                output_schema=spec["output_schema"],
                configuration_schema=spec["configuration_schema"],
                accepted_formats=spec["accepted_formats"],
                cost_in_credits=spec["cost_in_credits"],
            )
        )
        created += 1

    if created > 0:
        db.commit()

    return created

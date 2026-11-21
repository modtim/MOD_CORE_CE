"""node_definitions_domains/pdf_processing — PDF domain-primitive NodeDefinitions.

launch-completion-megaplan sz-6.3.1 (Phase 6: Domain primitives, Part A). Registers
four system node definitions backed by the pdf-processor container
(`CORE/DockerContainers/pdf-processor`): extract text, merge PDFs, OCR a scanned
PDF, and fill a form-capable PDF from a field map.

Posture notes
-------------
- Specs are plain dicts (same shape as `seed_converters.py`) so the module is
  importable + testable without a DB; `register_pdf_processing()` is the only
  DB-touching entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog already speaks:
  the conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility checks,
  AND named ports (`file`, `files`, `data`, `text`, `merged_file`, `filled_file`)
  so the UI can label the ports this domain actually exposes. `pdf`, `text` and
  `json` are all in `node_builder/analyze_prompt.py`'s VALID_INPUT_TYPES.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing system
  row rather than duplicating it (system-node names are globally unique).
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1), exactly as `seed_converters.py` does — existing rows are never deleted.
- One container serves four operations, selected by the `PDF_OP` env var
  (`CORE/DockerContainers/pdf-processor/Dockerfile:343` reads
  `os.getenv("PDF_OP", "extract")`). The user-facing knobs live in the nested
  `configuration_schema.extra_variables` map (the orchestrator
  `CORE/API/services/workflow_orchestrator.py:253` name-matches run-level
  inputs against it; `CORE/WorkflowBuilder/components/node-config-panel.tsx:867`
  renders it), but `PDF_OP` deliberately does NOT: it is a FLAT top-level
  `configuration_schema` key, injected at dispatch by
  `CORE/API/services/node_config_env_defaults.py:flat_env_defaults`, which picks
  every TOP-LEVEL UPPER_CASE key whose value is a dict with a non-None `default`
  and IGNORES `extra_variables`. A flat key is the only placement that reaches
  the container, so each spec pins `PDF_OP` to its own op at the top level and
  none of the four silently runs the container's `extract` default.
- `PDF_OP` is not user-editable: it is fixed per node. The WB never renders a
  flat key as an editable control, so there is nothing to guard with `enum` —
  the enum stays only as a belt-and-braces record of the container's op set.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

# Bare repo:tag of the container behind all four operations. The fully-qualified
# ref (registry + project) is resolved at seed time by platform_image() so a
# private-Harbor / on-prem deployment points at its own registry.
CONTAINER_REPO_TAG = "pdf-processor:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/pdf-processor/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# The ops the pdf-processor entrypoint dispatches on (Dockerfile:343 reads
# `os.getenv("PDF_OP", "extract")` and fails loudly on anything else). Each
# spec pins PDF_OP as a FLAT top-level configuration_schema key (a sibling of
# extra_variables), NOT inside extra_variables: it is injected at dispatch by
# services/node_config_env_defaults.py:flat_env_defaults, which only reads
# TOP-LEVEL UPPER_CASE keys carrying a non-None `default` and ignores
# extra_variables outright. The WB never renders a flat key, so PDF_OP is not
# user-editable — it is fixed per node. `enum` is kept only as a record of the
# container's op set; nothing in the platform can drift it.
OP_ENV_VAR = "PDF_OP"
PDF_OPS = ("extract", "merge", "ocr", "form_fill")

# The PDF_OP value each spec must pin, keyed by the same op id as
# NODE_DEFINITIONS_BY_OP. The container's entrypoint defaults to `extract`, so
# without this every one of the four nodes would run extraction.
OP_ENV_VALUES = {
    "pdf_extract": "extract",
    "pdf_merge": "merge",
    "pdf_ocr": "ocr",
    "pdf_form_fill": "form_fill",
}


def _op_var(op: str) -> dict:
    return {
        "type": "string",
        "default": op,
        "enum": list(PDF_OPS),
        "description": (
            f"Fixed for this node: selects the pdf-processor operation. "
            f"This node runs {op!r}; changing it turns this node into a "
            "different operation."
        ),
    }

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts — one per operation.
# ---------------------------------------------------------------------------

PDF_EXTRACT_SPEC = {
    "name": "PDF Extract",
    "description": (
        "Extract the text layer of a PDF file. Reads the file on the node's "
        "input port (`file`) and emits `text` — the page text in reading order, "
        "pages separated by a form feed. A PDF with no text layer (a scan) "
        "yields empty text and a warning, not an error: use PDF OCR for that. "
        "Backed by the pdf-processor container (pdfminer.six)."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "pdf_extract",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "pdf",
        "file": {"type": "pdf", "description": "Path/URL of the PDF to read."},
    },
    "output_schema": {
        "output_type": "text",
        "text": {"type": "text", "description": "Extracted page text."},
    },
    "configuration_schema": {
        "extra_variables": {
            "PAGE_RANGE": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional 1-based page selection, e.g. '1-3,7'. Blank = all pages."
                ),
            },
        },
        "PDF_OP": _op_var(OP_ENV_VALUES["pdf_extract"]),
    },
    "accepted_formats": ["pdf"],
    "cost_in_credits": 1,
}

PDF_MERGE_SPEC = {
    "name": "PDF Merge",
    "description": (
        "Merge multiple PDF files into one, preserving page order and embedded "
        "resources (forms, annotations, outlines). Takes a list of PDFs on the "
        "input port (`files`, first page of the first file first) and emits "
        "`merged_file`. Backed by the pdf-processor container (pikepdf)."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "pdf_merge",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "pdf",
        "files": {
            "type": "pdf",
            "description": "List of PDF paths; merged in the order supplied.",
        },
    },
    "output_schema": {
        "output_type": "pdf",
        "merged_file": {"type": "pdf", "description": "Path to the merged PDF."},
    },
    "configuration_schema": {
        "extra_variables": {
            "DEDUP_PAGES": {
                "type": "string",
                "default": "false",
                "description": "Drop byte-identical pages (watermark/stamp sheets).",
            },
        },
        "PDF_OP": _op_var(OP_ENV_VALUES["pdf_merge"]),
    },
    "accepted_formats": ["pdf"],
    "cost_in_credits": 2,
}

PDF_OCR_SPEC = {
    "name": "PDF OCR",
    "description": (
        "Run OCR on a scanned PDF and emit `text`. Rasterises each page and "
        "runs Tesseract over it; pages that already carry a text layer are "
        "passed through so a mixed scan/text PDF is read correctly. `language` "
        "selects the Tesseract language data. Backed by the pdf-processor "
        "container (pytesseract + pdfminer.six fallback)."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "pdf_ocr",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "pdf",
        "file": {"type": "pdf", "description": "Path to the scanned PDF."},
    },
    "output_schema": {
        "output_type": "text",
        "text": {"type": "text", "description": "OCR result string."},
    },
    "configuration_schema": {
        "extra_variables": {
            "LANGUAGE": {
                "type": "string",
                "default": "eng",
                "description": "Tesseract language data (eng, deu, fra, ...).",
            },
            "DPI": {
                "type": "string",
                "default": "300",
                "description": "Rasterisation DPI; 300 is the OCR sweet spot.",
            },
        },
        "PDF_OP": _op_var(OP_ENV_VALUES["pdf_ocr"]),
    },
    "accepted_formats": ["pdf"],
    "cost_in_credits": 3,
}

PDF_FORM_FILL_SPEC = {
    "name": "PDF Form Fill",
    "description": (
        "Fill a form-capable PDF with data. Reads the AcroForm field map from "
        "the `data` port (field name -> value) and writes the values into the "
        "matching fields, emitting `filled_file`. Unknown field names are "
        "reported, never silently dropped. Backed by the pdf-processor "
        "container (pikepdf forms)."
    ),
    "type": "Document",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "pdf_form_fill",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "pdf",
        "file": {"type": "pdf", "description": "PDF that carries form fields."},
        "data": {
            "type": "json",
            "description": "Dict of field-name -> value pairs.",
        },
    },
    "output_schema": {
        "output_type": "pdf",
        "filled_file": {"type": "pdf", "description": "Path to the filled PDF."},
    },
    "configuration_schema": {
        "extra_variables": {
            "FLATTEN": {
                "type": "string",
                "default": "false",
                "description": "Flatten fields into page content after filling.",
            },
        },
        "PDF_OP": _op_var(OP_ENV_VALUES["pdf_form_fill"]),
    },
    "accepted_formats": ["pdf"],
    "cost_in_credits": 2,
}

# Registry entries, keyed by the operation id the rest of the catalog uses.
NODE_DEFINITIONS = [
    PDF_EXTRACT_SPEC,
    PDF_MERGE_SPEC,
    PDF_OCR_SPEC,
    PDF_FORM_FILL_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "pdf_extract": PDF_EXTRACT_SPEC,
    "pdf_merge": PDF_MERGE_SPEC,
    "pdf_ocr": PDF_OCR_SPEC,
    "pdf_form_fill": PDF_FORM_FILL_SPEC,
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


def register_pdf_processing(db: Session) -> int:
    """Register the 4 PDF operations as system node definitions.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping PDF domain-primitive seed"
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


# ---------------------------------------------------------------------------
# Entrypoint source — byte-identical to the heredoc in the pdf-processor
# Dockerfile.  Used by the golden-path tests to exec the real entrypoint
# without docker.  NOTE: no triple-single-quotes inside this block.
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r'''
#!/usr/bin/env python3
"""pdf-processor entrypoint. See the header comment in the Dockerfile."""

import hashlib
import json
import os
import re
import shutil
import sys
import pikepdf
from pathlib import Path

IN = Path("/data/input")
OUT = Path("/data/output")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fail(msg, code="INTERNAL"):
    """One-line JSON error to stdout on every failure, then non-zero exit."""
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    sys.exit(1)


def inputs():
    return [f for f in sorted(IN.iterdir()) if f.is_file() and not f.name.startswith(".")]


def pdfs():
    return [f for f in inputs() if f.suffix.lower() == ".pdf"]


def write_text(name, value):
    (OUT / name).write_text(value, encoding="utf-8")


def extract_text(path, page_numbers=None):
    from pdfminer.high_level import extract_text
    return extract_text(str(path), page_numbers=page_numbers) or ""


# ---------------------------------------------------------------------------
# PAGE_RANGE parser  —  "1-3,5,8-"  →  [1,2,3, 5, 8, 9, … (all rest)]
# 1-based, inclusive; open end is allowed (e.g. "8-" = 8..last).
# Strict regex validation at call site; out-of-range pages silently skipped.
# ---------------------------------------------------------------------------

_PAGE_RE = re.compile(r"^\d+(-\d*)?(,\d+(-\d*)?)*$")


def parse_page_range(spec: str, total_pages: int) -> list[int]:
    """Return the list of 1-based page numbers selected by *spec*.

    Raises ``SystemExit`` on invalid format.  Returns all 1..N when
    *spec* is blank (the container default).  Open-ended ranges like "8-"
    expand to 8..total_pages; out-of-range numbers are silently dropped
    (a warning is logged).
    """
    spec = spec.strip()
    if not spec:
        return list(range(1, total_pages + 1))

    if not _PAGE_RE.match(spec):
        log(f"ERROR: invalid PAGE_RANGE {spec!r}")
        fail(f"invalid PAGE_RANGE {spec!r}", "BAD_PARAM")

    # Enforce the "max 200 ranges" rule from the spec.
    parts = spec.split(",")
    if len(parts) > 200:
        log(f"ERROR: PAGE_RANGE has {len(parts)} groups, max 200")
        fail(f"PAGE_RANGE has {len(parts)} groups, max 200", "BAD_PARAM")

    result: list[int] = []
    seen = set()
    for part in parts:
        if "-" in part:
            low_str, high_str = part.split("-", 1)
            lo = int(low_str)
            if high_str:
                hi = int(high_str)
            else:
                hi = total_pages
            if lo < 1:
                log(f"WARN: PAGE_RANGE value {lo} < 1 — clamped to 1")
                lo = 1
            if hi > total_pages:
                log(f"WARN: PAGE_RANGE value {hi} > {total_pages} — clamped")
                hi = total_pages
            for n in range(lo, hi + 1):
                if n not in seen:
                    seen.add(n)
                    result.append(n)
        else:
            n = int(part)
            if n < 1 or n > total_pages:
                log(f"WARN: PAGE_RANGE page {n} out of range (1..{total_pages}) — ignored")
                continue
            if n not in seen:
                seen.add(n)
                result.append(n)
    return result


# ---------------------------------------------------------------------------
# DEDUP_PAGES helper.  The keep/drop decision is a PURE function
# (_content_fingerprint / _dedup_decision) over plain bytes/str data so it is
# unit-testable without pikepdf; only _page_fingerprint/dedup_pages touch
# pikepdf for actual PDF IO.
# ---------------------------------------------------------------------------


def _content_fingerprint(content_bytes: bytes, resource_keys) -> str:
    """Pure: sha256 of page content bytes + sorted resource key names.

    No pikepdf involved — takes/returns plain Python values so it can be
    unit-tested without a real PDF.
    """
    data = content_bytes + str(sorted(resource_keys)).encode()
    return hashlib.sha256(data).hexdigest()


def _dedup_decision(fingerprints: list) -> tuple:
    """Pure: given ordered page fingerprints, return (keep_indices, dropped_count).

    The first occurrence of each fingerprint is kept; later duplicates are
    dropped. No pikepdf involved — plain list of strings in, plain data out.
    """
    seen = set()
    keep: list[int] = []
    dropped = 0
    for i, fp in enumerate(fingerprints):
        if fp in seen:
            dropped += 1
            continue
        seen.add(fp)
        keep.append(i)
    return keep, dropped


def _page_fingerprint(pdf_path: str, idx: int) -> str:
    """Read page *idx* of *pdf_path* via pikepdf, return its content fingerprint."""
    with pikepdf.open(pdf_path) as pdf:
        page = pdf.pages[idx]
        parts: list[bytes] = []
        # Contents can be a single stream or an array of streams.
        contents = page.get("/Contents")
        if isinstance(contents, pikepdf.PdfObject):
            contents = [contents]
        for obj in contents:
            if hasattr(obj, "read_bytes"):
                parts.append(obj.read_bytes())
            elif isinstance(obj, pikepdf.models.multidict.MultiDict):
                parts.append(obj.read_bytes())
            elif isinstance(obj, pikepdf.Dictionary):
                # inline or direct — read raw bytes
                parts.append(obj.read_bytes() if hasattr(obj, "read_bytes") else b"")
        # Resources keys (flat, not recursive)
        res = page.get("/Resources")
        resource_keys = sorted(res.keys()) if isinstance(res, pikepdf.Dictionary) else []
        content_bytes = b"".join(parts)
    return _content_fingerprint(content_bytes, resource_keys)


def dedup_pages(paths: list[Path]) -> tuple[pikepdf.Pdf, int]:
    """Merge *paths* while dropping pages whose fingerprint matches an earlier page.

    Returns (merged_pdf, dropped_count). All IO lives here; the keep/drop
    decision itself is the pure _dedup_decision function above.
    """
    page_locs: list[tuple[Path, int]] = []
    fingerprints: list[str] = []
    for pdf_path in paths:
        with pikepdf.open(pdf_path) as src:
            n = len(src.pages)
        for idx in range(n):
            fingerprints.append(_page_fingerprint(str(pdf_path), idx))
            page_locs.append((pdf_path, idx))

    keep_indices, dropped = _dedup_decision(fingerprints)
    keep_set = set(keep_indices)

    merged = pikepdf.Pdf.new()
    opened: dict[Path, "pikepdf.Pdf"] = {}
    try:
        for i, (pdf_path, idx) in enumerate(page_locs):
            if i not in keep_set:
                log(f"DEDUP: dropping page {idx + 1} of {pdf_path.name} (duplicate content)")
                continue
            if pdf_path not in opened:
                opened[pdf_path] = pikepdf.open(pdf_path)
            merged.pages.append(opened[pdf_path].pages[idx])
    finally:
        for src in opened.values():
            src.close()
    return merged, dropped


def op_extract(paths):
    """Extract text with optional PAGE_RANGE filtering.

    PAGE_RANGE selects pages *inside* the single input PDF (paths[0]) — this
    op's input_schema declares one `file` port, so page numbers are NOT an
    index into the files list.
    """
    pdf = pikepdf.open(str(paths[0]))
    total = len(pdf.pages)
    pdf.close()
    page_nums = parse_page_range(os.getenv("PAGE_RANGE", ""), total)
    text_parts = [extract_text(paths[0], page_numbers=[pg - 1]) for pg in page_nums]
    write_text("text.txt", "\f".join(text_parts))
    if not any(p.strip() for p in text_parts):
        log("WARN: no text layer found — this looks like a scan, use PDF OCR")


def op_merge(paths):
    """Merge PDFs with optional DEDUP_PAGES."""
    dedup = os.getenv("DEDUP_PAGES", "false").lower() in ("true", "1", "yes")
    merged, dropped = dedup_pages(paths) if dedup else _plain_merge(paths)
    merged.save(str(OUT / "merged.pdf"))
    # Dedup count goes to stderr logs only — output_schema declares a single
    # `merged_file` port and an extra file in /data/output would confuse
    # output collection.
    if dropped:
        log(f"merged {len(paths)} pdf -> merged.pdf ({len(merged.pages)} pages, {dropped} deduped)")
    else:
        log(f"merged {len(paths)} pdf -> merged.pdf ({len(merged.pages)} pages)")


def _plain_merge(paths):
    """No-dedup merge — used when DEDUP_PAGES is off."""
    merged = pikepdf.Pdf.new()
    for p in paths:
        with pikepdf.open(p) as src:
            merged.pages.extend(src.pages)
    return merged, 0


# Must match the tesseract-ocr-* language packs installed above — any other
# value is rejected rather than handed to the tesseract binary.
ALLOWED_LANGS = {"eng", "deu", "fra"}


def resolve_lang():
    raw = os.getenv("LANGUAGE", "eng").strip().lower()
    parts = [p for p in raw.split("+") if p]
    if not parts or any(p not in ALLOWED_LANGS for p in parts):
        log(f"WARN: LANGUAGE {raw!r} not in {sorted(ALLOWED_LANGS)} — falling back to eng")
        return "eng"
    return "+".join(parts)


def op_ocr(paths):
    from pdf2image import convert_from_path
    from pytesseract.pytesseract import image_to_string
    lang = resolve_lang()
    dpi = int(os.getenv("DPI", "300"))
    pages = []
    for p in paths:
        native = extract_text(p)
        if native.strip():
            pages.append(native)
            continue
        pages.append("\n".join(image_to_string(img, lang=lang) for img in convert_from_path(str(p), dpi=dpi)))
    write_text("text.txt", "\f".join(pages))


def op_form_fill(paths):
    fields_path = IN / "fields.json"
    if not fields_path.is_file():
        fail("PDF Form Fill needs fields.json on its input port", "NO_INPUT")
    data = json.loads(fields_path.read_text(encoding="utf-8"))
    if os.getenv("FLATTEN", "false").lower() in ("true", "1", "yes"):
        flatten = True
    else:
        flatten = False
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(paths[0]))
    available = set(reader.fields or [])
    writer = PdfWriter()
    for page in reader.pages:
        writer.write_page(page)
    writer.set_form_data({k: str(v) for k, v in data.items() if k in available})
    staged = OUT / ".filled.tmp.pdf"
    with open(staged, "wb") as fh:
        writer.write(fh)
    if flatten:
        # pypdf has no flatten; pikepdf does. Only pay for it when asked.
        tmp = pikepdf.open(str(staged))
        tmp.flatten()
        tmp.save(str(OUT / "filled.pdf"))
    else:
        shutil.move(str(staged), str(OUT / "filled.pdf"))
    missing = sorted(set(data) - available)
    if missing:
        log(f"WARN: fields not present in the form: {', '.join(missing)}")


OPS = {"extract": op_extract, "merge": op_merge, "ocr": op_ocr, "form_fill": op_form_fill}


def main():
    op = os.getenv("PDF_OP", "extract").strip().lower()
    if op not in OPS:
        log(f"ERROR: unknown PDF_OP {op!r}")
        fail(f"unknown PDF_OP {op!r}", "BAD_PARAM")
    paths = pdfs()
    if not paths:
        log("ERROR: no PDF found in /data/input")
        fail("no PDF found in /data/input", "NO_INPUT")
    log(f"=== pdf-processor: {op} ({len(paths)} pdf) ===")
    OPS[op](paths)


try:
    main()
except SystemExit:
    raise
except Exception:
    import traceback
    fail(f"{type(sys.exc_info()[1]).__name__}: {sys.exc_info()[1]}\n{traceback.format_exc()}", "INTERNAL")
'''

# pdf-processor — MOD node container for the PDF domain primitives
# (launch-completion-megaplan sz-6.3.1). One container, four operations:
#
#   PDF_OP=extract      text layer of a PDF             -> text.txt
#   PDF_OP=merge        N PDFs -> one PDF               -> merged.pdf
#   PDF_OP=ocr          scanned PDF -> text (Tesseract) -> text.txt
#   PDF_OP=form_fill    AcroForm + fields.json          -> filled.pdf
#
# Same I/O contract as every other MOD node container: the worker stages the
# node's input files into /data/input and collects /data/output.
#
# Authored only in sz-6.3.1 — build/push is a lead deploy step (no disk space
# for a build in the agent lane).

FROM python:3.11.17-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929

# Alpine 3.x — tesseract-ocr language packs + poppler-utils, crit-free via upgrade.
# tesseract-ocr-data-* = language packs (eng, deu, fra). poppler-utils = pdftotext.
RUN apk upgrade --no-cache && apk add --no-cache \
        tesseract-ocr \
        tesseract-ocr-data-eng \
        tesseract-ocr-data-deu \
        tesseract-ocr-data-fra \
        poppler-utils

RUN pip install --no-cache-dir \
        pikepdf==9.0.0 \
        pypdf==5.1.0 \
        pdfminer.six==20231228 \
        pytesseract==0.3.13 \
        pdf2image==1.17.0 \
        Pillow==10.4.0 \
        numpy==1.26.4

RUN mkdir -p /data/input /data/output /app

# The entrypoint is written here rather than COPYed so the container is
# self-contained (one file, no sidecar to keep in sync).
RUN cat > /app/pdf_process.py <<'PYEOF'
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
PYEOF

WORKDIR /data
ENTRYPOINT ["python3", "/app/pdf_process.py"]

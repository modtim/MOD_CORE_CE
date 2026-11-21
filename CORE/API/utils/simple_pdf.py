# API/utils/simple_pdf.py
"""FX.97 part 2 (owner decision Q83=A) — pure-Python PDF writer (NO new deps).

The install ships only `pypdf==6.19.0` (a READER — it cannot lay out text), and
adding a PDF-writing dependency would need an image rebuild = a restart window,
so this module writes the simplest possible valid PDF 1.4 itself:

* built-in fonts only: Helvetica (F1) and Helvetica-Bold (F2), WinAnsiEncoding;
* Letter page (612x792 pt), simple top-down text layout, word-wrap at a FIXED
  character width (`MAX_CHARS` — a monospace-style approximation, good enough
  for the fixed-pitch-ish report lines and the 64-hex sha256 digests);
* automatic page breaks + a "Page n of N" footer on every page;
* correct xref offsets + trailer (byte offsets recorded while emitting).

Deterministic: the same input yields the SAME bytes (no timestamps, no random
ids inside the document) so the PDF can be content-hashed.

Character handling: any character not representable in Latin-1 is replaced by
"?" (the built-in Helvetica fonts are 8-bit); "(", ")" and "\\" are then escaped
for a PDF text string.
"""
from __future__ import annotations

from typing import Any, List, Sequence, Tuple

# --- page geometry (points; US Letter) --------------------------------------
PAGE_W = 612.0
PAGE_H = 792.0
MARGIN_L = 72.0
TOP = 72.0                 # baseline of the first line = PAGE_H - TOP
BOTTOM = 90.0              # body text stops here; footer lives below
LINE_H = 13.0
MAX_CHARS = 92             # fixed char width for word-wrap (see docstring)

TITLE_SIZE = 16.0
HEAD_SIZE = 11.0
BODY_SIZE = 10.0
FOOTER_SIZE = 8.0
FOOTER_Y = 42.0            # footer baseline, points from the page bottom

# F1 = Helvetica, F2 = Helvetica-Bold
FONTS: Tuple[str, ...] = ("Helvetica", "Helvetica-Bold")
_FONT_INDEX = {"Helvetica": 1, "Helvetica-Bold": 2}

_LINES_PER_PAGE = int((PAGE_H - TOP - BOTTOM) / LINE_H)


def _sanitize(text: str) -> str:
    """Non-Latin-1 characters -> '?' (8-bit built-in fonts only)."""
    out = []
    for ch in text:
        try:
            ch.encode("latin-1")
        except UnicodeEncodeError:
            ch = "?"
        out.append(ch)
    return "".join(out)


def _pdf_str(text: str) -> bytes:
    """Sanitized + PDF-escaped ( \\ ( ) ) text string body."""
    s = _sanitize(text)
    s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return s.encode("latin-1")


def _wrap(text: str, max_chars: int = MAX_CHARS) -> List[str]:
    """Greedy word-wrap at a fixed character width; long tokens are split."""
    chunks: List[str] = []
    for word in text.split():
        while len(word) > max_chars:
            chunks.append(word[:max_chars])
            word = word[max_chars:]
        chunks.append(word)
    if not chunks:
        return [""]
    lines: List[str] = []
    cur = ""
    for chunk in chunks:
        if cur and len(cur) + 1 + len(chunk) > max_chars:
            lines.append(cur)
            cur = chunk
        else:
            cur = f"{cur} {chunk}" if cur else chunk
    if cur:
        lines.append(cur)
    return lines


def _layout(title: str,
            sections: Sequence[Tuple[str, Sequence[Any]]]) -> List[Tuple[str, str]]:
    """Flatten (title, sections) into (kind, text) logical lines.

    kind: 'title' | 'head' | 'body' | 'blank'. A None section body is treated
    as empty; None items render as ""."""
    lines: List[Tuple[str, str]] = []
    for text in _wrap(str(title), MAX_CHARS - 8) if title else []:
        lines.append(("title", text))
    for heading, body in sections:
        for text in _wrap(str(heading), MAX_CHARS - 4):
            lines.append(("head", text))
        for item in (body or []):
            for text in _wrap("" if item is None else str(item)):
                lines.append(("body", text))
        lines.append(("blank", ""))
    return lines


def _paginate(lines: Sequence[Tuple[str, str]]) -> List[List[Tuple[str, str]]]:
    """Split into pages of at most _LINES_PER_PAGE lines (no orphan logic)."""
    pages: List[List[Tuple[str, str]]] = []
    rest = list(lines)
    while True:
        pages.append(rest[:_LINES_PER_PAGE])
        rest = rest[_LINES_PER_PAGE:]
        if not rest:
            return pages


def _content_stream(page_lines: Sequence[Tuple[str, str]],
                    page_no: int, total: int) -> bytes:
    """One page's content stream: absolute Tm per line + footer."""
    parts = [b"BT"]
    y = PAGE_H - TOP
    first = True
    for kind, text in page_lines:
        if kind == "blank":
            y -= LINE_H
            continue
        is_title = first and kind == "title"
        size = TITLE_SIZE if is_title else (
            HEAD_SIZE if kind == "head" else BODY_SIZE)
        font = "Helvetica-Bold" if (is_title or kind == "head") else "Helvetica"
        parts.append(f"/F{_FONT_INDEX[font]}".encode("ascii"))
        parts.append(f"{size:.1f} Tf".encode("ascii"))
        parts.append(f"1 0 0 1 {MARGIN_L:.2f} {y:.2f} Tm".encode("ascii"))
        parts.append(b"(" + _pdf_str(text) + b") Tj")
        y -= LINE_H
        first = False
    parts.append(b"ET")
    # Footer: "Page n of N", centred by the fixed-char-width estimate.
    footer = f"Page {page_no} of {total}"
    est_w = FOOTER_SIZE * 0.5 * len(footer)
    fx = max(MARGIN_L, (PAGE_W - est_w) / 2.0)
    parts.append(b"BT")
    parts.append(f"/F1".encode("ascii"))
    parts.append(f"{FOOTER_SIZE:.1f} Tf".encode("ascii"))
    parts.append(f"1 0 0 1 {fx:.2f} {FOOTER_Y:.2f} Tm".encode("ascii"))
    parts.append(b"(" + _pdf_str(footer) + b") Tj")
    parts.append(b"ET")
    return b"\n".join(parts)


def _build_pdf(pages_lines: Sequence[Sequence[Tuple[str, str]]]) -> bytes:
    """Assemble objects + xref + trailer with exact byte offsets.

    Object numbering: 1 Catalog, 2 Pages, 3 Helvetica, 4 Helvetica-Bold,
    then per page i: page obj = 5+2i, content obj = 6+2i."""
    total = len(pages_lines)
    buf = bytearray()
    offsets: dict[int, int] = {}

    def emit(n: int, body: bytes) -> None:
        offsets[n] = len(buf)
        buf.extend(f"{n} 0 obj\n".encode("ascii"))
        buf.extend(body)
        buf.extend(b"\nendobj\n")

    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(total))
    emit(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    emit(2, (f"<< /Type /Pages /Kids [{kids}] /Count {total} >>"
             ).encode("ascii"))
    emit(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>")
    emit(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>")
    for i, page_lines in enumerate(pages_lines):
        stream = _content_stream(page_lines, i + 1, total)
        pobj, cobj = 5 + 2 * i, 6 + 2 * i
        emit(pobj, (f"<< /Type /Page /Parent 2 0 R /MediaBox "
                    f"[0 0 {PAGE_W:.0f} {PAGE_H:.0f}] /Resources << "
                    f"/Font << /F1 3 0 R /F2 4 0 R >> >> /Contents "
                    f"{cobj} 0 R >>").encode("ascii"))
        emit(cobj, (f"<< /Length {len(stream)} >>\nstream\n").encode("ascii")
             + stream + b"\nendstream")

    n_objs = 4 + 2 * total
    return _rebuild_with_header(bytes(buf), offsets, n_objs)


def _rebuild_with_header(body: bytes, offsets: dict[int, int],
                         n_objs: int) -> bytes:
    """Final pass: prepend header, write xref at a known offset, trailer."""
    head = b"%PDF-1.4\n%FX.97 simple_pdf\n"
    out = bytearray(head)
    out.extend(body)
    xref_at = len(out)
    out.extend(b"xref\n")
    out.extend(f"0 {n_objs + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for n in range(1, n_objs + 1):
        out.extend(f"{offsets[n] + len(head):010d} 00000 n \n".encode("ascii"))
    out.extend(b"trailer\n")
    out.extend(b"<< /Root 1 0 R >>\n")
    out.extend(b"startxref\n")
    out.extend(f"{xref_at}\n".encode("ascii"))
    out.extend(b"%%EOF\n")
    return bytes(out)


def render_text_pdf(title: str,
                    sections: List[Tuple[str, List[str]]]) -> bytes:
    """Render a text-only PDF (PDF 1.4, Letter, Helvetica/Bold).

    `sections` is a list of (heading, [lines...]). Deterministic for the same
    input; page breaks automatic; "Page n of N" footer on every page.
    """
    lines = _layout(title or "", [
        (h, list(b) if b is not None else []) for h, b in (sections or [])
    ])
    pages = _paginate(lines)
    return _build_pdf(pages)

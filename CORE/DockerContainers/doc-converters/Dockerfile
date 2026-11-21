# doc-converters — MOD node container for the document-format domain primitives
# (launch-completion-megaplan sz-6.9.1). One container, seven operations:
#
#   DOC_CONVERT_OP=markdown_to_html  markdown file -> document.html (sanitized)
#   DOC_CONVERT_OP=html_to_markdown  html file     -> document.md
#   DOC_CONVERT_OP=html_to_pdf       html file     -> document.pdf   (weasyprint, offline)
#   DOC_CONVERT_OP=docx_to_markdown  .docx         -> document.md
#   DOC_CONVERT_OP=docx_to_text      .docx         -> document.txt
#   DOC_CONVERT_OP=text_to_docx      .txt/.md      -> document.docx
#   DOC_CONVERT_OP=markdown_to_docx  .md           -> document.docx
#
# Same I/O contract as every other MOD node container: the worker stages the
# node's input files into /data/input and collects /data/output. Configuration
# arrives as env vars from the node's configuration_schema (DOC_CONVERT_OP plus
# the per-op knobs); a params.json on the input port overrides them for hand-runs.
#
# Posture: no LibreOffice (it is gigabytes of image layer) — python-docx, weasyprint
# and a stdlib markdown/html subset do the job. No network access at runtime: the
# markdown subset escapes raw HTML, the sanitizer whitelists tags/attrs and refuses
# non-http(s) links, and html->pdf installs a weasyprint url handler that REFUSES
# every URL except an inline data: image. Inputs are untrusted: no eval/exec, no
# shell=True, inputs capped at 10 MB, bad input exits non-zero with one line of
# error JSON on stdout.
#
# Authored only in sz-6.9.1 — build/push is a lead deploy step (no disk space
# for a build in the agent lane).

FROM python:3.11-slim

# fontconfig + DejaVu so weasyprint can lay out text 100% offline (a PDF with no
# usable font family renders blank glyphs, which is worse than an error).
# pango (libpango-1.0-0 + libpangoft2-1.0-0) is the system library weasyprint
# dlopens at import (weasyprint/text/ffi.py); without it html_to_pdf fails.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-dejavu \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Pinned. python-docx reads/writes .docx (its lxml dep is pulled by pip);
# weasyprint does the html->pdf layout. markdown/bleach are NOT used: the
# markdown subset and the sanitizer are stdlib-only inside the entrypoint, which
# keeps the layer count and the image size down.
RUN pip install --no-cache-dir \
        python-docx==1.1.2 \
        weasyprint==61.2

# Build-time check: importing weasyprint dlopens pango/pangoft2/harfbuzz/
# fontconfig (weasyprint/text/ffi.py, module level), so a missing system
# library fails the BUILD here instead of the html_to_pdf op at runtime.
RUN python -c "import weasyprint"

RUN mkdir -p /data/input /data/output /app

# The entrypoint is written here rather than COPYed so the container is
# self-contained (one file, no sidecar to keep in sync). This block is a
# verbatim mirror of ENTRYPOINT_SOURCE in
# CORE/API/node_definitions_domains/file_converters.py — the test suite
# asserts the two are byte-identical.
RUN cat > /app/doc_convert.py <<'PYEOF'
#!/usr/bin/env python3
"""doc-converters entrypoint. See the header comment in the Dockerfile."""

import json
import os
import re
import sys
from pathlib import Path
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.error import URLError
from zipfile import BadZipFile, ZipFile, is_zipfile

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_INPUT_BYTES = 10 * 1024 * 1024

ALLOWED_TAGS = frozenset(
    "p br hr ul ol li blockquote pre code em strong a table thead tbody tr "
    "td th div span h1 h2 h3 h4 h5 h6".split()
)
ALLOWED_ATTRS = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan"}),
}
VOID_TAGS = frozenset({"br", "hr", "img"})
DROP_TAGS = frozenset({"script", "style", "iframe", "object", "embed", "form", "link", "meta", "base"})
SAFE_SCHEMES = frozenset({"http", "https"})
BLEACH_ATTRS = frozenset(n for names in ALLOWED_ATTRS.values() for n in names)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


CODE_NO_INPUT = "NO_INPUT"
CODE_BAD_PARAM = "BAD_PARAM"
CODE_PROCESSING = "PROCESSING_FAILURE"
CODE_OUTPUT_NOT_WRITTEN = "OUTPUT_NOT_WRITTEN"
CODE_INTERNAL = "INTERNAL"


def fail(msg, code=CODE_INTERNAL):
    # every bad-input path ends here: one line of error JSON on stdout, then a
    # non-zero exit.
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    sys.exit(1)


def inputs():
    return [f for f in sorted(IN.iterdir()) if f.is_file() and not f.name.startswith(".")]


def check_size(path):
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        fail("%s is %d bytes, over the %d MB input cap"
             % (path.name, size, MAX_INPUT_BYTES // (1024 * 1024)), CODE_NO_INPUT)
    return path


PARAM_ENV_NAMES = ("SANITIZE", "WRAP_PARAGRAPHS", "INCLUDE_TABLES")


def params():
    # Configuration comes from the node's configuration_schema, which the worker
    # injects as env vars (same contract as the pdf-processor container). A
    # params.json on the input port is accepted as an override for hand-runs.
    lowered = {name.lower(): os.getenv(name, "") for name in PARAM_ENV_NAMES}
    path = IN / "params.json"
    if not path.is_file():
        return lowered
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail("params.json is not valid JSON: %s" % exc, CODE_BAD_PARAM)
        return lowered
    if not isinstance(data, dict):
        fail("params.json must be a JSON object", CODE_BAD_PARAM)
    lowered.update({k.lower(): v for k, v in data.items()})
    unknown = sorted(set(lowered) - {name.lower() for name in PARAM_ENV_NAMES})
    if unknown:
        fail("params keys %s are not doc-converters parameters" % ", ".join(unknown), CODE_BAD_PARAM)
    return lowered


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail("%s is not UTF-8 text" % path.name, CODE_NO_INPUT)
    except OSError as exc:
        fail("cannot read %s: %s" % (path.name, exc), CODE_NO_INPUT)


def write_text(name, value):
    try:
        (OUT / name).write_text(value, encoding="utf-8")
    except OSError as exc:
        fail("could not write /data/output/%s: %s" % (name, exc), CODE_OUTPUT_NOT_WRITTEN)
    if not (OUT / name).is_file():
        fail("expected output /data/output/%s was not written" % name, CODE_OUTPUT_NOT_WRITTEN)


def flag(cfg, name, default=True):
    """A configuration knob arrives as an env var, so an unset one is the empty
    string — fall back to `default` rather than treating '' as false."""
    raw = cfg.get(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("true", "1", "yes")


def safe_url(url):
    """True only for http/https URLs; javascript:, file:, data:, protocol-relative
    and everything else is refused before it can reach an href."""
    return urlsplit(url.strip()).scheme.lower() in SAFE_SCHEMES


def safe_src(url):
    """<img> targets: http(s) or an inline data: image. Never a network fetch at
    runtime — the html->pdf url_fetcher drops even these, see op_html_to_pdf."""
    scheme = urlsplit(url.strip()).scheme.lower()
    return scheme in SAFE_SCHEMES or scheme == "data"


class _Sanitizer(HTMLParser):
    """bleach-style whitelist sanitizer, stdlib only (bleach is not in the API
    image and the node container must stay small). ALLOWED_TAGS survive with
    ALLOWED_ATTRS per tag; DROP_TAGS subtrees are removed whole; every other tag
    is unwrapped (markup gone, text kept and re-escaped). Text is always
    escaped, so input can never re-open a tag."""

    def __init__(self, keep_img=True):
        HTMLParser.__init__(self)
        self.keep_img = keep_img
        self.out = []
        self.skip = 0
        self.open = []

    def _attrs(self, tag, attrs):
        allowed = ALLOWED_ATTRS.get(tag, frozenset())
        kept = []
        for name, value in attrs:
            if name not in allowed:
                continue
            value = value or ""
            if name == "href" and not safe_url(value):
                continue
            if name == "src" and not safe_src(value):
                continue
            kept.append('%s="%s"' % (name, escape(value, quote=True)))
        return (" " + " ".join(kept)) if kept else ""

    def handle_starttag(self, tag, attrs):
        if tag in DROP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "img" and not self.keep_img:
            return
        if tag in ALLOWED_TAGS:
            self.out.append("<%s%s>" % (tag, self._attrs(tag, attrs)))
            if tag not in VOID_TAGS:
                self.open.append(tag)
        else:
            # unknown tag: markup dropped, its text is kept and escaped below.
            return

    def handle_startendtag(self, tag, attrs):
        if self.skip or tag in DROP_TAGS:
            return
        if tag in ALLOWED_TAGS and (self.keep_img or tag != "img"):
            self.out.append("<%s%s/>" % (tag, self._attrs(tag, attrs)))

    def handle_endtag(self, tag):
        if self.skip:
            self.skip -= 1
            return
        if tag in self.open:
            while self.open and self.open[-1] != tag:
                self.out.append("</%s>" % self.open.pop())
            if self.open:
                self.out.append("</%s>" % self.open.pop())

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(escape(data, quote=False))

    def handle_comment(self, data):
        return

    def handle_decl(self, data):
        return

    def unknown_decl(self, data):
        return


def sanitize_html(fragment, keep_img=True):
    """Public entry: whitelist-sanitize an html fragment (see _Sanitizer)."""
    sanitizer = _Sanitizer(keep_img=keep_img)
    sanitizer.feed(fragment)
    while sanitizer.open:
        sanitizer.out.append("</%s>" % sanitizer.open.pop())
    return "".join(sanitizer.out)


def _collapse(text):
    """Collapse runs of spaces/tabs and >2 newlines; strip trailing space."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _collapse_text(text):
    return re.sub(r"\s+", " ", text).strip()


FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*\S*$|^\s{0,3}(`{3,}|~{3,})\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BREAK_RE = re.compile(r"^\s{0,3}(?:\*[ \t]*){3,}$|^\s{0,3}(?:-[ \t]*){3,}$|^\s{0,3}(?:_[ \t]*){3,}$")
QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
ULIST_RE = re.compile(r"^([ \t]*)[-*+]\s+(.*)$")
OLIST_RE = re.compile(r"^([ \t]*)(\d{1,9})[.)]\s+(.*)$")
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
STRONG_RE = re.compile(r"\*\*(?=\S)(.+?\S)\*\*")
EMPH_RE = re.compile(r"(?<!\*)\*(?=\S)([^*\n]+?\S)\*(?!\*)")
PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")


def md_blocks(text):
    """Tokenise the markdown subset into blocks. Shared by md->html and md->docx
    so both agree on what the input means."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = FENCE_RE.match(line)
        if fence:
            marker = (fence.group(1) or fence.group(2))[0]
            width = len(fence.group(1) or fence.group(2))
            i += 1
            body = []
            while i < n:
                closer = FENCE_RE.match(lines[i])
                if closer and (closer.group(1) or closer.group(2))[0] == marker \
                        and len(closer.group(1) or closer.group(2)) >= width:
                    i += 1
                    break
                body.append(lines[i])
                i += 1
            blocks.append({"kind": "code", "text": "\n".join(body)})
            continue
        heading = HEADING_RE.match(line)
        if heading:
            blocks.append({"kind": "heading", "level": len(heading.group(1)),
                           "text": heading.group(2)})
            i += 1
            continue
        if BREAK_RE.match(line):
            blocks.append({"kind": "hr"})
            i += 1
            continue
        table = _table_at(lines, i)
        if table:
            blocks.append({"kind": "table", "rows": table[0]})
            i = table[1]
            continue
        quote = QUOTE_RE.match(line)
        if quote:
            body = [quote.group(1)]
            i += 1
            while i < n and QUOTE_RE.match(lines[i]):
                body.append(QUOTE_RE.match(lines[i]).group(1))
                i += 1
            blocks.append({"kind": "quote", "text": "\n".join(body)})
            continue
        bullet = ULIST_RE.match(line)
        if bullet:
            items = [bullet.group(2)]
            i += 1
            while i < n and ULIST_RE.match(lines[i]):
                items.append(ULIST_RE.match(lines[i]).group(2))
                i += 1
            blocks.append({"kind": "ul", "items": items})
            continue
        ordered = OLIST_RE.match(line)
        if ordered:
            items = [ordered.group(3)]
            i += 1
            while i < n and OLIST_RE.match(lines[i]):
                items.append(OLIST_RE.match(lines[i]).group(3))
                i += 1
            blocks.append({"kind": "ol", "items": items})
            continue
        para = []
        while i < n and lines[i].strip() and not _block_start(lines[i]):
            para.append(lines[i].strip())
            i += 1
        blocks.append({"kind": "para", "text": "\n".join(para)})
    return blocks


def _split_row(line):
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _is_sep_row(line):
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", c or "") for c in cells)


def _table_at(lines, index):
    """GFM-style pipe table at `index` -> (rows, next_index), else None."""
    if "|" not in lines[index] or index + 1 >= len(lines) or not _is_sep_row(lines[index + 1]):
        return None
    rows = [_split_row(lines[index])]
    j = index + 2
    while j < len(lines) and "|" in lines[j] and lines[j].strip():
        rows.append(_split_row(lines[j]))
        j += 1
    return rows, j


def _block_start(line):
    return any((FENCE_RE.match(line), HEADING_RE.match(line), BREAK_RE.match(line),
                QUOTE_RE.match(line), ULIST_RE.match(line), OLIST_RE.match(line),
                "|" in line))


def md_inline(text):
    """Inline markdown -> html. Escapes everything, then re-admits only the
    subset: code spans, images (http(s)/data only), links (http(s) only),
    **strong**, *emphasis*. Output is always safe by construction."""
    stash = []

    def hold_code(match):
        stash.append("<code>%s</code>" % escape(match.group(1), quote=False))
        return "\x00%d\x00" % (len(stash) - 1)

    text = CODE_SPAN_RE.sub(hold_code, text)
    text = escape(text, quote=False)

    def hold_image(match):
        alt, url = match.group(1), match.group(2)
        if not safe_src(url):
            return escape(alt, quote=False)
        return '<img alt="%s" src="%s" />' % (escape(url, quote=True), escape(alt, quote=True))

    def hold_link(match):
        label, url = match.group(1), match.group(2)
        if not safe_url(url):
            return label
        return '<a href="%s">%s</a>' % (escape(url, quote=True), label)

    text = IMAGE_RE.sub(hold_image, text)
    text = LINK_RE.sub(hold_link, text)
    text = STRONG_RE.sub(r"<strong>\1</strong>", text)
    text = EMPH_RE.sub(r"<em>\1</em>", text)
    return PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], text)


def md_to_html(text):
    """The markdown subset -> an html fragment (no <html> wrapper)."""
    parts = []
    for block in md_blocks(text):
        kind = block["kind"]
        if kind == "code":
            parts.append("<pre><code>%s</code></pre>"
                         % escape(block["text"], quote=False))
        elif kind == "heading":
            level = min(6, max(1, block["level"]))
            parts.append("<h%d>%s</h%d>" % (level, md_inline(block["text"]), level))
        elif kind == "hr":
            parts.append("<hr />")
        elif kind == "table":
            rows = block["rows"]
            head, *body = rows
            parts.append(
                "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
                % ("".join("<th>%s</th>" % md_inline(cell) for cell in head),
                   "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % md_inline(cell)
                                                   for cell in row) for row in body))
            )
        elif kind == "quote":
            parts.append("<blockquote>%s</blockquote>" % md_inline(block["text"]))
        elif kind in ("ul", "ol"):
            tag = "ol" if kind == "ol" else "ul"
            parts.append("<%s>%s</%s>"
                         % (tag, "".join("<li>%s</li>" % md_inline(item)
                                         for item in block["items"]), tag))
        else:
            parts.append("<p>%s</p>" % md_inline(block["text"]).replace("\n", "<br />"))
    return "\n".join(parts)


def html_to_md(text):
    """html -> markdown. Block tags become markdown, inline tags become md
    markers, everything else keeps its text. <script>/<style> bodies are dropped
    outright (mammoth's html->markdown, stdlib HTMLParser instead of a DOM)."""
    parts = []
    stack = []
    list_counters = []
    link_stack = []

    def emit(tag, attrs, closing):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            if not closing:
                parts.append("#" * int(tag[1]) + " ")
            parts.append("\n\n")
        elif tag == "p":
            parts.append("\n\n")
        elif tag == "br" and not closing:
            parts.append("\n")
        elif tag == "hr" and not closing:
            parts.append("\n---\n")
        elif tag == "table":
            if closing:
                parts.append("\n")
        elif tag == "tr":
            parts.append("\n" if closing else "\n")
        elif tag in ("td", "th"):
            parts.append(" | ")
        elif tag == "thead" or tag == "tbody":
            return
        elif tag in ("ul", "ol"):
            if not closing:
                list_counters.append(0 if tag == "ul" else 1)
            else:
                if list_counters:
                    list_counters.pop()
                parts.append("\n")
        elif tag == "li":
            if not closing:
                depth = max(0, len(list_counters) - 1)
                if list_counters and list_counters[-1]:
                    parts.append("%d. " % list_counters[-1])
                    list_counters[-1] += 1
                else:
                    parts.append("- ")
                parts.append("  " * depth)
            else:
                parts.append("\n")
        elif tag == "blockquote":
            if not closing:
                parts.append("> ")
            else:
                parts.append("\n")
        elif tag == "code":
            parts.append("`")
        elif tag == "pre":
            parts.append("\n```\n")
        elif tag in ("strong", "b"):
            parts.append("**")
        elif tag in ("em", "i"):
            parts.append("*")
        elif tag == "a" and not closing:
            href = dict(attrs).get("href") or ""
            keep = safe_url(href)
            link_stack.append(href if keep else "")
            if keep:
                parts.append("[")
        elif tag == "a" and closing:
            href = link_stack.pop() if link_stack else ""
            if href:
                parts.append("](%s)" % href)

    class _Walker(HTMLParser):
        def __init__(self):
            HTMLParser.__init__(self)
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in DROP_TAGS:
                self.skip += 1
                return
            if self.skip:
                return
            if tag == "img":
                src = dict(attrs).get("src") or ""
                alt = dict(attrs).get("alt") or ""
                if safe_src(src):
                    parts.append("![%s](%s)" % (alt, src))
                return
            emit(tag, attrs, False)

        def handle_startendtag(self, tag, attrs):
            if tag == "img":
                src = dict(attrs).get("src") or ""
                alt = dict(attrs).get("alt") or ""
                if safe_src(src):
                    parts.append("![%s](%s)" % (alt, src))
            elif not self.skip and tag not in DROP_TAGS:
                emit(tag, attrs, False)

        def handle_endtag(self, tag):
            if self.skip:
                self.skip -= 1
                return
            if tag in DROP_TAGS:
                return
            emit(tag, dict(), True)

        def handle_data(self, data):
            if self.skip:
                return
            parts.append(data)

        def handle_comment(self, data):
            return

        def handle_decl(self, data):
            return

        def unknown_decl(self, data):
            return

        def handle_pi(self, data):
            return

    walker = _Walker()
    walker.feed(text)
    markdown = _collapse("".join(parts))
    # a heading marker and its text are emitted separately, so re-join them.
    markdown = re.sub(r"(?m)^(#{1,6})[ \t]*\n+", r"\1 ", markdown)
    return _add_table_separators(markdown)


def _add_table_separators(markdown):
    """A pipe-table row gets a `---` separator under its header row, which is
    what makes the output re-readable as markdown."""
    lines = markdown.split("\n")
    out = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = re.sub(r"\|\s*\|", "|", stripped)
            stripped = stripped.strip("|").strip()
            stripped = "| " + " | ".join(_split_row(stripped)) + " |"
        out.append(stripped)
        if not stripped.startswith("|"):
            continue
        nxt = ""
        for follower in lines[index + 1:]:
            if follower.strip():
                nxt = follower.strip()
                break
        if nxt.startswith("|") and not _is_sep_row(nxt):
            out.append("| " + " | ".join("---" for _ in _split_row(stripped)) + " |")
    return "\n".join(out)


def preflight_docx(path):
    # A .docx is a zip of XML parts; reject zip bombs / entity bombs before
    # python-docx (lxml) ever parses the archive (same preflight shape as the
    # spreadsheet node's _preflight_xlsx — same OOXML container format).
    if not is_zipfile(path):
        fail("%s is not a valid .docx container (a .docx is a zip)" % path.name, CODE_NO_INPUT)
    try:
        with ZipFile(path) as zf:
            total = 0
            for info in zf.infolist():
                total += info.file_size
                if total > 50 * 1024 * 1024:
                    fail("%s unpacks to over 50 MB — refused (zip bomb)" % path.name, CODE_NO_INPUT)
                if info.file_size and info.compress_size and info.file_size > 200 * info.compress_size:
                    fail("%s has a %dx compression ratio — refused"
                         % (path.name, info.file_size // info.compress_size), CODE_NO_INPUT)
                if info.filename.lower().endswith(".xml"):
                    raw = zf.read(info.filename)
                    if b"<!ENTITY" in raw or b"<!DOCTYPE" in raw:
                        fail("%s declares XML entities — refused (entity-expansion risk)" % path.name, CODE_NO_INPUT)
    except BadZipFile as exc:
        fail("%s is not a readable .docx: %s" % (path.name, exc), CODE_NO_INPUT)


def docx_document(path):
    try:
        import docx
        from docx.opc.exceptions import PackageNotFoundError
    except ImportError:
        fail("%s is not a readable .docx: python-docx is not installed" % path.name, CODE_PROCESSING)
    preflight_docx(path)
    try:
        return docx.Document(str(path))
    except (OSError, ValueError, KeyError, BadZipFile, PackageNotFoundError) as exc:
        fail("%s is not a readable .docx: %s" % (path.name, exc), CODE_NO_INPUT)


def docx_paragraphs(path):
    """(style_name, text) per paragraph, in document order."""
    doc = docx_document(path)
    return [(p.style.name if p.style is not None else "Normal", p.text) for p in doc.paragraphs]


def docx_tables(path):
    """Rows of cell text per table, in document order."""
    doc = docx_document(path)
    return [[[cell.text for cell in row.cells] for row in table.rows] for table in doc.tables]


def _style(document, name, fallback=None):
    """A named style only if the template actually has it — never raise."""
    return name if name in {s.name for s in document.styles} else fallback


def docx_write(blocks):
    """Blocks (md_blocks shape) -> a real .docx at /data/output/document.docx.
    python-docx's own XML writer, no shell, no templates."""
    import docx
    document = docx.Document()
    for block in blocks:
        kind = block["kind"]
        if kind == "heading":
            document.add_heading(block["text"], level=min(4, max(1, block["level"])))
        elif kind == "code":
            document.add_paragraph(block["text"], style=_style(document, "Code"))
        elif kind == "quote":
            document.add_paragraph(block["text"], style=_style(document, "Quote"))
        elif kind == "hr":
            document.add_paragraph()
        elif kind == "table":
            rows = block["rows"]
            width = max(len(r) for r in rows)
            table = document.add_table(rows=len(rows), cols=width)
            for r, row in enumerate(rows):
                for c in range(width):
                    table.cell(r, c).text = row[c] if c < len(row) else ""
        elif kind in ("ul", "ol"):
            name = "List Bullet" if kind == "ul" else "List Number"
            style = _style(document, name)
            for item in block["items"]:
                document.add_paragraph(item, style=style)
        else:
            document.add_paragraph(block["text"])
    document.save(str(OUT / "document.docx"))


def html_to_pdf(document, base=None):
    """weasyprint render, 100% offline: the url_implementation hook refuses every
    URL except an inline data: image, so no http/file fetch is ever attempted.
    Older weasyprint builds take the hook on the CSS instead — patched there."""
    import weasyprint
    sheet = weasyprint.CSS(string=PDF_CSS)
    try:
        handle = weasyprint.HTML(string=document, base_url=base,
                                 url_implementation=_refuse_url)
    except TypeError:
        weasyprint.url.get_resource = _refuse_url
        handle = weasyprint.HTML(string=document, base_url=base)
    handle.write_pdf(str(OUT / "document.pdf"), stylesheets=[sheet])


def _refuse_url(url, encoding=None, accept_internet=True, media_type=None,
                language=None, locale=None):
    """weasyprint's url_implementation hook: only an inline data: URI is served
    (decoded here, no I/O); every other URL — http(s), file:, absolute paths —
    raises, so the render cannot reach the network or the host filesystem."""
    scheme = urlsplit(url.strip()).scheme.lower()
    if scheme == "data":
        return _decode_data_uri(url)
    raise URLError("doc-converters refuses every URL except data: (%r)" % url)


def _decode_data_uri(url):
    """data: image -> raw bytes, stdlib only (no fetch, no file read)."""
    from urllib.parse import unquote
    import base64
    spec, _, payload = url[5:].partition(",")
    if spec.lower().endswith("base64"):
        try:
            return base64.b64decode(payload)
        except ValueError:
            return b""
    return unquote(payload).encode("utf-8", "replace")



class URLError(Exception):
    pass


PDF_CSS = (
    "@page { size: A4; margin: 20mm 18mm; }\n"
    "body { font-family: serif; font-size: 10.5pt; line-height: 1.45; }\n"
    "h1 { font-size: 18pt; } h2 { font-size: 15pt; } h3 { font-size: 13pt; }\n"
    "pre { font-family: monospace; font-size: 9pt; white-space: pre-wrap; }\n"
    "blockquote { margin-left: 8mm; font-style: italic; }\n"
    "table { border-collapse: collapse; } th, td { padding: 1.5mm; }\n"
)


def _pick(paths, suffixes):
    """First staged input matching one of `suffixes`, else the first input."""
    for path in paths:
        if path.suffix.lower() in suffixes:
            return path
    if not paths:
        fail("no input file found in /data/input", CODE_NO_INPUT)
    return paths[0]


def op_markdown_to_html(paths, cfg):
    source = _pick(paths, {".md", ".markdown", ".txt", ".html"})
    body = md_to_html(read_text(source))
    if flag(cfg, "sanitize", True):
        body = sanitize_html(body)
    write_text("document.html", body)


def op_html_to_markdown(paths, cfg):
    source = _pick(paths, {".html", ".htm", ".md", ".txt"})
    write_text("document.md", html_to_md(read_text(source)))


def op_html_to_pdf(paths, cfg):
    source = _pick(paths, {".html", ".htm", ".md", ".txt"})
    raw = read_text(source)
    if source.suffix.lower() in (".md", ".txt"):
        raw = md_to_html(raw)
    # <img> is dropped for this op: weasyprint would otherwise try to fetch it.
    body = sanitize_html(raw, keep_img=False)
    document = (
        "<!DOCTYPE html><html><head><meta charset='utf-8' /></head><body>%s</body></html>" % body
    )
    try:
        html_to_pdf(document)
    except Exception as exc:  # weasyprint raises a wide family of errors
        fail("weasyprint could not render %s: %s" % (source.name, exc), CODE_PROCESSING)


def op_docx_to_markdown(paths, cfg):
    source = _pick(paths, {".docx"})
    lines = []
    for style, text in docx_paragraphs(source):
        if not text.strip():
            continue
        name = (style or "").lower()
        if name.startswith("heading") or name.startswith("title"):
            level = 1 if name == "title" else (int(name[-1]) if name[-1].isdigit() else 1)
            lines.append("#" * min(6, max(1, level)) + " " + text)
        elif name.startswith("quote"):
            lines.append("> " + text)
        elif name.startswith("list bullet"):
            lines.append("- " + text)
        elif name.startswith("list number"):
            lines.append("1. " + text)
        elif name == "code":
            lines.append("```\n%s\n```" % text)
        else:
            lines.append(text)
    if flag(cfg, "include_tables", True):
        for table in docx_tables(source):
            header, *rest = table
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join("---" for _ in header) + " |")
            for row in rest:
                lines.append("| " + " | ".join(row) + " |")
    write_text("document.md", _collapse("\n\n".join(lines)) + "\n")


def op_docx_to_text(paths, cfg):
    source = _pick(paths, {".docx"})
    parts = [text for _, text in docx_paragraphs(source) if text.strip()]
    for table in docx_tables(source):
        for row in table:
            parts.append("\t".join(row))
    write_text("document.txt", "\n".join(parts) + "\n")


def op_text_to_docx(paths, cfg):
    source = _pick(paths, {".txt", ".text", ".md", ".docx"})
    text = read_text(source)
    if flag(cfg, "wrap_paragraphs", True):
        blocks = [{"kind": "para", "text": para.strip()} for para in text.split("\n\n") if para.strip()]
    else:
        blocks = [{"kind": "para", "text": line} for line in text.split("\n") if line.strip()]
    docx_write(blocks)


def op_markdown_to_docx(paths, cfg):
    source = _pick(paths, {".md", ".markdown", ".txt"})
    docx_write(md_blocks(read_text(source)))


OPS = {
    "markdown_to_html": op_markdown_to_html,
    "html_to_markdown": op_html_to_markdown,
    "html_to_pdf": op_html_to_pdf,
    "docx_to_markdown": op_docx_to_markdown,
    "docx_to_text": op_docx_to_text,
    "text_to_docx": op_text_to_docx,
    "markdown_to_docx": op_markdown_to_docx,
}


def main():
    try:
        op = os.getenv("DOC_CONVERT_OP", "markdown_to_html").strip().lower()
        if op not in OPS:
            fail("unknown DOC_CONVERT_OP %r — expected one of %s" % (op, sorted(OPS)), CODE_BAD_PARAM)
        if not IN.is_dir():
            fail("no input directory at /data/input", CODE_NO_INPUT)
        paths = [check_size(p) for p in inputs() if p.name != "params.json"]
        if not paths:
            fail("no input file found in /data/input", CODE_NO_INPUT)
        log("=== doc-converters: %s (%d file) ===" % (op, len(paths)))
        OPS[op](paths, params())
    except SystemExit:  # fail() already printed the contract line
        raise
    except Exception as exc:  # vet step 5: never leak a traceback to stdout
        fail("%s: %s" % (type(exc).__name__, exc), CODE_INTERNAL)

main()
PYEOF

WORKDIR /data
ENTRYPOINT ["python3", "/app/doc_convert.py"]

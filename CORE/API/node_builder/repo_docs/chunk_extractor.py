"""Format-aware chunk extractor (wizard-rebuild §3a).

`extract_chunks(filename, content) -> list[DocChunk]` is the single
public entry. Dispatches on filename to the right splitter:

  - Markdown (`*.md`, `*.rst`, README* without extension):
    split on h2/h3 boundaries. Preamble before the first heading is
    its own chunk with heading=None.
  - Makefile: one chunk per target. Top-level variables / .PHONY
    lines form a preamble chunk.
  - docker-compose (compose.y*ml, docker-compose.y*ml): one chunk
    per service. Top-level fields (version, networks, volumes)
    form a preamble chunk.
  - `.env.example` / `env.example` / `.env.template`: one chunk
    per non-comment line. Trailing comment lines BEFORE the var
    line attach as the heading.
  - Anything else (plain text fallback): 50-line chunks.

Properties:
  - Deterministic: same content → same chunks.
  - Line numbers are 1-indexed and inclusive — they map directly
    back to file lines (deep-link friendly).
  - Empty content returns []. No exceptions raised on malformed
    input — chunker is best-effort.
  - No LLM, no network, no I/O. Pure parsing.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .types import DocChunk

logger = logging.getLogger(__name__)


# 50-line chunks for plain-text fallback. Big enough that each chunk
# is self-contained; small enough that a single intent tag is
# usually defensible.
_PLAIN_TEXT_CHUNK_LINES = 50


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_chunks(filename: str, content: str) -> list[DocChunk]:
    """Split a single file into typed `DocChunk`s.

    Args:
      filename: the file's name as it appears in the repo (e.g.
        `README.md`, `docker-compose.yml`, `docs/install.md`).
        Used for format dispatch and stored on each chunk.
      content: the raw file content. UTF-8 already decoded.

    Returns:
      list of `DocChunk`. Empty list if content is empty or
      whitespace-only.
    """
    if not content or not content.strip():
        return []

    fmt = _detect_format(filename)
    if fmt == "markdown":
        return _split_markdown(filename, content)
    if fmt == "makefile":
        return _split_makefile(filename, content)
    if fmt == "compose":
        return _split_compose(filename, content)
    if fmt == "envfile":
        return _split_envfile(filename, content)
    return _split_plain(filename, content)


# ---------------------------------------------------------------------------
# Format dispatch
# ---------------------------------------------------------------------------

# Filename → format. Order matters: more specific matches first.
def _detect_format(filename: str) -> str:
    """Return one of 'markdown', 'makefile', 'compose', 'envfile',
    'plain'. Case-insensitive on the suffix; basename-only match
    (so `docs/install.md` still dispatches to markdown)."""
    name = (filename or "").rsplit("/", 1)[-1]
    lower = name.lower()

    if lower in ("makefile", "gnumakefile"):
        return "makefile"
    if lower in (
        "docker-compose.yml", "docker-compose.yaml",
        "compose.yml", "compose.yaml",
    ):
        return "compose"
    if lower in (".env.example", "env.example", ".env.template"):
        return "envfile"
    if lower.endswith((".md", ".markdown", ".rst")):
        return "markdown"
    # README/INSTALL/USAGE/CONTRIBUTING without extension → treat
    # as markdown (they're conventionally markdown-formatted).
    stem = lower.rsplit(".", 1)[0]
    if stem in (
        "readme", "install", "usage", "howto", "contributing",
        "changelog", "authors",
    ):
        return "markdown"
    return "plain"


# ---------------------------------------------------------------------------
# Markdown — split on h2/h3 boundaries
# ---------------------------------------------------------------------------

# `## Heading` or `### Heading` at column 0. Setext-style `===` /
# `---` headings are rarer in repo docs and not worth handling in v1.
_MD_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*#*\s*$")


def _split_markdown(filename: str, content: str) -> list[DocChunk]:
    """Split markdown on h2/h3 boundaries.

    Preamble (everything before the first h2/h3) is its own chunk
    with heading=None. Each subsequent chunk starts at its heading
    line and runs until the next h2/h3 (or EOF). h1 is treated as
    plain content — most repo READMEs have one h1 (the project
    name) as the document title.
    """
    lines = content.splitlines()
    chunks: list[DocChunk] = []

    # Find heading line indices.
    heading_indices: list[int] = []
    for i, line in enumerate(lines):
        if _MD_HEADING_RE.match(line):
            heading_indices.append(i)

    if not heading_indices:
        # No h2/h3 — entire content is one chunk with no heading.
        return [_make_chunk(filename, 1, len(lines), None, lines)]

    # Preamble chunk if there's content before the first heading.
    first = heading_indices[0]
    if first > 0:
        preamble = lines[:first]
        # Skip emitting if the preamble is just blank lines.
        if any(l.strip() for l in preamble):
            chunks.append(_make_chunk(
                filename, 1, first, None, preamble,
            ))

    # One chunk per heading section.
    boundaries = heading_indices + [len(lines)]
    for idx, start in enumerate(heading_indices):
        end = boundaries[idx + 1] - 1   # inclusive
        heading_match = _MD_HEADING_RE.match(lines[start])
        heading_text = heading_match.group(2) if heading_match else None
        section_lines = lines[start:end + 1]
        chunks.append(_make_chunk(
            filename, start + 1, end + 1, heading_text, section_lines,
        ))

    return chunks


# ---------------------------------------------------------------------------
# Makefile — split per target
# ---------------------------------------------------------------------------

# A target line: `target:` or `target: prereqs` at column 0, alphanumeric
# + `_`/`-`/`.`/`/`. Excludes assignment lines (`VAR := value`) and
# `.PHONY:` declarations (which we treat as preamble metadata).
_MAKE_TARGET_RE = re.compile(
    r"^(?P<target>[A-Za-z0-9_./-]+)\s*:\s*(?!=)"
)


def _split_makefile(filename: str, content: str) -> list[DocChunk]:
    """Split a Makefile per target.

    Top-level non-target lines (variable assignments, `.PHONY`,
    `include`, comments) form a preamble chunk. Each target's lines
    (the `target:` line plus subsequent TAB-indented recipe lines
    and trailing blanks until the next target) form one chunk.
    """
    lines = content.splitlines()
    chunks: list[DocChunk] = []

    # Find target line indices. Skip lines that look like targets
    # but are actually assignments (e.g. `.PHONY: foo` IS a target
    # of `.PHONY` which is metadata, so exclude).
    target_indices: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _MAKE_TARGET_RE.match(line)
        if not m:
            continue
        target = m.group("target")
        if target.startswith(".") and target != ".":
            continue   # .PHONY, .SUFFIXES, etc.
        target_indices.append((i, target))

    if not target_indices:
        # No targets — entire content is one chunk.
        return [_make_chunk(filename, 1, len(lines), None, lines)]

    # Preamble: everything before the first target line.
    first = target_indices[0][0]
    if first > 0:
        preamble = lines[:first]
        if any(l.strip() for l in preamble):
            chunks.append(_make_chunk(
                filename, 1, first, None, preamble,
            ))

    # One chunk per target.
    boundaries = [i for i, _ in target_indices] + [len(lines)]
    for idx, (start, target) in enumerate(target_indices):
        end = boundaries[idx + 1] - 1
        section_lines = lines[start:end + 1]
        chunks.append(_make_chunk(
            filename, start + 1, end + 1, target, section_lines,
        ))

    return chunks


# ---------------------------------------------------------------------------
# docker-compose — split per service block
# ---------------------------------------------------------------------------

def _split_compose(filename: str, content: str) -> list[DocChunk]:
    """Split a docker-compose file per service.

    Uses regex line-pattern recognition rather than a full YAML
    parse — we want line numbers from the original file, and PyYAML
    doesn't preserve them. The pattern looks for:

      services:
        <service-name>:           # 2-space indent
          image: ...              # 4-space indent
          ...

    Top-level keys (version, networks, volumes) and the `services:`
    line itself form a preamble chunk.
    """
    lines = content.splitlines()
    chunks: list[DocChunk] = []

    # Locate the `services:` block and any service entries inside it.
    services_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if re.match(r"^services\s*:\s*(#.*)?$", line):
            services_idx = i
            break

    if services_idx is None:
        # Not a recognizable compose file. Fall back to plain.
        return _split_plain(filename, content)

    # Find service name lines: indented under `services:`. We accept
    # 2- or 4-space indentation (compose v1 used 2, modern uses
    # whatever, but consistency within a file is the norm).
    service_indices: list[tuple[int, str]] = []
    # Detect the indent of the first service entry to pin the rest.
    service_indent: Optional[int] = None
    for i in range(services_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # End of services block: a line with indent <= services:
        indent = len(line) - len(line.lstrip())
        # `services:` is at indent 0, so any line back at indent 0
        # ends the block.
        if indent == 0:
            break
        if service_indent is None:
            service_indent = indent
        if indent == service_indent:
            # Match a service-name line: `<name>:` (possibly with comment).
            m = re.match(r"^\s+([A-Za-z0-9_.-]+)\s*:\s*(#.*)?$", line)
            if m:
                service_indices.append((i, m.group(1)))

    if not service_indices:
        # `services:` present but empty (or unparseable). Plain.
        return _split_plain(filename, content)

    # Preamble: everything from line 1 up to and including `services:`.
    preamble_end = services_idx
    if preamble_end > 0:
        preamble = lines[:preamble_end + 1]
        if any(l.strip() for l in preamble):
            chunks.append(_make_chunk(
                filename, 1, preamble_end + 1, "services", preamble,
            ))

    # One chunk per service.
    boundaries = [i for i, _ in service_indices] + [len(lines)]
    for idx, (start, service_name) in enumerate(service_indices):
        end = boundaries[idx + 1] - 1
        section_lines = lines[start:end + 1]
        chunks.append(_make_chunk(
            filename, start + 1, end + 1, service_name, section_lines,
        ))

    return chunks


# ---------------------------------------------------------------------------
# .env.example — one chunk per env var line
# ---------------------------------------------------------------------------

# Trailing comment lines directly above a non-comment line are
# treated as that line's documentation. Blank lines reset the
# comment-buffer (so a comment separated from the next var by a
# blank doesn't get attached).
_ENV_VAR_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _split_envfile(filename: str, content: str) -> list[DocChunk]:
    """One chunk per non-comment / non-blank line. Adjacent
    preceding comment lines are folded into the chunk's content
    (the LLM tagger sees `# This sets the foo \\n FOO=bar` as one
    chunk with intent "env")."""
    lines = content.splitlines()
    chunks: list[DocChunk] = []

    pending_comments: list[tuple[int, str]] = []  # (line_idx, line)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            pending_comments = []
            continue
        if stripped.startswith("#"):
            pending_comments.append((i, line))
            continue
        m = _ENV_VAR_RE.match(line)
        if not m:
            # Skip random non-var lines (legal in some env files).
            pending_comments = []
            continue

        var_name = m.group(1)
        # Chunk spans from the first pending comment (if any)
        # through this var line.
        if pending_comments:
            start_line = pending_comments[0][0] + 1
            section_lines = [l for _, l in pending_comments] + [line]
        else:
            start_line = i + 1
            section_lines = [line]

        chunks.append(_make_chunk(
            filename, start_line, i + 1, var_name, section_lines,
        ))
        pending_comments = []

    return chunks


# ---------------------------------------------------------------------------
# Plain text — 50-line chunks
# ---------------------------------------------------------------------------

def _split_plain(filename: str, content: str) -> list[DocChunk]:
    """Fallback chunker: contiguous 50-line slices. No heading."""
    lines = content.splitlines()
    chunks: list[DocChunk] = []
    n = len(lines)
    i = 0
    while i < n:
        end = min(i + _PLAIN_TEXT_CHUNK_LINES, n)
        section_lines = lines[i:end]
        # Skip emitting chunks that are entirely blank.
        if any(l.strip() for l in section_lines):
            chunks.append(_make_chunk(
                filename, i + 1, end, None, section_lines,
            ))
        i = end
    return chunks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(
    source_file: str,
    start_line: int,
    end_line: int,
    heading: Optional[str],
    lines: list[str],
) -> DocChunk:
    """Construct a DocChunk with the lines joined back into content.

    Joined with `\\n`; no trailing newline (so chunks round-trip
    through content.splitlines() consistently). Intent labels left
    empty — the §3b tagger fills those in.
    """
    return DocChunk(
        source_file=source_file,
        start_line=start_line,
        end_line=end_line,
        heading=heading,
        content="\n".join(lines),
        intents=[],
    )

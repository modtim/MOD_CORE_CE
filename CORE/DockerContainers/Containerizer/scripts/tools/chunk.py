"""Chunk source files for embedding.

AST-aware when the language has a stdlib parser (Python); line-based
otherwise. Each chunk carries enough metadata that a Qdrant query can
answer "show me the torch.cuda-using functions in the `inference/`
directory" without fetching the whole codebase.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


# Defaults tuned for the embed model's 512-token window (snowflake-
# arctic-embed-s; was bge-small before the aligned-provider swap),
# with headroom for metadata. Languages with long function bodies
# (Java) may overflow; the chunker falls back to a line-based split
# above MAX_LINES.
TARGET_LINES = 80
MAX_LINES = 200
TARGET_CHARS = 3200     # ~800 tokens at 4 char/token average
MIN_LINES = 4           # trivia too small to be worth indexing


@dataclass
class Chunk:
    id: str                    # deterministic hash — used as Qdrant point id
    source_path: str           # relative to repo root
    language: str
    role: str                  # source / test / config / doc
    symbol: Optional[str] = None   # "ClassName.method_name" when AST-extracted
    kind: str = "block"        # function | class | module | block
    start_line: int = 1
    end_line: int = 1
    text: str = ""
    framework_hints: list[str] = field(default_factory=list)   # torch, flask, …
    uses_gpu: bool = False


def chunk_file(
    repo_root: str,
    rel_path: str,
    *,
    language: str,
    role: str = "source",
    framework_hints: Optional[list[str]] = None,
    uses_gpu: bool = False,
) -> list[Chunk]:
    """Return all chunks for a single file. Language-aware where possible."""
    try:
        source = (Path(repo_root) / rel_path).read_text(errors="replace")
    except OSError:
        return []
    if not source.strip():
        return []

    if language == "Python":
        chunks = _chunk_python(source, rel_path)
    else:
        chunks = _chunk_generic(source, rel_path)

    for c in chunks:
        c.source_path = rel_path
        c.language = language
        c.role = role
        c.framework_hints = list(framework_hints or [])
        c.uses_gpu = uses_gpu
        c.id = _chunk_id(rel_path, c.start_line, c.end_line, c.text)
    return chunks


def chunk_many(
    repo_root: str,
    files: Iterable[tuple[str, str, str, list[str], bool]],
) -> list[Chunk]:
    """Batch: each item is (rel_path, language, role, framework_hints, uses_gpu)."""
    out: list[Chunk] = []
    for rel, lang, role, hints, gpu in files:
        out.extend(chunk_file(
            repo_root, rel, language=lang, role=role,
            framework_hints=hints, uses_gpu=gpu,
        ))
    return out


# ---------------------------------------------------------------------------
# Python — AST-aware: one chunk per top-level function/class; module-level
# body becomes a single "module" chunk.
# ---------------------------------------------------------------------------

def _chunk_python(source: str, rel_path: str) -> list[Chunk]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Syntax broken? Fall back to line chunking so we still index SOMETHING.
        return _chunk_generic(source, rel_path)

    lines = source.splitlines()
    chunks: list[Chunk] = []
    module_lines: list[int] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", None) or (start + 1)
            if end - start + 1 >= MIN_LINES:
                text = "\n".join(lines[start - 1:end])
                symbol = node.name
                if isinstance(node, ast.ClassDef):
                    kind = "class"
                else:
                    kind = "function"
                    # If the function lives inside a class, tree would
                    # have nested it — we're only walking top-level here.
                    # Good enough.
                chunks.extend(_split_if_huge(Chunk(
                    id="",   # filled later
                    source_path=rel_path, language="Python", role="source",
                    symbol=symbol, kind=kind, start_line=start, end_line=end,
                    text=text,
                )))
            else:
                # Tiny function — roll into module chunk.
                module_lines.extend(range(start, end + 1))
        else:
            start = getattr(node, "lineno", 1)
            end = getattr(node, "end_lineno", None) or start
            module_lines.extend(range(start, end + 1))

    if module_lines:
        lo, hi = min(module_lines), max(module_lines)
        text = "\n".join(lines[lo - 1:hi])
        if text.strip():
            chunks.append(Chunk(
                id="", source_path=rel_path, language="Python", role="source",
                symbol=None, kind="module", start_line=lo, end_line=hi, text=text,
            ))
    return chunks


def _split_if_huge(chunk: Chunk) -> list[Chunk]:
    """If an AST chunk exceeds MAX_LINES, subdivide by lines."""
    span = chunk.end_line - chunk.start_line + 1
    if span <= MAX_LINES and len(chunk.text) <= TARGET_CHARS * 2:
        return [chunk]
    lines = chunk.text.splitlines()
    parts: list[Chunk] = []
    for i in range(0, len(lines), TARGET_LINES):
        sub_text = "\n".join(lines[i:i + TARGET_LINES])
        if not sub_text.strip():
            continue
        parts.append(Chunk(
            id="", source_path=chunk.source_path, language=chunk.language,
            role=chunk.role, symbol=f"{chunk.symbol or ''}[{i + 1}]",
            kind=chunk.kind, start_line=chunk.start_line + i,
            end_line=min(chunk.start_line + i + TARGET_LINES - 1, chunk.end_line),
            text=sub_text,
        ))
    return parts or [chunk]


# ---------------------------------------------------------------------------
# Generic — sliding window over non-blank lines.
# ---------------------------------------------------------------------------

def _chunk_generic(source: str, rel_path: str) -> list[Chunk]:
    lines = source.splitlines()
    chunks: list[Chunk] = []
    i = 0
    while i < len(lines):
        j = min(i + TARGET_LINES, len(lines))
        text = "\n".join(lines[i:j])
        if len(text) > TARGET_CHARS * 2:
            j = i + max(TARGET_LINES // 2, MIN_LINES)
            text = "\n".join(lines[i:j])
        if text.strip() and j - i >= MIN_LINES:
            chunks.append(Chunk(
                id="", source_path=rel_path, language="", role="",
                symbol=None, kind="block", start_line=i + 1, end_line=j,
                text=text,
            ))
        if j == i:
            i += 1
        else:
            i = j
    return chunks


# ---------------------------------------------------------------------------
# Id derivation — deterministic, so re-ingests overwrite old points rather
# than accumulating duplicates in Qdrant.
# ---------------------------------------------------------------------------

def _chunk_id(path: str, start: int, end: int, text: str) -> str:
    h = hashlib.sha1(f"{path}\n{start}-{end}\n{text}".encode("utf-8")).hexdigest()
    return h

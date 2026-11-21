"""Repo-docs pipeline data shapes (wizard-rebuild §3a-c).

  RawChunk          input to the chunker — just (source_file, content)
  DocChunk          post-split — heading + body + line range
  ChunkRef          pointer to a DocChunk by id (BuildPlan provenance)
  RepoDocsExtract   top-level cache object — all chunks for a (repo, ref)

These are plain dataclasses; no Pydantic. The §3c cache schema does
the boundary validation when persisting / loading from Postgres.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(slots=True)
class RawChunk:
    """One file fetched from the repo, pre-split.

    The chunk_extractor's input. Source file name drives format
    dispatch: `Makefile` → makefile splitter, `*.md` → markdown
    splitter, `docker-compose.y*ml` → compose splitter, etc.
    """
    source_file: str
    content: str


@dataclass(slots=True)
class DocChunk:
    """One chunk after format-aware splitting.

    Stable identity: `(source_file, start_line, end_line)` is a
    deterministic primary key — the same input bytes produce the same
    chunks every time. The §3c cache uses this for change detection.

    Fields:
      source_file: the file the chunk came from (e.g. `README.md`).
      start_line / end_line: 1-indexed, inclusive line numbers from
        the original file. Used by the View Build Plan UI to deep-
        link a plan field back to its source line.
      heading: for markdown chunks, the section heading text minus
        the `#` prefixes (or `None` for the preamble before the
        first heading). Makefile chunks use the target name as the
        heading; compose chunks use the service name; env-file
        chunks use the variable name.
      content: the chunk body — the original lines from the file,
        joined with `\\n`. Includes the heading line itself for
        markdown so an LLM sees the section in context.
      intents: labels assigned by the §3b intent tagger — filled in
        AFTER chunking, so the extractor emits chunks with intents=[].
    """
    source_file: str
    start_line: int
    end_line: int
    heading: Optional[str]
    content: str
    intents: list[str] = field(default_factory=list)

    def chunk_id(self) -> str:
        """Stable string id — primary key in the §3c cache."""
        return f"{self.source_file}#L{self.start_line}-L{self.end_line}"


@dataclass(slots=True)
class ChunkRef:
    """Pointer to a DocChunk in the cache.

    Identical shape to build_plan.types.ChunkRef so the BuildPlan
    `extracted_from` list can hold these directly. Duplicated here
    so the repo_docs module doesn't reach into build_plan and
    create a circular import.

    `intents` is denormalized onto the ref so the View Plan UI
    doesn't need a second lookup just to render the label badge.
    """
    source_file: str
    start_line: int
    end_line: int
    intents: list[str] = field(default_factory=list)
    heading: Optional[str] = None


@dataclass(slots=True)
class RepoDocsExtract:
    """Cached extraction result for a (repo_url, ref) pair.

    Persisted to the `repo_docs_extract` table (§3c). TTL'd so
    stale extracts get re-fetched. `extractor_version` matches
    BuildPlan.EXTRACTOR_VERSION — when that bumps, cached extracts
    invalidate so the new shape gets re-emitted.
    """
    repo_url: str
    ref: str                      # branch / tag / commit sha
    chunks: list[DocChunk]
    fetched_at: datetime
    expires_at: datetime
    extractor_version: int
    tenant_id: Optional[int] = None
    # Files we tried to fetch but didn't find — surfaces in the
    # View Plan UI so users know we LOOKED but it wasn't there.
    files_missing: list[str] = field(default_factory=list)

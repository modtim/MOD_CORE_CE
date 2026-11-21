"""Stage 5 — parallel per-module LLM analysis.

One asyncio task per module. Each task builds a compact context (AST
signals + up to K retrieved chunks) and asks the LLM for a
JSON-shaped summary (see module_analysis_prompt.py). vLLM's continuous
batching packs multiple concurrent completions into the same GPU
forward pass, so this scales well even on dozens of modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

try:
    from tools import embed_index
    _RAG_AVAILABLE = True
except ImportError:   # pragma: no cover
    _RAG_AVAILABLE = False

from tools.llm_providers import LLMConfig, get_provider
from tools.module_analysis_prompt import SYSTEM_PROMPT


logger = logging.getLogger(__name__)


# Budget caps so a runaway module doesn't blow up prompt size on a
# huge repo. The prompt template is ~1.5K tokens; we give the RAG
# excerpts ~2K tokens and the AST signals the rest.
MAX_PROMPT_CHARS = 12_000           # ~3K tokens
MAX_RAG_CHUNKS = 4
MAX_PARALLEL = 8                    # cap even on big repos; respect vLLM --max-num-seqs
MODULE_TIMEOUT_SECONDS = 120


@dataclass
class ModuleInput:
    """What a single map call needs. Built by _build_module_inputs."""
    path: str
    loc: int
    primary_lang: str
    languages: dict[str, int]
    file_paths: list[str]
    framework_hints: list[str]
    uses_gpu: bool
    # AST excerpts aggregated from ast_extract / ast_polyglot records
    imports: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    env_reads: list[str] = field(default_factory=list)


@dataclass
class ModuleAnalysis:
    path: str
    raw: str                       # raw LLM text, useful for debugging
    parsed: Optional[dict] = None  # parsed JSON or None on parse failure
    error: Optional[str] = None


async def map_modules(
    scan_result: dict,
    llm: LLMConfig,
    *,
    collection: Optional[str] = None,
) -> list[ModuleAnalysis]:
    """Build one ModuleInput per module in scan_result, call the LLM in
    parallel, collect ModuleAnalysis records.

    `scan_result` is the dict returned by codebase_scan.scan_codebase.
    `collection` is the Qdrant collection name from the embed stage;
    pass None to skip RAG retrieval (AST signals only).
    """
    repo_map = scan_result.get("repo_map") or {}
    modules = repo_map.get("modules") or []
    inputs = _build_module_inputs(scan_result, modules)

    sem = asyncio.Semaphore(MAX_PARALLEL)
    provider = get_provider(llm)

    async def _one(mi: ModuleInput) -> ModuleAnalysis:
        async with sem:
            return await _analyse_module(provider, mi, collection)

    results = await asyncio.gather(*(_one(mi) for mi in inputs), return_exceptions=True)
    out: list[ModuleAnalysis] = []
    for mi, r in zip(inputs, results):
        if isinstance(r, Exception):
            out.append(ModuleAnalysis(path=mi.path, raw="", parsed=None,
                                      error=f"{type(r).__name__}: {r}"))
        else:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Input construction
# ---------------------------------------------------------------------------

def _build_module_inputs(
    scan_result: dict, modules: list[dict],
) -> list[ModuleInput]:
    """Flatten AST records by module path so each module call has the
    signals for its own files — not the whole repo."""
    py_ast = scan_result.get("python_ast") or []
    poly_ast = scan_result.get("polyglot_ast") or []

    # path → consolidated signals
    imports_by_path: dict[str, list[str]] = {}
    entry_by_path: dict[str, list[str]] = {}
    env_by_path: dict[str, list[str]] = {}
    frameworks_by_path: dict[str, list[str]] = {}
    gpu_by_path: dict[str, bool] = {}

    for r in py_ast:
        p = r.get("path") or ""
        imports_by_path[p] = list(r.get("imports") or []) + [
            f"{m}:{n}" for m, n in (r.get("from_imports") or [])
        ]
        entry_by_path[p] = list(r.get("entry_points") or [])
        env_by_path[p] = [e["name"] for e in (r.get("env_reads") or []) if isinstance(e, dict)]
        frameworks_by_path[p] = (
            list(r.get("web_frameworks") or []) +
            list(r.get("queue_frameworks") or []) +
            list(r.get("ml_frameworks") or [])
        )
        gpu_by_path[p] = bool(r.get("uses_gpu"))

    for r in poly_ast:
        p = r.get("path") or ""
        imports_by_path[p] = list(r.get("imports") or [])
        entry_by_path[p] = list(r.get("entry_points") or [])
        env_by_path[p] = list(r.get("env_reads") or [])
        frameworks_by_path[p] = (
            list(r.get("web_frameworks") or []) +
            list(r.get("queue_frameworks") or []) +
            list(r.get("ml_frameworks") or [])
        )

    # path prefix lookup: the module's dir prefixes a file's path.
    out: list[ModuleInput] = []
    for m in modules:
        mpath = m.get("path") or ""
        prefix = mpath + "/" if mpath else ""
        files_in_mod = [
            p for p in imports_by_path
            if (not prefix and "/" not in p) or (prefix and p.startswith(prefix))
        ]
        imports, entries, envs, frameworks = [], [], [], []
        gpu = False
        for p in files_in_mod:
            imports.extend(imports_by_path.get(p) or [])
            entries.extend(entry_by_path.get(p) or [])
            envs.extend(env_by_path.get(p) or [])
            frameworks.extend(frameworks_by_path.get(p) or [])
            gpu = gpu or gpu_by_path.get(p, False)

        langs = m.get("languages") or {}
        primary = max(langs.items(), key=lambda kv: kv[1])[0] if langs else "other"
        out.append(ModuleInput(
            path=mpath,
            loc=sum(langs.values()) if langs else 0,
            primary_lang=primary,
            languages=langs,
            file_paths=files_in_mod,
            framework_hints=_dedupe_keep_order(frameworks),
            uses_gpu=gpu,
            imports=_dedupe_keep_order(imports)[:80],   # cap per-module
            entry_points=_dedupe_keep_order(entries),
            env_reads=_dedupe_keep_order(envs),
        ))
    return out


def _dedupe_keep_order(xs: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in xs:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out


# ---------------------------------------------------------------------------
# Per-module LLM call
# ---------------------------------------------------------------------------

async def _analyse_module(
    provider, mi: ModuleInput, collection: Optional[str],
) -> ModuleAnalysis:
    # RAG: pull the few most relevant chunks for THIS module.
    rag_excerpts = _retrieve_chunks(mi, collection)
    user_prompt = _render_user_prompt(mi, rag_excerpts)

    try:
        raw = await asyncio.wait_for(
            provider.generate(SYSTEM_PROMPT, user_prompt),
            timeout=MODULE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return ModuleAnalysis(path=mi.path, raw="",
                              error=f"LLM timeout after {MODULE_TIMEOUT_SECONDS}s")
    except Exception as e:
        return ModuleAnalysis(path=mi.path, raw="",
                              error=f"{type(e).__name__}: {e}")

    parsed = _parse_json(raw)
    return ModuleAnalysis(path=mi.path, raw=raw, parsed=parsed,
                          error=None if parsed else "failed to parse JSON response")


def _retrieve_chunks(mi: ModuleInput, collection: Optional[str]) -> list[dict]:
    """Get up to MAX_RAG_CHUNKS code excerpts relevant to this module."""
    if not collection or not _RAG_AVAILABLE:
        return []
    query_bits = [mi.path]
    query_bits.extend(mi.framework_hints[:3])
    query_bits.extend(mi.imports[:8])
    query = " ".join(str(x) for x in query_bits if x) or mi.path
    try:
        return embed_index.search(collection, query, top_k=MAX_RAG_CHUNKS)
    except Exception as e:
        logger.debug("RAG retrieve failed for %s: %s", mi.path, e)
        return []


def _render_user_prompt(mi: ModuleInput, rag: list[dict]) -> str:
    parts: list[str] = []
    parts.append(f"Module path: {mi.path or '(repository root)'}")
    parts.append(f"Primary language: {mi.primary_lang}  (LOC: {mi.loc})")
    if mi.languages:
        parts.append("Language breakdown: " + ", ".join(
            f"{k} {v}LOC" for k, v in sorted(mi.languages.items(), key=lambda kv: -kv[1])
        ))
    if mi.file_paths:
        visible = mi.file_paths[:30]
        extra = len(mi.file_paths) - len(visible)
        parts.append("Files:\n" + "\n".join(f"  - {p}" for p in visible) +
                     (f"\n  ... (+{extra} more)" if extra > 0 else ""))
    if mi.framework_hints:
        parts.append("AST framework hints: " + ", ".join(mi.framework_hints))
    if mi.imports:
        parts.append("AST imports (deduplicated): " +
                     ", ".join(mi.imports[:60]))
    if mi.entry_points:
        parts.append("AST entry-point markers: " + ", ".join(mi.entry_points))
    if mi.env_reads:
        parts.append("AST env-var reads: " + ", ".join(mi.env_reads[:30]))
    parts.append(f"GPU flagged by AST: {mi.uses_gpu}")

    if rag:
        parts.append("\nRepresentative code excerpts from this module:")
        for i, r in enumerate(rag, 1):
            snippet = (r.get("text") or "").strip()
            if not snippet:
                continue
            loc_hint = f"{r.get('path')}:{r.get('start_line')}-{r.get('end_line')}"
            parts.append(f"\n--- excerpt {i} ({loc_hint}) ---\n{snippet}")

    text = "\n".join(parts)
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS] + "\n\n[truncated]"
    return text


_JSON_OBJ = re.compile(r"\{[\s\S]*\}")


def _parse_json(raw: str) -> Optional[dict]:
    if not raw or not raw.strip():
        return None
    # Strip ```json fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        if first_nl >= 0:
            cleaned = cleaned[first_nl + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    # Try verbatim
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Last-ditch: largest {...} block
    m = _JSON_OBJ.search(cleaned)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None

"""Stage 6 — reduce all module analyses + manifests into ONE build profile.

Runs once per codebase. Input: the list of ModuleAnalysis records from
Stage 5 + the parsed manifests from Stage 2 + the existing Dockerfile
hints if any. Output: a single JSON "build profile" matching the shape
defined in reduce_prompt.py, which Stage 7's four artifact generators
all read from.

This is the single LLM call where MOD commits to "the codebase → the
one MOD node". Everything after this is just turning the build profile
into text files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from tools.llm_providers import LLMConfig, get_provider
from tools.reduce_prompt import SYSTEM_PROMPT


logger = logging.getLogger(__name__)


REDUCE_TIMEOUT_SECONDS = 300    # one big call, bigger ctx than map calls
MAX_PROMPT_CHARS = 50_000       # ~12K tokens — vLLM Production has the ctx


@dataclass
class BuildProfile:
    raw: str
    parsed: Optional[dict] = None
    error: Optional[str] = None


async def reduce(
    scan_result: dict,
    module_analyses: list,           # list[ModuleAnalysis] from map_modules
    llm: LLMConfig,
    *,
    user_hints: Optional[str] = None,
) -> BuildProfile:
    """Produce a single build profile from all the Stage-2/5 signals.

    `user_hints` is free-form text supplied by the wizard user
    (e.g. "this is an ML inference node that loads a BLIP model").
    When present, the reducer weighs it heavily against the AST-
    derived evidence.
    """
    user_prompt = _render_user_prompt(scan_result, module_analyses, user_hints)
    provider = get_provider(llm)

    try:
        raw = await asyncio.wait_for(
            provider.generate(SYSTEM_PROMPT, user_prompt),
            timeout=REDUCE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return BuildProfile(raw="", error=f"LLM timeout after {REDUCE_TIMEOUT_SECONDS}s")
    except Exception as e:
        return BuildProfile(raw="", error=f"{type(e).__name__}: {e}")

    parsed = _parse_json(raw)
    if parsed is None:
        return BuildProfile(raw=raw, error="failed to parse JSON response")
    return BuildProfile(raw=raw, parsed=parsed)


# ---------------------------------------------------------------------------
# User-prompt assembly
# ---------------------------------------------------------------------------

def _render_user_prompt(
    scan_result: dict, module_analyses: list, user_hints: Optional[str],
) -> str:
    parts: list[str] = []
    repo_map = scan_result.get("repo_map") or {}
    manifests = scan_result.get("manifests") or []

    # --- High-level context --------------------------------------
    parts.append("=== Codebase map ===")
    parts.append(
        f"Total: {repo_map.get('total_files', 0)} files, "
        f"{repo_map.get('total_source_loc', 0)} source LOC, "
        f"{len(repo_map.get('modules') or [])} modules."
    )
    parts.append(
        "Languages: " + ", ".join(
            f"{lang} ({len(paths)} files)"
            for lang, paths in sorted(
                (repo_map.get("source_files_by_lang") or {}).items(),
                key=lambda kv: -len(kv[1]),
            )
        )
    )

    # --- Module summaries -----------------------------------------
    parts.append("\n=== Per-module analysis ===")
    for ma in module_analyses:
        p = getattr(ma, "parsed", None)
        if p is None:
            parts.append(f"[{getattr(ma, 'path', '?')}] analysis failed: "
                         f"{getattr(ma, 'error', 'unknown')}")
            continue
        parts.append(
            f"[{p.get('path', '?')}] role={p.get('role')} entry={p.get('is_entry_point')} "
            f"gpu={p.get('gpu_required')} confidence={p.get('confidence')}"
        )
        parts.append(f"  purpose: {p.get('purpose', '?')}")
        if p.get("frameworks"):
            parts.append(f"  frameworks: {', '.join(p['frameworks'])}")
        if p.get("external_services"):
            parts.append(f"  external services: {', '.join(p['external_services'])}")
        if p.get("env_vars"):
            envs = [f"{e['name']}{'*' if e.get('required') else ''}"
                    for e in p["env_vars"] if isinstance(e, dict) and e.get("name")]
            if envs:
                parts.append(f"  env vars: {', '.join(envs)}  (* = required)")
        if p.get("egress_hosts"):
            parts.append(f"  egress: {', '.join(p['egress_hosts'])}")
        io = p.get("io_shape") or {}
        if io.get("description"):
            parts.append(f"  io: {io['description']}")

    # --- Manifests ------------------------------------------------
    if manifests:
        parts.append("\n=== Parsed manifests ===")
        for m in manifests:
            eco = m.get("ecosystem")
            path = m.get("path")
            deps = m.get("deps") or []
            extra = m.get("extra") or {}
            if m.get("parse_error"):
                parts.append(f"[{path}] ({eco}) parse error: {m['parse_error']}")
                continue
            header = f"[{path}] ({eco}) {len(deps)} dep(s)"
            if extra:
                hint_keys = ("python_requires", "node_version", "go_version",
                             "rust_edition", "from", "expose", "commands",
                             "services", "keys", "processes")
                extras = {k: extra[k] for k in hint_keys if k in extra and extra[k]}
                if extras:
                    header += "; " + ", ".join(f"{k}={extras[k]!r}" for k in extras)
            parts.append(header)
            # Include up to 40 deps per manifest
            for d in deps[:40]:
                name = d.get("name"); ver = d.get("version"); kind = d.get("kind")
                parts.append(f"  - {name}{'=='+ver if ver else ''} ({kind})")
            if len(deps) > 40:
                parts.append(f"  ... (+{len(deps) - 40} more)")

    # --- Existing Dockerfiles / compose (as hints, not reproduction) ---
    existing_dockerfiles = repo_map.get("existing_dockerfiles") or []
    existing_compose = repo_map.get("existing_compose_files") or []
    if existing_dockerfiles:
        parts.append("\n=== Existing Dockerfiles in the repo (use as hints only) ===")
        for p in existing_dockerfiles:
            parts.append(f"  - {p}")
    if existing_compose:
        parts.append("\n=== Existing docker-compose files (use as hints only; we still output one MOD node) ===")
        for p in existing_compose:
            parts.append(f"  - {p}")

    if user_hints:
        parts.append("\n=== User-supplied hints ===")
        parts.append(user_hints.strip())

    text = "\n".join(parts)
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS] + "\n\n[truncated]"
    return text


_JSON_OBJ = re.compile(r"\{[\s\S]*\}")


def _parse_json(raw: str) -> Optional[dict]:
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        if first_nl >= 0:
            cleaned = cleaned[first_nl + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJ.search(cleaned)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None

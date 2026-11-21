"""Polyglot AST — tree-sitter-backed import/entry-point extraction
for every language where we don't have a built-in stdlib parser.

Returns the same-shaped result as ast_extract (PythonFile) but under
a generic name (LangFile) so the downstream pipeline can handle both
uniformly. Imports + entry points + env reads + framework hints — the
specific set the Containerizer actually uses to build Dockerfiles.

Tree-sitter gives us a consistent grammar interface; `tree_sitter_languages`
ships prebuilt grammars for ~40 languages so we don't build from source.
Install in the Containerizer image:

    pip install tree-sitter==0.22.3 tree-sitter-languages==1.10.2

If the package isn't available (dev box without it installed yet),
all helpers degrade to "unsupported" responses rather than crashing.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

try:
    from tree_sitter_languages import get_language, get_parser
    import tree_sitter
    _TS_OK = True
except ImportError:   # pragma: no cover
    _TS_OK = False


@dataclass
class LangFile:
    path: str
    language: str                              # "JavaScript", "TypeScript", "Go", ...
    imports: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    env_reads: list[str] = field(default_factory=list)
    web_frameworks: list[str] = field(default_factory=list)
    queue_frameworks: list[str] = field(default_factory=list)
    ml_frameworks: list[str] = field(default_factory=list)
    parse_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Per-language tree-sitter query strings
#
# Queries use tree-sitter's S-expression syntax. Each query captures
# exactly the nodes we want and hands them back via `@import`, `@entry`,
# `@env` labels that the extraction fn reads.
# ---------------------------------------------------------------------------

_JS_QUERY = r"""
(import_statement source: (string (string_fragment) @import))
(call_expression
  function: (identifier) @fname
  arguments: (arguments (string (string_fragment) @import))
  (#eq? @fname "require"))
(call_expression
  function: (member_expression
    object: (identifier) @obj
    property: (property_identifier) @prop)
  (#eq? @obj "process")
  (#eq? @prop "env")) @env
"""

_TS_QUERY = _JS_QUERY   # same grammar family, same shape

_GO_QUERY = r"""
(import_spec (interpreted_string_literal) @import)
(function_declaration name: (identifier) @fname) @func
(call_expression
  function: (selector_expression
    operand: (identifier) @pkg
    field: (field_identifier) @fn)
  (#eq? @pkg "os")
  (#eq? @fn "Getenv")) @env
"""

_JAVA_QUERY = r"""
(import_declaration (scoped_identifier) @import)
(method_declaration
  (modifiers) @mods
  name: (identifier) @name
  (#eq? @name "main")) @entry
"""

_RUST_QUERY = r"""
(use_declaration argument: (_) @import)
(function_item name: (identifier) @name (#eq? @name "main")) @entry
"""

_RUBY_QUERY = r"""
(call method: (identifier) @m
  arguments: (argument_list (string) @import)
  (#match? @m "^(require|require_relative|load)$"))
"""

_SHELL_QUERY = r"""
(command name: (command_name (word) @cmd)) @call
"""


_QUERIES: dict[str, str] = {
    "JavaScript": _JS_QUERY, "TypeScript": _TS_QUERY,
    "Go": _GO_QUERY, "Java": _JAVA_QUERY,
    "Rust": _RUST_QUERY, "Ruby": _RUBY_QUERY, "Shell": _SHELL_QUERY,
}

# Names tree-sitter-languages uses (slightly different capitalisation).
_LANG_SLUG: dict[str, str] = {
    "JavaScript": "javascript", "TypeScript": "typescript",
    "Go": "go", "Java": "java",
    "Rust": "rust", "Ruby": "ruby", "Shell": "bash",
}

# Framework fingerprints (import path → canonical name).
_FRAMEWORK_HINTS: dict[str, dict[str, str]] = {
    "JavaScript": {
        "express": "Express", "fastify": "Fastify", "koa": "Koa",
        "next": "Next.js", "nestjs": "NestJS", "@nestjs/core": "NestJS",
        "bull": "BullMQ", "bullmq": "BullMQ", "agenda": "Agenda",
        "@tensorflow/tfjs": "TensorFlow.js",
    },
    "TypeScript": {
        "express": "Express", "fastify": "Fastify", "koa": "Koa",
        "next": "Next.js", "nestjs": "NestJS", "@nestjs/core": "NestJS",
        "bull": "BullMQ", "bullmq": "BullMQ", "agenda": "Agenda",
    },
    "Go": {
        "github.com/gin-gonic/gin": "Gin", "github.com/labstack/echo": "Echo",
        "github.com/go-chi/chi": "Chi", "net/http": "net/http",
        "github.com/hibiken/asynq": "Asynq",
    },
    "Java": {
        "org.springframework": "Spring", "javax.servlet": "Servlet",
        "jakarta.servlet": "Servlet",
    },
    "Rust": {
        "actix_web": "Actix", "rocket": "Rocket", "axum": "Axum",
        "warp": "Warp",
    },
    "Ruby": {
        "rails": "Rails", "sinatra": "Sinatra", "sidekiq": "Sidekiq",
    },
}


def extract(repo_root: str, rel_path: str, language: str) -> LangFile:
    """Parse one file; fall back gracefully when tree-sitter is unavailable."""
    result = LangFile(path=rel_path, language=language)

    if not _TS_OK:
        result.parse_error = (
            "tree_sitter_languages not installed — polyglot AST disabled"
        )
        # Fall through to regex fallback so we still return useful data.
        return _regex_fallback(repo_root, rel_path, language, result)

    slug = _LANG_SLUG.get(language)
    query_src = _QUERIES.get(language)
    if slug is None or query_src is None:
        result.parse_error = f"no tree-sitter support wired for {language}"
        return _regex_fallback(repo_root, rel_path, language, result)

    fpath = Path(repo_root) / rel_path
    try:
        source_bytes = fpath.read_bytes()
    except OSError as e:
        result.parse_error = f"read failed: {e}"
        return result

    try:
        parser = get_parser(slug)
        tree = parser.parse(source_bytes)
    except Exception as e:
        result.parse_error = f"tree-sitter parse error: {e}"
        return _regex_fallback(repo_root, rel_path, language, result)

    try:
        lang = get_language(slug)
        query = lang.query(query_src)
        captures = query.captures(tree.root_node)
    except Exception as e:
        result.parse_error = f"tree-sitter query error: {e}"
        return _regex_fallback(repo_root, rel_path, language, result)

    for node, name in captures:
        text = source_bytes[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace"
        ).strip().strip('"').strip("'")
        if name == "import" and text:
            result.imports.append(text)
        elif name == "entry":
            result.entry_points.append("main")
        elif name == "env":
            result.env_reads.append(text)

    _note_frameworks(result, language)
    return result


def extract_all(repo_root: str, paths_with_lang: list[tuple[str, str]]) -> list[LangFile]:
    return [extract(repo_root, p, lang) for p, lang in paths_with_lang]


def _note_frameworks(result: LangFile, language: str) -> None:
    hints = _FRAMEWORK_HINTS.get(language) or {}
    for imp in result.imports:
        for needle, canonical in hints.items():
            if imp == needle or imp.startswith(needle + "/") or imp.startswith(needle + "."):
                # Classify: we only track web/queue/ml buckets here. Server-side
                # frameworks go into web_frameworks; queues get their own bucket.
                if "queue" in canonical.lower() or canonical in {"BullMQ", "Agenda", "Sidekiq", "Asynq"}:
                    bucket = result.queue_frameworks
                elif canonical in {"TensorFlow.js"}:
                    bucket = result.ml_frameworks
                else:
                    bucket = result.web_frameworks
                if canonical not in bucket:
                    bucket.append(canonical)


# ---------------------------------------------------------------------------
# Regex fallback — used when tree-sitter isn't available. Covers imports
# at least, so manifest-less repos still get SOMETHING in the profile.
# ---------------------------------------------------------------------------

_FALLBACK_IMPORT_RE: dict[str, list[re.Pattern]] = {
    "JavaScript": [
        re.compile(r"""import\s+(?:[^'"]+\s+from\s+)?['"]([^'"]+)['"]"""),
        re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    ],
    "TypeScript": [
        re.compile(r"""import\s+(?:[^'"]+\s+from\s+)?['"]([^'"]+)['"]"""),
        re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    ],
    "Go": [re.compile(r'"([a-zA-Z0-9_./-]+)"\s*$', re.M)],
    "Java": [re.compile(r"^\s*import\s+([\w.]+)\s*;", re.M)],
    "Rust": [re.compile(r"^\s*use\s+([\w:]+)", re.M)],
    "Ruby": [re.compile(r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]""", re.M)],
    "Shell": [],
}


def _regex_fallback(
    repo_root: str, rel_path: str, language: str, result: LangFile,
) -> LangFile:
    patterns = _FALLBACK_IMPORT_RE.get(language) or []
    if not patterns:
        return result
    try:
        source = (Path(repo_root) / rel_path).read_text(errors="replace")
    except OSError:
        return result
    for pat in patterns:
        for m in pat.finditer(source):
            imp = m.group(1)
            if imp and imp not in result.imports:
                result.imports.append(imp)
    _note_frameworks(result, language)
    return result

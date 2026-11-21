"""Repo scanner — build a structured repository map in a single pass.

Outputs a RepoMap: every file classified by language and role, every
directory counted, every manifest/Dockerfile/compose file located but
not yet parsed (that's `manifest_parse.py`'s job). Deterministic —
no LLM involvement.

The map is the spine every downstream stage hangs off:
- ast_extract walks `RepoMap.source_files_by_lang["Python"]`
- manifest_parse parses `RepoMap.manifest_files`
- map_modules iterates `RepoMap.modules`
- embed_index chunks only files where `role == "source"`
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Classification tables — deliberately narrow. Edge cases are labelled
# "other" and left for downstream LLM stages to reason about.
# ---------------------------------------------------------------------------

SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    "__pycache__", ".venv", "venv", "env", ".env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".coverage", ".cache",
    "dist", "build", "target", "out",
    ".eggs", ".parcel-cache", ".next", ".nuxt",
    "vendor",   # Go vendored deps
}
SKIP_DIR_SUFFIXES = (".egg-info",)

# role → classifier. Order matters: first match wins.
TEST_DIR_NAMES = {"tests", "test", "spec", "specs", "__tests__", "e2e", "integration_tests"}
TEST_FILE_PATTERNS = (
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.go$"),
    re.compile(r"\.(test|spec)\.(js|ts|jsx|tsx)$"),
    re.compile(r"Test\.java$"),
)
DOC_FILE_PATTERNS = (
    re.compile(r"(?i)^readme(\.|$)"),
    re.compile(r"(?i)^changelog(\.|$)"),
    re.compile(r"(?i)^contributing(\.|$)"),
    re.compile(r"(?i)^license(\.|$)"),
    re.compile(r"(?i)^notice(\.|$)"),
    re.compile(r"\.rst$"),
)
GENERATED_MARKERS = (
    "@generated",        # common in auto-gen code
    "DO NOT EDIT",       # in first 512 bytes
)

LANG_BY_EXT = {
    ".py": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java", ".kt": "Kotlin", ".scala": "Scala",
    ".rb": "Ruby", ".php": "PHP",
    ".cs": "CSharp", ".cpp": "Cpp", ".cc": "Cpp", ".cxx": "Cpp",
    ".c": "C", ".h": "CHeader", ".hpp": "CppHeader",
    ".swift": "Swift", ".m": "ObjectiveC", ".mm": "ObjectiveCpp",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".fish": "Shell", ".ksh": "Shell",
    ".lua": "Lua", ".pl": "Perl", ".r": "R", ".R": "R",
    ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "SCSS", ".sass": "SCSS",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
    ".toml": "TOML", ".xml": "XML", ".sql": "SQL",
    ".md": "Markdown", ".mdx": "Markdown",
    ".tf": "Terraform", ".hcl": "HCL",
    ".proto": "Proto", ".graphql": "GraphQL", ".gql": "GraphQL",
    ".vue": "Vue", ".svelte": "Svelte",
    ".ini": "INI", ".cfg": "INI",
    ".env": "DotEnv",
    ".dockerfile": "Dockerfile",
}

SHEBANG_LANG = {
    "python": "Python", "python3": "Python",
    "node": "JavaScript", "nodejs": "JavaScript",
    "bash": "Shell", "sh": "Shell", "zsh": "Shell",
    "ruby": "Ruby", "perl": "Perl", "php": "PHP",
}

BINARY_EXTS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll", ".exe",
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".avif",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".flac", ".ogg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".jar", ".war", ".class", ".wasm",
    ".onnx", ".pt", ".pth", ".ckpt", ".safetensors", ".h5", ".gguf",
    ".npy", ".npz", ".parquet", ".arrow", ".feather",
}

# Manifest files map directly to downstream dep-parsers.
MANIFEST_FILENAMES = {
    "requirements.txt": "python",
    "requirements-dev.txt": "python",
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "Pipfile": "python",
    "Pipfile.lock": "python",
    "poetry.lock": "python",
    "package.json": "node",
    "package-lock.json": "node",
    "yarn.lock": "node",
    "pnpm-lock.yaml": "node",
    "go.mod": "go",
    "go.sum": "go",
    "Cargo.toml": "rust",
    "Cargo.lock": "rust",
    "Gemfile": "ruby",
    "Gemfile.lock": "ruby",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "composer.json": "php",
    "composer.lock": "php",
    "Dockerfile": "dockerfile",
    "docker-compose.yml": "compose",
    "docker-compose.yaml": "compose",
    "compose.yml": "compose",
    "compose.yaml": "compose",
    ".env.example": "env",
    ".env.sample": "env",
    "Procfile": "procfile",
}
MANIFEST_FILENAME_PATTERNS = (
    re.compile(r"^[Dd]ockerfile(\..+)?$"),
    re.compile(r"^\.env(\.[A-Za-z0-9_.-]+)?$"),  # env, env.example, env.local
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FileEntry:
    path: str              # relative to repo root
    lang: str              # "Python" / "TypeScript" / "binary" / "other"
    role: str              # source | test | config | doc | generated | vendored | binary | other
    size_bytes: int
    lines: int = 0         # source lines (strips blank + comment-only for common langs)
    manifest_kind: Optional[str] = None   # set when this is a manifest file


@dataclass
class ModuleEntry:
    """A directory that's substantial enough to warrant its own LLM module-analysis call."""
    path: str              # relative to repo root; "" for the root itself
    languages: dict[str, int] = field(default_factory=dict)   # {lang: loc}
    file_count: int = 0


@dataclass
class RepoMap:
    root: str
    files: list[FileEntry] = field(default_factory=list)
    modules: list[ModuleEntry] = field(default_factory=list)
    # {"Python": [path, path, ...], "TypeScript": [...], ...}
    source_files_by_lang: dict[str, list[str]] = field(default_factory=dict)
    # Files worth feeding to the manifest parser.
    manifest_files: list[str] = field(default_factory=list)
    # Dockerfiles and compose files (subset of manifest_files, listed separately
    # because generate_artifacts wants to know "is there already a Dockerfile?").
    existing_dockerfiles: list[str] = field(default_factory=list)
    existing_compose_files: list[str] = field(default_factory=list)
    total_source_loc: int = 0
    total_files: int = 0
    total_bytes: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "root": self.root,
                "files": [asdict(f) for f in self.files],
                "modules": [asdict(m) for m in self.modules],
                "source_files_by_lang": self.source_files_by_lang,
                "manifest_files": self.manifest_files,
                "existing_dockerfiles": self.existing_dockerfiles,
                "existing_compose_files": self.existing_compose_files,
                "total_source_loc": self.total_source_loc,
                "total_files": self.total_files,
                "total_bytes": self.total_bytes,
            },
            indent=2, sort_keys=True,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def scan(root: str, *, module_min_loc: int = 50) -> RepoMap:
    """Walk `root`, classify every file, aggregate module stats.

    `module_min_loc` controls which directories qualify as "modules"
    (and therefore get their own LLM analysis later). 50 is a sensible
    default — ignores config-only subdirs while picking up even small
    libraries.
    """
    root_path = Path(root).resolve()
    rm = RepoMap(root=str(root_path))

    # Respect .gitignore at the top level if it exists. This keeps us
    # consistent with what the tenant thinks "the code" is, and cheaply
    # excludes build artifacts that SKIP_DIRS misses.
    gitignore_matches = _load_gitignore(root_path)

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        dirnames[:] = [d for d in dirnames if _keep_dir(d, Path(dirpath), gitignore_matches, root_path)]

        rel_dir = os.path.relpath(dirpath, root_path)
        rel_dir = "" if rel_dir == "." else rel_dir

        for fn in filenames:
            fpath = Path(dirpath) / fn
            rel = (os.path.join(rel_dir, fn) if rel_dir else fn).replace(os.sep, "/")
            entry = _classify(fpath, rel)
            if entry is None:
                continue
            rm.files.append(entry)
            rm.total_files += 1
            rm.total_bytes += entry.size_bytes

            if entry.manifest_kind is not None:
                rm.manifest_files.append(rel)
                if entry.manifest_kind == "dockerfile":
                    rm.existing_dockerfiles.append(rel)
                if entry.manifest_kind == "compose":
                    rm.existing_compose_files.append(rel)

            if entry.role == "source":
                rm.source_files_by_lang.setdefault(entry.lang, []).append(rel)
                rm.total_source_loc += entry.lines

    rm.modules = _compute_modules(rm.files, module_min_loc)
    return rm


# ---------------------------------------------------------------------------
# Per-file classification
# ---------------------------------------------------------------------------

def _classify(path: Path, rel: str) -> Optional[FileEntry]:
    try:
        size = path.stat().st_size
    except OSError:
        return None

    ext = path.suffix.lower()
    name = path.name
    parent = path.parent.name

    # Manifest?
    manifest_kind = MANIFEST_FILENAMES.get(name)
    if manifest_kind is None:
        for pat in MANIFEST_FILENAME_PATTERNS:
            if pat.match(name):
                # Crude disambiguation between Dockerfile variants and .env files.
                manifest_kind = "dockerfile" if "ockerfile" in name else "env"
                break

    # Binary?
    if ext in BINARY_EXTS:
        return FileEntry(path=rel, lang="binary", role="binary", size_bytes=size)

    # Probe shebang for unextended executables.
    lang = LANG_BY_EXT.get(ext)
    if lang is None and ext == "" and size > 0:
        lang = _shebang_lang(path)
    if lang is None and name in MANIFEST_FILENAMES:
        # Manifest files get a language label when applicable (e.g. pyproject.toml is TOML).
        lang = LANG_BY_EXT.get(ext, "other")

    role = _role_for(rel, name, parent, ext, lang)

    lines = 0
    if role == "source" and size <= 2 * 1024 * 1024:   # skip LOC count for mega files
        lines = _source_loc(path, lang)

    # Check for generated marker only for files we'd otherwise call source,
    # and only by peeking at the first 512 bytes (not the whole file).
    if role == "source" and _looks_generated(path):
        role = "generated"

    return FileEntry(
        path=rel, lang=lang or "other", role=role, size_bytes=size,
        lines=lines, manifest_kind=manifest_kind,
    )


def _role_for(rel: str, name: str, parent: str, ext: str, lang: Optional[str]) -> str:
    # Docs first — README.md needs to be labelled doc even though it's Markdown.
    for pat in DOC_FILE_PATTERNS:
        if pat.match(name):
            return "doc"
    if lang == "Markdown":
        return "doc"

    if name in MANIFEST_FILENAMES or any(p.match(name) for p in MANIFEST_FILENAME_PATTERNS):
        return "config"

    if parent in TEST_DIR_NAMES or any(part in TEST_DIR_NAMES for part in rel.split("/")):
        return "test"
    for pat in TEST_FILE_PATTERNS:
        if pat.search(rel):
            return "test"

    if lang in {
        "Python", "JavaScript", "TypeScript", "Go", "Rust", "Java", "Kotlin",
        "Ruby", "PHP", "CSharp", "Cpp", "C", "Swift", "ObjectiveC", "Shell",
        "Scala", "Lua", "Perl", "R", "Vue", "Svelte",
    }:
        return "source"

    # YAML, JSON, TOML, INI, HCL, Terraform → config
    if lang in {"YAML", "JSON", "TOML", "XML", "INI", "HCL", "Terraform", "DotEnv", "SQL"}:
        return "config"

    return "other"


def _shebang_lang(path: Path) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            head = f.read(200)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    try:
        first = head.decode("utf-8", errors="replace").splitlines()[0]
    except Exception:
        return None
    # match `#!/usr/bin/env python3` or `#!/bin/bash`
    parts = re.split(r"\s+", first)
    for p in parts:
        binname = p.rsplit("/", 1)[-1].split(" ", 1)[0]
        if binname in SHEBANG_LANG:
            return SHEBANG_LANG[binname]
    return None


def _source_loc(path: Path, lang: Optional[str]) -> int:
    """Count non-blank, non-pure-comment lines. Best effort, not surgical."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return 0
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")

    comment_prefixes: tuple[str, ...] = ("#",) if lang in {"Python", "Shell", "Ruby", "Perl", "R"} else ("//",)
    if lang in {"JavaScript", "TypeScript", "Go", "Rust", "Java", "Kotlin", "CSharp", "Cpp", "C", "Swift", "Scala"}:
        comment_prefixes = ("//",)

    count = 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(s.startswith(p) for p in comment_prefixes):
            continue
        count += 1
    return count


def _looks_generated(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return False
    for marker in GENERATED_MARKERS:
        if marker.encode() in head:
            return True
    return False


# ---------------------------------------------------------------------------
# Directory / .gitignore handling
# ---------------------------------------------------------------------------

def _keep_dir(name: str, parent: Path, gitignore_matches, root: Path) -> bool:
    if name in SKIP_DIRS:
        return False
    if any(name.endswith(sfx) for sfx in SKIP_DIR_SUFFIXES):
        return False
    rel = (parent / name).relative_to(root).as_posix()
    if gitignore_matches and gitignore_matches(rel):
        return False
    return True


def _load_gitignore(root: Path):
    """Return a callable that tests rel-paths against the top-level .gitignore, or None."""
    gi = root / ".gitignore"
    if not gi.is_file():
        return None
    try:
        lines = gi.read_text(errors="replace").splitlines()
    except OSError:
        return None
    patterns = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    # Extremely lightweight: we only honour simple top-level patterns
    # (no nested .gitignores, no `!`-negations). SKIP_DIRS handles the
    # common overlap; gitignore adds project-specific bits.
    def _match(rel: str) -> bool:
        for p in patterns:
            p_trim = p.rstrip("/")
            if not p_trim:
                continue
            if "*" in p_trim:
                # crude glob
                regex = "^" + re.escape(p_trim).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "(/.*)?$"
                if re.match(regex, rel):
                    return True
            else:
                if rel == p_trim or rel.startswith(p_trim + "/"):
                    return True
        return False
    return _match


# ---------------------------------------------------------------------------
# Module aggregation
# ---------------------------------------------------------------------------

def _compute_modules(files: Iterable[FileEntry], min_loc: int) -> list[ModuleEntry]:
    """Group source files by directory; promote directories whose
    source-LOC total meets `min_loc` to modules."""
    agg: dict[str, ModuleEntry] = {}
    for f in files:
        if f.role != "source":
            continue
        parent = "/".join(f.path.split("/")[:-1])
        m = agg.setdefault(parent, ModuleEntry(path=parent))
        m.file_count += 1
        m.languages[f.lang] = m.languages.get(f.lang, 0) + f.lines
    out = []
    for path, m in agg.items():
        loc = sum(m.languages.values())
        if loc >= min_loc:
            out.append(m)
    # Sort by depth then path so a caller looping gets top-level modules
    # before nested ones (useful when building prompts).
    out.sort(key=lambda m: (m.path.count("/"), m.path))
    return out

"""Manifest parsers — pull structured deps out of every package-manager
file we recognise, without asking the LLM. LLMs are creative; manifests
are not, and "what packages does this project need" is a question the
user expects a stable answer to.

Every parser returns a `ManifestEntry` with a normalised shape. The
downstream build-profile stage merges them all into one per-service
dependency list.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml  # PyYAML — Containerizer already pulls it for docker-compose work
except ImportError:  # pragma: no cover - graceful fallback
    yaml = None


@dataclass
class Dep:
    """A single dependency, normalised across ecosystems."""
    name: str
    version: Optional[str] = None     # "^1.2.3", ">=4.0", exact tag, or None
    kind: str = "runtime"             # runtime | dev | test | build | optional
    extras: list[str] = field(default_factory=list)


@dataclass
class ManifestEntry:
    path: str                          # relative to repo root
    ecosystem: str                     # python | node | go | rust | ruby | java | php | dockerfile | compose | env | procfile
    deps: list[Dep] = field(default_factory=list)
    # Free-form keys surfaced to the build profile (python_version,
    # node_version, framework hints from pyproject.toml [tool.*], etc.)
    extra: dict[str, object] = field(default_factory=dict)
    parse_error: Optional[str] = None


def parse(repo_root: str, rel_path: str, ecosystem: str) -> ManifestEntry:
    """Parse one manifest file into a ManifestEntry.

    `ecosystem` is the value stored in RepoMap.files[*].manifest_kind,
    which came from repo_scan's MANIFEST_FILENAMES table — so callers
    always know which parser to invoke.
    """
    fpath = Path(repo_root) / rel_path
    try:
        dispatch = {
            "python": _parse_python,
            "node": _parse_node,
            "go": _parse_go,
            "rust": _parse_rust,
            "ruby": _parse_ruby,
            "java": _parse_java,
            "php": _parse_php,
            "dockerfile": _parse_dockerfile,
            "compose": _parse_compose,
            "env": _parse_env,
            "procfile": _parse_procfile,
        }
        fn = dispatch.get(ecosystem)
        if fn is None:
            return ManifestEntry(path=rel_path, ecosystem=ecosystem,
                                 parse_error=f"no parser for ecosystem={ecosystem}")
        return fn(rel_path, fpath)
    except Exception as e:
        return ManifestEntry(path=rel_path, ecosystem=ecosystem,
                             parse_error=f"{type(e).__name__}: {e}"[:500])


# ---------------------------------------------------------------------------
# Python — requirements.txt, pyproject.toml, setup.py/cfg, Pipfile
# ---------------------------------------------------------------------------

_PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]+)\])?\s*([<>=!~].+)?$")

def _parse_python(rel: str, path: Path) -> ManifestEntry:
    name = path.name
    if name in {"requirements.txt", "requirements-dev.txt"} or name.endswith("requirements.txt"):
        return _parse_requirements_txt(rel, path)
    if name == "pyproject.toml":
        return _parse_pyproject(rel, path)
    if name == "Pipfile":
        return _parse_pipfile(rel, path)
    if name == "setup.cfg":
        return _parse_setup_cfg(rel, path)
    if name == "setup.py":
        return _parse_setup_py(rel, path)
    return ManifestEntry(path=rel, ecosystem="python",
                         parse_error=f"unknown python manifest {name}")


def _parse_requirements_txt(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="python")
    kind = "dev" if "dev" in path.name else "runtime"
    for line in path.read_text(errors="replace").splitlines():
        s = line.split("#", 1)[0].strip()
        if not s or s.startswith("-"):
            continue    # -r, -e, --hash, etc. skipped
        d = _parse_pep508(s, kind)
        if d is not None:
            entry.deps.append(d)
    return entry


def _parse_pep508(spec: str, kind: str) -> Optional[Dep]:
    # Strip git/url installables — we still record them but without a
    # clean version spec.
    if " @ " in spec:
        name_part, _, ref = spec.partition(" @ ")
        m = _PEP508_NAME.match(name_part.strip())
        if m:
            return Dep(name=m.group(1), version=ref.strip(), kind=kind,
                       extras=[e.strip() for e in (m.group(2) or "").split(",") if e.strip()])
        return Dep(name=name_part.strip(), version=ref.strip(), kind=kind)
    m = _PEP508_NAME.match(spec)
    if not m:
        return None
    extras = [e.strip() for e in (m.group(2) or "").split(",") if e.strip()]
    return Dep(name=m.group(1), version=(m.group(3) or "").strip() or None,
               kind=kind, extras=extras)


def _parse_pyproject(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="python")
    data = tomllib.loads(path.read_text(errors="replace"))

    proj = data.get("project", {}) or {}
    entry.extra["name"] = proj.get("name")
    entry.extra["python_requires"] = proj.get("requires-python")
    for spec in proj.get("dependencies", []) or []:
        d = _parse_pep508(spec, "runtime")
        if d: entry.deps.append(d)
    for group, specs in (proj.get("optional-dependencies") or {}).items():
        for spec in specs:
            d = _parse_pep508(spec, "optional")
            if d: d.extras.append(group); entry.deps.append(d)

    # Poetry format
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for n, v in (poetry.get("dependencies") or {}).items():
        if n.lower() == "python":
            entry.extra["python_requires"] = str(v); continue
        entry.deps.append(Dep(name=n, version=_stringify(v), kind="runtime"))
    for n, v in (poetry.get("dev-dependencies") or {}).items():
        entry.deps.append(Dep(name=n, version=_stringify(v), kind="dev"))
    for group, d in (poetry.get("group") or {}).items():
        for n, v in (d.get("dependencies") or {}).items():
            entry.deps.append(Dep(name=n, version=_stringify(v),
                                  kind="dev" if group in ("dev", "test") else "optional"))

    return entry


def _stringify(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        # Poetry's {version="1.2", python="..."} shape
        return v.get("version")
    return str(v)


def _parse_pipfile(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="python")
    data = tomllib.loads(path.read_text(errors="replace"))
    for n, v in (data.get("packages") or {}).items():
        entry.deps.append(Dep(name=n, version=_stringify(v), kind="runtime"))
    for n, v in (data.get("dev-packages") or {}).items():
        entry.deps.append(Dep(name=n, version=_stringify(v), kind="dev"))
    if "requires" in data:
        entry.extra["python_requires"] = data["requires"].get("python_version")
    return entry


def _parse_setup_cfg(rel: str, path: Path) -> ManifestEntry:
    import configparser
    entry = ManifestEntry(path=rel, ecosystem="python")
    cp = configparser.ConfigParser()
    cp.read(path)
    if cp.has_section("options") and cp.has_option("options", "install_requires"):
        for spec in cp.get("options", "install_requires").splitlines():
            s = spec.strip()
            if not s: continue
            d = _parse_pep508(s, "runtime")
            if d: entry.deps.append(d)
    if cp.has_option("options", "python_requires"):
        entry.extra["python_requires"] = cp.get("options", "python_requires")
    return entry


def _parse_setup_py(rel: str, path: Path) -> ManifestEntry:
    # We deliberately do NOT exec setup.py. Grep the common patterns
    # instead; anything dynamic falls through to "parse_error" and the
    # LLM stage can re-examine it.
    entry = ManifestEntry(path=rel, ecosystem="python")
    text = path.read_text(errors="replace")
    m = re.search(r"install_requires\s*=\s*\[([^\]]*)\]", text, re.S)
    if m:
        for spec in re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)):
            d = _parse_pep508(spec, "runtime")
            if d: entry.deps.append(d)
    m = re.search(r"python_requires\s*=\s*['\"]([^'\"]+)['\"]", text)
    if m:
        entry.extra["python_requires"] = m.group(1)
    return entry


# ---------------------------------------------------------------------------
# Node — package.json (+ lockfiles for version pinning when available)
# ---------------------------------------------------------------------------

def _parse_node(rel: str, path: Path) -> ManifestEntry:
    name = path.name
    if name == "package.json":
        return _parse_package_json(rel, path)
    # lockfiles — skip, package.json is authoritative for deps
    return ManifestEntry(path=rel, ecosystem="node",
                         parse_error=f"lockfile {name} not parsed")


def _parse_package_json(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="node")
    data = json.loads(path.read_text(errors="replace"))
    entry.extra["name"] = data.get("name")
    entry.extra["node_version"] = (data.get("engines") or {}).get("node")
    entry.extra["scripts"] = data.get("scripts") or {}
    entry.extra["main"] = data.get("main")
    entry.extra["type"] = data.get("type")
    for n, v in (data.get("dependencies") or {}).items():
        entry.deps.append(Dep(name=n, version=v, kind="runtime"))
    for n, v in (data.get("devDependencies") or {}).items():
        entry.deps.append(Dep(name=n, version=v, kind="dev"))
    for n, v in (data.get("peerDependencies") or {}).items():
        entry.deps.append(Dep(name=n, version=v, kind="optional"))
    for n, v in (data.get("optionalDependencies") or {}).items():
        entry.deps.append(Dep(name=n, version=v, kind="optional"))
    return entry


# ---------------------------------------------------------------------------
# Go — go.mod
# ---------------------------------------------------------------------------

def _parse_go(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="go")
    if path.name != "go.mod":
        # go.sum — not parsed, go.mod is authoritative
        return entry
    lines = path.read_text(errors="replace").splitlines()
    in_require = False
    for line in lines:
        s = line.strip()
        if s.startswith("module "):
            entry.extra["module"] = s.split(None, 1)[1]
            continue
        if s.startswith("go "):
            entry.extra["go_version"] = s.split(None, 1)[1]
            continue
        if s.startswith("require ("):
            in_require = True; continue
        if in_require and s == ")":
            in_require = False; continue
        if in_require:
            parts = s.split("//", 1)[0].split()
            if len(parts) >= 2:
                entry.deps.append(Dep(name=parts[0], version=parts[1], kind="runtime"))
            continue
        if s.startswith("require "):
            parts = s[len("require "):].split("//", 1)[0].split()
            if len(parts) >= 2:
                entry.deps.append(Dep(name=parts[0], version=parts[1], kind="runtime"))
    return entry


# ---------------------------------------------------------------------------
# Rust — Cargo.toml
# ---------------------------------------------------------------------------

def _parse_rust(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="rust")
    if path.name != "Cargo.toml":
        return entry
    data = tomllib.loads(path.read_text(errors="replace"))
    pkg = data.get("package") or {}
    entry.extra["name"] = pkg.get("name")
    entry.extra["rust_edition"] = pkg.get("edition")
    for kind_key, kind in [("dependencies", "runtime"),
                            ("dev-dependencies", "dev"),
                            ("build-dependencies", "build")]:
        for n, v in (data.get(kind_key) or {}).items():
            entry.deps.append(Dep(name=n, version=_stringify(v), kind=kind))
    return entry


# ---------------------------------------------------------------------------
# Ruby — Gemfile
# ---------------------------------------------------------------------------

def _parse_ruby(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="ruby")
    if path.name != "Gemfile":
        return entry
    pattern = re.compile(r'^\s*gem\s+[\'"]([^\'"]+)[\'"](?:\s*,\s*[\'"]([^\'"]+)[\'"])?', re.M)
    for m in pattern.finditer(path.read_text(errors="replace")):
        entry.deps.append(Dep(name=m.group(1), version=m.group(2), kind="runtime"))
    return entry


# ---------------------------------------------------------------------------
# Java — pom.xml, build.gradle
# ---------------------------------------------------------------------------

def _parse_java(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="java")
    if path.name == "pom.xml":
        text = path.read_text(errors="replace")
        for m in re.finditer(
            r"<dependency>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>\s*(?:<version>([^<]+)</version>)?",
            text,
        ):
            entry.deps.append(
                Dep(name=f"{m.group(1)}:{m.group(2)}", version=m.group(3), kind="runtime")
            )
    elif path.name.startswith("build.gradle"):
        text = path.read_text(errors="replace")
        for m in re.finditer(r"(implementation|api|compile|runtime|testImplementation)\s+['\"]([^'\"]+)['\"]", text):
            coord = m.group(2)
            parts = coord.split(":")
            name = ":".join(parts[:2]) if len(parts) >= 2 else coord
            version = parts[2] if len(parts) >= 3 else None
            kind = "test" if "test" in m.group(1).lower() else "runtime"
            entry.deps.append(Dep(name=name, version=version, kind=kind))
    return entry


# ---------------------------------------------------------------------------
# PHP — composer.json
# ---------------------------------------------------------------------------

def _parse_php(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="php")
    if path.name != "composer.json":
        return entry
    data = json.loads(path.read_text(errors="replace"))
    entry.extra["name"] = data.get("name")
    for n, v in (data.get("require") or {}).items():
        kind = "runtime"
        if n.startswith("php"): kind = "runtime"   # php version pin
        entry.deps.append(Dep(name=n, version=v, kind=kind))
    for n, v in (data.get("require-dev") or {}).items():
        entry.deps.append(Dep(name=n, version=v, kind="dev"))
    return entry


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------

_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+\S+)?", re.I | re.M)
_EXPOSE = re.compile(r"^\s*EXPOSE\s+(.+)$", re.I | re.M)
_CMD = re.compile(r"^\s*(CMD|ENTRYPOINT)\s+(.+)$", re.I | re.M)
_WORKDIR = re.compile(r"^\s*WORKDIR\s+(\S+)", re.I | re.M)

def _parse_dockerfile(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="dockerfile")
    text = path.read_text(errors="replace")
    entry.extra["from"] = [m.group(1) for m in _FROM.finditer(text)]
    entry.extra["expose"] = [p.strip() for m in _EXPOSE.finditer(text) for p in m.group(1).split()]
    cmds = []
    for m in _CMD.finditer(text):
        cmds.append({"directive": m.group(1).upper(), "value": m.group(2).strip()})
    entry.extra["commands"] = cmds
    wd = _WORKDIR.search(text)
    if wd: entry.extra["workdir"] = wd.group(1)
    return entry


# ---------------------------------------------------------------------------
# Docker compose
# ---------------------------------------------------------------------------

def _parse_compose(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="compose")
    if yaml is None:
        entry.parse_error = "PyYAML not installed"; return entry
    data = yaml.safe_load(path.read_text(errors="replace")) or {}
    services = data.get("services") or {}
    entry.extra["services"] = {}
    for name, svc in services.items():
        entry.extra["services"][name] = {
            "image": svc.get("image"),
            "build": svc.get("build"),
            "ports": svc.get("ports") or [],
            "environment": svc.get("environment") or {},
            "depends_on": svc.get("depends_on") or [],
        }
    entry.extra["volumes"] = list((data.get("volumes") or {}).keys())
    entry.extra["networks"] = list((data.get("networks") or {}).keys())
    return entry


# ---------------------------------------------------------------------------
# .env — just capture the KEY names, not values. Downstream generation
# uses this to know which env vars to expose in Dockerfile/metadata.
# ---------------------------------------------------------------------------

def _parse_env(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="env")
    keys = []
    for line in path.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"): continue
        k = s.split("=", 1)[0].strip()
        if k and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            keys.append(k)
    entry.extra["keys"] = keys
    return entry


# ---------------------------------------------------------------------------
# Procfile (Heroku-style: "<process>: <cmd>" lines)
# ---------------------------------------------------------------------------

def _parse_procfile(rel: str, path: Path) -> ManifestEntry:
    entry = ManifestEntry(path=rel, ecosystem="procfile")
    procs = {}
    for line in path.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"): continue
        name, _, cmd = s.partition(":")
        if cmd:
            procs[name.strip()] = cmd.strip()
    entry.extra["processes"] = procs
    return entry


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------

def parse_all(repo_root: str, manifests: list[tuple[str, str]]) -> list[ManifestEntry]:
    """Parse every (rel_path, ecosystem) pair; swallow no exceptions (each
    ManifestEntry carries its own parse_error if it failed)."""
    return [parse(repo_root, rel, eco) for rel, eco in manifests]

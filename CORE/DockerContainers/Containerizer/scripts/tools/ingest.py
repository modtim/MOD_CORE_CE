"""Ingest — fetch a codebase into a local workspace for the pipeline.

The codebase-engineer pipeline operates on a single workspace directory.
This module normalises every input shape (git URL, tarball, local path,
single pasted script) into that shape:

    /data/input/codebase/
        repo-a/
        repo-b/          # multi-repo case
        _meta.json       # ingest manifest (inputs, sizes, source shas)

Everything downstream (repo_scan, ast_extract, embed_index) assumes this
layout. No LLM involvement — deterministic I/O only.

Size cap: 5 GB total across all ingested inputs. Enforced before any
expensive scan/embed work.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional


MAX_TOTAL_BYTES = 5 * 1024 * 1024 * 1024   # 5 GB hard cap
CLONE_TIMEOUT_SECONDS = 600                 # fallback 10 min per repo
CLONE_TIMEOUT_MIN_S = 30
CLONE_TIMEOUT_MAX_S = 3600
GIT_URL_PREFIXES = ("git@", "https://", "http://", "ssh://")


def _clone_timeout_seconds() -> int:
    """GSEC R-100: honour CLONE_TIMEOUT_S (DB platform setting
    `wizard.clone_timeout_s`, passed by the build's generate stage) with the
    same 30–3600s clamp as `git_operations.clone_repo_tool`. Falls back to
    `CLONE_TIMEOUT_SECONDS` when unset or unparsable."""
    raw = (os.getenv("CLONE_TIMEOUT_S") or "").strip()
    if raw:
        try:
            return max(CLONE_TIMEOUT_MIN_S,
                       min(int(float(raw)), CLONE_TIMEOUT_MAX_S))
        except (TypeError, ValueError):
            pass
    return CLONE_TIMEOUT_SECONDS


class IngestError(RuntimeError):
    """Raised when an input can't be fetched or exceeds the size cap."""


@dataclass
class IngestedInput:
    """One ingested tree. Multiple inputs can coexist in one workspace."""

    name: str                  # unique directory name under the workspace
    kind: str                  # git | tarball | zip | folder | inline
    source: str                # URL, path, or "inline" marker
    path: str                  # absolute path to the ingested root
    size_bytes: int = 0
    file_count: int = 0
    # For git sources, the SHA of HEAD after clone. Lets downstream
    # artifacts embed a "generated from commit ..." provenance line.
    commit_sha: Optional[str] = None


@dataclass
class IngestManifest:
    """_meta.json contents written at the workspace root."""

    workspace: str
    inputs: list[IngestedInput] = field(default_factory=list)
    total_bytes: int = 0
    ingested_at: float = field(default_factory=time.time)
    cap_bytes: int = MAX_TOTAL_BYTES

    def to_json(self) -> str:
        return json.dumps(
            {
                "workspace": self.workspace,
                "inputs": [asdict(i) for i in self.inputs],
                "total_bytes": self.total_bytes,
                "ingested_at": self.ingested_at,
                "cap_bytes": self.cap_bytes,
            },
            indent=2,
            sort_keys=True,
        )


def ingest(
    workspace: str,
    sources: Iterable[str] | None = None,
    inline_script: Optional[str] = None,
    inline_filename: str = "user_script.py",
) -> IngestManifest:
    """Populate `workspace` with every input; return the manifest.

    `sources` is an iterable of strings, each one of:
      - git URL (starts with ``git@``, ``https://``, ``http://``, ``ssh://``;
        or looks like ``host/org/repo(.git)``)
      - local path to a directory
      - local path to a tarball (``*.tar.gz``, ``*.tgz``, ``*.tar``)
      - local path to a zip (``*.zip``)

    `inline_script` is the current "paste a script" single-file path —
    it's written verbatim as ``inline/<inline_filename>`` so the rest
    of the pipeline can treat it identically to any other source file.

    Raises IngestError on any fetch failure or size-cap breach.
    """
    workspace_path = Path(workspace).resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)

    manifest = IngestManifest(workspace=str(workspace_path))

    if inline_script is not None:
        inp = _ingest_inline(workspace_path, inline_script, inline_filename)
        _check_cap(manifest, inp)
        manifest.inputs.append(inp)

    for src in sources or []:
        if _looks_like_git_url(src):
            inp = _ingest_git(workspace_path, src)
        elif os.path.isdir(src):
            inp = _ingest_folder(workspace_path, src)
        elif src.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
            inp = _ingest_tarball(workspace_path, src)
        elif src.endswith(".zip"):
            inp = _ingest_zip(workspace_path, src)
        else:
            raise IngestError(f"Unrecognised source: {src!r}")
        _check_cap(manifest, inp)
        manifest.inputs.append(inp)

    manifest.total_bytes = sum(i.size_bytes for i in manifest.inputs)
    (workspace_path / "_meta.json").write_text(manifest.to_json())
    return manifest


# ---------------------------------------------------------------------------
# Per-source implementations
# ---------------------------------------------------------------------------

def _ingest_git(workspace: Path, url: str) -> IngestedInput:
    """Clone the given URL into a unique subdir; recurse submodules shallowly."""
    name = _safe_name_from_url(url)
    dest = _unique_subdir(workspace, name)
    cmd = [
        "git", "clone",
        "--depth", "1",
        "--recurse-submodules",
        "--shallow-submodules",
        "--single-branch",
        url, str(dest),
    ]
    timeout_s = _clone_timeout_seconds()
    try:
        subprocess.run(
            cmd, check=True, capture_output=True, timeout=timeout_s,
        )
    except subprocess.CalledProcessError as e:
        raise IngestError(
            f"git clone failed for {url}: {e.stderr.decode(errors='replace')[:500]}"
        ) from e
    except subprocess.TimeoutExpired:
        raise IngestError(f"git clone timed out after {timeout_s}s: {url}")

    sha = _git_head_sha(dest)
    size, count = _tree_size(dest)
    return IngestedInput(
        name=dest.name, kind="git", source=url, path=str(dest),
        size_bytes=size, file_count=count, commit_sha=sha,
    )


def _ingest_folder(workspace: Path, path: str) -> IngestedInput:
    """Copy a local folder into the workspace."""
    src = Path(path).resolve()
    name = _unique_subdir(workspace, _safe_name(src.name or "folder"))
    shutil.copytree(src, name, dirs_exist_ok=False, symlinks=False)
    size, count = _tree_size(name)
    return IngestedInput(
        name=name.name, kind="folder", source=str(src), path=str(name),
        size_bytes=size, file_count=count,
    )


def _ingest_tarball(workspace: Path, path: str) -> IngestedInput:
    name = _unique_subdir(workspace, _safe_name(Path(path).stem))
    name.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:*") as tf:
        _safe_extract_tar(tf, name)
    size, count = _tree_size(name)
    return IngestedInput(
        name=name.name, kind="tarball", source=path, path=str(name),
        size_bytes=size, file_count=count,
    )


def _ingest_zip(workspace: Path, path: str) -> IngestedInput:
    name = _unique_subdir(workspace, _safe_name(Path(path).stem))
    name.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        _safe_extract_zip(zf, name)
    size, count = _tree_size(name)
    return IngestedInput(
        name=name.name, kind="zip", source=path, path=str(name),
        size_bytes=size, file_count=count,
    )


def _ingest_inline(
    workspace: Path, body: str, filename: str,
) -> IngestedInput:
    """Write a pasted script into workspace/inline/<filename>."""
    dest = _unique_subdir(workspace, "inline")
    # _unique_subdir returns a NON-EXISTING path by contract — it's
    # the caller's job to create it. tarball / zip / git all handle
    # this implicitly (shutil.copytree, tarfile.extractall, git clone
    # all create the target dir), but inline writes don't, so we have
    # to mkdir ourselves or write_text fails with ENOENT.
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / _safe_name(filename)
    target.write_text(body)
    size = target.stat().st_size
    return IngestedInput(
        name=dest.name, kind="inline", source="inline", path=str(dest),
        size_bytes=size, file_count=1,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_like_git_url(src: str) -> bool:
    if src.startswith(GIT_URL_PREFIXES):
        return True
    # bare "github.com/org/repo" shape
    if "/" in src and not os.path.exists(src) and "." in src.split("/")[0]:
        return True
    return False


def _safe_name_from_url(url: str) -> str:
    """Produce a stable, filesystem-safe directory name from a git URL."""
    parsed = urllib.parse.urlparse(url) if "://" in url else None
    path = parsed.path if parsed else url.split(":")[-1]
    tail = path.rstrip("/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return _safe_name(tail or "repo")


def _safe_name(name: str) -> str:
    """Strip unsafe chars from a directory name."""
    clean = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return clean[:80] or "item"


def _unique_subdir(workspace: Path, base: str) -> Path:
    """Return a non-existing subpath of workspace derived from `base`."""
    candidate = workspace / base
    if not candidate.exists():
        return candidate
    # Disambiguate by short content-independent hash of time + base.
    tag = hashlib.sha1(f"{base}{time.time_ns()}".encode()).hexdigest()[:6]
    return workspace / f"{base}_{tag}"


def _git_head_sha(repo: Path) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, timeout=10,
        )
        return r.stdout.decode().strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _tree_size(root: Path) -> tuple[int, int]:
    """Return (total bytes, file count) under root (symlinks not followed)."""
    total = 0
    count = 0
    for dirpath, _, filenames in os.walk(root, followlinks=False):
        for fn in filenames:
            try:
                st = os.stat(os.path.join(dirpath, fn), follow_symlinks=False)
            except OSError:
                continue
            total += st.st_size
            count += 1
    return total, count


def _check_cap(manifest: IngestManifest, incoming: IngestedInput) -> None:
    running = manifest.total_bytes + incoming.size_bytes
    if running > manifest.cap_bytes:
        raise IngestError(
            f"Total ingest size {running} B exceeds cap {manifest.cap_bytes} B "
            f"({incoming.source!r} added {incoming.size_bytes} B)"
        )
    manifest.total_bytes = running


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract tar members while rejecting path traversal."""
    dest = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise IngestError(f"tarball path traversal: {member.name!r}")
    tf.extractall(dest)


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for name in zf.namelist():
        target = (dest / name).resolve()
        if not str(target).startswith(str(dest)):
            raise IngestError(f"zip path traversal: {name!r}")
    zf.extractall(dest)

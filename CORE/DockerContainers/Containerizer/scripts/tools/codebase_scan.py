"""Phase-1 orchestrator — the deterministic half of the codebase pipeline.

Wires: ingest → repo_scan → manifest_parse → ast_extract (+ polyglot) →
chunk → embed_index. Emits a ScanResult dict that later LLM stages
(map, reduce, generate) consume.

Callers (today: `Containerizer/scripts/app.py` when
``OUTPUT_MODE=codebase``) get a single JSON-serialisable structure back
plus a Qdrant collection name they can retrieve against.

The module also handles **streaming progress**: pass a ``progress``
callback that receives ``{stage, status, detail}`` dicts so the API can
surface stage transitions in real time to the wizard UI.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Callable, Iterable, Optional

from tools import ast_extract, ast_polyglot, chunk as chunking, ingest, manifest_parse, repo_scan

try:
    from tools import embed_index
    _EMBED_AVAILABLE = True
except ImportError:   # pragma: no cover
    _EMBED_AVAILABLE = False


logger = logging.getLogger(__name__)

ProgressFn = Callable[[dict], None]

# Languages that get full AST treatment via ast_polyglot; others fall
# back to regex in ast_polyglot itself.
POLYGLOT_LANGS = {
    "JavaScript", "TypeScript", "Go", "Java", "Rust", "Ruby", "Shell",
}


def scan_codebase(
    task_id: str,
    workspace: str,
    sources: Optional[Iterable[str]] = None,
    inline_script: Optional[str] = None,
    inline_filename: str = "user_script.py",
    *,
    progress: Optional[ProgressFn] = None,
    index_embeddings: bool = True,
) -> dict:
    """Run every deterministic stage and return a summary dict.

    Shape of the return value:

        {
            "task_id": str,
            "workspace": str,
            "manifest": {ingest manifest},
            "repo_map": {scan repo map},
            "manifests": [{parsed manifest entries}],
            "python_ast": [{per-file Python AST}],
            "polyglot_ast": [{per-file polyglot AST}],
            "index": {"collection": str, "points": int, "dim": int} | None,
            "errors": [{stage, detail}],
        }
    """
    p = progress or (lambda _: None)
    errors: list[dict] = []

    # ---------------- 1. ingest -----------------------------------
    p({"stage": "ingest", "status": "running"})
    try:
        manifest = ingest.ingest(
            workspace=workspace, sources=sources,
            inline_script=inline_script, inline_filename=inline_filename,
        )
        p({
            "stage": "ingest", "status": "ok",
            "detail": f"{len(manifest.inputs)} input(s), "
                      f"{manifest.total_bytes / 1024 / 1024:.1f} MB",
        })
    except ingest.IngestError as e:
        errors.append({"stage": "ingest", "detail": str(e)})
        p({"stage": "ingest", "status": "error", "detail": str(e)})
        return _emit_result(
            task_id=task_id, workspace=workspace,
            manifest=None, repo_map=None, manifests=[], py_ast=[],
            poly_ast=[], index=None, errors=errors,
        )

    # ---------------- 2. repo scan --------------------------------
    # Scan every ingested root, union the results. Multi-repo inputs
    # stay as sibling dirs under workspace/ and share a single map.
    p({"stage": "scan", "status": "running"})
    rm = _scan_all_roots(workspace, manifest)
    p({
        "stage": "scan", "status": "ok",
        "detail": f"{rm.total_files} files, "
                  f"{rm.total_source_loc} source LOC, "
                  f"{len(rm.modules)} modules, "
                  f"{len(rm.manifest_files)} manifests",
    })

    # ---------------- 3. manifest parse ---------------------------
    p({"stage": "manifests", "status": "running"})
    manifests_out: list[dict] = []
    for rel in rm.manifest_files:
        # Look up ecosystem from the file entry (set by repo_scan).
        eco = next(
            (fe.manifest_kind for fe in rm.files if fe.path == rel),
            None,
        )
        if not eco:
            continue
        entry = manifest_parse.parse(rm.root, rel, eco)
        manifests_out.append(_manifest_to_dict(entry))
    p({"stage": "manifests", "status": "ok",
       "detail": f"{len(manifests_out)} manifests parsed"})

    # ---------------- 4. Python AST -------------------------------
    p({"stage": "python_ast", "status": "running"})
    py_files = rm.source_files_by_lang.get("Python", [])
    py_records: list[dict] = []
    for rel in py_files:
        rec = ast_extract.extract(rm.root, rel)
        py_records.append(asdict(rec))
    p({"stage": "python_ast", "status": "ok",
       "detail": f"{len(py_records)} .py files parsed"})

    # ---------------- 5. Polyglot AST -----------------------------
    p({"stage": "polyglot_ast", "status": "running"})
    poly_records: list[dict] = []
    for lang in POLYGLOT_LANGS:
        for rel in rm.source_files_by_lang.get(lang, []):
            rec = ast_polyglot.extract(rm.root, rel, lang)
            poly_records.append(asdict(rec))
    p({"stage": "polyglot_ast", "status": "ok",
       "detail": f"{len(poly_records)} files parsed across "
                 f"{sum(1 for l in POLYGLOT_LANGS if l in rm.source_files_by_lang)} "
                 f"languages"})

    # ---------------- 6. Chunk + Embed + Qdrant upsert ------------
    index_result: Optional[dict] = None
    if index_embeddings and _EMBED_AVAILABLE:
        p({"stage": "index", "status": "running"})
        try:
            framework_map = _framework_map_from_ast(py_records, poly_records)
            gpu_map = {r["path"]: r.get("uses_gpu", False) for r in py_records}
            file_specs = list(_file_specs_for_chunking(rm, framework_map, gpu_map))
            all_chunks = chunking.chunk_many(rm.root, file_specs)
            if all_chunks:
                index_result = embed_index.index(task_id=task_id, chunks=all_chunks)
                p({"stage": "index", "status": "ok",
                   "detail": f"{index_result['points']} chunks in "
                             f"{index_result['collection']}"})
            else:
                p({"stage": "index", "status": "ok", "detail": "0 chunks to index"})
        except Exception as e:
            # Embedding is nice-to-have; if Qdrant is down, keep the
            # deterministic pipeline data and let later stages decide.
            errors.append({"stage": "index", "detail": str(e)})
            p({"stage": "index", "status": "error", "detail": str(e)})
            logger.warning("Embedding skipped: %s", e)
    else:
        p({"stage": "index", "status": "skipped",
           "detail": "embed_index unavailable — deps missing or disabled"})

    return _emit_result(
        task_id=task_id, workspace=workspace,
        manifest=manifest, repo_map=rm, manifests=manifests_out,
        py_ast=py_records, poly_ast=poly_records, index=index_result,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scan_all_roots(workspace: str, manifest: "ingest.IngestManifest") -> repo_scan.RepoMap:
    """Run repo_scan over every ingested input, then unify into one RepoMap
    keyed by paths relative to the workspace root (so downstream stages
    see a single tree, even if multi-repo was used)."""
    # Simplest correct: scan the whole workspace. repo_scan's SKIP_DIRS
    # covers .git / node_modules / etc.; the _meta.json at the workspace
    # root is config and gets labelled accordingly.
    return repo_scan.scan(workspace)


def _manifest_to_dict(entry: "manifest_parse.ManifestEntry") -> dict:
    d = asdict(entry)
    # Deps are dataclasses themselves
    d["deps"] = [asdict(dep) if not isinstance(dep, dict) else dep for dep in entry.deps]
    return d


def _framework_map_from_ast(py_records: list[dict], poly_records: list[dict]) -> dict[str, list[str]]:
    """path → framework hints (web + queue + ml bucketed together)."""
    out: dict[str, list[str]] = {}
    for r in py_records:
        out[r["path"]] = (
            list(r.get("web_frameworks") or []) +
            list(r.get("queue_frameworks") or []) +
            list(r.get("ml_frameworks") or [])
        )
    for r in poly_records:
        out[r["path"]] = (
            list(r.get("web_frameworks") or []) +
            list(r.get("queue_frameworks") or []) +
            list(r.get("ml_frameworks") or [])
        )
    return out


def _file_specs_for_chunking(
    rm: repo_scan.RepoMap,
    framework_map: dict[str, list[str]],
    gpu_map: dict[str, bool],
):
    """Yield (rel_path, language, role, framework_hints, uses_gpu) tuples
    for every source file, restricted to languages where the LLM can
    actually reason about the content (skip binaries + generated)."""
    for fe in rm.files:
        if fe.role not in ("source", "doc"):
            continue
        if fe.lang in ("binary", "other"):
            continue
        yield (
            fe.path, fe.lang, fe.role,
            framework_map.get(fe.path, []),
            gpu_map.get(fe.path, False),
        )


def _emit_result(
    *, task_id, workspace, manifest, repo_map, manifests,
    py_ast, poly_ast, index, errors,
) -> dict:
    return {
        "task_id": task_id,
        "workspace": workspace,
        "manifest": (
            json.loads(manifest.to_json()) if manifest else None
        ),
        "repo_map": (
            json.loads(repo_map.to_json()) if repo_map else None
        ),
        "manifests": manifests,
        "python_ast": py_ast,
        "polyglot_ast": poly_ast,
        "index": index,
        "errors": errors,
    }

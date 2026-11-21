"""End-to-end orchestrator — wires every stage into one callable.

The Containerizer's app.py dispatches `OUTPUT_MODE=codebase` here.
This module walks the 8 stages in order, emits streaming progress
events (one line of JSON per transition on stdout), and at the end
prints the final ArtifactResult JSON so the API side can pick it up
from the task log the way `albedo` mode already works.

Stages:
  1. ingest         — fetch / extract / copy into /data/input/codebase
  2. repo_scan      — file classification, module aggregation
  3. manifests      — parse every package manifest
  4. ast + embed    — Python AST, polyglot AST, Qdrant upsert
  5. map            — parallel per-module LLM analysis (semantic summary)
  6. reduce         — one LLM call → build profile
  7. generate       — 4 parallel LLM calls → 4 artifacts
  8. validate       — static checks
  9. refine         — up to 2 iterations fixing flagged errors
 10. build_verify   — optional real `docker build` of the result

Every stage print-emits a JSON line like:
    {"stage": "map", "status": "running"}
    {"stage": "map", "status": "ok", "detail": "14 modules / 68s"}
so the API poller can stream them to the wizard timeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from typing import Any, Iterable, Optional

from tools import (
    build_verify,
    codebase_scan,
    generate_artifacts_v2,
    map_modules,
    reduce_profile,
    refine as refine_mod,
    validate_static,
)
from tools.llm_providers import LLMConfig


logger = logging.getLogger(__name__)


async def run_codebase_pipeline(
    task_id: str,
    *,
    workspace: str,
    sources: Optional[Iterable[str]] = None,
    inline_script: Optional[str] = None,
    inline_filename: str = "user_script.py",
    user_hints: Optional[str] = None,
    llm: Optional[LLMConfig] = None,
    do_build_verify: bool = False,
    registry: str = "example.com",
) -> dict:
    """Execute the full pipeline. Returns the final result dict.

    Progress is also printed to stdout as one JSON line per event so
    the API's task-log poller can reflect it in real time.
    """
    if llm is None:
        llm = LLMConfig.from_env()

    started_at = time.time()
    errors: list[dict] = []

    # ---- 1–4: deterministic scan ------------------------------------
    # codebase_scan already emits its own progress events through the
    # callback, so we just wrap each call.
    def _progress(event: dict) -> None:
        _emit(event)

    _emit({"stage": "pipeline", "status": "started",
           "detail": f"task={task_id}"})

    scan_result = codebase_scan.scan_codebase(
        task_id=task_id,
        workspace=workspace,
        sources=sources,
        inline_script=inline_script,
        inline_filename=inline_filename,
        progress=_progress,
        index_embeddings=True,
    )

    # If ingest bombed, surface and stop.
    if any(e.get("stage") == "ingest" for e in (scan_result.get("errors") or [])):
        _emit({"stage": "pipeline", "status": "error",
               "detail": "ingest failed — pipeline aborted"})
        return _finalise(task_id, scan_result, build_profile=None,
                         artifacts=None, validation=None, build=None,
                         errors=scan_result.get("errors") or [])

    collection = None
    if scan_result.get("index"):
        collection = scan_result["index"].get("collection")

    # ---- 5: map modules ---------------------------------------------
    t0 = time.time()
    _emit({"stage": "map", "status": "running",
           "detail": f"{len(scan_result.get('repo_map', {}).get('modules') or [])} modules"})
    module_analyses = await map_modules.map_modules(
        scan_result, llm, collection=collection,
    )
    failed = [m for m in module_analyses if getattr(m, "parsed", None) is None]
    _emit({"stage": "map", "status": "ok",
           "detail": f"{len(module_analyses) - len(failed)}/{len(module_analyses)} "
                     f"modules analysed in {time.time() - t0:.1f}s"})
    if failed:
        for m in failed:
            errors.append({"stage": "map", "module": m.path,
                           "detail": m.error or "unknown"})

    # ---- 6: reduce --------------------------------------------------
    t0 = time.time()
    _emit({"stage": "reduce", "status": "running"})
    profile_result = await reduce_profile.reduce(
        scan_result, module_analyses, llm, user_hints=user_hints,
    )
    if not profile_result.parsed:
        _emit({"stage": "reduce", "status": "error",
               "detail": profile_result.error or "parse failure"})
        errors.append({"stage": "reduce",
                       "detail": profile_result.error or "parse failure"})
        return _finalise(task_id, scan_result, build_profile=None,
                         artifacts=None, validation=None, build=None,
                         errors=errors)
    build_profile = profile_result.parsed
    _emit({"stage": "reduce", "status": "ok",
           "detail": f"node_name={build_profile.get('node_name')!r} "
                     f"gpu={build_profile.get('gpu_required')} "
                     f"in {time.time() - t0:.1f}s"})

    # ---- 7: generate artifacts -------------------------------------
    t0 = time.time()
    _emit({"stage": "generate", "status": "running",
           "detail": "4 parallel LLM calls: Dockerfile, entrypoint, build-push, node metadata"})
    artifacts = await generate_artifacts_v2.generate(
        build_profile, llm, collection=collection, registry=registry,
    )
    if artifacts.errors:
        _emit({"stage": "generate", "status": "warn",
               "detail": f"{len(artifacts.errors)} artifact(s) failed: "
                         f"{list(artifacts.errors.keys())}"})
        for k, v in artifacts.errors.items():
            errors.append({"stage": "generate", "artifact": k, "detail": v})
    _emit({"stage": "generate", "status": "ok",
           "detail": f"4 artifacts in {time.time() - t0:.1f}s"})

    # ---- 8–9: validate + refine loop -------------------------------
    t0 = time.time()
    _emit({"stage": "validate", "status": "running"})
    artifacts, validation = await refine_mod.refine(artifacts, build_profile, llm)
    if validation.has_errors:
        _emit({"stage": "validate", "status": "warn",
               "detail": f"{sum(1 for i in validation.issues if i.severity == 'error')} "
                         f"error(s) remain after refine"})
    else:
        n_warn = sum(1 for i in validation.issues if i.severity == "warn")
        _emit({"stage": "validate", "status": "ok",
               "detail": f"no errors ({n_warn} warning(s)) in "
                         f"{time.time() - t0:.1f}s"})

    # ---- 10: optional build verify ---------------------------------
    build_outcome = None
    if do_build_verify:
        _emit({"stage": "build_verify", "status": "running",
               "detail": "docker build — this can take several minutes on first run"})
        build_outcome = build_verify.build_and_verify(
            artifacts.to_dict(), workspace, smoke_test=False,
            registry_hint=registry,
        )
        if build_outcome.success:
            _emit({"stage": "build_verify", "status": "ok",
                   "detail": f"image builds in {build_outcome.duration_seconds:.1f}s"})
        else:
            _emit({"stage": "build_verify", "status": "error",
                   "detail": f"build failed in {build_outcome.duration_seconds:.1f}s"})
            for i in build_outcome.issues:
                errors.append({"stage": "build_verify", "kind": i.kind,
                               "detail": i.message[:400]})

    _emit({"stage": "pipeline", "status": "done",
           "detail": f"total {time.time() - started_at:.1f}s"})

    return _finalise(task_id, scan_result, build_profile=build_profile,
                     artifacts=artifacts, validation=validation,
                     build=build_outcome, errors=errors)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit(event: dict) -> None:
    """Print a progress event as a single JSON line. The API task-log
    poller watches for lines starting with `{"stage":` and forwards
    them to the wizard UI."""
    event.setdefault("ts", round(time.time(), 3))
    try:
        print(json.dumps(event, separators=(",", ":")), flush=True)
    except Exception:
        # stdout blocked? keep the pipeline running; the API will
        # just not see progress for this event.
        pass


def _finalise(
    task_id: str, scan_result: dict, *,
    build_profile: Optional[dict],
    artifacts,
    validation: Optional[validate_static.ValidationReport],
    build: Optional[build_verify.BuildOutcome],
    errors: list[dict],
) -> dict:
    """Emit the final JSON payload the API picks up via
    `_extract_albedo_artifacts`. Shape is a superset of the existing
    albedo output so old poll code keeps working."""
    # The existing albedo extractor looks for the last balanced JSON
    # block with `dockerfile`, `entrypoint_sh`, `build_and_push_sh`
    # keys. Put those at the top level + stash everything else under
    # `codebase_*` keys so the API can optionally surface richer
    # context in the UI.
    payload: dict = {
        "dockerfile": "", "entrypoint_sh": "", "build_and_push_sh": "",
        "node_metadata_py": "",
        "success": False,
        "image_name": "",
        "errors": errors,
        "codebase_task_id": task_id,
    }
    if artifacts is not None:
        d = artifacts.to_dict()
        payload.update({
            "dockerfile": d.get("dockerfile", ""),
            "entrypoint_sh": d.get("entrypoint_sh", ""),
            "build_and_push_sh": d.get("build_and_push_sh", ""),
            "node_metadata_py": d.get("node_metadata_py", ""),
            "image_name": d.get("image_name", ""),
            "success": d.get("success", False),
        })
    if build_profile is not None:
        payload["codebase_build_profile"] = build_profile
    if validation is not None:
        payload["codebase_validation"] = validation.to_dict()
    if build is not None:
        payload["codebase_build_verify"] = {
            "success": build.success,
            "duration_seconds": build.duration_seconds,
            "log_tail": build.log_tail,
        }
    payload["codebase_scan_summary"] = {
        "total_files": (scan_result.get("repo_map") or {}).get("total_files", 0),
        "total_source_loc": (scan_result.get("repo_map") or {}).get("total_source_loc", 0),
        "modules": len((scan_result.get("repo_map") or {}).get("modules") or []),
        "manifests": len(scan_result.get("manifests") or []),
        "index_collection": (scan_result.get("index") or {}).get("collection"),
    }
    _emit({"stage": "pipeline", "status": "result",
           "detail": f"success={payload['success']} "
                     f"errors={len(payload['errors'])}"})
    # Final flush: print the full payload as a single final JSON line
    # so the API's balanced-JSON scanner finds it cleanly.
    print(json.dumps(payload), flush=True)
    # ALSO write to /data/output/albedo-artifacts.json so the worker's
    # upload sidecar picks it up + the downstream ingest_artifacts task
    # has something to read. Without this write the codebase pipeline
    # produces a stdout-only result that the worker's MinIO upload step
    # never sees, leading to "0 outputs" reported back to the API and
    # the workflow_run getting stuck at ingest_artifacts (E2E repro:
    # Git-mode wizard build 2026-05-26 02:35-03:07 UTC).
    #
    # Match the albedo-mode shape (output_mode="codebase" so the
    # downstream ingest_artifacts shape-based reader recognizes it).
    output_dir = os.getenv("OUTPUT_DIR", "/data/output").strip() or "/data/output"
    artifacts_payload = {"output_mode": "codebase", **payload}
    try:
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "albedo-artifacts.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(artifacts_payload, f)
        _emit({"stage": "pipeline", "status": "wrote_artifacts",
               "detail": f"wrote {path}"})
    except Exception as exc:
        _emit({"stage": "pipeline", "status": "warn",
               "detail": f"failed to write albedo-artifacts.json: {exc}"})
    return payload


# ---------------------------------------------------------------------------
# CLI entry — so app.py can `python -m tools.codebase_pipeline` instead of
# importing + asyncio.run itself.
# ---------------------------------------------------------------------------

def _cli_main() -> int:
    task_id = os.getenv("TASK_ID") or f"wizard-{int(time.time())}"
    workspace = os.getenv("CODEBASE_WORKSPACE", "/data/input/codebase")
    sources_env = os.getenv("CODEBASE_SOURCES", "").strip()
    sources = [s for s in sources_env.split(",") if s] if sources_env else None
    inline_script = os.getenv("INLINE_SOURCE_BODY")
    inline_filename = os.getenv("INLINE_SOURCE_FILENAME", "user_script.py")
    user_hints = os.getenv("USER_HINTS")
    do_build_verify = os.getenv("BUILD_VERIFY", "").lower() in ("1", "true", "yes")
    registry = os.getenv("MOD_REGISTRY", "example.com")

    asyncio.run(run_codebase_pipeline(
        task_id=task_id,
        workspace=workspace,
        sources=sources,
        inline_script=inline_script,
        inline_filename=inline_filename,
        user_hints=user_hints,
        do_build_verify=do_build_verify,
        registry=registry,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())

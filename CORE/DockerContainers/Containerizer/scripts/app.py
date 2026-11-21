#!/usr/bin/env python3
"""
Containerizer CLI — generate a Dockerfile from a Git repo, script, or folder.

Everything is configured via environment variables:

  Input (at least one required, checked in priority order):
    REPO_URL              GitHub/Git repo URL to clone and analyze
    SOURCE_DIR            Path to a local directory to containerize
    (fallback)            If neither is set, scans /data/input/ automatically

  Git auth (optional, for private repos):
    GITHUB_TOKEN          Token auth (injected into HTTPS URL)
    GIT_USERNAME          Basic auth username (used with GIT_PASSWORD)
    GIT_PASSWORD          Basic auth password (used with GIT_USERNAME)
    GIT_SSH_KEY_PATH      Path to SSH private key

  General:
    ADDITIONAL_INSTRUCTIONS  Extra instructions for the LLM
    OUTPUT_DIR            Directory to write Dockerfile to (default: /data/output)

  LLM:
    LLM_PROVIDER          openai | anthropic | google | ollama | groq | mistral | deepseek | openai_compatible
    LLM_MODEL             Model identifier (e.g. gpt-4o-mini, claude-sonnet-4-20250514, qwen3:8b)
    LLM_API_KEY           API key (not needed for ollama)
    LLM_BASE_URL          Base URL (required for ollama, openai_compatible)
    LLM_TEMPERATURE       Generation temperature (default: 0.3)
    LLM_MAX_TOKENS        Max output tokens (default: 4096)

Usage:
  # From a repo
  REPO_URL=https://github.com/user/repo LLM_PROVIDER=ollama LLM_MODEL=qwen3:8b LLM_BASE_URL=http://localhost:11434 python app.py

  # From a local folder
  SOURCE_DIR=/path/to/project LLM_PROVIDER=ollama LLM_MODEL=qwen3:8b LLM_BASE_URL=http://localhost:11434 python app.py

  # Via Docker (script/folder in /data/input/)
  docker run --rm \\
    -v ./my-script.py:/data/input/my-script.py \\
    -e LLM_PROVIDER=ollama \\
    -e LLM_MODEL=qwen3:8b \\
    -e LLM_BASE_URL=http://host.docker.internal:11434 \\
    containerizer
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from tools.git_operations import clone_repo_tool
from tools.gitingest import gitingest_tool
from tools.source_scanner import scan_source
from tools.create_container import create_container_tool, create_albedo_pattern_tool
from tools.llm_providers import LLMConfig, PROVIDERS


def log(msg: str):
    """Print to stderr so stdout stays clean for piping."""
    print(msg, file=sys.stderr, flush=True)


def _detect_input_mode():
    """Determine input mode from environment and filesystem."""
    repo_url = os.getenv("REPO_URL", "").strip()
    if repo_url:
        return "repo", repo_url

    source_dir = os.getenv("SOURCE_DIR", "").strip()
    if source_dir:
        return "local", source_dir

    # Auto-detect: check /data/input/ for files
    input_dir = "/data/input"
    if os.path.isdir(input_dir):
        entries = [e for e in os.listdir(input_dir) if not e.startswith(".")]
        if entries:
            # Single file -> point directly at it; multiple files -> scan the dir
            if len(entries) == 1:
                single = os.path.join(input_dir, entries[0])
                if os.path.isfile(single):
                    return "local", single
            return "local", input_dir

    return None, None


async def _run_llm_ping() -> int:
    """llm-ping OUTPUT_MODE: curl the LLM's /api/tags endpoint and emit
    a single-line JSON diagnostic. Used by the wizard's "Check worker
    reachability" button to verify that a worker-dispatched container
    can actually reach the configured LLM on whatever network
    SelectNetworkMode landed it on. No source-scan, no LLM call — the
    whole point is to isolate *network* reachability from *LLM
    behaviour*.

    Emits `{"llm_ping": bool, "host": str, "endpoint": str,
    "status_code": int|null, "latency_ms": int, "error": str|null}`
    to stdout as the last JSON block (the API's
    `_extract_albedo_artifacts` scans for the last balanced JSON and
    returns it to the wizard via /generation-artifacts).

    Exit 0 on reachable, 1 on not — so failure is visible in the task
    queue too.
    """
    import time
    import urllib.request
    import urllib.error

    base_url = (os.getenv("LLM_BASE_URL", "") or "").rstrip("/")
    # Prefer a cheap list-models probe over /api/chat so we don't
    # incur token generation just to verify the HTTP path.
    endpoint = f"{base_url}/api/tags" if base_url else ""

    host = ""
    if base_url:
        from urllib.parse import urlparse
        host = urlparse(base_url).hostname or ""

    result = {
        "llm_ping": False,
        "host": host,
        "endpoint": endpoint,
        "status_code": None,
        "latency_ms": 0,
        "error": None,
    }

    if not endpoint:
        result["error"] = "LLM_BASE_URL is empty — nothing to ping."
        print(json.dumps(result))
        return 1

    log(f"Pinging {endpoint} from worker-dispatched container...")
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(endpoint, headers={"User-Agent": "mod-llm-ping/1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result["status_code"] = resp.status
            result["latency_ms"] = int((time.monotonic() - t0) * 1000)
            result["llm_ping"] = 200 <= resp.status < 300
            if not result["llm_ping"]:
                result["error"] = f"HTTP {resp.status}"
    except urllib.error.URLError as e:
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        # urllib wraps socket.gaierror in URLError. Unwrap so the
        # wizard sees "name resolution" vs "connection refused"
        # distinctly — they diagnose very different worker misconfigs.
        cause = getattr(e, "reason", e)
        result["error"] = f"{type(cause).__name__}: {cause}"
    except Exception as e:
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        result["error"] = f"{type(e).__name__}: {e}"

    log(f"llm-ping result: {result}")
    print(json.dumps(result))
    return 0 if result["llm_ping"] else 1


async def main():
    output_mode = os.getenv("OUTPUT_MODE", "").strip().lower()

    # ---- llm-ping mode (short-circuit) ----
    # Dispatched by POST /node_definitions/worker-llm-ping. Verifies
    # the worker-spawned container can reach Ollama / whatever LLM is
    # configured, without running a full source-scan + LLM pass. This
    # is the user-facing "test the plumbing" button.
    if output_mode == "llm-ping":
        sys.exit(await _run_llm_ping())

    # ---- codebase mode — full multi-stage pipeline ----
    # The "big input" path: ingest → scan → AST → embed → map (parallel
    # LLM) → reduce → generate (4 parallel LLM) → validate → refine →
    # optional build verify. Output is the same 4-file MOD node shape
    # as albedo, so the API's existing artifact extractor works.
    # See tools/codebase_pipeline.py for the per-stage details.
    if output_mode == "codebase":
        from tools.codebase_pipeline import run_codebase_pipeline
        task_id = os.getenv("TASK_ID") or "wizard-codebase"
        workspace = os.getenv("CODEBASE_WORKSPACE", "/data/input/codebase")
        sources_env = os.getenv("CODEBASE_SOURCES", "").strip()
        sources = [s for s in sources_env.split(",") if s] if sources_env else None
        inline_body = os.getenv("INLINE_SOURCE_BODY")
        inline_filename = os.getenv("INLINE_SOURCE_FILENAME", "user_script.py")
        user_hints = os.getenv("ADDITIONAL_INSTRUCTIONS") or os.getenv("USER_HINTS")
        do_verify = os.getenv("BUILD_VERIFY", "").lower() in ("1", "true", "yes")
        registry = os.getenv("MOD_REGISTRY", "example.com")
        try:
            llm_config = LLMConfig.from_env()
            llm_config.validate()
        except ValueError as e:
            log(f"ERROR: LLM configuration invalid: {e}")
            sys.exit(1)
        await run_codebase_pipeline(
            task_id=task_id,
            workspace=workspace,
            sources=sources,
            inline_script=inline_body,
            inline_filename=inline_filename,
            user_hints=user_hints,
            llm=llm_config,
            do_build_verify=do_verify,
            registry=registry,
        )
        sys.exit(0)

    # ---- Detect input mode ----
    wrap_base_image = os.getenv("WRAP_BASE_IMAGE", "").strip()

    if wrap_base_image:
        mode, source = "wrap_image", wrap_base_image
    else:
        mode, source = _detect_input_mode()

    if mode is None:
        log("ERROR: No input source found.")
        log("")
        log("Provide one of:")
        log("  REPO_URL=https://github.com/user/repo   (clone a git repo)")
        log("  SOURCE_DIR=/path/to/folder               (local directory)")
        log("  WRAP_BASE_IMAGE=image:tag                (wrap an existing image)")
        log("  Mount files to /data/input/               (auto-detected)")
        log("")
        log("LLM config is also required: LLM_PROVIDER, LLM_MODEL, etc.")
        sys.exit(1)

    additional_instructions = os.getenv("ADDITIONAL_INSTRUCTIONS", "").strip()
    output_dir = os.getenv("OUTPUT_DIR", "/data/output").strip()

    # ---- Validate LLM config ----
    try:
        llm_config = LLMConfig.from_env()
        llm_config.validate()
    except ValueError as e:
        log(f"ERROR: LLM configuration invalid: {e}")
        log(f"Supported providers: {', '.join(sorted(PROVIDERS.keys()))}")
        sys.exit(1)

    log(f"Provider: {llm_config.provider} / {llm_config.model}")
    log(f"Mode:     {mode}")
    log(f"Source:   {source}")
    if additional_instructions:
        log(f"Extra:    {additional_instructions[:80]}{'...' if len(additional_instructions) > 80 else ''}")
    log("")

    # ---- Get source code analysis ----
    project_name = None

    if mode == "wrap_image":
        # Wrap an existing Docker image as a MOD node.
        #
        # Two sub-cases that share this branch:
        #
        #   (a) Bare image wrap: the caller gave us just WRAP_BASE_IMAGE
        #       and nothing else. There's no user source to analyse —
        #       the LLM gets the image ref + node hint and generates
        #       a generic entrypoint.
        #
        #   (b) Script-plus-base-image (the wizard's common path): the
        #       user pasted a script AND picked a base image. The
        #       script was materialised to /data/input/ by entrypoint.sh
        #       before app.py ran. We MUST scan it and feed it to
        #       analysis + generation — otherwise the LLM produces a
        #       placeholder "process input files" entrypoint that never
        #       actually runs the user's code (observed on the BLIP
        #       smoke test, 2026-04-22).
        log("Step 1/2: Preparing image wrap request...")
        image_ref = source
        node_hint = os.getenv("NODE_NAME", "").strip() or image_ref.split(":")[0].split("/")[-1]
        project_name = node_hint

        # Was a script inlined alongside the image? entrypoint.sh
        # writes it to /data/input/ BEFORE launching app.py.
        inline_scan = None
        if os.path.isdir("/data/input") and any(os.scandir("/data/input")):
            try:
                inline_scan = await scan_source("/data/input")
                if not inline_scan.get("success"):
                    log(f"  warn: /data/input scan failed: {inline_scan.get('error')}")
                    inline_scan = None
            except Exception as e:
                log(f"  warn: /data/input scan errored: {e}")
                inline_scan = None

        summary_lines = [
            "Wrap existing Docker image as a MOD_CORE workflow node.",
            f"Base image: {image_ref}",
            f"Node name: {node_hint}",
            f"The Dockerfile MUST use 'FROM {image_ref}' as its base.",
        ]
        if inline_scan:
            summary_lines.append(
                f"The Dockerfile + entrypoint.sh MUST install this script's "
                f"dependencies and invoke it against files in /data/input/, "
                f"writing results to /data/output/. See the source below."
            )
            summary_lines.append(inline_scan.get("summary", "").strip())
            tree = inline_scan.get("tree") or "(user script materialised to /data/input/)"
            content = inline_scan.get("content") or ""
        else:
            summary_lines.append(
                "The entrypoint.sh should invoke whatever tool this image "
                "provides against files in /data/input/ and write results "
                "to /data/output/."
            )
            tree = "(no source files — wrapping an existing image)"
            content = ""
        summary = "\n".join(summary_lines)
    elif mode == "repo":
        # Clone + gitingest (existing flow)
        log("Step 1/3: Cloning repository...")
        clone_result = await clone_repo_tool(source, target_dir="/data/repos")

        if not clone_result["success"]:
            log(f"ERROR: Failed to clone: {clone_result['error']}")
            sys.exit(1)

        log(f"  Cloned {clone_result['repo_name']} ({clone_result['repo_size_mb']} MB, {clone_result['file_count']} files)")
        project_name = clone_result["repo_name"]

        log("Step 2/3: Analyzing repository...")
        ingest_result = await gitingest_tool(clone_result["local_path"])

        if not ingest_result["success"]:
            log(f"ERROR: Failed to analyze: {ingest_result['error']}")
            sys.exit(1)

        log("  Analysis complete.")
        summary = ingest_result["summary"]
        tree = ingest_result["tree"]
        content = ingest_result["content"]
    else:
        # Local file or directory
        log("Step 1/2: Scanning source files...")
        scan_result = await scan_source(source)

        if not scan_result["success"]:
            log(f"ERROR: Failed to scan: {scan_result['error']}")
            sys.exit(1)

        log(f"  Found {scan_result['file_count']} file(s), {scan_result['total_size']:,} bytes")

        # Derive project name from path
        if os.path.isfile(source):
            project_name = os.path.splitext(os.path.basename(source))[0]
        else:
            project_name = os.path.basename(source.rstrip("/"))

        summary = scan_result["summary"]
        tree = scan_result["tree"]
        content = scan_result["content"]

    output_mode = os.getenv("OUTPUT_MODE", "dockerfile").strip().lower()

    # ---- Generate ----
    step = "Step 3/3" if mode == "repo" else "Step 2/2"

    if output_mode == "analysis-only":
        log(f"{step}: Running wizard analysis (no Dockerfile/entrypoint output)...")
        from tools.analyze_node import analyze_node_tool

        analysis = await analyze_node_tool(
            source_summary=summary,
            source_tree=tree,
            source_content=content,
            project_name=project_name or "node",
            source_type=("repo" if mode == "repo" else "script"),
        )

        os.makedirs(output_dir, exist_ok=True)
        analysis_path = os.path.join(output_dir, "analysis.json")
        with open(analysis_path, "w") as f:
            json.dump(analysis, f, indent=2)
        log(f"Wrote: {analysis_path}")

        result = {
            "output_mode": "analysis-only",
            "analysis": analysis,
            "source": {
                "mode": mode,
                "path": source,
                "project_name": project_name,
            },
            "llm": {
                "provider": llm_config.provider,
                "model": llm_config.model,
            },
        }
        # Sentinel-wrapped print for O(n) downstream extraction. Mode
        # is "analysis-only" but kept under the same sentinel name so
        # the extractor only needs to look in one place.
        print("=====MOD_ALBEDO_ARTIFACTS_BEGIN=====")
        print(json.dumps(result, indent=2))
        print("=====MOD_ALBEDO_ARTIFACTS_END=====")
        return

    if output_mode == "albedo":
        log(f"{step}: Generating Albedo-pattern artifacts (Dockerfile + entrypoint.sh + build-and-push.sh)...")

        image_name = os.getenv("IMAGE_NAME", "").strip() or (project_name or "node").lower().replace(" ", "-")
        tenant_id = os.getenv("TENANT_ID", "").strip() or "0"
        registry_target = os.getenv("REGISTRY_TARGET", "local").strip().lower()
        local_registry = os.getenv("LOCAL_REGISTRY", "registry:5000").strip()
        harbor_registry = os.getenv("HARBOR_REGISTRY", "ghcr.io").strip()
        harbor_project = os.getenv("HARBOR_PROJECT", "mod_core").strip()

        # Multi-file input (Phase 1 variant-aware wizard). Parsed
        # here so create_albedo_pattern_tool can fan the adapter
        # across every (file × variant) instead of wrapping the
        # single-paste body into a 1-element list.
        #
        # inline_filename is read HERE (not borrowed from the codebase
        # branch's local) — the albedo path didn't previously define
        # that variable at all, so reaching for it was an
        # UnboundLocalError. Env var is the truth for both paths.
        import json as _json
        albedo_inline_filename = os.getenv("INLINE_SOURCE_FILENAME", "").strip() or "user_script.py"
        source_files_env = os.getenv("INLINE_SOURCE_FILES", "").strip()
        source_files = None
        if source_files_env:
            try:
                source_files = _json.loads(source_files_env)
                if not isinstance(source_files, list):
                    source_files = None
            except Exception as e:
                log(f"WARN: INLINE_SOURCE_FILES is not valid JSON: {e}")
                source_files = None

        container_result = await create_albedo_pattern_tool(
            source_summary=summary,
            source_tree=tree,
            source_content=content,
            project_name=project_name or "node",
            image_name=image_name,
            tenant_id=tenant_id,
            registry_target=registry_target,
            local_registry=local_registry,
            harbor_registry=harbor_registry,
            harbor_project=harbor_project,
            additional_instructions=additional_instructions or None,
            source_filename=albedo_inline_filename,
            source_files=source_files,
        )

        if not container_result["success"]:
            log(f"ERROR: Generation failed: {container_result['error']}")
            sys.exit(1)

        log("")
        log("Done!")
        log("")

        dockerfile = container_result.get("dockerfile", "")
        entrypoint_sh = container_result.get("entrypoint_sh", "")
        build_and_push_sh = container_result.get("build_and_push_sh", "")
        reasoning = container_result.get("base_image_reasoning", "")
        tech_stack = container_result.get("technology_stack", "")
        notes = container_result.get("additional_notes", "")
        full_image_reference = container_result.get("full_image_reference", "")

        os.makedirs(output_dir, exist_ok=True)

        # Hero + entrypoint + build-and-push
        for filename, content_str, executable in [
            ("Dockerfile", dockerfile, False),
            ("entrypoint.sh", entrypoint_sh, True),
            ("build-and-push.sh", build_and_push_sh, True),
        ]:
            path = os.path.join(output_dir, filename)
            with open(path, "w") as f:
                f.write(content_str)
            if executable:
                os.chmod(path, 0o755)
            log(f"Wrote: {path}")

        # Variant Dockerfiles — one per compatible GPU backend. The hero
        # variant is also emitted as Dockerfile.<name> so build-and-push
        # can iterate a single for-loop and treat all variants uniformly.
        variants = container_result.get("dockerfile_variants") or {}
        for variant_name, variant_df in variants.items():
            path = os.path.join(output_dir, f"Dockerfile.{variant_name}")
            with open(path, "w") as f:
                f.write(variant_df)
            log(f"Wrote: {path}")

        meta = {
            "mode": mode,
            "source": source,
            "project_name": project_name,
            "image_name": image_name,
            "tenant_id": tenant_id,
            "registry_target": registry_target,
            "full_image_reference": full_image_reference,
            "llm_provider": llm_config.provider,
            "llm_model": llm_config.model,
            "technology_stack": tech_stack,
            "base_image_reasoning": reasoning,
            "additional_notes": notes,
            "output_mode": "albedo",
        }
        if mode == "repo":
            meta["repo_url"] = source

        meta_path = os.path.join(output_dir, "generation-info.json")
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)
        log(f"Wrote: {meta_path}")

        # Piggyback: run the wizard analysis while we have the LLM
        # warm and write analysis.json alongside the Dockerfile. Any
        # downstream consumer (reviewer UI, audit log, auto-approval)
        # now has the same structured data the sync
        # POST /analyze-script endpoint returns. Best-effort — a
        # failure here doesn't break the Dockerfile pipeline.
        analysis_out: dict = {}
        try:
            from tools.analyze_node import analyze_node_tool
            analysis_out = await analyze_node_tool(
                source_summary=summary,
                source_tree=tree,
                source_content=content,
                project_name=project_name or "node",
                source_type=("repo" if mode == "repo" else "script"),
            )
            analysis_path = os.path.join(output_dir, "analysis.json")
            with open(analysis_path, "w") as f:
                json.dump(analysis_out, f, indent=2)
            log(f"Wrote: {analysis_path}")
        except Exception as exc:
            log(f"  WARN: analysis side-output failed (non-fatal): {exc}")

        result = {
            "output_mode": "albedo",
            "dockerfile": dockerfile,
            "entrypoint_sh": entrypoint_sh,
            "build_and_push_sh": build_and_push_sh,
            "dockerfile_variants": container_result.get("dockerfile_variants") or {},
            "gpu_variants": container_result.get("gpu_variants") or {},
            # P9f.1 — per-variant adapter output. Without these the
            # API's /generation-artifacts/{id} parser has nothing to
            # forward, so the Wizard's per-variant tabs sit on
            # "pending…" forever for non-hero variants.
            "source_variants": container_result.get("source_variants") or {},
            "variant_adaptations": container_result.get("variant_adaptations") or {},
            "source_files": container_result.get("source_files") or [],
            "hero_variant": container_result.get("hero_variant") or "",
            # P9a — kernel_porter outputs (deep-CUDA repos only).
            "port_plans": container_result.get("port_plans") or {},
            "port_incompatible_reasons": container_result.get("port_incompatible_reasons") or {},
            "port_not_recommended": container_result.get("port_not_recommended") or {},
            "image_name": image_name,
            "full_image_reference": full_image_reference,
            "technology_stack": tech_stack,
            "base_image_reasoning": reasoning,
            "additional_notes": notes,
            "analysis": analysis_out,
            "source": {
                "mode": mode,
                "path": source,
                "project_name": project_name,
            },
            "llm": {
                "provider": llm_config.provider,
                "model": llm_config.model,
            },
        }
        # Sentinel-wrapped print so the API's extractor can locate the
        # artifact JSON in O(n) instead of walking every `{` backwards
        # through a multi-MB log_tail. Stage repro: TRELLIS.2 task 11570
        # had a 4,478,585-char log_tail; the old brute-force extractor
        # took 0.86s per UI poll, slow enough that the wizard UI fell
        # behind and the user saw "Generating" for 30+ minutes on a
        # build that actually finished in ~2.
        artifact_json = json.dumps(result, indent=2)
        print("=====MOD_ALBEDO_ARTIFACTS_BEGIN=====")
        print(artifact_json)
        print("=====MOD_ALBEDO_ARTIFACTS_END=====")
        # Persist a structured copy alongside the per-component files
        # the worker already uploads to MinIO. Downstream consumers
        # (audit, reviewer UI, post-mortem) get a single canonical
        # bundle without re-parsing logs.
        try:
            artifacts_path = os.path.join(output_dir, "albedo-artifacts.json")
            with open(artifacts_path, "w") as f:
                f.write(artifact_json)
            log(f"Wrote: {artifacts_path}")
        except Exception as exc:
            log(f"  WARN: albedo-artifacts.json write failed (non-fatal): {exc}")
        return

    # ---- Default: Dockerfile-only mode (unchanged legacy behavior) ----
    log(f"{step}: Generating Dockerfile...")

    container_result = await create_container_tool(
        source_summary=summary,
        source_tree=tree,
        source_content=content,
        project_name=project_name,
        additional_instructions=additional_instructions or None,
    )

    if not container_result["success"]:
        log(f"ERROR: Generation failed: {container_result['error']}")
        sys.exit(1)

    log("")
    log("Done!")
    log("")

    # ---- Output ----
    dockerfile = container_result.get("dockerfile", "")
    docker_compose = container_result.get("docker_compose_suggestion", "")
    reasoning = container_result.get("base_image_reasoning", "")
    tech_stack = container_result.get("technology_stack", "")
    notes = container_result.get("additional_notes", "")

    # Write to output dir
    os.makedirs(output_dir, exist_ok=True)

    dockerfile_path = os.path.join(output_dir, "Dockerfile")
    with open(dockerfile_path, "w") as f:
        f.write(dockerfile)
    log(f"Wrote: {dockerfile_path}")

    if docker_compose:
        compose_path = os.path.join(output_dir, "docker-compose.yml")
        with open(compose_path, "w") as f:
            f.write(docker_compose)
        log(f"Wrote: {compose_path}")

    # Write metadata
    meta = {
        "mode": mode,
        "source": source,
        "project_name": project_name,
        "llm_provider": llm_config.provider,
        "llm_model": llm_config.model,
        "technology_stack": tech_stack,
        "base_image_reasoning": reasoning,
        "additional_notes": notes,
    }
    if mode == "repo":
        meta["repo_url"] = source

    meta_path = os.path.join(output_dir, "generation-info.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    log(f"Wrote: {meta_path}")

    # Also print JSON to stdout for piping
    result = {
        "output_mode": "dockerfile",
        "dockerfile": dockerfile,
        "docker_compose": docker_compose,
        "technology_stack": tech_stack,
        "base_image_reasoning": reasoning,
        "additional_notes": notes,
        "source": {
            "mode": mode,
            "path": source,
            "project_name": project_name,
        },
        "llm": {
            "provider": llm_config.provider,
            "model": llm_config.model,
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

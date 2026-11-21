"""
Return/Export Container — creates the rich output folder structure.

Ported from Return_EXPORT_Dag.py. Runs as a Docker container on the
Go worker. Calls existing API endpoints to create:
- Numbered node folders (1_input, 2_albedo, etc.)
- Processed files with proper naming
- file_hashes.txt, workflow_info.json, README.txt, settings.json
- errors.log (if any failures)
- preview image

Input: Environment variables + /data/input/config.json (written by worker)
Output: /data/output/export_result.json
"""

import json
import os
import sys
from datetime import datetime

# worker-mediated-node-credentials: auth headers now come from the shared helper,
# which fetches short-lived run_task tokens from the worker on demand (no static
# API_KEY in this container). Drop-in replacement for the old inline functions.
from mod_node_auth import get_headers, get_service_headers, request_with_refresh  # noqa: E402


API_URL = os.environ.get("API_URL", "http://api:80")
WORKFLOW_RUN_ID = int(os.environ.get("WORKFLOW_RUN_ID", "0"))
TENANT_ID = int(os.environ.get("TENANT_ID", "0"))
INPUT_COG_ID = int(os.environ.get("INPUT_COG_ID", "0"))
WORKFLOW_NAME = os.environ.get("WORKFLOW_NAME", "workflow")
ORIGINAL_FILENAME = os.environ.get("ORIGINAL_FILENAME", "processed_file.jpg")

# NODE_OUTPUTS is a JSON string of all parent outputs
NODE_OUTPUTS_JSON = os.environ.get("NODE_OUTPUTS", "{}")


def log_api_error(context: str, resp):
    """Log full error details from an API response."""
    print(f"   ERROR [{context}]: HTTP {resp.status_code}")
    try:
        print(f"   Response: {resp.text[:500]}")
    except Exception:
        pass


def api_get(path, timeout=30):
    import requests
    resp = requests.get(f"{API_URL}/v1{path}", headers=get_headers(), timeout=timeout)
    return resp


def api_post_form(path, data, timeout=60):
    import requests
    try:
        return requests.post(f"{API_URL}/v1{path}", data=data, headers=get_service_headers(), timeout=timeout)
    except Exception as exc:
        # Transport error (connection refused, timeout, ...): synthesize a
        # non-2xx response so the caller's existing handling logs + counts the
        # upload as failed (fail-loud) and keeps attempting the rest, instead
        # of crashing mid-loop with a traceback.
        class _TransportFailure:
            status_code = 599
            text = ""

            def json(self):
                return {}

        failure = _TransportFailure()
        failure.text = f"transport error: {exc}"
        return failure


upload_failures = []


def note_upload_failure(context: str, path: str, resp=None, exception: str = ""):
    """Record a failed REQUIRED upload (processed file or text file).

    Tim (11-19): a failed upload must NOT let the run complete with an empty
    results folder. Every file is still attempted (the loops keep going); the
    failures are counted here and main() exits non-zero at the end so the node
    (and the workflow run) is marked FAILED with a clear message in the node log.
    create-preview failures never go through this helper (preview stays a warning).
    """
    detail = f"HTTP {resp.status_code}" if resp is not None else exception
    upload_failures.append({"context": context, "path": path, "detail": detail})


def main():
    import requests

    started_at = datetime.utcnow()

    print(f"\n{'='*70}")
    print(f"RETURN EXPORT - OUTPUT STRUCTURE CREATION")
    print(f"{'='*70}")
    print(f"Workflow Run: {WORKFLOW_RUN_ID}")
    print(f"Tenant: {TENANT_ID}")
    print(f"Input Cog: {INPUT_COG_ID}")
    print(f"Workflow: {WORKFLOW_NAME}")
    print()

    # =========================================================================
    # STEP 1: Create output folder
    # =========================================================================
    print("Step 1: Creating output folder...")

    resp = request_with_refresh(
        "POST",
        f"{API_URL}/v1/assets/service/workflows/{WORKFLOW_RUN_ID}/create-output-folder",
        service=True,
        params={"input_cog_id": INPUT_COG_ID, "workflow_name": WORKFLOW_NAME},
        timeout=30
    )

    if resp.status_code not in (200, 201):
        log_api_error("create output folder", resp)
        sys.exit(1)

    folder_result = resp.json()
    output_folder_id = folder_result["cog_id"]
    print(f"   Created: {folder_result.get('name', 'output')} (cog_id: {output_folder_id})")

    # =========================================================================
    # STEP 2: Fetch all node runs
    # =========================================================================
    print("\nStep 2: Fetching node runs...")

    resp = request_with_refresh(
        "GET",
        f"{API_URL}/v1/runs/service/workflow/{WORKFLOW_RUN_ID}",
        timeout=30
    )
    if resp.status_code != 200:
        log_api_error("fetch node runs", resp)
        sys.exit(1)

    all_node_runs = resp.json()
    print(f"   Found {len(all_node_runs)} node runs")

    # =========================================================================
    # STEP 3: Collect and order files from all nodes
    # =========================================================================
    print("\nStep 3: Collecting output files...")

    ordered_nodes = []
    for node_run in all_node_runs:
        workflow_node = node_run.get("workflow_node", {}) or {}
        node_name = workflow_node.get("graph_node_id", "unknown")
        node_kind = workflow_node.get("node_kind", "") or ""

        # Skip export nodes
        if node_kind.endswith("_export"):
            print(f"   Skipping export node: {node_name}")
            continue

        # Determine category from the OUTPUT file extension when
        # available — that's what the user sees in the export bundle
        # ("2_pdf" makes sense, "2_png" lies when the node converted to
        # PDF). Pre-fix this used `node_kind.split("_")[0]` which for
        # converter nodes like "png_to_pdf_converter" picked "png" (the
        # INPUT side) and produced misleading folder names + filenames.
        #
        # Carve-out: `image_input` keeps the special-case "input" label
        # — using the raw "png" / "jpg" extension for the input folder
        # buries the fact that it's the source file.
        if node_kind == "image_input":
            category = "input"
        else:
            # Peek at the first output's file_extension. If the node
            # produced an output (success path) this is the most
            # accurate descriptor. If outputs is empty (failure path)
            # fall back to the legacy node_kind heuristic so the
            # bundle still labels the empty folder usefully.
            outputs = node_run.get("outputs") or []
            first_ext = ""
            if isinstance(outputs, list) and outputs and isinstance(outputs[0], dict):
                first_ext = (outputs[0].get("file_extension") or "").lower().lstrip(".")
            if first_ext:
                category = first_ext
            elif node_kind and "_" in node_kind:
                category = node_kind.split("_")[0].lower()
            elif node_kind:
                category = node_kind.lower()
            else:
                category = "unknown"

        ordered_nodes.append((0, category, node_run, node_name))

    # Sort by started_at timestamp (actual execution order, not node ID number)
    def sort_key(item):
        nr = item[2]
        started = nr.get("started_at") or nr.get("created_at") or ""
        return started

    ordered_nodes.sort(key=sort_key)

    # Assign sequential order numbers (1, 2, 3, ...)
    ordered_nodes = [(i + 1, n[1], n[2], n[3]) for i, n in enumerate(ordered_nodes)]

    total_nodes = len(ordered_nodes)
    folder_pad_width = len(str(total_nodes)) if total_nodes >= 10 else 1

    # Collect file items
    all_file_items = []
    node_file_counts = {}
    node_errors = {}

    for node_order, category, node_run, node_name in ordered_nodes:
        node_status = node_run.get("status", "unknown")

        # Track errors
        if node_status in ("failure", "failed"):
            node_errors[f"{node_order}_{category}"] = {
                "node_name": node_name,
                "category": category,
                "order": node_order,
                "error": node_run.get("error_message", "Unknown error"),
                "started_at": node_run.get("started_at"),
                "completed_at": node_run.get("completed_at"),
            }

        # Extract outputs
        node_outputs = node_run.get("outputs", {})
        if not node_outputs:
            if node_status in ("success", "completed"):
                print(f"   WARNING: Node '{node_name}' succeeded but has no outputs")
            continue

        # Normalize outputs to list
        file_items = []
        if isinstance(node_outputs, list):
            file_items = node_outputs
        elif isinstance(node_outputs, dict):
            if "outputs" in node_outputs:
                file_items = node_outputs["outputs"]
                if isinstance(file_items, dict):
                    file_items = [file_items]
            elif "file_path" in node_outputs or "location" in node_outputs:
                file_items = [node_outputs]
            else:
                for value in node_outputs.values():
                    if isinstance(value, list):
                        file_items.extend(value)
                    elif isinstance(value, dict):
                        file_items.append(value)

        node_key = f"{node_order}_{category}"
        for item in file_items:
            if isinstance(item, dict):
                file_path = item.get("file_path") or item.get("location") or ""
                content_hash = item.get("content_hash", "")
            else:
                file_path = str(item)
                content_hash = ""

            # If no file_path but we have a content_hash, build the path
            if not file_path and content_hash:
                bucket = f"tenant-{TENANT_ID}-files"
                file_path = f"{bucket}/{content_hash}"

            if not file_path:
                continue

            # Parse MinIO URI: minio://bucket/hash -> hash
            parsed_path = file_path
            for prefix in ("minio://", "s3://"):
                if parsed_path.startswith(prefix):
                    parsed_path = parsed_path[len(prefix):]
                    break

            if not content_hash and "/" in parsed_path:
                content_hash = parsed_path.split("/")[-1]

            node_file_counts[node_key] = node_file_counts.get(node_key, 0) + 1
            all_file_items.append({
                "node_order": node_order,
                "category": category,
                "node_name": node_name,
                "parsed_path": parsed_path,
                "content_hash": content_hash,
                "file_extension": item.get("file_extension", "") if isinstance(item, dict) else "",
            })

    # Calculate padding widths
    file_pad_widths = {}
    for node_key, count in node_file_counts.items():
        file_pad_widths[node_key] = len(str(count)) if count >= 10 else (0 if count == 1 else 1)

    print(f"   Collected {len(all_file_items)} file(s) from {total_nodes} node(s)")

    # =========================================================================
    # STEP 4: Upload/export files via API
    # =========================================================================
    print(f"\nStep 4: Exporting {len(all_file_items)} file(s)...")

    uploaded_files = []
    total_file_size = 0
    file_hash_records = []
    node_sequence_counters = {}

    for idx, item in enumerate(all_file_items):
        node_order = item["node_order"]
        category = item["category"]
        node_key = f"{node_order}_{category}"

        folder_name = f"{str(node_order).zfill(folder_pad_width)}_{category}"

        node_sequence_counters[node_key] = node_sequence_counters.get(node_key, 0) + 1
        file_sequence = node_sequence_counters[node_key]
        node_file_count = node_file_counts.get(node_key, 1)
        file_pad_width = file_pad_widths.get(node_key, 0)

        print(f"   [{idx+1}/{len(all_file_items)}] Exporting to {folder_name}/...")

        # Use the file's actual extension (from sidecar upload) if original has none
        export_filename = ORIGINAL_FILENAME
        file_ext = item.get("file_extension", "")
        if file_ext and '.' not in ORIGINAL_FILENAME:
            export_filename = f"{ORIGINAL_FILENAME}.{file_ext}"

        # `output_extension`: the extension we want the EXPORTED file to
        # carry. The server-side handler historically built the final
        # filename as `{base}_{category}.{original_ext}` — but for a
        # converter node the original input was a different format
        # (e.g. PNG input → PDF output) and using the input's extension
        # ends up writing PDF bytes to a `.png` file. Pass the actual
        # output extension so the handler can prefer it.
        resp = api_post_form(
            "/assets/service/workflows/upload-processed-file",
            data={
                "output_folder_id": output_folder_id,
                "file_path": item["parsed_path"],
                "original_filename": export_filename,
                "category": category,
                "node_order": node_order,
                "folder_pad_width": folder_pad_width,
                "file_sequence": file_sequence,
                "file_count": node_file_count,
                "file_pad_width": file_pad_width,
                "output_extension": file_ext,
                "tenant_id": TENANT_ID,
            }
        )

        if resp.status_code not in (200, 201):
            log_api_error(f"export file to {folder_name}", resp)
            note_upload_failure(f"export file to {folder_name}", "upload-processed-file", resp=resp)
            continue

        result = resp.json()
        uploaded_files.append(result.get("filename", ""))
        file_size = result.get("size_bytes", 0)
        total_file_size += file_size
        actual_hash = result.get("content_hash", item["content_hash"]) or "unknown"

        file_hash_records.append({
            "folder": folder_name,
            "filename": result.get("filename", ""),
            "hash": actual_hash,
        })

        print(f"   Exported: {folder_name}/{result.get('filename', '?')} ({file_size/(1024*1024):.2f} MB)")

    # =========================================================================
    # STEP 5: Create metadata files
    # =========================================================================
    print(f"\nStep 5: Creating metadata files...")

    completed_at = datetime.utcnow()

    # --- file_hashes.txt ---
    hashes_content = f"# Workflow Run: {WORKFLOW_RUN_ID}\n"
    hashes_content += f"# Generated: {completed_at.strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
    for record in file_hash_records:
        hashes_content += f"{record['folder']}/{record['filename']}\n"
        hashes_content += f"  SHA256: {record['hash']}\n\n"

    _upload_text_file("file_hashes.txt", hashes_content, output_folder_id)

    # --- workflow_info.json ---
    # Fetch per-node resource usage (cpu/ram/gpu/vram integrals + peaks).
    # Service endpoint returns one entry per node_run keyed by graph_node_id.
    resources_by_graph_id = {}
    try:
        resp = api_get(f"/runs/service/workflow/{WORKFLOW_RUN_ID}/resources")
        if resp.status_code == 200:
            for entry in resp.json() or []:
                gid = entry.get("graph_node_id")
                if gid:
                    resources_by_graph_id[gid] = entry
        else:
            log_api_error("fetch node-run resources", resp)
    except Exception as e:
        print(f"   WARNING: Failed to fetch resource metrics: {e}")

    def _round(v, n=4):
        try:
            return round(float(v), n) if v is not None else None
        except (TypeError, ValueError):
            return None

    workflow_info = {
        "workflow_name": WORKFLOW_NAME,
        "workflow_run_id": WORKFLOW_RUN_ID,
        "tenant_id": TENANT_ID,
        "input_cog_id": INPUT_COG_ID or None,
        "input_filename": ORIGINAL_FILENAME,
        "export_type": "Return Assets Export",
        "started_at": started_at.isoformat() + "Z",
        "completed_at": completed_at.isoformat() + "Z",
        "duration_seconds": _round((completed_at - started_at).total_seconds(), 3),
        "status": "success" if not node_errors else "partial",
        "totals": {
            "files": len(uploaded_files),
            "size_bytes": total_file_size,
            "size_mb": _round(total_file_size / (1024 * 1024), 2),
            "cpu_core_seconds": 0.0,
            "ram_gb_seconds": 0.0,
            "gpu_seconds": 0.0,
            "vram_gb_seconds": 0.0,
            "disk_read_mb": 0.0,
            "disk_write_mb": 0.0,
            "wall_clock_seconds": 0.0,
        },
        "nodes": [],
    }

    for node_order, category, node_run, node_name in ordered_nodes:
        node_key = f"{node_order}_{category}"
        node_outputs = node_run.get("outputs") or []
        # Per-node file digest summary (what landed under N_category/).
        node_files = []
        for rec in file_hash_records:
            if rec["folder"] == f"{str(node_order).zfill(folder_pad_width)}_{category}":
                node_files.append({"filename": rec["filename"], "sha256": rec["hash"]})

        resources = resources_by_graph_id.get(node_name, {})
        # Roll into totals.
        for fld in ("cpu_core_seconds", "ram_gb_seconds", "gpu_seconds",
                    "vram_gb_seconds", "disk_read_mb", "disk_write_mb",
                    "wall_clock_seconds"):
            v = resources.get(fld) or 0
            try:
                workflow_info["totals"][fld] += float(v)
            except (TypeError, ValueError):
                pass

        workflow_info["nodes"].append({
            "order": node_order,
            "type": category,
            "name": node_name,
            "node_kind": resources.get("node_kind"),
            "status": node_run.get("status", "unknown"),
            "started_at": node_run.get("started_at"),
            "completed_at": node_run.get("completed_at"),
            "duration_seconds": _round(node_run.get("duration_seconds")),
            "files_count": node_file_counts.get(node_key, 0),
            "output_files": node_files,
            "parameters": node_run.get("parameters") or {},
            "outputs_raw": node_outputs,
            "resources": {
                "worker": resources.get("worker"),
                "sample_count": resources.get("sample_count", 0),
                "wall_clock_seconds": _round(resources.get("wall_clock_seconds")),
                "cpu_core_seconds": _round(resources.get("cpu_core_seconds")),
                "ram_gb_seconds": _round(resources.get("ram_gb_seconds")),
                "gpu_seconds": _round(resources.get("gpu_seconds")),
                "vram_gb_seconds": _round(resources.get("vram_gb_seconds")),
                "disk_read_mb": _round(resources.get("disk_read_mb")),
                "disk_write_mb": _round(resources.get("disk_write_mb")),
                "peak_cpu_percent": _round(resources.get("peak_cpu_percent")),
                "peak_ram_mb": _round(resources.get("peak_ram_mb"), 1),
                "peak_vram_mb": _round(resources.get("peak_vram_mb"), 1),
                "peak_gpu_util_pct": _round(resources.get("peak_gpu_util_pct")),
                "peak_gpu_temp_c": _round(resources.get("peak_gpu_temp_c"), 1),
            },
            "cost_credits": node_run.get("cost", 0) or 0,
        })

    # Round totals once at the end so the integrand stays precise.
    for fld in ("cpu_core_seconds", "ram_gb_seconds", "gpu_seconds",
                "vram_gb_seconds", "disk_read_mb", "disk_write_mb",
                "wall_clock_seconds"):
        workflow_info["totals"][fld] = _round(workflow_info["totals"][fld])

    total_cost = sum(n[2].get("cost", 0) or 0 for n in ordered_nodes)
    workflow_info["totals"]["cost_credits"] = total_cost

    if node_errors:
        workflow_info["errors"] = [
            {
                "order": e["order"],
                "type": e["category"],
                "name": e["node_name"],
                "error": e["error"],
                "started_at": e.get("started_at"),
                "completed_at": e.get("completed_at"),
            }
            for e in node_errors.values()
        ]

    _upload_text_file("workflow_info.json", json.dumps(workflow_info, indent=2), output_folder_id)

    # --- errors.log ---
    if node_errors:
        errors_content = f"# Workflow Run: {WORKFLOW_RUN_ID}\n"
        errors_content += f"# Generated: {completed_at.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        errors_content += f"# Total Errors: {len(node_errors)}\n\n"
        for node_key, err in node_errors.items():
            errors_content += f"[{err['order']}_{err['category']}] {err['node_name']}\n"
            errors_content += f"  Status: FAILURE\n"
            errors_content += f"  Error: {err['error']}\n\n"
        _upload_text_file("errors.log", errors_content, output_folder_id)

    # --- settings.json ---
    settings = {}
    for node_order, category, node_run, node_name in ordered_nodes:
        folder_name = f"{str(node_order).zfill(folder_pad_width)}_{category}"
        params = node_run.get("parameters", {})
        if params:
            settings[folder_name] = params
    if settings:
        _upload_text_file("settings.json", json.dumps(settings, indent=2), output_folder_id)

    # --- README.txt ---
    readme = f"""Workflow: {WORKFLOW_NAME}
Export Type: Return Assets Export
Processed: {completed_at.strftime('%B %d, %Y at %I:%M %p')}
Input: {ORIGINAL_FILENAME}
Output: {len(uploaded_files)} files across {total_nodes} processing stages
Total Size: {total_file_size / (1024 * 1024):.2f} MB
Total Cost: {total_cost} credits

Folders are numbered in processing order.
See file_hashes.txt for verification checksums.
See workflow_info.json for detailed metadata.
"""
    if node_errors:
        readme += f"\n{len(node_errors)} node(s) had errors. See errors.log for details.\n"

    _upload_text_file("README.txt", readme, output_folder_id)

    # --- preview ---
    if file_hash_records:
        first = file_hash_records[0]
        if first["hash"] and first["hash"] != "unknown":
            ext = first["filename"].rsplit(".", 1)[-1] if "." in first["filename"] else "jpg"
            resp = api_post_form(
                "/assets/service/workflows/create-preview",
                data={
                    "output_folder_id": output_folder_id,
                    "source_hash": first["hash"],
                    "preview_filename": f"preview.{ext}",
                    "tenant_id": TENANT_ID,
                }
            )
            if resp.status_code in (200, 201):
                print(f"   Created preview.{ext}")
            else:
                log_api_error("create preview", resp)

    # =========================================================================
    # STEP 6: Write result
    # =========================================================================
    result = {
        "status": "success",
        "output_folder_id": output_folder_id,
        "files_exported": len(uploaded_files),
        "total_size_bytes": total_file_size,
        "errors": len(node_errors),
    }

    with open("/data/output/export_result.json", "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nExport complete: {len(uploaded_files)} files, {total_file_size/(1024*1024):.2f} MB")
    if node_errors:
        print(f"   {len(node_errors)} node(s) had errors")

    # --- fail loud on upload errors (owner 11-19) ---
    # A required upload (processed file / text file) that returned non-2xx used to
    # be logged and skipped, so the run COMPLETED with an empty results folder.
    # Every file is still attempted above; now, if any required upload failed,
    # print one clear line (count + first failure's status and path, never tokens)
    # and exit non-zero so the node/run is marked FAILED with it in the node log.
    if upload_failures:
        first = upload_failures[0]
        print(
            f"EXPORT FAILED: {len(upload_failures)} required upload(s) failed "
            f"(first: {first['detail']} on {first['path']} - {first['context']}); "
            f"{len(uploaded_files)} of {len(all_file_items)} file(s) exported"
        )
        sys.exit(1)


def _upload_text_file(filename, content, output_folder_id):
    """Upload a text metadata file to the output folder."""
    resp = api_post_form(
        "/assets/service/workflows/upload-text-file",
        data={
            "output_folder_id": output_folder_id,
            "filename": filename,
            "content": content,
            "tenant_id": TENANT_ID,
        }
    )
    if resp.status_code in (200, 201):
        print(f"   Created {filename}")
    else:
        log_api_error(f"create {filename}", resp)
        note_upload_failure(f"create {filename}", "upload-text-file", resp=resp)


if __name__ == "__main__":
    main()

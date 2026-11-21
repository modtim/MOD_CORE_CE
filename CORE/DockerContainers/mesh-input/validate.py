"""
Input Validator Container — validates and ingests files for workflow processing.

Handles ALL input node types (image, mesh, audio, video, nerf, comic_image,
texture_set, text, resource, script) via environment variable configuration.

Ported from input_validator.py. Runs as a Docker container on the Go worker.

Input: Environment variables
Output: /data/output/validation_result.json with validated file references
"""

import json
import os
import sys
from datetime import datetime

# worker-mediated-node-credentials: auth headers now come from the shared helper,
# which fetches short-lived run_task tokens from the worker on demand (no static
# API_KEY in this container). Drop-in replacement for the old inline functions.
from mod_node_auth import get_headers, get_service_headers  # noqa: E402


API_URL = os.environ.get("API_URL", "http://api:80")
WORKFLOW_RUN_ID = int(os.environ.get("WORKFLOW_RUN_ID", "0"))
TENANT_ID = int(os.environ.get("TENANT_ID", "0"))
COG_IDS = json.loads(os.environ.get("COG_IDS", "[]"))
INPUT_TYPE = os.environ.get("INPUT_TYPE", "image")
SUPPORTED_EXTENSIONS = json.loads(os.environ.get("SUPPORTED_EXTENSIONS", "[]"))
MIME_TYPE_PREFIX = os.environ.get("MIME_TYPE_PREFIX", "")
MIN_SIZE_BYTES = int(os.environ.get("MIN_SIZE_BYTES", "100"))
VALIDATION_MODE = os.environ.get("VALIDATION_MODE", "validate_exists")
NODE_KIND = os.environ.get("NODE_KIND", "mesh_input")
NODE_ID = os.environ.get("NODE_ID", "input-1")


def log_api_error(context: str, resp):
    """Log full error details from an API response."""
    print(f"   ERROR [{context}]: HTTP {resp.status_code}")
    try:
        body = resp.text[:500]
        print(f"   Response: {body}")
    except Exception:
        pass


def _calibration_synthetic_output(started_at):
    """When CALIBRATION_RUN=true and no COG_IDS were passed, treat the
    file the calibration sidecar already downloaded to /data/input/ as
    the validated input. The script-runner job for input nodes during
    calibration is just to confirm the file is present + write a
    /data/output/validation_result.json so downstream calibration
    machinery has something to bake against. We don't need to round-
    trip through /workers/tasks/resolve-cog-hash because the
    calibration plan owns the file already."""
    input_dir = "/data/input"
    if not os.path.isdir(input_dir):
        print(f"ERROR: calibration mode but {input_dir} missing")
        sys.exit(1)
    candidates = sorted(
        os.path.join(input_dir, f) for f in os.listdir(input_dir)
        if os.path.isfile(os.path.join(input_dir, f)) and not f.startswith(".")
    )
    if not candidates:
        print(f"ERROR: calibration mode but {input_dir} is empty")
        sys.exit(1)
    input_file = candidates[0]
    size_bytes = os.path.getsize(input_file)
    file_extension = os.path.splitext(input_file)[1].lstrip(".").lower()
    print(f"   calibration mode: using {input_file} ({size_bytes} bytes)")

    completed_at = datetime.utcnow()
    result = {
        "status": "success",
        "input_type": INPUT_TYPE,
        "validated_files": 1,
        "errors": 0,
        "outputs": [{
            "type": INPUT_TYPE,
            "location": input_file,
            "file_path": input_file,
            "content_hash": "calibration",
            "file_extension": file_extension,
            "size_bytes": size_bytes,
            "cog_id": 0,
            "node_id": NODE_ID,
        }],
        "error_details": [],
        "calibration_run": True,
        "duration_seconds": (completed_at - started_at).total_seconds(),
    }
    os.makedirs("/data/output", exist_ok=True)
    with open("/data/output/validation_result.json", "w") as f:
        json.dump(result, f, indent=2)
    print("Validation complete (calibration mode)")


def main():
    import requests

    started_at = datetime.utcnow()

    print(f"\n{'='*70}")
    print(f"INPUT VALIDATOR - {INPUT_TYPE.upper()}")
    print(f"{'='*70}")
    print(f"Workflow Run: {WORKFLOW_RUN_ID}")
    print(f"Tenant: {TENANT_ID}")
    print(f"Cog IDs: {COG_IDS}")
    print(f"Supported Extensions: {SUPPORTED_EXTENSIONS}")
    print(f"Validation Mode: {VALIDATION_MODE}")
    print()

    # Calibration plans dispatch input nodes without COG_IDS — the
    # plan owns the input fixture and pre-downloads it via the sidecar.
    # Short-circuit the cog-resolution path in that case so input
    # nodes don't all fail with "No cog_ids provided" during training.
    if os.environ.get("CALIBRATION_RUN", "").lower() == "true" and not COG_IDS:
        return _calibration_synthetic_output(started_at)

    if not COG_IDS:
        print("ERROR: No cog_ids provided")
        sys.exit(1)

    # =========================================================================
    # STEP 1: Fetch cog metadata from API
    # =========================================================================
    print("Step 1: Fetching cog metadata...")

    validated_files = []
    errors = []

    for cog_id in COG_IDS:
        print(f"   Validating cog {cog_id}...")

        # Get cog details including content hash
        resp = requests.get(
            f"{API_URL}/v1/workers/tasks/resolve-cog-hash/{cog_id}",
            headers=get_headers(),
            timeout=10
        )

        if resp.status_code != 200:
            errors.append({"cog_id": cog_id, "error": f"Could not resolve cog: {resp.status_code}"})
            log_api_error(f"resolve cog {cog_id}", resp)
            continue

        cog_data = resp.json()
        content_hash = cog_data.get("content_hash", "")
        file_extension = cog_data.get("file_extension", "")
        size_bytes = cog_data.get("size_bytes", 0)

        if not content_hash:
            errors.append({"cog_id": cog_id, "error": "No content hash found"})
            print(f"   ERROR: No content hash for cog {cog_id}")
            continue

        # =====================================================================
        # STEP 2: Validate file extension
        # =====================================================================
        if SUPPORTED_EXTENSIONS:
            ext_with_dot = f".{file_extension.lower()}" if file_extension else ""
            ext_lower = file_extension.lower() if file_extension else ""

            valid_ext = (
                ext_with_dot in [e.lower() for e in SUPPORTED_EXTENSIONS]
                or ext_lower in [e.lower().lstrip('.') for e in SUPPORTED_EXTENSIONS]
            )

            if not valid_ext:
                errors.append({
                    "cog_id": cog_id,
                    "error": f"Unsupported extension: .{file_extension}",
                    "supported": SUPPORTED_EXTENSIONS
                })
                print(f"   WARNING: Unsupported extension .{file_extension} for cog {cog_id}")
                # Don't fail — still include the file but flag the warning

        # =====================================================================
        # STEP 3: Validate file size
        # =====================================================================
        if size_bytes > 0 and size_bytes < MIN_SIZE_BYTES:
            errors.append({
                "cog_id": cog_id,
                "error": f"File too small: {size_bytes} bytes (min: {MIN_SIZE_BYTES})"
            })
            print(f"   WARNING: File too small ({size_bytes} bytes) for cog {cog_id}")

        # =====================================================================
        # STEP 4: Validate MIME type (basic check from extension)
        # =====================================================================
        if MIME_TYPE_PREFIX and file_extension:
            mime_map = {
                "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
                "tiff": "image/tiff", "tif": "image/tiff", "heic": "image/heic",
                "mp4": "video/mp4", "mov": "video/quicktime", "avi": "video/x-msvideo",
                "mkv": "video/x-matroska", "webm": "video/webm",
                "wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac",
                "aac": "audio/aac", "ogg": "audio/ogg",
                "obj": "model/obj", "fbx": "model/fbx", "glb": "model/gltf-binary",
                "gltf": "model/gltf+json", "stl": "model/stl",
            }
            inferred_mime = mime_map.get(file_extension.lower(), "")
            if inferred_mime and not inferred_mime.startswith(MIME_TYPE_PREFIX):
                print(f"   WARNING: MIME type {inferred_mime} doesn't match prefix {MIME_TYPE_PREFIX}")

        # File is valid — add to validated list
        storage_scheme = os.environ.get("STORAGE_SCHEME", "s3")
        bucket = f"tenant-{TENANT_ID}-files"

        validated_files.append({
            "type": INPUT_TYPE,
            "location": f"{storage_scheme}://{bucket}/{content_hash}",
            "file_path": f"{storage_scheme}://{bucket}/{content_hash}",
            "content_hash": content_hash,
            "file_extension": file_extension,
            "size_bytes": size_bytes,
            "cog_id": cog_id,
            "node_id": NODE_ID,
        })

        print(f"   OK: {content_hash[:16]}... (.{file_extension}, {size_bytes/(1024*1024):.2f} MB)")

    # =========================================================================
    # STEP 5: Update processing metadata
    # =========================================================================
    print(f"\nStep 2: Updating processing metadata...")

    try:
        completed_at = datetime.utcnow()
        duration = (completed_at - started_at).total_seconds()

        metadata_payload = {
            "cog_ids": COG_IDS,
            "workflow_run_id": WORKFLOW_RUN_ID,
            "tenant_id": TENANT_ID,
            "metadata_entry": {
                "node_id": NODE_ID,
                "node_name": f"{INPUT_TYPE} Input",
                "node_kind": NODE_KIND,
                "status": "success" if validated_files else "failure",
                "started_at": started_at.isoformat() + "Z",
                "completed_at": completed_at.isoformat() + "Z",
                "duration_seconds": duration,
                "cost": 0,
                "output_files": [],
                "parameters": {},
            }
        }

        resp = requests.post(
            f"{API_URL}/v1/assets/service/batch-update-processing-history",
            json=metadata_payload,
            headers=get_headers(),
            timeout=30
        )
        if resp.status_code == 200:
            print(f"   Processing metadata updated")
        else:
            print(f"   WARNING: Metadata update failed: HTTP {resp.status_code}")
            print(f"   Response: {resp.text[:500]}")
            print(f"   Payload sent: {json.dumps(metadata_payload, default=str)[:500]}")
    except Exception as e:
        print(f"   WARNING: Metadata update error: {type(e).__name__}: {e}")

    # =========================================================================
    # STEP 6: Write output
    # =========================================================================
    result = {
        "status": "success" if validated_files else "failure",
        "input_type": INPUT_TYPE,
        "validated_files": len(validated_files),
        "errors": len(errors),
        "outputs": validated_files,
        "error_details": errors,
    }

    with open("/data/output/validation_result.json", "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nValidation complete: {len(validated_files)} valid, {len(errors)} errors")

    if not validated_files:
        print("FATAL: No valid files — exiting with error")
        sys.exit(1)


if __name__ == "__main__":
    main()

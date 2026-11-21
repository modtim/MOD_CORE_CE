"""Wizard ingest-artifacts stage.

Runs between the wizard's `generate` node (Containerizer LLM) and
the variant build nodes (test-and-repair-hero, build-{variant}, ...).

Inputs (normal workflow node-to-node data flow):
  /data/input/albedo-artifacts.json — written by the upstream generate
  node to /data/output, flowed to this node's /data/input by the
  worker's standard data-passing machinery.

Outputs:
  Calls POST /v1/woodsman/wizard/ingest-artifacts {spec_id, artifacts}
  to merge the artifact dict onto the BuildSpec. Downstream nodes
  (test-and-repair-hero, etc.) read the merged Dockerfile/entrypoint/
  scripts from the spec.

Failure modes (any → non-zero exit, workflow_run fails at this node):
  - albedo-artifacts.json missing under /data/input/
  - file is not valid JSON
  - API returns 4xx (validation reason in response body)
  - API returns 5xx (rare; surfaces as a generic stage failure)
"""
from __future__ import annotations

import json
import os
import sys

import requests

from _common import env, log, report_back


def _read_artifacts_file() -> dict:
    """Locate + load the Containerizer's artifact JSON in /data/input.

    The worker's download sidecar renames upstream outputs to
    `input_<node_kind>{file_extension}`, so the canonical name
    `albedo-artifacts.json` is NOT the on-disk name even when the
    orchestrator chose the right blob. We probe in order:

      1. `/data/input/albedo-artifacts.json` — only present if a future
         worker change preserves original filenames; kept first for
         forward-compat.
      2. Any file under /data/input whose contents parse as JSON AND
         whose top-level keys match the artifact bundle shape
         (`output_mode` is the most reliable discriminator — every
         Containerizer-emitted artifact JSON has it).

    Exits non-zero with a clear message + a listing of /data/input
    contents if no match is found, so the operator sees what arrived.
    """
    legacy_path = "/data/input/albedo-artifacts.json"
    if os.path.exists(legacy_path):
        try:
            with open(legacy_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            log(f"[ingest_artifacts] loaded {legacy_path}")
            return data
        except json.JSONDecodeError as e:
            log(f"[ingest_artifacts] ERROR: {legacy_path} is not valid JSON: {e}")
            sys.exit(1)

    # Walk /data/input looking for the artifact bundle by shape.
    try:
        entries = sorted(os.listdir("/data/input"))
    except FileNotFoundError:
        entries = []
    for entry in entries:
        path = f"/data/input/{entry}"
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and "output_mode" in data:
            log(f"[ingest_artifacts] loaded {path} (matched by artifact-bundle shape)")
            return data

    log("[ingest_artifacts] ERROR: no Containerizer artifact bundle found in /data/input.")
    log("[ingest_artifacts] Looked for /data/input/albedo-artifacts.json (legacy path) and")
    log("[ingest_artifacts] any JSON file with a top-level `output_mode` key (current path).")
    log("[ingest_artifacts] The upstream `generate` node should have written this; if it's")
    log("[ingest_artifacts] missing, the generate node likely failed before writing artifacts.")
    log("[ingest_artifacts] Check the generate node's log_tail for the actual error.")
    log("[ingest_artifacts] What IS in /data/input:")
    for entry in entries or ["(empty)"]:
        log(f"[ingest_artifacts]   /data/input/{entry}")
    sys.exit(1)


def main() -> int:
    api_url = env("API_URL", required=True)
    api_key = env("API_KEY", required=True)
    spec_id = int(env("BUILD_SPEC_ID", required=True))

    log(f"[ingest_artifacts] spec={spec_id}")
    artifacts = _read_artifacts_file()
    log(f"[ingest_artifacts] loaded artifact bundle with {len(artifacts)} top-level keys")

    body = {"spec_id": spec_id, "artifacts": artifacts}
    try:
        r = requests.post(
            f"{api_url}/v1/woodsman/wizard/ingest-artifacts",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            # Bumped from 60→300s: server-side Critic C (audit_artifacts)
            # runs up to 5 LLM iterations per ingest, each ~10-30s.
            # 60s caused intermittent ingest-artifacts FAILED on
            # otherwise-fine builds (wfr 53 hit this).
            json=body, timeout=300,
        )
    except requests.RequestException as e:
        log(f"[ingest_artifacts] ERROR: POST to API failed: {e}")
        report_back(api_url, api_key, spec_id=spec_id, stage="ingest_artifacts",
                    variant=None, success=False, payload={"reason": str(e)})
        return 1

    if r.status_code >= 400:
        body_text = r.text[:1000]
        log(f"[ingest_artifacts] ERROR: API returned {r.status_code}: {body_text}")
        report_back(api_url, api_key, spec_id=spec_id, stage="ingest_artifacts",
                    variant=None, success=False,
                    payload={"status": r.status_code, "body": body_text})
        return 1

    resp = r.json() or {}
    fields = resp.get("fields_merged") or []
    log(f"[ingest_artifacts] merged {len(fields)} field(s) onto BuildSpec: {', '.join(fields[:10])}"
        + (" ..." if len(fields) > 10 else ""))
    report_back(api_url, api_key, spec_id=spec_id, stage="ingest_artifacts",
                variant=None, success=True,
                payload={"fields_merged": fields})
    return 0


if __name__ == "__main__":
    sys.exit(main())

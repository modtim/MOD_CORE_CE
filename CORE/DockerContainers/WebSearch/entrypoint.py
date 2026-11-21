#!/usr/bin/env python3
"""WebSearch — system web-search node (add-web-search-node).

A reusable workflow node. It runs a keyless DuckDuckGo web search for
``QUERY``, mines the results for domain ``jargon``, candidate
``github_repos`` and ``pypi_packages``, and collects the top ``TOP_K``
hits as ``{title, url, snippet}``. It returns that as a single JSON
artifact to the platform via an internal, service-account-authenticated
callback — mirroring HarborScanFollowup's API round-trip + auth.

The search + extraction logic is NOT reimplemented here: it is the
platform's ``web_expand`` module, vendored verbatim into this image as
``web_expand.py`` so the container has no dependency on the API package.

No Harbor / Vault / platform-DB credentials live in the container. The
artifact is POSTed to an internal API endpoint the worker authenticates
against using a service-account token passed via env (mod_node_auth).

Env contract (threaded by the platform / worker):
    QUERY            the search text (default "")
    TOP_K            max results as a string (default "5"; coerced to int)
    API_URL          base URL of the platform API (set by the worker)
    WORKFLOW_RUN_ID  the WorkflowRun id (callback body / artifact routing)
    NODE_ID          the graph node id (callback body / artifact routing)
    RESULT_ENDPOINT  callback path (default "/system/web-search/result")
    API_KEY / MOD_TASK_TOKEN_URL  service-account creds (via mod_node_auth)

OUTPUT artifact JSON (also the NodeDefinition.output_schema shape) —
this is both written to stdout and sent as the callback ``artifact``::

    {"results": [{"title": str, "url": str, "snippet": str}],
     "github_repos": [str], "pypi_packages": [str],
     "jargon": [str], "ok": bool}

Result-return contract (container -> API) — POST body::

    {"workflow_run_id": <int>, "node_id": <str>, "artifact": <artifact>}

Exit code:
    0  on success AND on soft failures — an empty / unreachable web
       search yields ``ok=false`` with empty lists, but the (valid,
       empty) artifact is still delivered.
    1  only on genuine infra errors: a missing delivery target
       (API_URL / token / WORKFLOW_RUN_ID / NODE_ID) or the callback
       POST failing (the platform never received the artifact).

Failure contract (CE node JSON contract, vet step 5): on ANY failure the
container prints exactly ONE line of JSON to stdout and exits 1::

    {"code": <C>, "error": <msg>}   # sort_keys=True

with C one of NO_INPUT / BAD_PARAM / PROCESSING_FAILURE / OUTPUT_NOT_WRITTEN
/ INTERNAL. This node uses BAD_PARAM (missing/invalid env: API_URL, the
service-account token, WORKFLOW_RUN_ID, NODE_ID), PROCESSING_FAILURE (the
result callback POST failed) and INTERNAL (unexpected exception). Error
messages name variables only — never token values or URL userinfo.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests

# worker-mediated-node-credentials: auth headers come from the shared helper,
# which fetches short-lived run_task tokens from the worker on demand (with a
# static API_KEY dual-read fallback). Identical to HarborScanFollowup.
from mod_node_auth import get_headers, refresh  # noqa: E402

# The vendored search + extraction logic (copy of the API's
# node_builder/web_expand.py). web_search() returns the node OUTPUT artifact
# JSON directly and never raises.
import web_expand  # noqa: E402

DEFAULT_RESULT_ENDPOINT = "/system/web-search/result"
DEFAULT_TOP_K = "5"


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default)
    return v if v is not None else default


def fail(msg: str, code: str = "INTERNAL") -> None:
    """CE node JSON error contract: ONE stdout JSON line, exit non-zero.

    Messages name variables only — never token values, URL userinfo, or
    input file contents."""
    print(json.dumps({"code": code, "error": msg}, sort_keys=True), flush=True)
    sys.exit(1)


def _api_post(api_url: str, path: str, body: dict) -> Any:
    """POST the artifact to the internal callback endpoint.

    X-API-Key (via mod_node_auth), not ``Authorization: Bearer`` — the
    platform-service key is an opaque ``sk_…`` value, not a JWT. On a 401
    we drop the cached token and retry once (mod_node_auth.refresh), the
    documented pattern for a mid-run token expiry."""
    url = api_url.rstrip("/") + path
    resp = requests.post(url, json=body, headers=get_headers(), timeout=30)
    if resp.status_code == 401:
        refresh()
        resp = requests.post(url, json=body, headers=get_headers(), timeout=30)
    resp.raise_for_status()
    # The callback may return an empty body; tolerate that.
    try:
        return resp.json()
    except ValueError:
        return None


def main() -> int:
    # INTERNAL catch-all: fail()/sys.exit() raise SystemExit (a BaseException,
    # not caught here) so the BAD_PARAM/PROCESSING_FAILURE paths below pass
    # through uncaught, exactly as emitted. Only a genuinely unexpected
    # exception (e.g. web_expand raising despite its no-raise contract, or a
    # bug in this script) lands here and is reported as INTERNAL instead of
    # crashing with a bare traceback.
    try:
        return _main_inner()
    except Exception as e:  # noqa: BLE001 - deliberate catch-all per CE contract
        print(f"unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        fail(f"unexpected internal error: {type(e).__name__}", "INTERNAL")
        return 1  # unreachable: fail() calls sys.exit()


def _main_inner() -> int:
    query = _env("QUERY", "")
    top_k = _env("TOP_K", DEFAULT_TOP_K).strip() or DEFAULT_TOP_K
    api_url = _env("API_URL", "").strip()
    node_id = _env("NODE_ID", "").strip()
    workflow_run_id_raw = _env("WORKFLOW_RUN_ID", "").strip()
    result_endpoint = _env("RESULT_ENDPOINT", "").strip() or DEFAULT_RESULT_ENDPOINT

    # Genuine infra errors: without a delivery target the artifact can't be
    # returned, so fail fast (exit 1) rather than doing the search for nothing.
    errors: list[str] = []
    if not api_url:
        errors.append("API_URL is required")
    if not (os.environ.get("MOD_TASK_TOKEN_URL") or os.environ.get("API_KEY")):
        errors.append("a service-account token (API_KEY / MOD_TASK_TOKEN_URL) is required")
    if not workflow_run_id_raw:
        errors.append("WORKFLOW_RUN_ID is required")
    if not node_id:
        errors.append("NODE_ID is required")
    workflow_run_id: int | None = None
    if workflow_run_id_raw:
        try:
            workflow_run_id = int(workflow_run_id_raw)
        except ValueError:
            errors.append("WORKFLOW_RUN_ID is not an integer")
    if errors:
        fail("; ".join(errors), "BAD_PARAM")

    # Run the search. Best-effort by contract: web_search never raises, and an
    # empty / unreachable result is a VALID artifact with ok=false — a soft
    # failure we still deliver and exit 0 for.
    artifact = web_expand.web_search(query, top_k=top_k)

    # Return the artifact to the platform (result-return contract).
    body = {
        "workflow_run_id": workflow_run_id,
        "node_id": node_id,
        "artifact": artifact,
    }
    try:
        _api_post(api_url, result_endpoint, body)
    except requests.RequestException as e:
        # The platform never received the artifact — a genuine infra error.
        # Full exception detail (may carry the URL) goes to stderr only; the
        # contract line names variables, never URL userinfo.
        print(f"result callback POST failed: {type(e).__name__}: {e}", file=sys.stderr)
        fail("result callback POST failed (API_URL / RESULT_ENDPOINT)", "PROCESSING_FAILURE")

    # Success (or soft-empty). Emit the artifact for operator visibility.
    print(json.dumps(artifact))
    return 0


if __name__ == "__main__":
    sys.exit(main())

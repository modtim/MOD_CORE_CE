"""protocol-ecosystem 1.4 — a published workflow -> OpenAPI 3.1 document.

Pure generator: given a workflow-like object (name/id/graph_json/node_catalog)
it derives the workflow's public IO contract with
``workflow_schema_export.build_workflow_mcp_schema`` (entry nodes = inputs,
terminal nodes = outputs) and wraps it as a minimal, self-contained OpenAPI
3.1.0 document a client can point at:

    POST /run              requestBody  = the MCP input JSON Schema
                           202 = {run_id, status} (async accepted)
                           200 = sync result (the MCP output JSON Schema)
                           401 / 402 / 429 via reusable #/components/responses
    GET  /runs/{run_id}    run status poll

What the document deliberately does NOT contain: tenant ids, internal hostnames,
node configurations, secrets, or the workflow's private middle nodes — only
names, descriptions and JSON Schema fragments. A scrub pass enforces that even
when a caller hands us a schema fragment that happens to carry a secret-ish key.

NEVER RAISES. A malformed / empty workflow yields a valid minimal document
(empty object schemas), mirroring the MCP builder's contract, so a docs
endpoint can always serve something.

Mounting the doc (a router + main.py mount) is a lead follow-up; this module
has no DB, no FastAPI, no I/O.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

from services.workflow_schema_export import build_workflow_mcp_schema

logger = logging.getLogger(__name__)

_EMPTY_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}}

# Keys that must never be echoed into a public document.
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|secret|password|passwd|token|credential|auth|"
    r"signature|private|bearer|hmac|salt|access[_-]?key|refresh[_-]?key)",
    re.IGNORECASE,
)
# Values that look like credentials even under a benign key.
_SECRET_VALUE_RE = re.compile(
    r"^(sk|pk|rk|ak|ghp|glpat|xox[baprs]|ya29|AIza)[A-Za-z0-9_\-]{8,}$"
)
_NAME_RE = re.compile(r"[^a-z0-9]+")
_NAME_MAX = 200


def _field(obj: Any, name: str, *fallbacks: str) -> Optional[Any]:
    """Read ``name`` from a dict-like or attribute-bearing workflow object."""
    if isinstance(obj, dict):
        val = obj.get(name)
    else:
        val = getattr(obj, name, None)
    if val is not None:
        return val
    for fb in fallbacks:
        if isinstance(obj, dict):
            val = obj.get(fb)
        else:
            val = getattr(obj, fb, None)
        if val is not None:
            return val
    return None


def _slug(text: Any, fallback: str) -> str:
    raw = str(text or "").strip().lower()
    slugged = _NAME_RE.sub("_", raw).strip("_")
    return slugged or fallback


def _scrub(value: Any) -> Any:
    """Drop secret-looking keys/values from a schema fragment (never mutate)."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                continue
            cleaned = _scrub(val)
            if isinstance(cleaned, str) and _SECRET_VALUE_RE.match(cleaned.strip()):
                continue
            out[key] = cleaned
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return "" if _SECRET_VALUE_RE.match(value.strip()) else value
    return value


def _io_pair(workflow: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """MCP IO schemas for a workflow-like object, degraded never-raising."""
    graph = _field(workflow, "graph_json", "graph")
    catalog = _field(workflow, "node_catalog", "node_catalog_by_id")
    try:
        schemas = build_workflow_mcp_schema(graph if isinstance(graph, dict) else {},
                                            catalog if isinstance(catalog, dict) else {})
    except Exception:  # defensive: the builder already swallows everything
        logger.exception("openapi_workflow_export: MCP schema derivation failed")
        schemas = {}
    return (schemas.get("input_schema") or _EMPTY_SCHEMA,
            schemas.get("output_schema") or _EMPTY_SCHEMA)


def _components() -> Dict[str, Any]:
    return {
        "securitySchemes": {
            "apiKeyAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "MOD API key issued to this workflow's caller.",
            }
        },
        "responses": {
            "Unauthorized": {
                "description": "Missing or invalid API key.",
                "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "detail": {"type": "string"},
                        "error_id": {"type": "string", "format": "uuid"},
                    },
                }}},
            },
            "PaymentRequired": {
                "description": "Caller is out of credits for this workflow run.",
                "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "detail": {"type": "string"},
                        "credits_required": {"type": "number"},
                        "credits_remaining": {"type": "number"},
                    },
                }}},
            },
            "TooManyRequests": {
                "description": "Workflow run rate limit exceeded.",
                "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "detail": {"type": "string"},
                        "retry_after_seconds": {"type": "integer"},
                    },
                }}},
            },
        },
        "schemas": {
            "RunAccepted": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "status": {"type": "string",
                               "enum": ["queued", "running", "succeeded", "failed"]},
                },
                "required": ["run_id", "status"],
            },
            "RunStatus": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "status": {"type": "string",
                               "enum": ["queued", "running", "succeeded", "failed"]},
                    "updated_at": {"type": "string", "format": "date-time"},
                },
                "required": ["run_id", "status"],
            },
        },
    }


def build_workflow_openapi(workflow: Any, *, base_url: str, version: str = "1") -> Dict[str, Any]:
    """Workflow-like object -> self-contained OpenAPI 3.1.0 document.

    ``workflow`` may be a dict or an object carrying ``name`` / ``id`` /
    ``graph_json`` / ``node_catalog`` (the caller resolves the catalog, exactly
    as the MCP listing does). Never raises: anything malformed degrades to a
    valid minimal document with empty object schemas.
    """
    try:
        name = _field(workflow, "name") or ""
        wf_id = _field(workflow, "id")
        pair = _io_pair(workflow)
        input_schema = _scrub(pair[0] if isinstance(pair[0], dict) else {})
        output_schema = _scrub(pair[1] if isinstance(pair[1], dict) else {})
        operation = _slug(wf_id if wf_id is not None else name, f"workflow_{_slug(name, 'unnamed')}")
        servers = [{"url": str(base_url) if isinstance(base_url, str) and base_url.strip() else "/"}]
        doc: Dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {
                "title": str(name)[:_NAME_MAX] if name else f"Workflow {wf_id}",
                "version": str(version),
                "description": (f"Public contract for workflow {wf_id} "
                                f"({name or 'unnamed'}): POST /run to execute, "
                                "GET /runs/{run_id} to poll."),
            },
            "servers": servers,
            "paths": {
                "/run": {
                    "post": {
                        "operationId": f"run_{operation}",
                        "summary": f"Run {name or 'the workflow'}",
                        "security": [{"apiKeyAuth": []}],
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": input_schema}},
                        },
                        "responses": {
                            "202": {
                                "description": "Run accepted and queued.",
                                "content": {"application/json": {
                                    "schema": {"$ref": "#/components/schemas/RunAccepted"}}},
                            },
                            "200": {
                                "description": "Synchronous completion result.",
                                "content": {"application/json": {"schema": output_schema}},
                            },
                            "401": {"$ref": "#/components/responses/Unauthorized"},
                            "402": {"$ref": "#/components/responses/PaymentRequired"},
                            "429": {"$ref": "#/components/responses/TooManyRequests"},
                        },
                    }
                },
                "/runs/{run_id}": {
                    "get": {
                        "operationId": f"status_{operation}",
                        "summary": "Status of a run started by this workflow",
                        "security": [{"apiKeyAuth": []}],
                        "parameters": [{
                            "name": "run_id",
                            "in": "path",
                            "required": True,
                            "description": "Run id returned by POST /run.",
                            "schema": {"type": "string"},
                        }],
                        "responses": {
                            "200": {
                                "description": "Current run status.",
                                "content": {"application/json": {
                                    "schema": {"$ref": "#/components/schemas/RunStatus"}}},
                            },
                            "401": {"$ref": "#/components/responses/Unauthorized"},
                            "402": {"$ref": "#/components/responses/PaymentRequired"},
                            "429": {"$ref": "#/components/responses/TooManyRequests"},
                        },
                    }
                },
            },
            "components": _components(),
        }
        return json.loads(json.dumps(doc))
    except Exception:  # defensive: a public docs surface must never 500
        logger.exception("openapi_workflow_export: failed to build document")
        return _minimal_doc(base_url, version)


def _minimal_doc(base_url: Any, version: Any) -> Dict[str, Any]:
    """The smallest document we can still serve when everything else failed."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "Workflow (unnamed)", "version": str(version),
                 "description": "Public contract: POST /run, GET /runs/{run_id}."},
        "servers": [{"url": str(base_url) if isinstance(base_url, str)
                     and base_url.strip() else "/"}],
        "paths": {
            "/run": {"post": {
                "operationId": "run_unnamed",
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": dict(_EMPTY_SCHEMA)}}},
                "responses": {
                    "202": {"description": "Run accepted and queued.", "content": {
                        "application/json": {"schema": {
                            "$ref": "#/components/schemas/RunAccepted"}}}},
                    "200": {"description": "Synchronous completion result.", "content": {
                        "application/json": {"schema": dict(_EMPTY_SCHEMA)}}},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "402": {"$ref": "#/components/responses/PaymentRequired"},
                    "429": {"$ref": "#/components/responses/TooManyRequests"},
                }}},
            "/runs/{run_id}": {"get": {
                "operationId": "status_unnamed",
                "parameters": [{"name": "run_id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "Current run status.", "content": {
                        "application/json": {"schema": {
                            "$ref": "#/components/schemas/RunStatus"}}}},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "402": {"$ref": "#/components/responses/PaymentRequired"},
                    "429": {"$ref": "#/components/responses/TooManyRequests"},
                }}},
        },
        "components": _components(),
    }

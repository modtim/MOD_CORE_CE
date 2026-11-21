"""Canonical API-key capability-scope constants.

Kept as a dependency-free leaf module so schemas, dependencies, and routers can
all import the scope names without risking a circular import.

``ApiKey.capability_scope`` (models.py) is a free-form JSONB allowlist of
``"resource_type:capability"`` strings, matched at authorization time by
``resource_permissions._api_key_scope_allows``. This module names the scopes
this build knows about by name and provides the small helpers the mint endpoint
and the node-authoring dependency share.
"""
from __future__ import annotations

import re
from typing import Any

# Scope that authorises the external node-authoring endpoints (register a
# pre-built image / build a node from a Dockerfile). A key carrying only this
# scope can create private-draft nodes and nothing else.
NODE_AUTHOR_SCOPE = "node:author"

# --- MCP tool fleet scopes -------------------------------------------------
# Each locked-down MCP front door (one per wizard/power-tool) is gated by its
# own scope. A key minted for one tool cannot drive another; the runtime check
# is the per-tool require_<x>_key dependency in dependencies.py.
WORKFLOW_RUN_SCOPE = "workflow:run"        # run/cancel/restart/status a workflow
WORKFLOW_AUTHOR_SCOPE = "workflow:author"  # workflow-wizard mechanical core
PROJECT_AUTHOR_SCOPE = "project:author"    # project-wizard mechanical core
STACK_AUTHOR_SCOPE = "stack:author"        # stack-wizard persistent-stack lifecycle
AUTOMATION_MANAGE_SCOPE = "automation:manage"  # trigger lifecycle + run/event logs

# Scopes this build recognises by name. A requested scope is accepted at mint
# time if it is one of these OR looks like a well-formed "resource:capability"
# pair (so the pre-existing free-form scope allowlist keeps working).
KNOWN_API_KEY_SCOPES = frozenset({
    NODE_AUTHOR_SCOPE,
    WORKFLOW_RUN_SCOPE,
    WORKFLOW_AUTHOR_SCOPE,
    PROJECT_AUTHOR_SCOPE,
    STACK_AUTHOR_SCOPE,
    AUTOMATION_MANAGE_SCOPE,
})

_SCOPE_PAIR_RE = re.compile(r"^[A-Za-z0-9_]+:[A-Za-z0-9_]+$")


def is_valid_scope(scope: Any) -> bool:
    """True if ``scope`` is a scope this build will accept when minting a key."""
    if not isinstance(scope, str) or not scope.strip():
        return False
    s = scope.strip()
    if s.lower() in {k.lower() for k in KNOWN_API_KEY_SCOPES}:
        return True
    return bool(_SCOPE_PAIR_RE.match(s))


def key_has_scope(api_key: Any, scope: str) -> bool:
    """True if ``api_key.capability_scope`` carries ``scope``.

    Matching is case-insensitive so both the enum-value form (``node:author``)
    and the enum-name form (``NODE:author``) satisfy it, mirroring
    ``resource_permissions._api_key_scope_allows``.
    """
    scopes = getattr(api_key, "capability_scope", None) or []
    target = scope.lower()
    return any(isinstance(s, str) and s.lower() == target for s in scopes)

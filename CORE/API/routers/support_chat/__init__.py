"""Aggregator for the support_chat router package (Gate 2a domain split).

Behavior-preserving: re-exports the same ``router`` (all /support/* endpoints),
plus the ``workflow_authoring_router`` and ``project_authoring_router`` that
main.py mounts separately. Endpoints were MOVED verbatim into per-domain
sub-modules; paths, methods, dependencies and schemas are unchanged.

Backwards-compatibility: the pre-split ``routers.support_chat`` module was a flat
namespace that many test modules import helpers, DTOs and constants from
directly. Re-exporting everything from ``_shared`` (which preserves the original
``__module__``) keeps ``from routers.support_chat import <symbol>`` working.
"""
from __future__ import annotations

from fastapi import APIRouter

# Re-export the flat namespace (helpers/DTOs/constants) for import-compat.
from ._shared import *  # noqa: F401,F403
from ._shared import __all__ as _shared_all

from . import assistant_support, node_wizard, workflows, projects_agentic

router = APIRouter(tags=["support"])
router.include_router(assistant_support.router)
router.include_router(node_wizard.router)
router.include_router(workflows.router)
router.include_router(projects_agentic.router)

# Mounted separately in main.py (MCP fleet surfaces).
workflow_authoring_router = assistant_support.workflow_authoring_router
project_authoring_router = assistant_support.project_authoring_router

# Per-domain handler re-exports (import-compat / test patchability). These
# endpoint functions were MOVED verbatim into their domain sub-module by the
# Gate 2a split and were never re-exported at the package level, so tests
# that reach them via `import routers.support_chat as sc; sc.<name>` (either
# to call/inspect them directly, or to monkeypatch a facade's delegate)
# failed with AttributeError. Additive only — does not change routing.
from .node_wizard import workflow_wizard_apply  # noqa: F401
from .workflows import workflow_wizard_test_run  # noqa: F401
from .assistant_support import (  # noqa: F401
    emergency_alert,
    workflow_wizard_stream,
    workflow_wizard_audit_action,
    workflow_wizard_test_run_status,
    workflow_wizard_quality_signal,
    workflow_wizard_queue_missing,
    wa_apply,
    wa_test_run,
    wa_test_run_status,
    wa_queue_missing,
    wa_audit_action,
    project_wizard_criteria,
    pa_record_criteria,
)

__all__ = list(_shared_all) + [
    "router",
    "workflow_authoring_router",
    "project_authoring_router",
    "workflow_wizard_apply",
    "workflow_wizard_test_run",
    "emergency_alert",
    "workflow_wizard_stream",
    "workflow_wizard_audit_action",
    "workflow_wizard_test_run_status",
    "workflow_wizard_quality_signal",
    "workflow_wizard_queue_missing",
    "wa_apply",
    "wa_test_run",
    "wa_test_run_status",
    "wa_queue_missing",
    "wa_audit_action",
    "project_wizard_criteria",
    "pa_record_criteria",
]

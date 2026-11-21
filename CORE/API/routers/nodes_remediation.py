# API/routers/nodes_remediation.py
"""Nodes/remediation — the clean `/v1/nodes/remediation` resource.

clean-v2-api / node-definitions-collapse wave (openspec: clean-v2-api,
design.md "Domain 2"). Replaces `/v1/node-definitions/remediation/*`,
`/v1/node-definitions/federation/remediation/share`,
`/v1/node-definitions/admin/remediation/*` and
`/v1/node-definitions/feedback/node-broken`. Rule 1 (kill grab-bag
`/admin` prefixes) applies to the two `admin/remediation/*` paths.

REUSE, NOT REWRITE — see routers/nodes.py's module docstring for the reuse
pattern; the corresponding `@router.*` decorators in
`routers/node_definitions_domains/node_authoring.py` have been removed.

CRITICAL: `CORE/DockerContainers/build-task/stages/remediation_client.py`
and `CORE/DockerContainers/ImageBuilder/llm_remediation.py` call
`lookup`/`record`/`record-failure`/`record-failed-attempt` directly by
hardcoded `/v1/node-definitions/remediation/...` URL — see the wave
report for the exact file:line callers that must be repointed.
"""
from __future__ import annotations

from fastapi import APIRouter

from routers.node_definitions import (
    remediation_lookup,
    remediation_record,
    remediation_record_failure,
    remediation_record_failed_attempt,
    federation_remediation_share,
    admin_remediation_stats,
    admin_demote_remediation,
    remediation_promoted,
    feedback_node_broken,
    _RemediationLookupResponse,
)

router = APIRouter(
    prefix="/nodes/remediation",
    tags=["nodes/remediation"],
    responses={404: {"description": "Not found"}},
)

router.get("/lookup", response_model=_RemediationLookupResponse)(remediation_lookup)
router.post("/record")(remediation_record)
router.post("/record-failure")(remediation_record_failure)
router.post("/record-failed-attempt")(remediation_record_failed_attempt)
router.post("/federation/share")(federation_remediation_share)
router.get("/promoted")(remediation_promoted)
router.post("/feedback/node-broken")(feedback_node_broken)

# Renamed from "/admin/remediation/*" (rule 1: no /admin URL segment).
router.get("/stats")(admin_remediation_stats)
router.post("/{remediation_id}/demote")(admin_demote_remediation)

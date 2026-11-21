# API/routers/runs.py
"""Runs — the clean `/v1/runs` resource (clean-v2-api "workflows + runs +
assets + files" wave, design.md target resource model: "runs (node-runs, run
controls ...)").

This REPLACES `/v1/node-runs` (per-node execution records: fanout summary,
previews, node-run status by node id, service-account lookups the worker/
node-container callback path uses) with the clean `/v1/runs` address.

REUSE, NOT REWRITE: same router objects `routers.node_runs` already builds
(`router`, `service_router` — themselves aggregates of
`routers/node_runs_domains/*.py`'s domain sub-modules from the Gate 2a
service/CRUD split). `routers/node_runs.py` bakes NO prefix into its own
`router` (prefix comes entirely from the main.py mount), so it is reused here
unedited via a plain re-export — behavior/auth/validation are byte-identical,
only the URL address changes. `service_router` keeps its own baked-in
`prefix="/service"`, so mounting it at "/runs" yields `/v1/runs/service/*`
(mirrors the old `/v1/node-runs/service/*` shape exactly).

The OLD `/v1/node-runs/*` mount has been REMOVED (clean-v2-api final
clean-cut): every baked node-container image (PlatformExport, ReturnExport,
unity-export, unreal-export) and WorkflowBuilder caller has been repointed
+ rebuilt to hit `/v1/runs/*` instead. Dev server — no back-compat.

sz-fortress-p34.4 adds ONE route here, mounted onto the SAME shared `router`
object (so `routers.runs.router is routers.node_runs.router` stays true and
the route lands at `/v1/runs/{run_id}/safety-scans/{scan_id}/override`).
"""
from typing import List

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from models import OutputSafetyScanResult
import schemas
from dependencies import get_db, require_active_and_onboarded_user

from routers.node_runs import router, service_router  # noqa: F401 (re-exported for main.py's mount)

__all__ = ["router", "service_router"]


@router.post("/{run_id}/safety-scans/{scan_id}/override",
             response_model=schemas.OutputSafetyScanOverrideResult,
             responses={404: {"description": "Scan not found in this tenant"},
                        409: {"description": "Verdict is not overridable"}})
def override_output_safety_scan(
    run_id: int,
    scan_id: int,
    override: schemas.OutputSafetyScanOverride,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """sz-fortress-p34.4 — explicit user override of a `block` output-safety
    verdict, as an APPEND to the fortress-3.4 scan table.

    The scanner's rejection stays the recorded truth: the original scan row is
    never updated (the table is paired with the append-only audit guard, so an
    UPDATE is impossible by construction). The override is a NEW row that keeps
    the same `block` verdict and stamps `overridden_by_user_id` +
    `override_reason` — exactly the contract `models.OutputSafetyScanResult`'s
    own docstring names. Downstream readers join "is there an override row for
    this scan/output_ref" against the scan rows; compliance tooling alerts on
    the paired AuditEvent without polling the results table.

    Tenant-scoped: a scan belonging to another tenant 404s (existence is never
    leaked across tenants).
    """
    scan = db.execute(
        select(OutputSafetyScanResult).where(
            OutputSafetyScanResult.id == scan_id,
            OutputSafetyScanResult.tenant_id == current_user.tenant_id,
        )
    ).scalar_one_or_none()
    if scan is None:
        raise HTTPException(status_code=404,
                            detail="output safety scan not found for this tenant")
    if scan.workflow_run_id != run_id:
        raise HTTPException(status_code=404,
                            detail="output safety scan not found for this tenant")

    if scan.overridden_by_user_id is not None:
        # Already an override row — overriding an override is not a thing.
        raise HTTPException(status_code=409,
                            detail="this row is itself an override; nothing to override")
    if scan.verdict != "block":
        # pass/warn/error: there is no rejection to override.
        raise HTTPException(status_code=409,
                            detail=f"verdict {scan.verdict} is not overridable")

    row = models.OutputSafetyScanResult(
        tenant_id=scan.tenant_id,
        workflow_run_id=scan.workflow_run_id,
        node_run_id=scan.node_run_id,
        output_ref=scan.output_ref,
        mime=scan.mime,
        scanner=scan.scanner,
        verdict=scan.verdict,  # kept, never downgraded — append-only contract
        findings=list(scan.findings or []),  # same masked findings, never raw content
        overridden_by_user_id=current_user.id,
        override_reason=override.reason,
    )
    db.add(row)
    db.add(models.AuditEvent(
        tenant_id=scan.tenant_id,
        user_id=current_user.id,
        event_name="OUTPUT_SAFETY_SCAN_OVERRIDDEN",
        details={
            "scan_id": scan_id,
            "output_ref": scan.output_ref,
            "mime": scan.mime,
            "scanner": scan.scanner,
            "workflow_run_id": scan.workflow_run_id,
            "node_run_id": scan.node_run_id,
            "reason": override.reason,
            "categories": sorted({str(f.get("category"))
                                  for f in (scan.findings or [])
                                  if isinstance(f, dict)}),
        },
    ))
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "scan_id": scan_id,
        "tenant_id": row.tenant_id,
        "workflow_run_id": row.workflow_run_id,
        "node_run_id": row.node_run_id,
        "output_ref": row.output_ref,
        "mime": row.mime,
        "scanner": row.scanner,
        "verdict": row.verdict,
        "findings": row.findings,
        "overridden_by_user_id": row.overridden_by_user_id,
        "override_reason": row.override_reason,
        "created_at": row.created_at,
    }

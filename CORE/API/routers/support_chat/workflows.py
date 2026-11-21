from __future__ import annotations

from fastapi import APIRouter
from ._shared import *  # noqa: F401,F403

# Gate 2b db.query migration — query layer + session-ownership policy
# for this sub-module now live in crud/support_chat_workflows.py and
# services/support_chat_workflows_service.py (see those modules'
# docstrings).
from crud import support_chat_workflows as _crud  # noqa: F401  (used via service)
from services import support_chat_workflows_service as _wsvc
from services._base import NotFoundError as _NotFoundError
from services.support_chat_workflows_service import ForbiddenError as _ForbiddenError

router = APIRouter()


@router.post(
    "/support/workflow-wizard/test-run",
    response_model=WizardTestRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def workflow_wizard_test_run(
    body: WizardTestRunRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    if not _pkg_dispatch("SUPPORT_CHATBOT_ENABLED", SUPPORT_CHATBOT_ENABLED, None):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Support chat is disabled")
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="user has no tenant")

    from utils.rate_limiter import check_wizard_test_run_rate_limit
    check_wizard_test_run_rate_limit(current_user.tenant_id)

    try:
        _wsvc.get_owned_session(db, body.session_id, current_user.id)
    except _NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
    except _ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message)

    proposal_graph = _wsvc.find_latest_proposal_graph(db, body.session_id)
    if proposal_graph is None:
        raise HTTPException(status_code=422,
                            detail="no proposal found in this session")

    _sti = None
    try:
        import services.wizard_test_input as _wti_mod
        _sti = _wti_mod.select_test_input
    except ImportError as _exc:
        if not _ce_is_absent(_exc, "services.wizard_test_input"):
            raise

        def _sti(*_a, **_kw):  # CE stand-in: paid test-input picker absent
            return None

    def _minio_writer_manual(pointer, payload, mime):
        from utils.minio_client import MinioClient
        import io as _io
        mc = MinioClient(tenant_id=current_user.tenant_id)
        mc.client.put_object(mc.bucket_name, pointer, _io.BytesIO(payload),
                             len(payload), content_type=mime)

    descriptor = _sti(
        db, tenant_id=current_user.tenant_id,
        candidate_graph=proposal_graph,
        wizard_session_id=body.session_id,
        iteration_n=0,
        minio_writer=_minio_writer_manual,
    )
    if descriptor is None:
        raise HTTPException(
            status_code=422,
            detail="no viable test input found — upload a sample file first",
        )

    source_cog_id = getattr(descriptor, "source_cog_id", None)

    import threading
    import crud
    import schemas
    from database import SessionLocal
    from services.workflow_orchestrator import start_workflow_execution

    if not source_cog_id:
        source_cog_id = _wsvc.get_or_create_standin_cog(
            db, tenant_id=current_user.tenant_id,
            session_id=body.session_id, descriptor=descriptor,
        )

    raw_nodes = proposal_graph.get("nodes", [])
    name_to_id, name_to_nd, _missing = _resolve_proposal_node_definitions(
        db, current_user, raw_nodes,
    )
    enriched_nodes = _enrich_resolved_nodes(raw_nodes, name_to_id, name_to_nd)
    enriched_graph = {
        "nodes": enriched_nodes,
        "edges": proposal_graph.get("edges", []),
    }

    wf_name = f"_wizard_manual_test/{body.session_id[:8]}"
    db_workflow = crud.create_workflow(
        db,
        workflow=schemas.WorkflowCreate(name=wf_name),
        user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    try:
        db_workflow.is_system = True
        db.flush()
    except Exception:
        pass

    db_version = crud.create_workflow_version(
        db,
        workflow=db_workflow,
        version_create=schemas.WorkflowVersionCreate(
            graph_json=enriched_graph,
        ),
        user_id=current_user.id,
    )

    conf = {
        "tenant_id": current_user.tenant_id,
        "nodes": enriched_nodes,
        "edges": proposal_graph.get("edges", []),
        "project_info": {
            "user_id": current_user.id,
            "workflow_name": wf_name,
            "cog_ids": [source_cog_id],
        },
    }
    db_run = crud.create_workflow_run(
        db,
        version=db_version,
        user_id=current_user.id,
        conf=conf,
        run_name=wf_name,
        purpose="wizard_test_run",
    )
    db.commit()
    workflow_run_id = db_run.id

    def _bg_dispatch():
        try:
            start_workflow_execution(SessionLocal, conf, workflow_run_id)
        except Exception as e:
            logger.warning(
                "wizard manual test-run dispatch failed (bg): %s", e,
            )
    threading.Thread(
        target=_bg_dispatch,
        name=f"wizard-manual-test-{workflow_run_id}",
        daemon=True,
    ).start()

    test_run_id = _persist_wizard_test_run_row(
        db=db,
        wizard_session_id=body.session_id,
        iteration_n=0,
        candidate_graph=proposal_graph,
        test_input_descriptor=descriptor.as_dict(),
        workflow_run_id=workflow_run_id,
        critic_verdict=None,
        critic_reason=None,
    )

    # GSEC FX.16 R-022 — this explicit endpoint call IS the user's
    # confirmation to execute the LLM-generated graph. Record it
    # (metadata only: node/edge names + counts, never payloads).
    try:
        from services.wizard_agent import _graph_summary
        db.add(models.AuditEvent(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="WIZARD_AGENT_TEST_RUN_USER_CONFIRMED",
            details={
                "wizard_session_id": body.session_id,
                "workflow_run_id": workflow_run_id,
                "test_input_tier": descriptor.tier,
                "graph": _graph_summary(proposal_graph),
            },
        ))
        db.commit()
    except Exception as _audit_exc:  # noqa: BLE001
        logger.warning(
            "wizard R-022 confirm audit emit failed: %s", _audit_exc,
        )
        try:
            db.rollback()
        except Exception:
            pass

    return WizardTestRunResponse(
        workflow_run_id=workflow_run_id,
        test_input_tier=descriptor.tier,
        wizard_test_run_id=test_run_id,
    )

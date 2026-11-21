from __future__ import annotations

from fastapi import APIRouter
from ._shared import *  # noqa: F401,F403

# Gate 2b db.query migration — query layer + session-ownership policy
# for this sub-module now live in crud/support_chat_node_wizard.py and
# services/support_chat_node_wizard_service.py (see those modules'
# docstrings).
from crud import support_chat_node_wizard as _crud  # noqa: F401  (used via service)
from services import support_chat_node_wizard_service as _nwsvc
from services._base import ConflictError as _ConflictError
from services._base import NotFoundError as _NotFoundError
from services.support_chat_node_wizard_service import ForbiddenError as _ForbiddenError
from utils.paid_import import _is_absent as _ce_is_absent, optional_paid, require_paid

router = APIRouter()


@router.post("/support/workflow-wizard/apply", response_model=WizardApplyResponse,
             status_code=status.HTTP_201_CREATED)
def workflow_wizard_apply(
    body: WizardApplyRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Materialize the most recent assistant proposal in a wizard session.

    Two paths, branching on the stored proposal's `apply_mode`:
      - "replace" (default): create a NEW Workflow + WorkflowVersion.
      - "merge": load the supplied workflow_id+version_id, validate
        anchors against the live graph, compute the merged graph, and
        persist as a NEW WorkflowVersion of the SAME Workflow.
    Looks up node ids by name against the user's visible NodeDefinitions;
    missing ids → 422 in both modes.
    """
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="user has no tenant")

    # wizard-learning-from-feedback §6.3 — resolve RAG bucket once for
    # this apply request so every apply-mode metric emit is correctly
    # tagged. Same deterministic-hash semantics as the generate handler.
    try:
        from services.wizard_ml.ab import resolve_bucket as _wizard_resolve_bucket
    except ImportError as _exc:
        if not _ce_is_absent(_exc, "services.wizard_ml.ab"):
            raise
        def _wizard_resolve_bucket(*_a, **_kw):  # CE stand-in: control bucket, no A/B
            return "control"

    _rag_bucket = _wizard_resolve_bucket(current_user.tenant_id, "rag")

    try:
        sess = _nwsvc.get_owned_wizard_session(db, body.session_id, current_user.id)
    except _NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
    except _ForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message)
    except _ConflictError as e:
        raise HTTPException(status_code=409, detail=e.message)

    last_assistant = _nwsvc.get_latest_assistant_message(db, sess.id)
    if last_assistant is None:
        raise HTTPException(status_code=409,
                             detail="No assistant turn yet — ask the wizard first")
    msg_meta = last_assistant.token_usage or {}
    proposal = msg_meta.get("proposal")
    stored_intent = msg_meta.get("intent")
    if not proposal or not isinstance(proposal, dict):
        raise HTTPException(status_code=409,
                             detail="Last assistant turn had no concrete proposal — "
                                    "ask the wizard to refine it")

    apply_mode = (proposal.get("apply_mode") or "replace").lower()
    if apply_mode not in ("replace", "merge"):
        apply_mode = "replace"

    # ---------- Merge branch ----------
    if apply_mode == "merge":
        return _apply_merge_proposal(
            db, current_user, body, sess, proposal, stored_intent,
            proposal_id=last_assistant.id,
        )

    # ---------- Replace branch (existing behavior) ----------

    import crud
    import schemas

    nodes_in = proposal.get("nodes") or []
    edges_in = proposal.get("edges") or []

    name_to_id, name_to_nd, missing = _resolve_proposal_node_definitions(
        db, current_user, nodes_in,
    )
    if missing:
        _record_wizard_apply_mode_metric("replace", "missing_nodes", tenant_bucket=_rag_bucket)
        _write_apply_feedback(db, tenant_id=current_user.tenant_id, proposal_id=last_assistant.id,
                              apply_mode="replace", outcome="missing_nodes", body=body, intent=stored_intent)
        # workflow-agentic-execution-loop §G — include node-wizard
        # handoff specs in the 422 payload so the FE can render a
        # "Build this missing node" CTA per missing name without a
        # second round-trip.
        node_build_handoffs: List[Dict[str, Any]] = []
        try:
            from services.wizard_node_handoff import build_node_handoffs
            _nb, _catalog, _allowed = _build_node_catalog(db, current_user)
            for h in build_node_handoffs(
                missing_nodes=set(missing), proposal=proposal,
                catalog_entries=_catalog,
                user_goal=(msg_meta.get("prompt") or ""),
            ):
                node_build_handoffs.append(h.as_dict())
        except Exception as _hand_exc:
            logger.debug("apply: handoff build failed (non-fatal): %s", _hand_exc)
        raise HTTPException(
            status_code=422,
            detail={
                "error": "missing_nodes",
                "message": "Proposal references nodes not visible to you. "
                           "Build them via Node Wizard first.",
                "missing": sorted(missing),
                "node_build_handoffs": node_build_handoffs,
            },
        )

    resolved_nodes = _enrich_resolved_nodes(nodes_in, name_to_id, name_to_nd)
    graph = {"nodes": resolved_nodes, "edges": edges_in}

    # Charge 1 credit after node resolution succeeds and before
    # persistence — a 422 (missing nodes) above pre-empts the charge so
    # the user isn't billed for an unbuildable apply call.
    _pkg_dispatch("_charge_wizard_credits", _charge_wizard_credits, None)(
        db, current_user, WORKFLOW_WIZARD_APPLY_COST, "workflow-wizard apply"
    )

    final_name = (body.name or proposal.get("name") or "Wizard Workflow").strip()
    db_workflow = crud.create_workflow(
        db,
        workflow=schemas.WorkflowCreate(name=final_name),
        user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    db_version = crud.create_workflow_version(
        db,
        workflow=db_workflow,
        version_create=schemas.WorkflowVersionCreate(graph_json=graph),
        user_id=current_user.id,
    )
    _record_wizard_apply_mode_metric("replace", "success", tenant_bucket=_rag_bucket)
    # wizard-learning-from-feedback §3 — write feedback event + per-tenant
    # + cross-tenant embeddings on success. All best-effort; the apply
    # response is built and returned regardless of whether these succeed.
    _fb_id = _write_apply_feedback(
        db, tenant_id=current_user.tenant_id, proposal_id=last_assistant.id,
        apply_mode="replace", outcome="success", body=body, intent=stored_intent,
        applied_node_ids_diff={
            "added_node_ids": [n.get("id") for n in nodes_in if n.get("id")],
            "added_edge_ids": [e.get("id") for e in edges_in if e.get("id")],
        },
        applied_workflow_version_id=db_version.id,
    )
    if _fb_id is not None:
        try:
            from services.wizard_feedback import record_apply_embeddings
            record_apply_embeddings(
                tenant_id=current_user.tenant_id, feedback_event_id=_fb_id,
                canvas=None, prompt=None, proposal=proposal, intent=stored_intent,
                was_edited=bool(getattr(body, "was_edited", False)),
            )
        except Exception as e:
            logger.warning("wizard apply embeddings failed: %s", e)
    return WizardApplyResponse(
        workflow_id=db_workflow.id,
        version_id=db_version.id,
        name=final_name,
        apply_mode="replace",
        applied=None,
    )

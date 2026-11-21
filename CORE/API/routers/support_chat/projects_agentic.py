from __future__ import annotations

from fastapi import APIRouter
from ._shared import *  # noqa: F401,F403

router = APIRouter()


@router.post("/support/project-wizard/{session_id}/acceptance-test",
             response_model=ProjectAcceptanceTestResponse)
def project_wizard_acceptance_test(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§9.1 — run the acceptance test and record its verdict (owner-scoped).

    This is what `/accept` waits on: it refuses (409) until the acceptance
    test has passed against the real example. Running it here grades the
    project's assembled work and records the verdict, after which the owner
    can accept.
    """
    _pa = None
    try:
        import services.project_acceptance as _pa_mod
        _pa = _pa_mod
    except ImportError as _exc:
        if not _ce_is_absent(_exc, "services.project_acceptance"):
            raise
    if _pa is None:
        from utils.paid_import import require_paid
        require_paid("services.project_acceptance", "project acceptance test")

    sess = _get_or_create_session(db, current_user, "project",
                                  session_id=session_id)
    result = _pa.run_acceptance_test(db, sess)
    return ProjectAcceptanceTestResponse(
        project_session_id=sess.id,
        test_passed=result["test_passed"],
        detail=result["detail"],
    )


@router.post("/support/project-wizard/{session_id}/tick",
             response_model=ProjectTickResponse)
def project_wizard_tick(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """§6.1 — advance the project by one step, and run the builds it dispatches.

    Three phases, in order, so dispatch → build → outcome is one closed loop
    driven by repeated ticks:

      1. reconcile builds from earlier ticks that have finished — a success
         records the artifact so its task drops out of the ready set; a
         failure advances the escalation ladder — BEFORE sequencing, so the
         tick sees the current state.
      2. `tick` sequences: it decides what is ready and writes a dispatch
         handoff for each (§6.1).
      3. consume those handoffs: actually run the node build, create a
         project-stamped WorkflowRun, and flip the handoff to consumed.

    Exposed rather than run purely on a schedule so a user can say "get on
    with it" and see exactly what happened, and so an operator can step a
    stuck project by hand.
    """
    _pb = None
    _tick_mod = None
    try:
        import services.project_build as _pb_mod
        _pb = _pb_mod
    except ImportError as _exc:
        if not _ce_is_absent(_exc, "services.project_build"):
            raise
    try:
        import services.project_loop as _pl_mod
        _tick_mod = _pl_mod
    except ImportError as _exc:
        if not _ce_is_absent(_exc, "services.project_loop"):
            raise
    if _pb is None or _tick_mod is None:
        from utils.paid_import import require_paid
        require_paid("services.project_build", "project tick")
        require_paid("services.project_loop", "project tick")
    if _pb is None or _tick_mod is None:
        from utils.paid_import import require_paid
        require_paid("services.project_build", "project tick")
        require_paid("services.project_loop", "project tick")

    sess = _get_or_create_session(db, current_user, "project",
                                  session_id=session_id)
    settled = _pb.reconcile_runs(db, sess)
    out = _tick_mod.tick(db, sess)
    out["settled"] = settled
    out["built"] = _pb.run_pending_dispatches(db, sess)
    return ProjectTickResponse(**out)

"""services/productivity_floor_rollout_dep.py — FastAPI dependency that enforces
the productivity-floor rollout gate.

Usage in a router::

    from fastapi import APIRouter, Depends
    from dependencies import get_db, require_active_and_onboarded_user
    from services.productivity_floor_rollout_dep import require_rollout

    router = APIRouter()

    @router.get("/live-debug")
    def live_debug_endpoint(
        db=Depends(get_db),
        current_user=Depends(require_active_and_onboarded_user),
        _gate=Depends(require_rollout("live_debugging")),
    ):
        ...

WebSocket variant (call *after* the ticket auth resolves the tenant)::

    from services.productivity_floor_rollout_dep import assert_rollout_ws
    from fastapi import WebSocketException

    async def debug_ws_handler(ws: WebSocket, ticket: str, run_id: int):
        token = debug_ws_token(ticket=ticket, run_id=run_id)
        if not token["ok"]:
            await ws.close(code=token["close_code"], reason=token["close_reason"])
            return
        tenant_id = token["tenant_id"]
        db = SessionLocal()
        try:
            assert_rollout_ws(db, tenant_id, "live_debugging")
            # ... proceed with socket loop
        finally:
            db.close()

The WS handler MUST call ``assert_rollout_ws`` after the ticket resolves
``tenant_id`` but BEFORE the main socket loop — that is the ONLY place the
gate matters (the ticket already proved authz; the rollout gate is an *
additional* permission layer).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, WebSocketException
from sqlalchemy.orm import Session

from dependencies import get_db, require_active_and_onboarded_user  # noqa: E402
from services import productivity_floor_rollout  # noqa: E402


def require_rollout(feature: str):
    """Factory — returns an HTTP dependency callable.

    Validates `feature` at *call time* (which is at `Depends(...)` decoration
    time, so a typo fails at app start, not per request).

    Disabled feature  -> HTTPException(403)
    Enabled feature   -> None (dependency succeeds)
    Unknown feature   -> ValueError
    """
    # Validate the feature name immediately (at factory call / Depends time).
    productivity_floor_rollout.is_enabled(None, 1, feature)

    def _dep(
        db: Session = Depends(get_db),
        current_user=Depends(require_active_and_onboarded_user),
    ):
        if not productivity_floor_rollout.is_enabled(
            db, current_user.tenant_id, feature
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Feature '{feature}' is not enabled for this "
                    f"tenant yet."
                ),
            )
        return None

    return _dep


def assert_rollout_ws(
    db: Session, tenant_id: int, feature: str
) -> None:
    """WebSocket rollout gate. Call after the ticket auth resolves *tenant_id*.

    Disabled feature  -> WebSocketException(code=1008)
    Enabled feature   -> None (returns, caller proceeds)
    Unknown feature   -> ValueError
    """
    # Validate the feature name immediately.
    productivity_floor_rollout.is_enabled(None, 1, feature)

    if not productivity_floor_rollout.is_enabled(db, tenant_id, feature):
        raise WebSocketException(
            code=1008,
            reason=(
                f"Feature '{feature}' is not enabled for this "
                f"tenant yet."
            ),
        )

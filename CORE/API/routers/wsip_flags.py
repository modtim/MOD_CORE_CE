"""WSIP 4.5a — per-tenant WSIP flags read endpoint.

Exposes the ``wsip.conversational_debug_enabled`` flag so the WB frontend can
gate the "Why did this happen?" button on the RunRail.

Route (mounted under /v1 by the lead; NOT mounted here):
    GET /v1/wsip/flags -> { "conversational_debug_enabled": bool }

Auth + db dep copied EXACTLY from routers/workflow_causal.py.
Fail-closed: any exception from the read helper -> False, log exception class
name only (never 500).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_bearer_or_api_key
from services import wsip_feature_flags as wsip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wsip", tags=["WSIP Flags"])


class _WsipFlagsOut(BaseModel):
    conversational_debug_enabled: bool


@router.get(
    "/flags",
    response_model=_WsipFlagsOut,
    summary="Read WSIP feature flags for the caller's tenant (WSIP 4.5a)",
)
def get_wsip_flags(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
) -> _WsipFlagsOut:
    """Return WSIP flags for the CALLER's tenant.

    Fail-closed: if the settings read helper raises, log the exception CLASS
    name only and answer ``False`` — never 500.
    """
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="No tenant for this caller")

    try:
        conv_debug = wsip.conversational_debug_enabled(db, tenant_id)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("wsip_flags: conversational_debug_enabled unreadable (%s)",
                       type(exc).__name__)
        conv_debug = False

    return _WsipFlagsOut(conversational_debug_enabled=conv_debug)

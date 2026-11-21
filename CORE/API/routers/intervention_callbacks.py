# API/routers/intervention_callbacks.py
"""Slack interactive-button callback for human-approval gates.

Receives Slack ``block_actions`` payloads (approve / reject buttons on the
``workflow.intervention_required`` notification), verifies Slack's request
signature, resolves the intervention id from the button payload, and calls
through to ``interventions_service.approve`` / ``reject``.

There is no Keycloak session on an inbound Slack webhook, so the acting
identity is resolved as the intervention's own ``assigned_to_user_id`` — the
same tenant-scoped approver `interventions_service._load_for_decision`
already requires `current_user` to match for the dashboard's own
approve/reject buttons (`routers/interventions.py`). The HMAC signature
verified below authorizes "this request came from the tenant's configured
Slack workspace"; it deliberately does NOT substitute a different (e.g.
system-audit) actor, because that would either always fail the assignee
check above (intervention never assigned to that user) or silently bypass
it (a intervention parked with no tenant isolation on who may decide it).

# auth-ok: Slack webhook with HMAC signature verification (no user auth)
# response_model-ok: simple dict (not a full Pydantic model)
"""

import hmac
import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from database import SessionLocal
from services import interventions_service
from services._base import ServiceError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/intervention-callbacks", tags=["intervention-callbacks"])


class SlackBlockActionsResponse(BaseModel):
    """Response shape for POST /intervention-callbacks/slack. Slack expects a
    simple ack body; the ``already_decided`` key is only present on the
    idempotent-replay branch, hence ``response_model_exclude_unset=True`` on
    the route so the two branches keep their distinct (not padded) shapes."""

    ok: bool
    intervention_id: int
    already_decided: Optional[bool] = None


# ---------------------------------------------------------------------------
# Slack signature verification (mirrors trigger_fire_service._verify_slack).
# ---------------------------------------------------------------------------

def _verify_slack(signing_secret: str, ts: Optional[str], body: bytes, sig: Optional[str]) -> bool:
    """Verify a Slack block_actions request per Slack's documented scheme:
    HMAC-SHA256 over ``v0:{timestamp}:{raw_body}``.
    Returns False if any component is missing, timestamp is stale (>5 min),
    or the digest does not match.
    """
    if not (signing_secret and ts and sig):
        return False
    try:
        if abs(time.time() - int(ts)) > 300:  # 5-minute replay window
            return False
    except (ValueError, TypeError):
        return False
    base = b"v0:" + ts.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, "sha256").hexdigest()  # noqa: S324
    return hmac.compare_digest(expected, sig)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_actor(db: Session, intervention_id: int) -> models.User:
    """Resolve the MOD user authorized to decide this intervention.

    Not "whoever clicked" — Slack's webhook has no per-user identity linked
    into MOD accounts here — but the intervention's own assignee, fetched
    fresh by primary key (``db.get``, not a filtered query) so this stays a
    single-row lookup rather than router-side business logic. Raises 404/403
    the same way the dashboard's own approve/reject would if a bare
    ``current_user`` couldn't be resolved for this intervention.
    """
    iv = db.get(models.ManualInterventionState, intervention_id)
    if iv is None:
        raise HTTPException(status_code=404, detail="intervention not found")
    if not iv.assigned_to_user_id:
        raise HTTPException(status_code=403, detail="intervention has no assigned approver")
    actor = db.get(models.User, iv.assigned_to_user_id)
    if actor is None:
        raise HTTPException(status_code=403, detail="assigned approver not found")
    return actor


_SLACK_RESPONSE_URL_HOST = "hooks.slack.com"


def _reply_to_slack(response_url: str, text: str) -> None:
    """POST a plain-text confirmation to Slack's response_url (replaces
    the interactive buttons so a second click can't double-fire).

    The host is restricted to Slack's own response_url domain before the
    fetch, regardless of what the (signature-verified) payload claims it
    is — belt-and-suspenders against a crafted response_url.
    """
    parsed = urlparse(response_url)
    if parsed.scheme != "https" or parsed.hostname != _SLACK_RESPONSE_URL_HOST:
        logger.warning(
            "intervention_callback: refusing response_url POST to non-Slack host %r",
            parsed.hostname,
        )
        return
    try:
        httpx.post(  # ssrf-ok: host restricted to hooks.slack.com above
            response_url,
            json={"text": text},
            timeout=5.0,
        )
    except Exception:  # noqa: BLE001
        logger.exception("intervention_callback: Slack response_url POST failed")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

# api-lint: public-by-design: Slack webhook — HMAC-SHA256 signature over
# v0:{timestamp}:{raw_body} + 5-min replay window verified in _verify_slack
# before any payload parsing or service call (see module docstring / R2 audit
# 1521).
@router.post("/slack", response_model=SlackBlockActionsResponse, response_model_exclude_unset=True)
async def slack_block_actions(
    request: Request,
    X_Slack_Request_Timestamp: Optional[str] = Header(default=None, alias="X-Slack-Request-Timestamp"),
    X_Slack_Signature: Optional[str] = Header(default=None, alias="X-Slack-Signature"),
):
    """Handle Slack ``block_actions`` interaction payloads for intervention
    approve / reject buttons.

    Verifies Slack's HMAC signature, extracts the action_id and value
    (intervention id), and dispatches to the appropriate service function.

    Deliberately does NOT declare a ``payload: str = Form(...)`` parameter.
    FastAPI's ``Form(...)`` dependency resolves by reading the ASGI request
    stream *before* the function body runs; Starlette's request stream is
    single-use, so a later ``await request.body()`` here (needed for HMAC
    verification over the exact raw bytes, per Slack's signing scheme) would
    raise ``RuntimeError: Stream consumed``. That exception is raised before
    this function's own try/except, propagates into this app's
    ``BaseHTTPMiddleware`` stack (``AuditMiddleware`` et al.), and — rather
    than a clean 500 — leaves the request hanging indefinitely (reproduced:
    the "reaped WIP" hangs this file was previously blocked on). Reading the
    body exactly once and parsing the single ``payload`` form field out of it
    ourselves avoids the double-read entirely.
    """
    raw_body = await request.body()

    # Read the signing secret from platform_settings.
    from services.platform_settings import get_setting
    signing_secret = get_setting("slack.signing_secret")
    if not signing_secret:
        logger.warning("intervention_callback: no slack.signing_secret configured")
        raise HTTPException(status_code=500, detail="slack integration misconfigured")

    # Verify Slack signature over the exact raw body bytes (Slack's documented
    # scheme signs the literal request body, not a reconstructed/re-encoded
    # form or JSON value).
    if not _verify_slack(signing_secret, X_Slack_Request_Timestamp, raw_body, X_Slack_Signature):
        logger.warning(
            "intervention_callback: Slack signature verification failed "
            "(ts=%s sig_len=%s)", X_Slack_Request_Timestamp, len(X_Slack_Signature or ""),
        )
        raise HTTPException(status_code=401, detail="invalid slack signature")

    # Slack's block_actions interactions are posted as
    # application/x-www-form-urlencoded with the full interaction JSON in a
    # single `payload` field — parse that field out of the raw body we just
    # verified the signature of (never re-read `request.body()`/`.form()`).
    try:
        form = parse_qs(raw_body.decode("utf-8"))
        payload_values = form.get("payload")
        if not payload_values:
            raise HTTPException(status_code=400, detail="missing payload field")
        data: Dict[str, Any] = json.loads(payload_values[0])
    except HTTPException:
        raise
    except (ValueError, TypeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid payload") from None

    # Extract action and value from the block_actions interaction.
    action = data.get("actions", [{}])[0] if data.get("actions") else {}
    action_id = action.get("action_id", "")
    value = action.get("value", "")  # intervention id string

    if "intervention_resume" not in action_id:
        logger.warning("intervention_callback: unexpected action_id %r", action_id)
        raise HTTPException(status_code=400, detail="unexpected action")

    try:
        intervention_id: int = int(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid intervention id")

    is_approve = action_id.endswith("approve")
    is_reject = action_id.endswith("reject")
    if not (is_approve or is_reject):
        raise HTTPException(status_code=400, detail="unsupported action")

    # Resolve the intervention's own assignee as the acting user (see
    # _resolve_actor docstring for why this is not a system-audit actor).
    db = SessionLocal()
    try:
        actor = _resolve_actor(db, intervention_id)

        notes = "Approved via Slack" if is_approve else "Rejected via Slack"

        if is_approve:
            interventions_service.approve(db, intervention_id, actor, notes)
            reply_text = ":white_check_mark: Intervention approved. Run will continue."
        else:
            interventions_service.reject(db, intervention_id, actor, notes)
            reply_text = ":x: Intervention rejected. Run will fail."

        # Acknowledge Slack's interaction.
        response_url = data.get("response_url")
        if response_url:
            _reply_to_slack(response_url, reply_text)

        return {"ok": True, "intervention_id": intervention_id}

    except ServiceError as exc:
        # Intervention already resolved — idempotent no-op.
        if "Already decided" in str(exc):
            response_url = data.get("response_url")
            if response_url:
                _reply_to_slack(response_url, f"Intervention #{intervention_id} was already decided.")
            return {"ok": True, "intervention_id": intervention_id, "already_decided": True}
        logger.warning("intervention_callback: decision rejected: %s", exc)
        raise HTTPException(status_code=400, detail="intervention decision could not be completed")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("intervention_callback: unexpected error")
        raise HTTPException(status_code=500, detail="internal server error")
    finally:
        db.close()

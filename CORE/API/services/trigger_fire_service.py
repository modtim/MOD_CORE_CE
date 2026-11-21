"""Service layer for `routers/trigger_fire.py` (Gate 2b migration).

Owns the inbound-trigger-fire business/policy logic (shared-token checks,
per-provider signature verification, the webhook/form secret check, the
tenant-scoping 404 trap, and dispatch orchestration via
`services.triggers.pipeline`); delegates all reads to
`crud/trigger_fire.py`. Firing a workflow run (`pipeline.evaluate_and_dispatch`)
and firing file-event triggers (`services.triggers.watchers`) are pre-existing
service-layer calls — this migration relocates them here unchanged (same
functions, same arguments); it does not alter that dispatch logic.

Follows the `services/_base.py` convention: functions raise `ServiceError`
subclasses (never `HTTPException`); the router maps them back to the EXACT
HTTP status/detail the original router raised, byte-for-byte, including the
anti-enumeration traps:
  * a bad webhook/form token, a missing trigger, a wrong-type trigger, an
    inactive trigger, a soft-deleted trigger, and (for api/manual/approval) a
    trigger owned by a different tenant are ALL the identical 404 — never a
    403 — so sequential trigger ids can't be enumerated;
  * the ONE real 403 in this domain is `fire_approval`'s "not an authorized
    approver" check, which only runs after the tenant-scoped trigger has
    already been found (so it never leaks trigger existence).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote_plus

from sqlalchemy.orm import Session

import crud.trigger_fire as trigger_fire_crud
import models
from security import verify_api_key
from services._base import NotFoundError, ServiceError

_expressiveness_metrics: Any = None
try:
    from services.expressiveness_metrics import (
        observe_event_trigger_to_dispatch_latency,
    )
except (ImportError, ModuleNotFoundError):
    pass
from services.triggers import pipeline, watchers

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')


# In SaaS (production) we fail CLOSED: a channel whose shared token is unset is
# REFUSED rather than world-callable. Onprem/dev keeps the open-when-unset
# convenience. (DEPLOYMENT_MODE is the existing saas/onprem switch.)
_FAIL_CLOSED_TOKENS = os.getenv("DEPLOYMENT_MODE", "saas").lower() == "saas"


class UnauthorizedError(ServiceError):
    """A shared-token / signature check failed (router maps -> 401)."""

    def __init__(self, message: str):
        super().__init__(message, code="unauthorized")


class ForbiddenError(ServiceError):
    """Caller is identified but not authorized for this action (router maps
    -> 403). Used only by `fire_approval`'s approver check."""

    def __init__(self, message: str):
        super().__init__(message, code="forbidden")


# ---------------------------------------------------------------------------
# Shared-token / signature verification (channel authentication policy)
# ---------------------------------------------------------------------------


def _shared_token_ok(env_name: str, presented: Optional[str]) -> bool:
    """Constant-time check of a channel's shared token. When the env secret is
    unset: refuse in SaaS (fail-closed), allow in onprem/dev (open convenience)."""
    expected = os.getenv(env_name, "")
    if not expected:
        if _FAIL_CLOSED_TOKENS:
            logger.warning("trigger channel token %s is unset in SaaS mode — refusing fire", env_name)
            return False
        return True
    return bool(presented) and hmac.compare_digest(presented, expected)


def check_shared_token(env_name: str, presented: Optional[str], *, message: str) -> None:
    """Raise `UnauthorizedError(message)` unless `presented` matches the
    channel's shared token (see `_shared_token_ok`)."""
    if not _shared_token_ok(env_name, presented):
        raise UnauthorizedError(message)


def _verify_github(secret: str, body: bytes, sig: Optional[str]) -> bool:
    if not secret or not sig:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _verify_slack(secret: str, ts: Optional[str], body: bytes, sig: Optional[str]) -> bool:
    if not (secret and ts and sig):
        return False
    try:
        if abs(time.time() - int(ts)) > 300:   # replay window
            return False
    except ValueError:
        return False
    base = b"v0:" + ts.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _verify_teams(secret_b64: str, body: bytes, auth: Optional[str]) -> bool:
    if not (secret_b64 and auth and auth.startswith("HMAC ")):
        return False
    try:
        key = base64.b64decode(secret_b64)
    except Exception:
        return False
    digest = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest("HMAC " + digest, auth)


def _dispatch_response(result: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Shared status-code convention: 200 if dispatched, else 202 (accepted
    but not yet dispatched — e.g. dedup/conditions/rate-limit held it back)."""
    status = 200 if result["dispatched"] else 202
    return status, {
        "dispatched": result["dispatched"],
        "reason": result["reason"],
        "run_id": result["run_id"],
    }


# ---------------------------------------------------------------------------
# §8 streaming status
# ---------------------------------------------------------------------------


def streaming_status(db: Session) -> Dict[str, Any]:
    from services.triggers.streaming import streaming_status as _status

    return _status(db)


# ---------------------------------------------------------------------------
# §6.1 / §6.2 file-event watchers
# ---------------------------------------------------------------------------


def dispatch_s3_records(db: Session, payload: Dict[str, Any]) -> int:
    """Fire matching file_event triggers for each S3 `Records` entry in a
    bucket-notification payload. Caller has already verified the shared token."""
    total = 0
    for rec in (payload.get("Records") or []):
        s3 = rec.get("s3", {})
        bucket = (s3.get("bucket") or {}).get("name")
        key = (s3.get("object") or {}).get("key")
        if not bucket or not key:
            continue
        # MinIO/S3 URL-ENCODE the object key: a "/" arrives as %2F and a space as
        # "+". unquote_plus reverses both. Without this, any key inside a prefix
        # (e.g. "uploads%2Fclip.mp4") fails prefix/suffix/glob matching and the
        # trigger silently never fires — the delivery 200s but matches nothing.
        key = unquote_plus(key)
        evt = watchers.normalize_s3_event(rec.get("eventName", ""))
        total += watchers.fire_file_event_triggers(db, source="s3", scope=bucket,
                                                   key=key, event_type=evt)
    return total


# Legacy alias: pre-rename callers (and the `/triggers/minio` webhook router)
# keep calling `dispatch_minio_records`; new code uses `dispatch_s3_records`.
dispatch_minio_records = dispatch_s3_records


def dispatch_fs_event(db: Session, path: str, key: str, event: str) -> int:
    """Fire matching file_event triggers for one on-prem worker filesystem
    event. Caller has already verified the shared token."""
    _t0 = time.perf_counter()
    try:
        return watchers.fire_file_event_triggers(db, source="fs", scope=path,
                                                 key=key, event_type=event)
    finally:
        try:
            observe_event_trigger_to_dispatch_latency(time.perf_counter() - _t0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# §5.1 webhook
# ---------------------------------------------------------------------------


def get_valid_webhook_trigger(
    db: Session, trigger_id: int, token: str
) -> Tuple[models.WorkflowTrigger, bool]:
    """Look up + secret-verify a webhook trigger. Returns `(trigger,
    forward_body)`. Raises `NotFoundError("webhook trigger not found")` for
    ANY failure (missing/wrong-type/inactive/soft-deleted/bad token) — same
    response for all, so a bad secret can't be distinguished from a missing
    trigger (no enumeration oracle on sequential trigger ids)."""
    trig = trigger_fire_crud.get_webhook_trigger(db, trigger_id)
    if not trig:
        raise NotFoundError("webhook trigger not found")
    secret_hash = (trig.configuration or {}).get("secret_hash")
    if not secret_hash or not verify_api_key(token, secret_hash):
        raise NotFoundError("webhook trigger not found")
    forward = (trig.configuration or {}).get("forward_body", True)
    return trig, forward


def dispatch_webhook(
    db: Session, trig: models.WorkflowTrigger, body: Dict[str, Any], forward: bool
) -> Tuple[int, Dict[str, Any]]:
    _t0 = time.perf_counter()
    try:
        event_ctx = {
            "webhook": body,
            "inputs": {"webhook_payload": body} if forward else None,
        }
        result = pipeline.evaluate_and_dispatch(db, trig, event_ctx)
        return _dispatch_response(result)
    finally:
        try:
            observe_event_trigger_to_dispatch_latency(time.perf_counter() - _t0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# §5.2 authenticated API trigger
# ---------------------------------------------------------------------------


def fire_api(
    db: Session, trigger_id: int, payload: Dict[str, Any], user: models.User
) -> Tuple[int, Dict[str, Any]]:
    """Authenticated, tenant-scoped fire — the caller's own workflows only.
    A foreign-tenant trigger is indistinguishable from a missing one (404,
    never 403)."""
    trig = trigger_fire_crud.get_active_trigger(db, trigger_id, models.WorkflowTriggerType.API_CALL)
    if not trig or trig.tenant_id != user.tenant_id:
        raise NotFoundError("api trigger not found")
    res = pipeline.evaluate_and_dispatch(
        db, trig, {"inputs": {"api_payload": payload}}, triggered_by_user_id=user.id)
    return _dispatch_response(res)


# ---------------------------------------------------------------------------
# §5.3 inbound email
# ---------------------------------------------------------------------------


def fire_email(db: Session, body: Dict[str, Any]) -> int:
    """Match EMAIL triggers by `to` address and dispatch each match. Caller
    has already verified the shared token."""
    to = (body.get("to") or "").strip().lower()
    fired = 0
    for trig in trigger_fire_crud.list_active_triggers_of_type(db, models.WorkflowTriggerType.EMAIL):
        if (trig.configuration or {}).get("address", "").strip().lower() != to:
            continue
        ctx = {"email": body, "inputs": {"email": body},
               "event_key": f"email:{trig.id}:{body.get('message_id') or body.get('subject')}"}
        if pipeline.evaluate_and_dispatch(db, trig, ctx).get("dispatched"):
            fired += 1
    return fired


# ---------------------------------------------------------------------------
# §5.5 git / CI
# ---------------------------------------------------------------------------


def fire_git(
    db: Session,
    trigger_id: int,
    raw: bytes,
    x_hub_signature_256: Optional[str],
    x_github_event: Optional[str],
    x_gitlab_token: Optional[str],
    x_gitlab_event: Optional[str],
    x_github_delivery: Optional[str] = None,
) -> Dict[str, Any]:
    """Verified git/CI provider webhook (GitHub HMAC-SHA256 / GitLab token),
    with event + branch filters.

    R-092 replay protection (GitHub only): GitHub delivers a unique
    `X-GitHub-Delivery` id with every push but the HMAC proves nothing about
    time, so a captured request can be replayed. The delivery id is claimed
    in redis (24 h TTL, key namespaced per trigger) before dispatch — a
    replayed id is 200-acked without re-firing. Redis down -> process
    normally (fail-open) with a warning.
    """
    trig = trigger_fire_crud.get_active_trigger(db, trigger_id, models.WorkflowTriggerType.GIT)
    if not trig:
        raise NotFoundError("git trigger not found")
    cfg = trig.configuration or {}
    secret = cfg.get("signing_secret", "")
    if cfg.get("provider") == "github":
        if not _verify_github(secret, raw, x_hub_signature_256):
            raise UnauthorizedError("invalid github signature")
        if x_github_delivery:
            from utils import webhook_replay
            dedup_key = f"webhook:github:delivery:{trig.id}:{x_github_delivery}"
            if not webhook_replay.claim_once(dedup_key):
                logger.info("github delivery replay acked (trigger %s)", trig.id)
                return {"fired": 0, "reason": "duplicate delivery"}
        event = x_github_event
    else:  # gitlab
        if not (x_gitlab_token and hmac.compare_digest(x_gitlab_token, secret)):
            raise UnauthorizedError("invalid gitlab token")
        event = x_gitlab_event
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        payload = {}
    if cfg.get("events") and event not in cfg["events"]:
        return {"fired": 0, "reason": "event filtered"}
    if cfg.get("branch"):
        ref = payload.get("ref", "")
        if not ref.endswith("/" + cfg["branch"]):
            return {"fired": 0, "reason": "branch filtered"}
    ctx = {"git": {"event": event, "ref": payload.get("ref")},
           "inputs": {"git_payload": payload}, "event_key": f"git:{trig.id}:{payload.get('after') or event}"}
    res = pipeline.evaluate_and_dispatch(db, trig, ctx)
    return {"fired": 1 if res["dispatched"] else 0, "reason": res["reason"]}


# ---------------------------------------------------------------------------
# §5.4 chat-ops (Slack / Teams)
# ---------------------------------------------------------------------------


def fire_chatops(
    db: Session,
    provider: str,
    raw: bytes,
    body: Dict[str, Any],
    x_slack_signature: Optional[str],
    x_slack_request_timestamp: Optional[str],
    authorization: Optional[str],
) -> int:
    """Verified chat-ops command (Slack v0 HMAC / Teams outgoing-webhook HMAC)."""
    if provider not in ("slack", "teams"):
        raise NotFoundError("unknown chatops provider")
    command = (body.get("command") or body.get("text") or "").strip()
    fired = 0
    for trig in trigger_fire_crud.list_active_triggers_of_type(db, models.WorkflowTriggerType.CHATOPS):
        cfg = trig.configuration or {}
        if cfg.get("provider") != provider:
            continue
        if cfg.get("command") and cfg["command"] != command:
            continue
        secret = cfg.get("signing_secret", "")
        ok = (_verify_slack(secret, x_slack_request_timestamp, raw, x_slack_signature)
              if provider == "slack" else _verify_teams(secret, raw, authorization))
        if not ok:
            continue
        ctx = {"chatops": body, "inputs": {"chatops": body}}
        if pipeline.evaluate_and_dispatch(db, trig, ctx).get("dispatched"):
            fired += 1
    return fired


# ---------------------------------------------------------------------------
# §7 manual / human
# ---------------------------------------------------------------------------


def fire_manual(
    db: Session, trigger_id: int, payload: Dict[str, Any], user: models.User
) -> Tuple[int, Dict[str, Any]]:
    """Manual fire (the Dashboard 'Run now' button) — authenticated,
    tenant-scoped. A foreign-tenant trigger is indistinguishable from a
    missing one (404, never 403)."""
    trig = trigger_fire_crud.get_active_trigger(db, trigger_id, models.WorkflowTriggerType.MANUAL)
    if not trig or trig.tenant_id != user.tenant_id:
        raise NotFoundError("manual trigger not found")
    res = pipeline.evaluate_and_dispatch(
        db, trig, {"inputs": {"manual_payload": payload}, "actor_user_id": user.id},
        triggered_by_user_id=user.id)
    return _dispatch_response(res)


def fire_approval(
    db: Session, trigger_id: int, decision: str, note: Optional[str], user: models.User
) -> Tuple[int, Dict[str, Any]]:
    """Human-in-the-loop: fire only when an authorized approver approves. A
    foreign-tenant trigger is indistinguishable from a missing one (404); the
    approver check (403) only runs once the tenant-scoped trigger is already
    resolved, so it never leaks trigger existence to an unauthorized caller."""
    trig = trigger_fire_crud.get_active_trigger(db, trigger_id, models.WorkflowTriggerType.APPROVAL)
    if not trig or trig.tenant_id != user.tenant_id:
        raise NotFoundError("approval trigger not found")
    approvers = (trig.configuration or {}).get("approvers")
    if approvers and user.id not in approvers and user.username not in approvers:
        raise ForbiddenError("not an authorized approver")
    if decision != "approve":
        return 200, {"dispatched": False, "reason": "rejected", "run_id": None}
    res = pipeline.evaluate_and_dispatch(
        db, trig, {"inputs": {"approval": {"by": user.id, "note": note}}},
        triggered_by_user_id=user.id)
    return _dispatch_response(res)


def get_valid_form_trigger(db: Session, trigger_id: int, token: str) -> models.WorkflowTrigger:
    """Look up + secret-verify a form trigger. Raises
    `NotFoundError("form trigger not found")` for ANY failure — same response
    for all, so a bad token can't be distinguished from a missing trigger."""
    trig = trigger_fire_crud.get_active_trigger(db, trigger_id, models.WorkflowTriggerType.FORM)
    if not trig:
        raise NotFoundError("form trigger not found")
    secret_hash = (trig.configuration or {}).get("secret_hash")
    if not secret_hash or not verify_api_key(token, secret_hash):
        raise NotFoundError("form trigger not found")
    return trig


def dispatch_form(db: Session, trig: models.WorkflowTrigger, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    res = pipeline.evaluate_and_dispatch(db, trig, {"form": body, "inputs": {"form": body}})
    return _dispatch_response(res)

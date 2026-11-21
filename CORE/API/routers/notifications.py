# API/routers/notifications.py

import asyncio
import json
import logging
import os
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
import redis as redis_lib

import crud
import schemas
import models
from database import SessionLocal
from dependencies import require_active_and_onboarded_user, require_bearer_or_api_key
from dependencies import get_db as deps_get_db
from crud import notifications as crud_notifications
from services._base import NotFoundError
from services.notifications_service import notification_service

logger = logging.getLogger(__name__)

router = APIRouter()


# Response models. Every field is Optional (default None) and extra keys are
# allowed, so these document the existing payloads without being able to
# reject or strip one. Every handler returns all of its model's keys on every
# path, so no null keys are added. /stream returns a StreamingResponse and
# declares response_model=None.


class UnreadCountResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    unread_count: Optional[int] = None


class MessageResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: Optional[str] = None


class MarkAllReadResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: Optional[str] = None
    count: Optional[int] = None


class BulkActionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: Optional[str] = None
    count: Optional[int] = None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------
# Realtime push — SSE channel backed by Redis pub/sub
# ---------------------------------------------------------------------
#
# Flow:
#   1. Every `db.add(Notification(...))` site across the API calls
#      `publish_notification_event(recipient_user_id)` right after
#      db.commit() to fan the insert out via Redis.
#   2. Both bells (Dashboard + WorkflowBuilder) open an EventSource
#      against `GET /notifications/stream` on user login. The handler
#      subscribes to Redis channel `notif:user:<id>` and relays any
#      message as an SSE `data: {...}` frame.
#   3. On message, the client invalidates the unread-count + fetches
#      the top N rows; the polling fallback (30s) still exists so a
#      lost EventSource connection eventually catches up.


def _redis_url() -> str:
    # Reuse the same broker worker_tasks._publish_event uses.
    return os.getenv("REDIS_URL", "redis://redis:6379/2")


def publish_notification_event(recipient_user_id: int) -> None:
    """Fire-and-forget signal for a new notification row. Called
    server-side immediately after every `db.add(Notification(...))`
    + commit. SSE subscribers for this user's channel wake up and
    re-fetch the list. Swallows errors — realtime push is nice to
    have; the 30s poll covers any missed wakeups."""
    try:
        r = redis_lib.Redis.from_url(_redis_url())
        r.publish(f"notif:user:{recipient_user_id}", "1")
    except Exception as e:
        logger.debug("notification publish failed for user %d: %s", recipient_user_id, e)


@router.get("/email-config/", response_model=schemas.EmailConfig)
def read_email_config(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Retrieve the email configuration for the user's tenant.
    """
    db_config = crud.get_email_config(db, tenant_id=current_user.tenant_id)
    if db_config is None:
        return schemas.EmailConfig(
            provider="system",
            sender_name=None,
            sender_email=None,
            smtp_password_set=False,
            sendgrid_api_key_set=False
        )

    return schemas.EmailConfig(
        provider=db_config.provider or "brevo",
        sender_name=db_config.sender_name,
        sender_email=db_config.sender_email,
        smtp_host=db_config.smtp_host,
        smtp_port=db_config.smtp_port,
        smtp_user=db_config.smtp_user,
        smtp_password_set=bool(db_config.encrypted_smtp_password),
        email_from=db_config.email_from,
        sendgrid_api_key_set=bool(db_config.encrypted_sendgrid_api_key)
    )


@router.post("/email-config/", response_model=schemas.EmailConfig)
def update_email_config(
        config: schemas.EmailConfigUpdate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Create or update the email configuration for the user's tenant.
    """
    db_config = crud.create_or_update_email_config(
        db, tenant_id=current_user.tenant_id, config=config, user=current_user
    )
    return schemas.EmailConfig(
        provider=db_config.provider or "brevo",
        sender_name=db_config.sender_name,
        sender_email=db_config.sender_email,
        smtp_host=db_config.smtp_host,
        smtp_port=db_config.smtp_port,
        smtp_user=db_config.smtp_user,
        smtp_password_set=bool(db_config.encrypted_smtp_password),
        email_from=db_config.email_from,
        sendgrid_api_key_set=bool(db_config.encrypted_sendgrid_api_key)
    )


# =============================================================================
# NOTIFICATION ENDPOINTS
# =============================================================================

@router.get("/list", response_model=schemas.NotificationListResponse)
def get_notifications(
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
    include_archived: bool = False,
    category: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get notifications for the current user.

    Args:
        limit: Maximum number of notifications to return (default: 50, max: 100)
        offset: Number of notifications to skip (for pagination)
        unread_only: If True, only return unread notifications
        include_archived: If False (default) archived rows drop out of
            the payload. Pass true to surface them inside the
            "Archived" filter chip.
        category: One of `workflows`, `nodes`, `billing`. Maps each
            chip to a notification_type prefix set (see
            CATEGORY_TO_TYPE_PREFIXES below). `None` means no filter.

    Returns:
        List of notifications, newest first
    """
    notifications, total_count = notification_service.list_for_user(
        db,
        user_id=current_user.id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
        include_archived=include_archived,
        category=category,
    )

    return {
        "notifications": notifications,
        "total_count": total_count
    }


# Chip label → notification_type prefix union. Canonical map now lives in
# crud/notifications.py; re-exported here for backward-compatible imports.
CATEGORY_TO_TYPE_PREFIXES = crud_notifications.CATEGORY_TO_TYPE_PREFIXES


@router.get("/unread-count", response_model=UnreadCountResponse)
def get_unread_count(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get count of unread notifications for the current user.

    Returns:
        { "unread_count": 5 }
    """
    count = notification_service.unread_count(db, user_id=current_user.id)

    return {"unread_count": count}


@router.put("/{notification_id}/mark-read", response_model=MessageResponse)
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Mark a specific notification as read.

    Args:
        notification_id: ID of the notification to mark as read

    Returns:
        Success message
    """
    try:
        notification_service.mark_read(
            db, user_id=current_user.id, notification_id=notification_id
        )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found"
        )

    return {"message": "Notification marked as read"}


@router.put("/mark-all-read", response_model=MarkAllReadResponse)
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Mark all notifications as read for the current user.

    Returns:
        Number of notifications marked as read
    """
    count = notification_service.mark_all_read(db, user_id=current_user.id)

    return {
        "message": f"Marked {count} notification(s) as read",
        "count": count
    }


def _bulk_filter_query(
    db: Session,
    user_id: int,
    *,
    unread_only: bool = False,
    include_archived: bool = False,
    category: str | None = None,
):
    """Build the same filter query the GET /list endpoint uses, for the
    bulk archive/delete endpoints. Mirrors `get_notifications` so that
    the filter chips in the UI scope the bulk action exactly to what
    the user is looking at."""
    return crud_notifications.filtered_query(
        db,
        user_id,
        unread_only=unread_only,
        include_archived=include_archived,
        category=category,
    )


@router.post("/archive-all", response_model=BulkActionResponse)
def archive_all_notifications(
    unread_only: bool = False,
    include_archived: bool = False,
    category: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Archive every notification matching the current filter for the
    current user. Filter params mirror /list — pass the same chip the
    UI is showing.

    Returns:
        { "message": "...", "count": <int> }
    """
    # Archive only currently-non-archived rows. include_archived=true
    # widens the scope; we still skip rows already archived to keep
    # the count meaningful.
    count = notification_service.archive_all(
        db,
        user_id=current_user.id,
        unread_only=unread_only,
        include_archived=include_archived,
        category=category,
    )
    return {
        "message": f"Archived {count} notification(s)",
        "count": count,
    }


@router.post("/delete-all", response_model=BulkActionResponse)
def delete_all_notifications(
    unread_only: bool = False,
    include_archived: bool = False,
    category: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Hard-delete every notification matching the current filter for the
    current user. Destructive; the UI requires the user to type the
    word "delete" to confirm.

    Returns:
        { "message": "...", "count": <int> }
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    count = notification_service.delete_all(
        db,
        user_id=current_user.id,
        unread_only=unread_only,
        include_archived=include_archived,
        category=category,
    )
    _log.info(
        "notifications.delete-all user=%s tenant=%s count=%d "
        "filter={unread_only=%s, include_archived=%s, category=%s}",
        current_user.id, current_user.tenant_id, count,
        unread_only, include_archived, category,
    )
    return {
        "message": f"Deleted {count} notification(s)",
        "count": count,
    }


@router.put("/{notification_id}/archive", response_model=MessageResponse)
def archive_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Archive a notification — soft-hide so it drops out of the
    default /list payload but stays in the DB for audit. Undo-able by
    passing include_archived=true on /list. Delete remains the hard
    option."""
    try:
        notification_service.archive(
            db, user_id=current_user.id, notification_id=notification_id
        )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found"
        )

    return {"message": "Notification archived"}


@router.put("/{notification_id}/unarchive", response_model=MessageResponse)
def unarchive_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Undo archive — puts the row back into the default list view."""
    try:
        notification_service.unarchive(
            db, user_id=current_user.id, notification_id=notification_id
        )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found"
        )

    return {"message": "Notification unarchived"}


@router.delete("/{notification_id}", response_model=MessageResponse)
def delete_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Delete a specific notification.

    Args:
        notification_id: ID of the notification to delete

    Returns:
        Success message
    """
    try:
        notification_service.delete(
            db, user_id=current_user.id, notification_id=notification_id
        )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found"
        )

    return {"message": "Notification deleted"}


# ---------------------------------------------------------------------
# SSE stream — bells subscribe here for realtime pushes
# ---------------------------------------------------------------------


@router.get("/stream", response_model=None)
async def stream_notifications(
    request: Request,
    access_token: str | None = None,
    ticket: str | None = None,
):
    """Server-Sent Events channel for the current user's notifications.

    Auth: EventSource can't set custom headers. Preferred path (WS5.34b) is
    a short-lived, single-use `?ticket=` minted via `POST /stream-tickets`
    with normal Bearer auth — the ticket never carries any identity claims
    itself, it's just a Valkey lookup key, so nothing sensitive lands in
    proxy access logs. The legacy `?access_token=<JWT>` fallback (behind
    `SSE_LEGACY_TOKEN_QUERY_ENABLED`, default on) still works for one
    release so already-cached frontend bundles don't break mid-deploy.

    The handler keeps the HTTP connection open and relays Redis
    pub/sub messages on channel `notif:user:<id>`. On each message the
    client re-fetches /notifications/unread-count + /notifications/list;
    the server side stays stateless beyond "did a new notification for
    this user land since the last poll?". SSE instead of WebSocket
    because nginx proxies plain HTTP cleanly, the traffic is one-way
    (server → client), and the client side is a 3-line EventSource
    shim vs. a full WebSocket protocol.

    Fallback path: if the connection can't hold (proxy timeout,
    browser doesn't support EventSource, network is flaky), both
    bells still poll every 30s. This endpoint is the fast lane, not
    the only lane.
    """
    user_id: int | None = None
    if ticket:
        from services.stream_tickets import redeem_ticket
        payload = redeem_ticket(ticket)
        if not payload or payload.get("scope") != "notifications":
            raise HTTPException(status_code=401, detail="invalid ticket")
        with SessionLocal() as s:
            user = s.query(models.User).filter(
                models.User.id == payload.get("user_id"),
            ).first()
            if not user or user.status != models.UserStatus.ACTIVE:
                raise HTTPException(status_code=403, detail="invalid ticket")
            user_id = user.id
    else:
        from services.stream_tickets import legacy_token_query_enabled
        if not access_token or not legacy_token_query_enabled():
            raise HTTPException(status_code=401, detail="ticket query param required")
        # Resolve user identity from the JWT. Same path the Bearer
        # dependency uses — just sourced from the query string.
        try:
            from keycloak_auth import get_current_user_claims as kc_claims
            claims = kc_claims(access_token)
        except Exception:
            raise HTTPException(status_code=401, detail="invalid token")
        sub = claims.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="token missing sub")
        # Resolve identity in a SHORT-LIVED session that closes before the
        # stream begins. Under pgbouncer transaction pooling, a
        # request-scoped Session (Depends(get_db)) would stay checked out —
        # its read transaction never committed — for the ENTIRE SSE
        # lifetime, pinning a server backend for minutes/hours and starving
        # the pool. The generator below is pure Redis, so once we have the
        # user id we need no DB connection at all.
        with SessionLocal() as s:
            user = s.query(models.User).filter(
                models.User.keycloak_user_id == sub,
            ).first()
            if not user or user.status != models.UserStatus.ACTIVE:
                raise HTTPException(status_code=403, detail="user not active")
            user_id = user.id
    channel = f"notif:user:{user_id}"

    async def event_generator() -> AsyncIterator[bytes]:
        # Each connection gets its own Redis client + pubsub — don't
        # share across users because subscribe is stateful.
        r = redis_lib.Redis.from_url(_redis_url())
        pubsub = r.pubsub(ignore_subscribe_messages=True)
        try:
            pubsub.subscribe(channel)
            # Tell the client it's connected. Browser EventSources
            # don't expose the underlying HTTP status until the first
            # data frame lands, so this unblocks readyState=OPEN.
            yield b": connected\n\n"
            while True:
                # Disconnect detection — if the client closed the tab
                # or navigated away, break the loop so the subscribe
                # releases cleanly.
                if await request.is_disconnected():
                    break
                # Block on Redis for up to 15s, then yield a comment-
                # only keepalive so proxies don't idle us out (nginx
                # default is 60s; 15s keepalive is comfortable).
                # OFFLOAD the blocking read to a thread: redis-py's
                # get_message is synchronous and would otherwise block the
                # uvicorn event loop for up to 15s PER open stream, starving
                # every other request on this worker. asyncio.to_thread keeps
                # the loop free so many SSE streams + normal traffic coexist.
                msg = await asyncio.to_thread(pubsub.get_message, timeout=15.0)
                if msg is None:
                    yield b": keepalive\n\n"
                    continue
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                yield f"data: {json.dumps({'type': 'new', 'signal': data})}\n\n".encode("utf-8")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("SSE stream error for user %d: %s", user_id, e)
        finally:
            try:
                pubsub.unsubscribe(channel)
                pubsub.close()
                r.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx: don't buffer SSE
        },
    )

# ---------------------------------------------------------------------
# GX.S8 — app notification fan-out (POST /v1/notifications)
# ---------------------------------------------------------------------
# Apps (scoped API key / stack identity, via require_bearer_or_api_key)
# fan a notification out to in-tenant users/groups. in-app + email are
# CE; push is PAID (usage_metering seam, TODO stub). See
# services/app_notifications.py.

APP_SEVERITIES = ("info", "warning", "error", "critical")


class AppNotificationTargets(BaseModel):
    model_config = ConfigDict(extra="allow")
    user_ids: Optional[list] = None
    group_ids: Optional[list] = None
    app_user_types: Optional[list] = None


class AppNotificationCreate(BaseModel):
    model_config = ConfigDict(extra="allow")
    targets: AppNotificationTargets
    title: str = Field(..., min_length=1, max_length=200)
    body: Optional[str] = Field(None, max_length=5000)
    severity: str = "info"
    link: Optional[str] = None
    channels: Optional[list] = None
    dedupe_key: Optional[str] = Field(None, max_length=255)

    def validate(self):
        if self.severity not in APP_SEVERITIES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="severity must be one of: info, warning, error, critical")
        for field in ("user_ids", "group_ids"):
            vals = getattr(self.targets, field) or []
            if not all(isinstance(v, int) for v in vals):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"targets.{field} must be a list of integers")
        for v in (self.targets.app_user_types or []):
            if not isinstance(v, str) or not v.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="targets.app_user_types must be a list of strings")
        for v in (self.channels or []):
            if not isinstance(v, str):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="channels must be a list of strings")


@router.post("", response_model=None)
def create_app_notification(
    payload: AppNotificationCreate,
    db: Session = Depends(deps_get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Fan one app-authored notification out (in-app + email = CE;
    push is not part of MOD Community Edition). Cross-tenant target ids are silently dropped and
    counted in `dropped`. `app_user_types` is accepted but returns a
    not_yet_supported note until GX.S1b lands.

    Returns per-channel delivery counts."""
    from services import app_notifications as an

    payload.validate()
    return an.fan_out(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        targets={
            "user_ids": list(payload.targets.user_ids or []),
            "group_ids": list(payload.targets.group_ids or []),
            "app_user_types": list(payload.targets.app_user_types or []),
        },
        title=payload.title,
        body=payload.body,
        severity=payload.severity,
        link=payload.link,
        channels=payload.channels,
        dedupe_key=payload.dedupe_key,
    )

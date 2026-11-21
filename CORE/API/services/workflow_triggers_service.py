"""Service layer for `routers/workflow_triggers.py` (Gate 2b migration).

Owns the trigger-lifecycle business logic (per-type configuration checks,
webhook/form secret minting, audit emission, in-memory snapshot refresh) and
response shaping; delegates all reads to `crud/workflow_triggers.py`. Writes
(create/update/soft-delete) stay here rather than in the CRUD module, matching
the convention set by `services/accelerators_service.py` — the CRUD layer is
query-only, writes are a service concern.

Follows the `services/_base.py` convention: functions raise `ServiceError`
subclasses (never `HTTPException`); the router maps them back to the EXACT
HTTP status/detail shape this domain used before the migration. There was no
403 in the original router (a foreign-tenant trigger/workflow was always a
plain 404, never distinguished from "doesn't exist") so no `ForbiddenError`
is declared here — see the migration's shared rule #5, which applies only
when the original code actually had a 403 to preserve.
"""
from __future__ import annotations

from product_names import themed_logger
import os
import secrets as _secrets
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

import crud.workflow_triggers as triggers_crud
import models
import schemas
from security import hash_api_key
from services._base import NotFoundError, ServiceError

logger = themed_logger(__name__, 'workflows')


class ValidationError(ServiceError):
    """A request fails a cross-row business rule the schema can't check
    (router maps -> 422). `detail` is stored verbatim in `.details` — it may
    be a plain string or the structured `{"loc", "msg", "type"}` dict the
    original router raised, and the router re-raises it unchanged so the HTTP
    body stays byte-identical."""

    def __init__(self, detail: Any):
        message = detail if isinstance(detail, str) else str(detail.get("msg", "Validation error"))
        super().__init__(message, code="unprocessable_entity", details=detail)


class BadRequestError(ServiceError):
    """A request is well-formed but not valid for this trigger's state
    (router maps -> 400)."""

    def __init__(self, message: str):
        super().__init__(message, code="bad_request")


# ---------------------------------------------------------------------------
# Helpers (pure / response-shaping — no db.query; reads go through the CRUD
# module above)
# ---------------------------------------------------------------------------

_SECRET_KEYS = ("secret_hash", "secret_token", "signing_secret")


def strip_secret_keys(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        return cfg
    return {k: v for k, v in cfg.items() if k not in _SECRET_KEYS}


def build_webhook_url(trigger_id: int, token: str) -> str:
    """Public firing URL — derived from BASE_DOMAIN/PROTOCOL/API_SUBDOMAIN env.

    Example: https://example.com/triggers/webhook/42/<token>
    """
    proto = os.getenv("PROTOCOL", "https")
    base = os.getenv("BASE_DOMAIN", "localhost")
    api_sub = os.getenv("API_SUBDOMAIN", "api")
    if base in ("localhost", "127.0.0.1"):
        host = base
    else:
        host = f"{api_sub}.{base}"
    return f"{proto}://{host}/triggers/webhook/{trigger_id}/{token}"


def _create_response(
    trigger: models.WorkflowTrigger,
    *,
    webhook_secret: Optional[str] = None,
    webhook_url: Optional[str] = None,
) -> schemas.WorkflowTriggerCreateResponse:
    return schemas.WorkflowTriggerCreateResponse(
        id=trigger.id,
        workflow_id=trigger.workflow_id,
        tenant_id=trigger.tenant_id,
        trigger_type=trigger.trigger_type.value if trigger.trigger_type else "schedule",
        configuration=strip_secret_keys(trigger.configuration or {}),
        is_active=trigger.is_active,
        created_at=trigger.created_at,
        last_triggered_at=trigger.last_triggered_at,
        trigger_count=trigger.trigger_count or 0,
        webhook_secret=webhook_secret,
        webhook_url=webhook_url,
    )


def _read_response(trigger: models.WorkflowTrigger) -> schemas.WorkflowTriggerResponse:
    return schemas.WorkflowTriggerResponse(
        id=trigger.id,
        workflow_id=trigger.workflow_id,
        tenant_id=trigger.tenant_id,
        trigger_type=trigger.trigger_type.value if trigger.trigger_type else "schedule",
        configuration=strip_secret_keys(trigger.configuration or {}),
        is_active=trigger.is_active,
        created_at=trigger.created_at,
        last_triggered_at=trigger.last_triggered_at,
        trigger_count=trigger.trigger_count or 0,
    )


def _validate_same_tenant_workflow(
    db: Session, workflow_id: int, tenant_id: int, *, field: str
) -> None:
    """Raise `ValidationError` (-> 422) if `workflow_id` doesn't belong to
    `tenant_id`."""
    wf = triggers_crud.get_tenant_workflow(db, workflow_id, tenant_id)
    if not wf:
        raise ValidationError({
            "loc": ["body", "configuration", field],
            "msg": (
                f"{field}={workflow_id} does not reference a workflow in this tenant"
            ),
            "type": "value_error.cross_tenant",
        })


def _validate_calendar_poll_config(
    db: Session, cfg: dict, tenant_id: int
) -> None:
    """sz-4.5c — create/update-time gate for calendar POLL triggers.

    A POLL trigger whose config says `source: calendar` is validated by the
    calendar validator (no url/kind requirement — the http/rss pollers never
    see it) and must point at a LIVE, ACTIVE, same-tenant google/microsoft365
    OAuthConnection. Missing / foreign-tenant / soft-deleted connections give
    one identical message so the endpoint cannot be used to probe which
    connection ids exist in other tenants.
    """
    from services.triggers.calendar_poll import validate_calendar_config
    from models import OAuthConnection

    try:
        validate_calendar_config(cfg)
    except ValueError as e:
        raise ValidationError({
            "loc": ["body", "configuration"],
            "msg": f"calendar configuration invalid: {e}",
            "type": "value_error.invalid_calendar_config",
        })

    conn = (db.query(OAuthConnection)
            .filter(OAuthConnection.id == cfg["connection_id"],
                    OAuthConnection.tenant_id == tenant_id,
                    OAuthConnection.deleted_at.is_(None)).first())
    if conn is None:
        raise ValidationError({
            "loc": ["body", "configuration", "connection_id"],
            "msg": "connection not found",
            "type": "value_error.connection_not_found",
        })
    if conn.provider not in ("google", "microsoft365"):
        raise ValidationError({
            "loc": ["body", "configuration", "connection_id"],
            "msg": ("connection provider must be google or microsoft365 "
                    "for a calendar trigger"),
            "type": "value_error.unsupported_provider",
        })
    if conn.status != "active":
        raise ValidationError({
            "loc": ["body", "configuration", "connection_id"],
            "msg": "connection needs reconnect",
            "type": "value_error.connection_inactive",
        })


def _refresh_snapshots_if_needed(db: Session, trigger_type: str) -> None:
    """Group B handlers (log_pattern, queue_empty) keep in-memory snapshots
    that need refreshing on CRUD writes. Both modules import lazily so this
    module stays import-cheap and can ship in Group A before Group B exists.
    """
    if trigger_type == "log_pattern_alert":
        try:
            from services.log_trigger_handler import refresh_active_snapshot
            refresh_active_snapshot(db)
        except Exception:
            # Not loaded yet (Group B not deployed) — silent.
            pass
    elif trigger_type == "queue_empty":
        try:
            from services.queue_empty_cache import refresh_tenant_cache
            refresh_tenant_cache(db)
        except Exception:
            pass


def get_trigger_or_404(db: Session, trigger_id: int, tenant_id: int) -> models.WorkflowTrigger:
    trigger = triggers_crud.get_trigger(db, trigger_id, tenant_id)
    if not trigger:
        raise NotFoundError("Trigger not found")
    return trigger


# ---------------------------------------------------------------------------
# CRUD (lifecycle)
# ---------------------------------------------------------------------------


def create_trigger(
    db: Session,
    trigger_in: "schemas.WorkflowTriggerCreate",
    current_user: models.User,
) -> schemas.WorkflowTriggerCreateResponse:
    """Create a workflow trigger.

    Returns `WorkflowTriggerCreateResponse`; for `webhook` triggers,
    `webhook_secret` (plaintext) and `webhook_url` are populated. They are
    returned exactly once and never reappear on subsequent reads.
    """
    workflow = triggers_crud.get_tenant_workflow(db, trigger_in.workflow_id, current_user.tenant_id)
    if not workflow:
        raise NotFoundError("Workflow not found")

    cfg = dict(trigger_in.configuration or {})
    plaintext_secret: Optional[str] = None

    ttype = trigger_in.trigger_type

    # add-workflow-triggers §1.2 — trigger types are unlocked (the launch-day-
    # followup-2 #16 cron-only gate is removed). Validate the type is a known
    # WorkflowTriggerType; per-type configuration validation follows below.
    try:
        models.WorkflowTriggerType(ttype)
    except ValueError:
        raise ValidationError(f"unknown trigger_type {ttype!r}")

    if ttype == "workflow_completion":
        _validate_same_tenant_workflow(
            db, cfg["source_workflow_id"], current_user.tenant_id,
            field="source_workflow_id",
        )

    elif ttype in ("webhook", "form"):
        # §7 form reuses the webhook secret mechanism (public fire URL + token).
        plaintext_secret = _secrets.token_urlsafe(32)
        cfg["secret_hash"] = hash_api_key(plaintext_secret)
        # Never persist plaintext.

    elif ttype == "file_event":
        # WS4.Q17.11 — file events for this tenant arrive in its EFFECTIVE
        # bucket (BYO bucket when an ACTIVE connection exists), so the
        # cross-tenant guard must compare against the resolved bucket, not
        # the house format.
        from utils.minio_client import get_tenant_bucket_name
        expected_bucket = get_tenant_bucket_name(current_user.tenant_id, db)
        if cfg.get("bucket") != expected_bucket:
            raise ValidationError({
                "loc": ["body", "configuration", "bucket"],
                "msg": (
                    f"bucket must equal {expected_bucket!r} for this tenant"
                ),
                "type": "value_error.cross_tenant",
            })

    elif ttype == "queue_empty":
        if cfg.get("scope") == "workflow":
            _validate_same_tenant_workflow(
                db, cfg["source_workflow_id"], current_user.tenant_id,
                field="source_workflow_id",
            )

    elif ttype == "message_bus":
        # Only providers with a real subscriber implementation are accepted.
        # kafka/nats/rabbitmq/mqtt are declared in the enum but have no
        # consumer loop yet (see services/triggers/streaming.py) — accepting
        # them would create a trigger that silently never fires. Gate at
        # create-time; lift automatically when a provider's subscriber lands.
        from services.triggers.streaming import (
            IMPLEMENTED_MESSAGE_BUS_PROVIDERS, validate_kafka_topic,
        )
        provider = str(cfg.get("provider") or "").lower()
        if provider not in IMPLEMENTED_MESSAGE_BUS_PROVIDERS:
            raise ValidationError({
                "loc": ["body", "configuration", "provider"],
                "msg": (
                    f"message_bus provider {provider!r} is not available. "
                    f"Supported: {sorted(IMPLEMENTED_MESSAGE_BUS_PROVIDERS)}. "
                    "nats/rabbitmq/mqtt are planned but not yet "
                    "implemented — bridge them to a webhook trigger instead."
                ),
                "type": "value_error.unsupported_provider",
            })
        if provider == "kafka":
            try:
                validate_kafka_topic(cfg.get("topic"))
            except ValueError as e:
                raise ValidationError({
                    "loc": ["body", "configuration", "topic"],
                    "msg": str(e),
                    "type": "value_error.invalid_topic",
                })

    elif ttype == "poll" and cfg.get("source") == "calendar":
        # sz-4.5c — calendar poll: no url/kind, config validated by the
        # calendar validator + the connection gate above.
        _validate_calendar_poll_config(db, cfg, current_user.tenant_id)

    trigger = models.WorkflowTrigger(
        workflow_id=trigger_in.workflow_id,
        tenant_id=current_user.tenant_id,
        created_by_user_id=current_user.id,
        trigger_type=models.WorkflowTriggerType(ttype),
        configuration=cfg,
        is_active=trigger_in.is_active,
    )
    db.add(trigger)
    db.commit()
    db.refresh(trigger)

    _refresh_snapshots_if_needed(db, ttype)

    logger.info(
        "Created %s trigger %d for workflow %d (tenant %d)",
        ttype, trigger.id, trigger_in.workflow_id, current_user.tenant_id,
    )

    # Audit (ISO 27001 §A.12.1.2 change management): trigger creation
    # is a configuration change to a scheduled execution path and must
    # be recorded. The freeform AuditEvent table is the home for this
    # since the AuditAction enum is permission-centric.
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="WORKFLOW_TRIGGER_CREATED",
            details={
                "trigger_id": trigger.id,
                "workflow_id": trigger_in.workflow_id,
                "trigger_type": ttype,
                "is_active": trigger_in.is_active,
                "configuration": strip_secret_keys(cfg),
            },
        ))
        db.commit()
    except Exception:
        logger.exception("audit emission failed for trigger create")

    if ttype == "webhook":
        return _create_response(
            trigger,
            webhook_secret=plaintext_secret,
            webhook_url=build_webhook_url(trigger.id, plaintext_secret),
        )
    if ttype == "form":
        proto = os.getenv("PROTOCOL", "https")
        base = os.getenv("BASE_DOMAIN", "localhost")
        api_sub = os.getenv("API_SUBDOMAIN", "api")
        host = base if base in ("localhost", "127.0.0.1") else f"{api_sub}.{base}"
        return _create_response(
            trigger,
            webhook_secret=plaintext_secret,
            webhook_url=f"{proto}://{host}/triggers/form/{trigger.id}/{plaintext_secret}",
        )
    return _create_response(trigger)


def list_triggers(
    db: Session,
    current_user: models.User,
    *,
    workflow_id: Optional[int] = None,
) -> List[schemas.WorkflowTriggerResponse]:
    """List workflow triggers for this tenant. Optionally filter by workflow_id."""
    triggers = triggers_crud.list_triggers(db, current_user.tenant_id, workflow_id=workflow_id)
    return [_read_response(t) for t in triggers]


def get_trigger(db: Session, trigger_id: int, current_user: models.User) -> schemas.WorkflowTriggerResponse:
    trigger = get_trigger_or_404(db, trigger_id, current_user.tenant_id)
    return _read_response(trigger)


def run_trigger_test(
    db: Session,
    trigger_id: int,
    current_user: models.User,
    *,
    event_ctx: Optional[Dict[str, Any]],
    mode: str,
) -> Dict[str, Any]:
    """add-workflow-triggers §9.2 — run a synthetic event through a trigger.
    `dry_run` (default) reports whether it WOULD fire (conditions/dedup/rate-limit)
    without dispatching; `fire` actually dispatches (tenant-scoped)."""
    trigger = get_trigger_or_404(db, trigger_id, current_user.tenant_id)
    from services.triggers import pipeline
    ctx = event_ctx or {}
    if mode == "fire":
        res = pipeline.evaluate_and_dispatch(db, trigger, ctx,
                                             triggered_by_user_id=current_user.id)
        return {"mode": "fire", **res}
    return {"mode": "dry_run", **pipeline.dry_run(db, trigger, ctx)}


def update_trigger(
    db: Session,
    trigger_id: int,
    trigger_update: "schemas.WorkflowTriggerUpdate",
    current_user: models.User,
) -> schemas.WorkflowTriggerResponse:
    """Update a trigger's configuration or active flag.

    `trigger_type` cannot be changed. `configuration.secret_hash` is server-
    managed; client-supplied values are stripped before persisting. For
    webhook triggers, the existing `secret_hash` is preserved so
    configuration replacement doesn't accidentally invalidate the webhook URL.
    """
    trigger = get_trigger_or_404(db, trigger_id, current_user.tenant_id)
    ttype = trigger.trigger_type.value if trigger.trigger_type else None

    if trigger_update.configuration is not None:
        new_cfg = strip_secret_keys(trigger_update.configuration)
        # Server-managed secrets are stripped from client input above; preserve
        # any that already exist so a config edit (changing a keyword, branch
        # filter, address, …) doesn't silently drop the trigger's secret
        # (secret_hash, signing_secret). Rotation has its own paths (e.g. the
        # webhook rotate-secret endpoint).
        existing = trigger.configuration or {}
        for k in _SECRET_KEYS:
            if k in existing:
                new_cfg[k] = existing[k]
        trigger.configuration = new_cfg

        # Same message_bus provider gate as create — a PATCH must not be a
        # back door to an unimplemented provider that silently never fires.
        if ttype == "message_bus":
            from services.triggers.streaming import IMPLEMENTED_MESSAGE_BUS_PROVIDERS
            provider = str(new_cfg.get("provider") or "").lower()
            if provider not in IMPLEMENTED_MESSAGE_BUS_PROVIDERS:
                raise ValidationError({
                    "loc": ["body", "configuration", "provider"],
                    "msg": (
                        f"message_bus provider {provider!r} is not available. "
                        f"Supported: {sorted(IMPLEMENTED_MESSAGE_BUS_PROVIDERS)}."
                    ),
                    "type": "value_error.unsupported_provider",
                })

        if ttype == "poll" and new_cfg.get("source") == "calendar":
            # sz-4.5c — PATCH is not a back door: a calendar config must
            # re-pass the same create-time gate (validator + live connection).
            _validate_calendar_poll_config(db, new_cfg, current_user.tenant_id)

    if trigger_update.is_active is not None:
        # add-workflow-triggers §1.2 — re-activation gate removed; all trigger
        # types may be (de)activated.
        trigger.is_active = trigger_update.is_active

    db.commit()
    db.refresh(trigger)

    if ttype:
        _refresh_snapshots_if_needed(db, ttype)

    # Audit (ISO 27001 §A.12.1.2): trigger config / active-flag changes
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="WORKFLOW_TRIGGER_UPDATED",
            details={
                "trigger_id": trigger.id,
                "trigger_type": ttype,
                "is_active": trigger.is_active,
                "configuration_keys_changed": list((trigger_update.configuration or {}).keys()),
            },
        ))
        db.commit()
    except Exception:
        logger.exception("audit emission failed for trigger update")

    return _read_response(trigger)


def delete_trigger(db: Session, trigger_id: int, current_user: models.User) -> None:
    """Soft-delete a trigger."""
    trigger = get_trigger_or_404(db, trigger_id, current_user.tenant_id)
    from datetime import datetime, timezone
    trigger.deleted_at = datetime.now(timezone.utc)
    trigger.is_active = False
    db.commit()

    ttype = trigger.trigger_type.value if trigger.trigger_type else None
    if ttype:
        _refresh_snapshots_if_needed(db, ttype)

    # Audit (ISO 27001 §A.12.1.2): trigger deletion is a config change
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="WORKFLOW_TRIGGER_DELETED",
            details={
                "trigger_id": trigger.id,
                "trigger_type": ttype,
                "workflow_id": trigger.workflow_id,
            },
        ))
        db.commit()
    except Exception:
        logger.exception("audit emission failed for trigger delete")

    return None


def rotate_webhook_secret(
    db: Session,
    workflow_id: int,
    trigger_id: int,
    current_user: models.User,
) -> schemas.WorkflowTriggerCreateResponse:
    """Rotate a webhook trigger's secret. Returns plaintext + URL exactly once."""
    trigger = get_trigger_or_404(db, trigger_id, current_user.tenant_id)
    if trigger.workflow_id != workflow_id:
        raise NotFoundError("Trigger not found")
    if trigger.trigger_type != models.WorkflowTriggerType.WEBHOOK:
        raise BadRequestError("rotate-secret is only valid for webhook triggers")

    plaintext_secret, url = _do_rotate(trigger, db)

    logger.info(
        "Rotated webhook secret for trigger %d (tenant %d)",
        trigger.id, current_user.tenant_id,
    )

    return _create_response(
        trigger,
        webhook_secret=plaintext_secret,
        webhook_url=url,
    )


def _do_rotate(trigger: models.WorkflowTrigger, db: Session) -> Tuple[str, str]:
    plaintext_secret = _secrets.token_urlsafe(32)
    cfg = dict(trigger.configuration or {})
    cfg["secret_hash"] = hash_api_key(plaintext_secret)
    trigger.configuration = cfg

    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(trigger, "configuration")

    db.commit()
    db.refresh(trigger)
    return plaintext_secret, build_webhook_url(trigger.id, plaintext_secret)


def trigger_events(db: Session, trigger_id: int, tenant_id: int, *, limit: int) -> Dict[str, Any]:
    """Recent event-arrival history for one tenant-scoped trigger (which
    events arrived, when, and dedup/debounce state)."""
    get_trigger_or_404(db, trigger_id, tenant_id)  # 404s a foreign trigger
    limit = max(1, min(int(limit), 200))
    rows = triggers_crud.list_trigger_events(db, trigger_id, tenant_id, limit=limit)
    return {"trigger_id": trigger_id, "events": [
        {"id": r.id, "event_key": r.event_key,
         "fired_at": r.fired_at.isoformat() if r.fired_at else None,
         "dedupe_until": r.dedupe_until.isoformat() if r.dedupe_until else None,
         "state": r.state}
        for r in rows]}

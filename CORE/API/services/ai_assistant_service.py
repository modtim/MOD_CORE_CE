"""Service layer for the ai_assistant domain (Gate 2b migration of
``routers/ai_assistant.py``).

Owns the policy the router previously inlined next to its ``db.query``
calls (conversation ownership -> 404 vs 403, per migrate_rules #5), the
worker-assignment / task-dispatch business logic, and the one-shot
DAG-analysis orchestration (dispatch + poll + cleanup). See
``services/_base.py`` for the layering contract this follows.

External I/O (the ApiTaskQueue dispatch's best-effort redis wake-up
publish) happens strictly AFTER ``db.commit()`` in ``dispatch_ai_task``
-- never interleaved with an open DB transaction.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, List, Optional, Tuple

from sqlalchemy.orm import Session

import models
from crud import ai_assistant as crud
from services._base import NotFoundError, ServiceError

logger = logging.getLogger(__name__)


class ForbiddenError(ServiceError):
    """Caller is authenticated but the conversation belongs to another
    tenant (router maps -> 403). ``services/_base.py`` ships
    ``NotFoundError`` / ``ConflictError`` but no forbidden — declared
    here per migrate_rules #5."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


# ---------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------

def is_enabled() -> bool:
    return os.getenv("ENABLE_AI_AGENT", "false").lower() == "true"


# ---------------------------------------------------------------------
# Worker assignment (pure policy, no db access)
# ---------------------------------------------------------------------

def get_ai_worker_count() -> int:
    """
    Count available AI workers. The Assistant runs in the Go worker's
    CPU queue via ApiTaskQueue (no Celery, no Airflow). Proper discovery
    would query worker heartbeats; for now the feature flag governs
    availability.
    """
    return 1


def assign_worker_to_conversation(conversation_id: str, worker_count: int) -> Optional[str]:
    """
    Assign a worker to a conversation for sticky routing.
    Uses consistent hashing to route conversation to same worker.
    """
    if worker_count == 0:
        return None
    elif worker_count == 1:
        return "default"
    else:
        # Hash conversation_id to worker index
        worker_idx = hash(conversation_id) % worker_count
        return f"ai_worker_{worker_idx}"


def get_worker_queue(assigned_worker_id: Optional[str]) -> str:
    """Get the ApiTaskQueue queue name for the assigned worker."""
    if assigned_worker_id and assigned_worker_id != "default":
        return assigned_worker_id
    return "CPU_Mid"  # AI tasks use CPU_Mid queue (8-16GB RAM)


def get_status() -> dict:
    """Resolve the `/ai/status` fields from the feature flag + worker count."""
    if not is_enabled():
        return {
            "enabled": False,
            "available_workers": 0,
            "estimated_wait_time": "N/A",
            "message": "AI Assistant is not enabled on this system",
        }

    worker_count = get_ai_worker_count()

    if worker_count == 0:
        return {
            "enabled": True,
            "available_workers": 0,
            "estimated_wait_time": "Queued (no workers available)",
            "message": "No AI workers available. Add an AI-enabled worker to use this feature.",
        }

    wait_time = "30-60s first message, then 1-3s" if worker_count == 1 else "1-3s"
    return {
        "enabled": True,
        "available_workers": worker_count,
        "estimated_wait_time": wait_time,
        "message": f"{worker_count} AI worker(s) available",
    }


# ---------------------------------------------------------------------
# Task dispatch
# ---------------------------------------------------------------------

def dispatch_ai_task(db: Session, conversation_id: Any, message: str, tenant_id: int):
    """Dispatch AI conversation task to the CPU queue.

    The Assistant currently runs in Cloud API / Self-hosted Ollama / Advanced
    modes — none of which require GPU on the claiming worker (the task
    container only does outbound HTTP). LLM_* env vars come from the per-tool
    Vault config via utils.tool_config.resolved_effective_env (Pro delegates
    to the paid ai_config resolver; CE returns {} — unconfigured).
    """
    from utils.minio_client import get_tenant_bucket_name

    # GT.P2.6d (E39) — LLM resolver via the CE shim: Pro delegates to
    # routers.ai_config._resolved_effective_env; CE returns {} so the
    # assistant LLM is "unconfigured" (no LLM_* env vars) rather than
    # crashing on the absent paid module.
    from utils.tool_config import resolved_effective_env
    llm_env = resolved_effective_env("assistant") or {}

    # The gov profile restricts the assistant to local models.
    # The refusal raises BEFORE anything is enqueued, so an external
    # binding never reaches a worker container and nothing falls back to
    # cloud.
    # 1788g CE carve: services/assistant_gov_models.py is UNCLASSIFIED in the
    # manifest (not denied, not api_allow), so it may be absent in a CE build;
    # fetch it defensively via optional_paid. CE default = no gov model
    # restrictions (dispatch_gate returns None to allow), which matches the
    # module's own inert posture on non-gov installs — CE ships no gov
    # profiles, so the gate would be inert there anyway.
    from utils.paid_import import optional_paid
    _assistant_gov_models = optional_paid("services.assistant_gov_models")
    gov_reason = _assistant_gov_models.dispatch_gate(
        db, tenant_id, llm_env,
        conversation_id=str(conversation_id)) \
        if _assistant_gov_models is not None else None
    if gov_reason is not None:
        raise ForbiddenError(gov_reason)

    from utils.registries import platform_registry
    registry = platform_registry()

    # GY.V4b — the prompt leaves this process for a worker container as
    # NEW_MESSAGE, so it is redacted before enqueueing under the
    # `privacy.redaction_profile` setting. Profile off -> the message is the
    # same object as today; any failure passes it through with one warning.
    # Only the env copy is redacted, never the stored conversation row.
    from utils.pii_redaction_surfaces import safe_redact
    new_message = safe_redact(message)

    container_env = {
        "CONVERSATION_ID": str(conversation_id),
        "NEW_MESSAGE": new_message,
        "DATABASE_URL": f"postgresql://{os.getenv('POSTGRES_USER', 'modTech')}:{os.getenv('POSTGRES_PASSWORD', '')}@postgres-app:5432/{os.getenv('POSTGRES_DB', 'modTech')}",
        "API_URL": "http://api:80",
        **llm_env,
    }

    task = models.ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id="ai-conversation",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=3,
        task_payload={
            "node_kind": "ai_conversation",
            "processing_image": f"{registry}/modtim/ai-assistant:latest",
            "container_env": container_env,
            "source_file_location": "",
            "minio_bucket": get_tenant_bucket_name(tenant_id, db),
            "gpu_required": False,
            "container_timeout": 300,
            "skip_download": True,
            "skip_upload": True,
        },
        status=models.ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace="ai_conversation",
    )
    crud.create_ai_task(db, task)
    # Best-effort worker wake-up publish. Runs AFTER the commit above, so
    # this network call never happens with an open DB transaction.
    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass
    return task.id


# ---------------------------------------------------------------------
# Conversation ownership (404-vs-403, migrate_rules #5)
# ---------------------------------------------------------------------

def get_conversation_scoped(
    db: Session, conversation_id: Any, *,
    tenant_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Optional[models.AIConversation]:
    """The ONLY conversation read a request-path caller uses.

    Tenant AND owner filters live in the SQL (see
    ``crud.ai_assistant.get_conversation_scoped``), so another tenant's /
    another user's chat history never materialises here. Returns ``None``
    for "not yours" — the router answers 404 exactly like "does not exist",
    which is what kills the pre-migration existence oracle
    (``get_owned_conversation`` answered 403 for a row that exists in
    another tenant, telling the caller that UUID is real).

    Pass both scopes for a user-authenticated caller; pass only
    ``tenant_id`` for a tenant-scoped service principal (no user context).
    """
    return crud.get_conversation_scoped(
        db, conversation_id, tenant_id=tenant_id, user_id=user_id,
    )


def service_tenant_scope(db: Session, claims: dict) -> Optional[int]:
    """Tenant a service-account token is confined to, or ``None`` when the
    token is cross-tenant by platform design.

    Mirrors ``dependencies._verify_impersonation_service_principal``: an API
    key's service account (``azp`` = ``api-key-<client>``) belongs to the key's
    owner and may only touch that tenant's conversations; the platform service
    client (``azp`` == ``KEYCLOAK_SERVICE_CLIENT_ID``) has no tenant claim and
    is trusted cross-tenant (the AI worker container is one fleet-wide
    principal serving every tenant)."""
    import os

    azp = (claims or {}).get("azp", "")
    if azp == os.getenv("KEYCLOAK_SERVICE_CLIENT_ID", "platform-service"):
        return None
    if not azp.startswith("api-key-"):
        # Unknown principal: the router's require_service_account_token already
        # refused this token, but fail closed here too.
        return -1
    from crud.api_key_cud import get_api_key_by_keycloak_client_id
    from crud.user_cud import get_user as _get_user

    key = get_api_key_by_keycloak_client_id(db, keycloak_client_id=azp)
    if not key:
        return -1
    owner = _get_user(db, key.user_id)
    return owner.tenant_id if owner else -1


def get_owned_conversation(db: Session, conversation_id: Any, tenant_id: int) -> models.AIConversation:
    """DEPRECATED: kept for compatibility only — no request-path
    caller uses it any more.

    It fetched unscoped and compared afterwards, answering ``ForbiddenError``
    (-> 403) for a row that exists in another tenant. That 403-vs-404 split
    is an existence oracle over other tenants' conversation UUIDs. Use
    ``get_conversation_scoped`` (404 for "not yours") instead."""
    conversation = crud.get_conversation(db, conversation_id)
    if conversation is None:
        raise NotFoundError("Conversation not found")
    if conversation.tenant_id != tenant_id:
        raise ForbiddenError("Not your conversation")
    return conversation


def get_conversation_unscoped(db: Session, conversation_id: Any) -> models.AIConversation:
    """SECURITY-SENSITIVE: no ownership check at all.

    Kept ONLY for the worker-facing ``/assistant/service/*`` surface — the
    remaining callers, with the reason each still needs unscoped access:

    * ``routers/ai_assistant.py:get_conversation_service`` and
      ``save_conversation_messages_service`` — the AI worker container
      (Go/DockerContainer) loads and appends to a conversation it was
      dispatched for. It authenticates as a Keycloak service account
      (``require_service_account_token``), which carries no user and, for
      the platform service client, no tenant claim, so there is nothing to
      scope by; the platform service account is cross-tenant by platform
      design (same trust basis as ``dependencies.require_service_account_token``
      and ``get_impersonated_user_from_service_principal``). Those handlers
      now scope whenever the token IS tenant-scoped (an API key's service
      account, ``azp`` = ``api-key-*``) and audit the cross-tenant case
      with ``ADMIN_CROSS_TENANT_ACCESS`` like other allowed privileged reads.

    Never call this from a user-authenticated route: a cross-tenant/cross-user
    read of chat history breaks tenant-data-isolation (ISO 27001 AC-3)."""
    conversation = crud.get_conversation(db, conversation_id)
    if conversation is None:
        raise NotFoundError("Conversation not found")
    return conversation


def list_conversations(
    db: Session, *, user_id: int, tenant_id: int, limit: int,
) -> List[models.AIConversation]:
    return crud.list_conversations_for_user(db, user_id=user_id, tenant_id=tenant_id, limit=limit)


def start_conversation(
    db: Session,
    *,
    node_kind: Optional[str],
    orchestrator_run_id: Optional[str],
    user_id: int,
    tenant_id: int,
    initial_message: str,
) -> models.AIConversation:
    """Create a conversation and dispatch its first message.

    Raises ``ServiceError`` if dispatch fails; the conversation row is
    kept either way (matches pre-migration behavior — no rollback on
    dispatch failure)."""
    worker_count = get_ai_worker_count()
    conversation_id = uuid.uuid4()
    assigned_worker = assign_worker_to_conversation(str(conversation_id), worker_count)

    title = initial_message[:50] + "..." if len(initial_message) > 50 else initial_message

    conversation = crud.create_conversation(
        db,
        conversation_id=conversation_id,
        node_kind=node_kind,
        orchestrator_run_id=orchestrator_run_id,
        user_id=user_id,
        tenant_id=tenant_id,
        assigned_worker_id=assigned_worker,
        title=title,
    )

    try:
        dispatch_ai_task(db, conversation_id, initial_message, tenant_id)
    except ForbiddenError:
        # The refusal carries the reason; the router maps it
        # to a 403, not the generic 503 "unavailable".
        raise
    except Exception as e:
        logger.error("Failed to start AI conversation: %s", e)
        raise ServiceError("Failed to start AI conversation", code="unavailable")

    return conversation


def send_message(db: Session, conversation: models.AIConversation, message: str, tenant_id: int) -> None:
    """Dispatch a follow-up message on an existing conversation."""
    try:
        dispatch_ai_task(db, conversation.conversation_id, message, tenant_id)
    except ForbiddenError:
        # Propagate the refusal reason to the router (403).
        raise
    except Exception as e:
        logger.error("Failed to send message: %s", e)
        raise ServiceError("Failed to send message", code="unavailable")


def append_conversation_messages(
    db: Session,
    conversation: models.AIConversation,
    *,
    user_message: str,
    ai_response: str,
    worker_id: Optional[str] = None,
) -> models.AIConversation:
    return crud.append_messages(
        db, conversation,
        user_message=user_message,
        ai_response=ai_response,
        worker_id=worker_id,
    )


def soft_delete_conversation(db: Session, conversation: models.AIConversation) -> None:
    crud.soft_delete_conversation(db, conversation)


# ---------------------------------------------------------------------
# One-shot DAG analysis
# ---------------------------------------------------------------------

def _fetch_workflow_run_logs(node_kind: str, orchestrator_run_id: str) -> str:
    """
    Fetch failed task logs from the database for AI analysis.
    Reads from api_task_queue (no external orchestrator).

    Uses its own short-lived session (``database.SessionLocal``),
    independent of the request's ``db`` session — matches pre-migration
    behavior (the request session is busy with the poll loop in
    ``run_dag_analysis`` while this runs).
    """
    try:
        from database import SessionLocal
        db = SessionLocal()
        try:
            run = crud.get_workflow_run_by_orchestrator_id(db, orchestrator_run_id)

            if not run:
                return f"Workflow run {orchestrator_run_id} not found."

            failed_tasks = crud.list_failed_tasks_for_run(db, run.id)

            if not failed_tasks:
                return f"Workflow run {run.id}: No failed tasks. All tasks succeeded or are still running."

            log_summary = f"=== Workflow Run #{run.id} ===\n"
            log_summary += f"Status: {run.status.value}\n"
            log_summary += f"Failed Tasks: {len(failed_tasks)}\n\n"

            for task in failed_tasks:
                log_summary += f"--- Node: {task.node_id} (Task #{task.id}) ---\n"
                if task.error_message:
                    log_summary += f"Error: {task.error_message}\n"
                if task.result_payload and task.result_payload.get("log_tail"):
                    log_tail = task.result_payload["log_tail"]
                    if len(log_tail) > 3000:
                        log_tail = log_tail[-3000:]
                    log_summary += log_tail + "\n"
                log_summary += "\n"

            return log_summary
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        return f"Error fetching workflow logs: {str(e)}"


def run_dag_analysis(
    db: Session,
    *,
    node_kind: str,
    orchestrator_run_id: str,
    user_id: int,
    tenant_id: int,
) -> Tuple[str, float]:
    """One-shot AI analysis of a failed workflow run: dispatch + poll +
    cleanup.

    Returns ``(analysis_text, elapsed_seconds)``. Raises ``ServiceError``
    with ``code="unavailable"`` if dispatch fails, or ``code="timeout"``
    if the worker doesn't respond within the wait window (router maps
    these to 503 / 504 respectively)."""
    start_time = time.time()

    logger.info(f"Starting AI analysis for {node_kind}/{orchestrator_run_id}")

    logs_summary = _fetch_workflow_run_logs(node_kind, orchestrator_run_id)

    conversation_id = uuid.uuid4()
    worker_count = get_ai_worker_count()
    assigned_worker = assign_worker_to_conversation(str(conversation_id), worker_count)

    initial_message = f"Analyze this failed workflow and explain what went wrong:\n\n{logs_summary}"

    conversation = crud.create_conversation(
        db,
        conversation_id=conversation_id,
        node_kind=node_kind,
        orchestrator_run_id=orchestrator_run_id,
        user_id=user_id,
        tenant_id=tenant_id,
        assigned_worker_id=assigned_worker,
        title=f"Analysis: {node_kind}",
    )

    logger.info(f"Created temporary conversation: {conversation_id}")

    try:
        dispatch_ai_task(db, str(conversation_id), initial_message, tenant_id)
    except ForbiddenError:
        # Refuse the analysis (the router maps it to 403);
        # clean up the temporary conversation this call just created.
        crud.hard_delete_conversation(db, conversation)
        raise
    except Exception as e:
        crud.hard_delete_conversation(db, conversation)
        logger.error("Failed to trigger AI analysis: %s", e)
        raise ServiceError("Failed to trigger AI analysis", code="unavailable")

    # Poll for AI response (max 120 seconds)
    max_wait_time = 120
    poll_interval = 2
    elapsed = 0

    logger.info("Waiting for AI response...")

    while elapsed < max_wait_time:
        time.sleep(poll_interval)
        elapsed += poll_interval

        # Refresh conversation from DB
        db.refresh(conversation)

        # Check if AI has responded
        if conversation.messages and len(conversation.messages) >= 2:
            # Found AI response (should be 2 messages: user + assistant)
            ai_message = conversation.messages[-1]

            if ai_message.get("role") == "assistant":
                analysis = ai_message.get("content", "No analysis generated")

                total_time = time.time() - start_time
                logger.info(f"AI analysis completed in {total_time:.1f}s")

                return analysis, total_time

        logger.debug(f"Still waiting for AI response... ({elapsed}s elapsed)")

    # Timeout - AI didn't respond in time
    logger.error(f"AI analysis timed out after {max_wait_time}s")

    # Clean up conversation
    crud.hard_delete_conversation(db, conversation)

    raise ServiceError(
        f"AI analysis timed out after {max_wait_time} seconds. The AI worker may be overloaded or unavailable.",
        code="timeout",
    )

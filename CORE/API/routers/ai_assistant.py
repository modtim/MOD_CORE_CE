"""
AI Assistant Router - Distributed Conversational Log Analysis

Manages AI conversations dispatched via the Postgres-backed ApiTaskQueue
to the Go worker (mod-worker). Supports multi-turn conversations with
sticky worker routing.

Gate 2b migration: data access + dispatch/orchestration logic moved to
crud/ai_assistant.py + services/ai_assistant_service.py (see those
modules' docstrings and services/_base.py for the layering contract).
Handlers below are HTTP-only: parse/validate input, map service errors,
shape the response.
"""

import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel, ConfigDict

import models
from dependencies import (get_db, require_active_and_onboarded_user,
                          require_service_account_token,
                          require_auditor_data_content)
from services import ai_assistant_service as _svc
from services._base import NotFoundError as _NotFoundError
from services._base import ServiceError as _ServiceError
from services.ai_assistant_service import ForbiddenError as _ForbiddenError

# clean-v2-api platform-services wave: canonical resource is `/v1/assistant`
# (the grab-bag `/ai` prefix collapsed into it). `_endpoints` carries the
# actual routes with no prefix of its own — populated below by the
# `@_endpoints...` decorators — and `router` mounts it once, at the
# canonical `/assistant` path. The legacy `/ai` dual-mount (kept for
# Dashboard `js/api.js`/`js/logs.js`) has been removed now that all callers
# are repointed (clean-v2-api final clean-cut).
#
# NOTE: `router.include_router(_endpoints, ...)` is called at the BOTTOM of
# this file, after every `@_endpoints...` handler is registered — FastAPI's
# `include_router` copies the target's *current* `.routes` at call time, not
# a live reference, so calling it here (before `_endpoints` has any routes)
# would silently mount an empty router.
_endpoints = APIRouter()
router = APIRouter()

# Service router for worker-container → API communication (no user
# authentication required; uses Keycloak service-account tokens). No grep-proven
# caller of the old `/ai/service/*` path (the AIAssistant DockerContainer talks
# to Postgres directly, not this HTTP surface) — canonical only, no alias.
service_router = APIRouter(
    prefix="/assistant/service",
    tags=["AI Assistant - Service"],
)


# ============================================================================
# Request/Response Models
# ============================================================================

class ConversationStartRequest(BaseModel):
    node_kind: Optional[str] = None  # node-type identifier (NodeDefinition.node_kind)
    orchestrator_run_id: Optional[str] = None  # workflow run identifier
    initial_message: str


class MessageRequest(BaseModel):
    message: str


class MessageResponse(BaseModel):
    role: str
    content: str
    timestamp: str


class ConversationResponse(BaseModel):
    conversation_id: str
    title: Optional[str]
    messages: List[MessageResponse]
    assigned_worker_id: Optional[str]
    created_at: str
    last_activity: str


class AIStatusResponse(BaseModel):
    enabled: bool
    available_workers: int
    estimated_wait_time: str
    message: Optional[str] = None


class DAGAnalysisResponse(BaseModel):
    analysis: str
    startup_time_seconds: float = 0.0
    model: str = "Qwen/Qwen2.5-Coder-7B-Instruct"


# Response models for WS1.R31 (R3: every endpoint declares a response_model).
# House style (WS1.R16): every field Optional[...] = None and extra keys are
# allowed, so these document the existing payloads without being able to reject
# or strip any field; handlers must return all of their model's keys on every
# path so no null keys are introduced.

class MessageItemResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Optional[str] = None
    content: Optional[str] = None
    timestamp: Optional[str] = None


class ConversationSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    conversation_id: Optional[str] = None
    title: Optional[str] = None
    message_count: Optional[int] = None
    last_activity: Optional[str] = None
    created_at: Optional[str] = None


class ConversationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Optional[str] = None
    message: Optional[str] = None


class ConversationDeletedResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Optional[str] = None


# ============================================================================
# Helper Functions
# ============================================================================

def _format_messages(conversation: models.AIConversation) -> List[MessageResponse]:
    """Shape a conversation's raw message dicts into the response schema.
    Pure/no-db — shared by the user-facing and service `get_conversation*`
    handlers."""
    return [
        MessageResponse(
            role=msg.get('role'),
            content=msg.get('content'),
            timestamp=msg.get('timestamp', '')
        )
        for msg in conversation.messages
    ]


# ============================================================================
# API Endpoints
# ============================================================================

@_endpoints.get("/status", response_model=AIStatusResponse)
def get_ai_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Check AI Assistant availability and worker status.
    """
    return AIStatusResponse(**_svc.get_status())


@_endpoints.post("/conversations", response_model=ConversationResponse)
def start_conversation(
    request: ConversationStartRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Start a new AI conversation.
    Optionally attach to a specific workflow run for context.
    """
    # Check if enabled
    if not _svc.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AI Assistant is not enabled"
        )

    try:
        conversation = _svc.start_conversation(
            db,
            node_kind=request.node_kind,
            orchestrator_run_id=request.orchestrator_run_id,
            user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            initial_message=request.initial_message,
        )
    except _ForbiddenError as e:
        # Gov local-models-only refusal (carries the reason)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message)
    except _ServiceError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to start AI conversation"
        )

    return ConversationResponse(
        conversation_id=str(conversation.conversation_id),
        title=conversation.title,
        messages=[],  # Will be populated asynchronously
        assigned_worker_id=conversation.assigned_worker_id,
        created_at=conversation.created_at.isoformat(),
        last_activity=conversation.last_activity.isoformat()
    )


@_endpoints.get("/conversations/{conversation_id}", response_model=ConversationResponse)
def get_conversation(
    conversation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get conversation details and message history.
    """
    # Tenant+owner scoped; "not yours" -> 404 (no existence oracle)
    conversation = _svc.get_conversation_scoped(
        db, conversation_id, tenant_id=current_user.tenant_id, user_id=current_user.id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return ConversationResponse(
        conversation_id=str(conversation.conversation_id),
        title=conversation.title,
        messages=_format_messages(conversation),
        assigned_worker_id=conversation.assigned_worker_id,
        created_at=conversation.created_at.isoformat(),
        last_activity=conversation.last_activity.isoformat()
    )


@_endpoints.post("/conversations/{conversation_id}/messages", response_model=ConversationStatusResponse)
def send_message(
    conversation_id: uuid.UUID,
    request: MessageRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Send a message in an existing conversation.
    Enqueues an AI task on ApiTaskQueue for the Go worker to claim.
    """
    conversation = _svc.get_conversation_scoped(
        db, conversation_id, tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    try:
        _svc.send_message(db, conversation, request.message, current_user.tenant_id)
    except _ForbiddenError as e:
        # Gov local-models-only refusal (carries the reason)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message)
    except _ServiceError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to send message"
        )

    return {"status": "processing", "message": "AI is processing your message"}


# Worker-side saving of an already-generated AI response lives on
# `service_router` (`save_conversation_messages_service`, below) — a second
# unauthenticated handler for the same path here was shadowed by `send_message`
# (FastAPI serves the first registration) and was removed.


@_endpoints.get("/conversations", response_model=List[ConversationSummaryResponse])
def list_conversations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    limit: int = 50
):
    """
    List user's AI conversations.
    """
    conversations = _svc.list_conversations(
        db, user_id=current_user.id, tenant_id=current_user.tenant_id, limit=limit,
    )

    return [
        {
            "conversation_id": str(conv.conversation_id),
            "title": conv.title,
            "message_count": len(conv.messages),
            "last_activity": conv.last_activity.isoformat(),
            "created_at": conv.created_at.isoformat()
        }
        for conv in conversations
    ]


@_endpoints.delete("/conversations/{conversation_id}", response_model=ConversationDeletedResponse)
def delete_conversation(
    conversation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Soft delete a conversation.
    """
    conversation = _svc.get_conversation_scoped(
        db, conversation_id, tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    _svc.soft_delete_conversation(db, conversation)

    return {"status": "deleted"}


# ============================================================================
# One-Shot DAG Analysis (Frontend Integration)
# ============================================================================

@_endpoints.post("/analyze-dag/{node_kind}/{dag_run_id}", response_model=DAGAnalysisResponse)
def analyze_dag_run(
    node_kind: str,
    dag_run_id: str,  # URL param kept for Dashboard compat; semantically the workflow run id
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    One-shot AI analysis of a failed workflow run.

    Called by the frontend "AI Analysis" button. Creates a temporary
    conversation, triggers AI analysis via ApiTaskQueue, waits for the
    result, and returns the analysis synchronously (may take 30-60s on
    first call).
    """
    if not _svc.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AI Assistant is not enabled on this system"
        )

    try:
        analysis, total_time = _svc.run_dag_analysis(
            db,
            node_kind=node_kind,
            orchestrator_run_id=dag_run_id,
            user_id=current_user.id,
            tenant_id=current_user.tenant_id,
        )
    except _ForbiddenError as e:
        # Gov local-models-only refusal (carries the reason)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message)
    except _ServiceError as e:
        if e.code == "timeout":
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=e.message)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=e.message)

    return DAGAnalysisResponse(
        analysis=analysis,
        startup_time_seconds=total_time
    )


# ============================================================================
# Service Endpoints (for DAG-to-API communication)
# ============================================================================

@service_router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
def get_conversation_service(
    conversation_id: uuid.UUID,
    db: Session = Depends(get_db),
    service_claims: dict = Depends(require_service_account_token)
):
    """
    Service endpoint to get conversation details.
    Used by the AI worker container to load conversation context.

    Authenticates via Keycloak service account token (client_credentials grant).
    """
    # The worker container authenticates as a service account, so
    # there is no user to scope by — but an API key's service account (azp
    # "api-key-*") IS tenant-scoped, and must not read another tenant's chat
    # history. The platform service client (cross-tenant by platform design,
    # same trust basis as dependencies.require_service_account_token /
    # get_impersonated_user_from_service_account) gets tenant scope None and
    # keeps the pre-migration unscoped read.
    scope = _svc.service_tenant_scope(db, service_claims)
    try:
        conversation = (
            _svc.get_conversation_scoped(db, conversation_id, tenant_id=scope)
            if scope is not None else _svc.get_conversation_unscoped(db, conversation_id)
        )
    except _NotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return ConversationResponse(
        conversation_id=str(conversation.conversation_id),
        title=conversation.title,
        messages=_format_messages(conversation),
        assigned_worker_id=conversation.assigned_worker_id,
        created_at=conversation.created_at.isoformat(),
        last_activity=conversation.last_activity.isoformat()
    )


@service_router.post("/conversations/{conversation_id}/messages", status_code=200, response_model=ConversationStatusResponse)
def save_conversation_messages_service(
    conversation_id: uuid.UUID,
    user_message: str,
    ai_response: str,
    worker_id: Optional[str] = None,
    db: Session = Depends(get_db),
    service_claims: dict = Depends(require_service_account_token)
):
    """
    Service endpoint to save conversation messages.
    Called by AI container after generating a response.

    Authenticates via Keycloak service account token (client_credentials grant).
    """
    # Same tenant scoping as the GET above (see its comment).
    scope = _svc.service_tenant_scope(db, service_claims)
    try:
        conversation = (
            _svc.get_conversation_scoped(db, conversation_id, tenant_id=scope)
            if scope is not None else _svc.get_conversation_unscoped(db, conversation_id)
        )
    except _NotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    _svc.append_conversation_messages(
        db, conversation,
        user_message=user_message,
        ai_response=ai_response,
        worker_id=worker_id,
    )

    return {"status": "saved"}


# ============================================================================
# Mount `_endpoints` onto `router` (must run after every `@_endpoints...`
# handler above is registered — see the NOTE by `_endpoints = APIRouter()`).
# ============================================================================
router.include_router(
    _endpoints, prefix="/assistant", tags=["AI Assistant"],
    dependencies=[
        Depends(require_active_and_onboarded_user),
        # GY.C16: auditor accounts have no chat/data content unless
        # their grant explicitly sets data_content_granted.
        Depends(require_auditor_data_content),
    ],
)

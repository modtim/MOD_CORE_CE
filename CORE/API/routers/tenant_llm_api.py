"""GX.S5 (launch-completion-megaplan Group X) — tenant LLM endpoint.

Routes (mounted under /v1 in main.py):
    POST /v1/llm/chat/completions    OpenAI-compatible chat completions
                                     (`stream: true` -> SSE passthrough;
                                     `messages[].content` may be a list of
                                     parts incl. `image_url` for vision).
    GET  /v1/llm/models              list the models this tenant can use.

Auth: `node_or_service_principal` — a run_task node token (X-API-Key, 30.4.7
"Stack model call" node) OR the existing Bearer token / scoped app key.
Tenant comes ONLY from the verified token or the authenticated user, never
from the request body. Business logic + the CE/Paid edition seam live in
services/tenant_llm_api.py.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db
from utils.run_task_auth import node_or_service_principal, pin_run_task_tenant

# Terminal WorkflowRun statuses — mirrors routers/mcp_tools_router.py:216-220
# (the existing terminal-status set for WorkflowRun.status).
_LLM_RUN_TASK_TERMINAL_STATUSES = frozenset({
    models.TaskStatus.SUCCESS,
    models.TaskStatus.COMPLETED,
    models.TaskStatus.FAILED,
    models.TaskStatus.FAILURE,
})


def _llm_caller(principal: Union[Dict[str, Any], models.User], db: Session) -> Tuple[int, Optional[int]]:
    """Resolve (tenant_id, user_id) for an LLM call from the verified principal.

    User principal -> (user.tenant_id, user.id), unchanged.
    run_task claims -> tenant ONLY from the verified token; the token's run (when
    present) must exist, belong to that tenant and be non-terminal, else 403.
    The token value is never echoed in any error detail.
    """
    if not isinstance(principal, dict):
        return principal.tenant_id, principal.id

    claims = principal
    tenant_id = int(claims["tenant_id"])
    run_id = claims.get("run_id")
    if run_id is not None:
        run = db.get(models.WorkflowRun, int(run_id))
        if run is None or run.status in _LLM_RUN_TASK_TERMINAL_STATUSES:
            raise HTTPException(
                status_code=403,
                detail="run_task token is not valid for this run.",
            )
        # Cross-tenant guard: the run's tenant (not the body) is the resource.
        pin_run_task_tenant(claims, tenant_id=run.tenant_id, run_id=run.id)
        return run.tenant_id, None
    pin_run_task_tenant(claims, tenant_id=tenant_id)
    return tenant_id, None

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm", tags=["Tenant LLM API"])


# ---------------------------------------------------------------------------
# OpenAI-compatible request shapes
# ---------------------------------------------------------------------------

class ImageUrlPart(BaseModel):
    url: str = Field(..., description="data: URI or https:// URL")
    detail: Optional[str] = None


class ContentPart(BaseModel):
    """One part of a multi-part `content` (text / image_url / ...)."""
    type: str
    text: Optional[str] = None
    image_url: Optional[ImageUrlPart] = None
    # unknown part types pass through untouched (forward-compatible).
    extra: Optional[Dict[str, Any]] = None


class ChatMessageIn(BaseModel):
    role: str
    content: Union[str, List[ContentPart], None] = None
    name: Optional[str] = None

    class Config:
        # Allow unknown extra fields (tool calls etc.) to pass through.
        extra = "allow"


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessageIn]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[Union[str, List[str]]] = None
    n: Optional[int] = None
    seed: Optional[int] = None
    user: Optional[str] = None
    response_format: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    stream_options: Optional[Dict[str, Any]] = None

    class Config:
        # Pass through any OpenAI fields we don't model (logit_bias,
        # logprobs, ...) straight to the backend.
        extra = "allow"


class ChatMessageOut(BaseModel):
    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    reasoning: Optional[str] = None


class ChoiceOut(BaseModel):
    index: int
    message: ChatMessageOut
    finish_reason: Optional[str] = None

    class Config:
        extra = "allow"


class UsageOut(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionOut(BaseModel):
    # Defaults on the identity fields so a slightly non-conforming backend
    # (e.g. a llama.cpp that omits `created`) still validates rather than 500.
    id: str = ""
    object: str = "chat.completion"
    created: int = 0
    model: str = ""
    choices: List[ChoiceOut] = Field(default_factory=list)
    usage: Optional[UsageOut] = None

    class Config:
        extra = "allow"


class ModelOut(BaseModel):
    id: str
    object: str = "model"
    owned_by: str = "mod"
    stack_id: Optional[int] = None
    stack_name: Optional[str] = None
    stack_status: Optional[str] = None
    status: Literal["running", "sleeping", "waking", "stopped"] = "stopped"
    keep_running: bool = False


class ModelListOut(BaseModel):
    object: str = "list"
    data: List[ModelOut]


@router.get("/models", response_model=ModelListOut)
def list_models(
    db: Session = Depends(get_db),
    principal = Depends(node_or_service_principal),
):
    """The models this tenant can call (union of the tenant's stacks'
    served model names). Cross-tenant stacks are never visible.

    Accepts a run_task node token (X-API-Key) in addition to Bearer/app-key
    auth; the tenant is taken from the verified token, never the request."""
    from services import tenant_llm_api
    tenant_id, _user_id = _llm_caller(principal, db)
    return {"object": "list",
            "data": tenant_llm_api.list_models(db, tenant_id)}


@router.post("/chat/completions", response_model=ChatCompletionOut)
def chat_completions(
    payload: ChatCompletionRequest,
    db: Session = Depends(get_db),
    principal = Depends(node_or_service_principal),
):
    """OpenAI-compatible chat completion against one of the tenant's own
    model stacks.

    CE: single-model endpoint, size + rate limits. Paid (`usage_metering`
    capability): per-call usage metering + the monthly token quota.
    `stream: true` returns the backend's `text/event-stream` unchanged.
    """
    from services import tenant_llm_api

    # Forward the full body (incl. extra fields) as-is to the backend —
    # OpenAI compatibility means we don't drop anything the client sent.
    body = payload.model_dump(exclude_none=True)
    tenant_id, user_id = _llm_caller(principal, db)
    tenant = tenant_llm_api.get_tenant(db, tenant_id)

    if payload.stream:
        gen = tenant_llm_api.chat_completion_stream(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            app_id=None,  # GX.A2 adds the apps axis
            tenant=tenant,
            body=body,
        )
        # Returning a Response makes FastAPI skip response_model validation —
        # the SSE stream passes through raw (declared response_model below
        # documents the NON-streaming shape for the docs).
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"},
        )
    return tenant_llm_api.chat_completion(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        app_id=None,  # GX.A2 adds the apps axis
        tenant=tenant,
        body=body,
    )

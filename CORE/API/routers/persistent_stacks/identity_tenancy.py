from __future__ import annotations
from ._shared import *  # noqa: F401,F403
from routers.persistent_stacks import _require_platform_admin, _require_stack_author  # noqa: F401  (defined in __init__; keeps __module__ stable)

from fastapi import APIRouter  # noqa: F811  (local, prefix-free; the aggregator carries prefix/tags/deps)
from dependencies import require_bearer_or_api_key  # noqa: F401,F811
from pydantic import BaseModel  # noqa: F401,F811
from utils.http_errors import ApiError  # noqa: F401,F811
from services import stack_identity as _si  # noqa: F401,F811

router = APIRouter()


def _err(description: str) -> dict:
    """responses= entry shape (persistent_stacks.py:1779) — same ErrorResponse model, own words."""
    return {"model": schemas.ErrorResponse, "description": description}


class StackIdentityRotateOut(BaseModel):
    """The read-back of an on-demand rotation: WHAT happened, never the token.

    `token_expires_at` is the new expiry (metadata), not a secret; the token
    itself only ever travels to the worker over the refresh dispatch."""
    stack_id: int
    status: str
    reason: Optional[str] = None
    token_expires_at: Optional[str] = None

@router.post("/{stack_id}/start", response_model=schemas.PersistentStack)
def start_stack(stack_id: int,
                db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """Resume a deliberately stopped stack.

    Sets desired state back to running so reconciliation resumes caring about
    it. If its host still has the containers, this starts them; if the stack is
    running nowhere, the reconciler closes the gap on its next pass — which is
    why this does not itself dispatch a deploy.
    """
    stack = _get_owned(db, stack_id, user)
    if stack.status == "torn_down":
        raise HTTPException(
            status_code=409,
            detail="Stack was torn down — its storage is gone. Deploy it again "
                   "rather than starting it.")

    stack.desired_state = "running"
    if stack.target_worker:
        _dispatch_stack_task(db, stack.target_worker, stack.tenant_id, "start",
                             _project_name(stack), stack_id=stack.id)
        _publish_event("task:available:CPU")
    stack.status_reason = None
    # persistent-stack-billing §2.3/§4.5 — waking re-arms the meter from NOW, so
    # the stopped window (including a spend-ceiling scale-to-zero) is never
    # back-charged. reservation_units is unchanged (the spec did not change).
    stack.metered_through_at = datetime.now(timezone.utc)
    _audit_stack(db, "STACK_STARTED", stack, user, {"host": stack.target_worker})
    db.commit()
    db.refresh(stack)
    return _out(stack)

@router.put("/{stack_id}/ha-class", response_model=schemas.PersistentStack, dependencies=[Depends(_require_platform_admin)])
def set_ha_class(stack_id: int,
                 ha_class: str,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """Declare whether automatic re-placement is correct for this stack.

    Not a preference — a statement about what the service keeps:

      stateless        nothing to move; re-place freely
      cache_backed     local data whose loss costs time, not correctness. vLLM
                       is this: HuggingFace weights are a cache, so a re-placed
                       stack serves correctly after a cold download
      snapshot_restore data returns from MinIO with an RPO of the last
                       snapshot; re-placement is correct only if that RPO is
                       acceptable for this service
      external         the app replicates its own data; MOD's only job is not
                       running two primaries

    Only `stateless` and `cache_backed` are ever auto-re-placed.
    """
    valid = ("stateless", "cache_backed", "snapshot_restore", "external")
    if ha_class not in valid:
        raise HTTPException(status_code=422,
                            detail=f"ha_class must be one of {valid}")
    stack = _get_owned(db, stack_id, user)
    previous = stack.ha_class
    stack.ha_class = ha_class
    _audit_stack(db, "STACK_HA_CLASS_SET", stack, user,
                 {"from": previous, "to": ha_class})
    db.commit()
    db.refresh(stack)
    return _out(stack)

@router.post(
    "/{stack_id}/identity-rotate",
    response_model=StackIdentityRotateOut,
    responses={
        404: _err("No stack with that id is owned by the caller (code not_found)."),
        409: _err("code stack_deleted: the stack row is soft-deleted; or code "
                  "rotation_in_progress: another rotation holds this stack's lock."),
    },
)
def rotate_stack_identity(stack_id: int,
                         db: Session = Depends(get_db),
                         user: models.User = Depends(require_bearer_or_api_key)):
    """30.6.8 c8-B — ask for an identity rotation NOW instead of waiting for the tick.

    Owner-only (`_get_owned` runs FIRST, so a foreign tenant's stack 404s) and
    under the router-wide STACK_AUTHOR gate. All the hard parts — the per-stack
    advisory lock, the re-mint, the redeploy_required gates and the
    `refresh_secret_files` dispatch — live in `push_rotated_identity`, which is
    called EXACTLY ONCE and never re-implemented here. `redeploy_required` is a
    200, not an error: nothing was rotated in place, so the UI tells the user to
    redeploy. Only a lock collision (another rotation already running on this
    stack) is a 409. No token, and nothing token-shaped, is echoed or audited.
    """
    stack = _get_owned(db, stack_id, user)
    if stack.status == "deleted":
        raise ApiError(409, "stack_deleted", "Persistent stack is deleted")

    result = _si.push_rotated_identity(db, stack)
    status, reason = result["status"], result.get("reason")

    if status == "skipped" and reason in ("in_progress", "lock_unreadable"):
        raise ApiError(409, "rotation_in_progress",
                       "Another identity rotation already holds this stack's lock")

    _audit_stack(db, "STACK_IDENTITY_ROTATE_REQUESTED", stack, user,
                 {"result": status, "reason": reason})
    db.commit()
    return {"stack_id": stack.id, "status": status, "reason": reason,
            "token_expires_at": result.get("token_expires_at")}


__all__ = [
    'start_stack',
    'set_ha_class',
    'rotate_stack_identity',
]

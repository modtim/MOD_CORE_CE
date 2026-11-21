from __future__ import annotations
from ._shared import *  # noqa: F401,F403
from routers.persistent_stacks import _require_platform_admin, _require_stack_author  # noqa: F401  (defined in __init__; keeps __module__ stable)

from fastapi import APIRouter  # noqa: F811  (local, prefix-free; the aggregator carries prefix/tags/deps)
stack_router = APIRouter()

@stack_router.post("/{stack_id}/start", response_model=schemas.PersistentStack)
def st_start_stack(stack_id: int,
                   to_worker: Optional[str] = None,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(_require_stack_author)):
    """Start (deploy) one of the caller's stacks."""
    # Gate-2a split fix: deploy_stack lives in the persistent_stacks sibling and
    # is re-exported on the package; `from ._shared import *` does NOT bring
    # sibling names into scope. Resolve through the package at call time so it is
    # monkeypatch-overridable in tests. Function-local import avoids any cycle.
    import routers.persistent_stacks as _pkg
    return _pkg.deploy_stack(stack_id=stack_id, to_worker=to_worker, db=db, user=user)

@stack_router.post("/{stack_id}/stop", response_model=schemas.PersistentStack)
def st_stop_stack(stack_id: int,
                  db: Session = Depends(get_db),
                  user: models.User = Depends(_require_stack_author)):
    """Stop one of the caller's stacks without tearing it down."""
    # Gate-2a split fix: stop_stack lives in the persistent_stacks sibling and is
    # re-exported on the package; resolve through the package at call time.
    import routers.persistent_stacks as _pkg
    return _pkg.stop_stack(stack_id=stack_id, db=db, user=user)

__all__ = [
    'st_start_stack',
    'st_stop_stack',
]

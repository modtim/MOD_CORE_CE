"""stack-definition-edit — durable persistent-stack revision service.

Mirrors ``services/node_source_revisions.py`` exactly: a stack's composition +
config (``PersistentStack.compose_spec`` + ``storage_class``) is stored here as
an ordered, append-only, FOREVER-RETAINED chain (``persistent_stack_revision``)
so every edit can be listed, diffed and reverted to without destroying history
(ISO 27001 — no hard deletes). ``PersistentStack.compose_spec`` itself always
holds the CURRENT revision's content; this module owns create / list /
get-latest / diff / revert plus lazy materialization of revision 1 for a stack
that predates this table (every stack that exists today).

Unlike node revisions there is no explicit delete/tier-aware-pruning path here
— a stack's edit history is small (bounded by how often a human edits it) and
nothing in this feature calls for reaping it.
"""
from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional

import yaml
from sqlalchemy import and_
from sqlalchemy.orm import Session

import models

# Canonical origins.
ORIGIN_CREATED = "created"
ORIGIN_EDITED = "edited"
ORIGIN_REVERTED = "reverted"


# --- reads ------------------------------------------------------------------

def list_revisions(db: Session, stack_id: int) -> List[models.PersistentStackRevision]:
    """All revisions for a stack, oldest first. Nothing here is ever deleted."""
    return (db.query(models.PersistentStackRevision)
            .filter(models.PersistentStackRevision.stack_id == stack_id)
            .order_by(models.PersistentStackRevision.revision_no.asc())
            .all())


def latest(db: Session, stack_id: int) -> Optional[models.PersistentStackRevision]:
    """The current (highest revision_no) revision for a stack, or None."""
    return (db.query(models.PersistentStackRevision)
            .filter(models.PersistentStackRevision.stack_id == stack_id)
            .order_by(models.PersistentStackRevision.revision_no.desc())
            .first())


def get(db: Session, revision_id: int) -> Optional[models.PersistentStackRevision]:
    return db.get(models.PersistentStackRevision, revision_id)


def get_by_no(db: Session, stack_id: int, revision_no: int
             ) -> Optional[models.PersistentStackRevision]:
    return (db.query(models.PersistentStackRevision)
            .filter(and_(models.PersistentStackRevision.stack_id == stack_id,
                         models.PersistentStackRevision.revision_no == revision_no))
            .first())


def _next_revision_no(db: Session, stack_id: int) -> int:
    cur = (db.query(models.PersistentStackRevision.revision_no)
           .filter(models.PersistentStackRevision.stack_id == stack_id)
           .order_by(models.PersistentStackRevision.revision_no.desc())
           .first())
    return (cur[0] + 1) if cur else 1


# --- writes -----------------------------------------------------------------

def create_revision(
    db: Session,
    stack: "models.PersistentStack",
    *,
    origin: str,
    author_user_id: Optional[int],
    compose_spec: Dict[str, Any],
    storage_class: Optional[str] = None,
    message: Optional[str] = None,
    parent_revision_id: Optional[int] = None,
    commit: bool = True,
    idempotency_key: Optional[str] = None,
    key_id: Optional[int] = None,
) -> models.PersistentStackRevision:
    """Append a new revision to a stack's chain. ``revision_no`` auto-increments.
    If ``parent_revision_id`` is not given it defaults to the current latest.
    Does NOT touch ``stack.compose_spec`` itself — the caller writes the live
    row; this only records the durable history entry for it."""
    if parent_revision_id is None:
        cur = latest(db, stack.id)
        parent_revision_id = cur.id if cur else None
    rev = models.PersistentStackRevision(
        stack_id=stack.id,
        stack_name=stack.name,
        tenant_id=stack.tenant_id,
        revision_no=_next_revision_no(db, stack.id),
        parent_revision_id=parent_revision_id,
        origin=origin,
        author_user_id=author_user_id,
        compose_spec=compose_spec,
        storage_class=storage_class if storage_class is not None else stack.storage_class,
        message=message,
        # 30.6.3 c3-J4c — the deploy credential that carried this revision
        # (defaults keep every existing caller unchanged).
        idempotency_key=idempotency_key,
        key_id=key_id,
    )
    db.add(rev)
    if commit:
        db.commit()
        db.refresh(rev)
    else:
        db.flush()
    return rev


def revert(db: Session, stack: "models.PersistentStack", target_revision_id: int,
           *, author_user_id: Optional[int]) -> models.PersistentStackRevision:
    """Create a NEW revision whose content equals ``target_revision_id``,
    preserving the intervening history (never rewrites past revisions)."""
    target = get(db, target_revision_id)
    if target is None or target.stack_id != stack.id:
        raise ValueError("target revision does not belong to this stack")
    return create_revision(
        db, stack,
        origin=ORIGIN_REVERTED,
        author_user_id=author_user_id,
        compose_spec=dict(target.compose_spec or {}),
        storage_class=target.storage_class,
    )


# --- legacy materialization ---------------------------------------------------

def materialize_initial(db: Session, stack: "models.PersistentStack"
                        ) -> models.PersistentStackRevision:
    """Build revision 1 for a stack that has none yet, from whatever
    ``compose_spec`` currently holds (create / import / wizard / template —
    there is no source of truth for it before this table existed other than
    the live row)."""
    existing = latest(db, stack.id)
    if existing is not None:
        return existing
    return create_revision(
        db, stack,
        origin=ORIGIN_CREATED,
        author_user_id=stack.created_by_user_id,
        compose_spec=dict(stack.compose_spec or {}),
        storage_class=stack.storage_class,
        message="materialized from the stack's current definition",
    )


# --- diff ---------------------------------------------------------------------

def _yaml_lines(spec: Dict[str, Any]) -> List[str]:
    return yaml.safe_dump(spec or {}, default_flow_style=False,
                          sort_keys=True).splitlines(keepends=True)


def diff(db: Session, stack_id: int, from_revision_no: int, to_revision_no: int
         ) -> Dict[str, Any]:
    """Unified diff of the two revisions' compose specs, plus a coarse
    per-service change summary (added/removed/changed service names) for a
    friendlier UI than a raw YAML diff alone."""
    a = get_by_no(db, stack_id, from_revision_no)
    b = get_by_no(db, stack_id, to_revision_no)
    if a is None or b is None:
        raise ValueError("revision number not found for this stack")

    left = _yaml_lines(a.compose_spec)
    right = _yaml_lines(b.compose_spec)
    unified = "".join(difflib.unified_diff(
        left, right,
        fromfile=f"rev{from_revision_no}/compose_spec.yaml",
        tofile=f"rev{to_revision_no}/compose_spec.yaml",
    ))

    svc_a = set(((a.compose_spec or {}).get("services") or {}).keys())
    svc_b = set(((b.compose_spec or {}).get("services") or {}).keys())
    added = sorted(svc_b - svc_a)
    removed = sorted(svc_a - svc_b)
    changed = sorted(
        name for name in (svc_a & svc_b)
        if ((a.compose_spec or {})["services"].get(name)
            != (b.compose_spec or {})["services"].get(name))
    )

    return {
        "unified_diff": unified,
        "services_added": added,
        "services_removed": removed,
        "services_changed": changed,
    }


def revision_summary(rev: models.PersistentStackRevision) -> Dict[str, Any]:
    return {
        "id": rev.id,
        "revision_no": rev.revision_no,
        "origin": rev.origin,
        "author_user_id": rev.author_user_id,
        "message": rev.message,
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
    }

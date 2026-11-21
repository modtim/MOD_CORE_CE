"""node-code-edit-rebuild §1 — durable node source revision service.

The auto-generated / hand-edited source for a node lives in the
``node_source_revision`` table as an ordered chain and is the durable source of
truth (``build_specs.spec_json`` is GC'd). This module owns create / list /
get-latest / diff / revert / explicit-delete plus lazy materialization of
revision 1 for legacy nodes.

RETENTION: explicitly deleted revision tombstones are pruned by
``prune_expired`` after 90 days. ``delete_revision`` itself is unchanged —
it only sets ``deleted_at`` (explicit, audited) and the row is retained at
that point. Age-based reaping of those tombstones is ``prune_expired``'s job,
wired into the §11b sweep in ``services/scheduler.py``. The router that calls
``delete_revision`` is still responsible for emitting the deletion's own
audit event (it holds request context); ``prune_expired`` reports what it
purged back to its caller so *that* emits the retention-expiry audit event.
"""
from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

import models

# Canonical origins.
ORIGIN_GENERATED = "generated"
ORIGIN_COPIED = "copied"
ORIGIN_EDITED = "edited"
ORIGIN_REVERTED = "reverted"


# --- reads ------------------------------------------------------------------

def list_revisions(db: Session, node_id: int, *, include_deleted: bool = False
                   ) -> List[models.NodeSourceRevision]:
    """All revisions for a node, oldest first."""
    q = db.query(models.NodeSourceRevision).filter(
        models.NodeSourceRevision.node_definition_id == node_id)
    if not include_deleted:
        q = q.filter(models.NodeSourceRevision.deleted_at.is_(None))
    return q.order_by(models.NodeSourceRevision.revision_no.asc()).all()


def latest(db: Session, node_id: int) -> Optional[models.NodeSourceRevision]:
    """The current (highest revision_no, non-deleted) revision, or None."""
    return (db.query(models.NodeSourceRevision)
            .filter(and_(models.NodeSourceRevision.node_definition_id == node_id,
                         models.NodeSourceRevision.deleted_at.is_(None)))
            .order_by(models.NodeSourceRevision.revision_no.desc())
            .first())


def get(db: Session, revision_id: int) -> Optional[models.NodeSourceRevision]:
    return db.get(models.NodeSourceRevision, revision_id)


def _next_revision_no(db: Session, node_id: int) -> int:
    cur = (db.query(models.NodeSourceRevision.revision_no)
           .filter(models.NodeSourceRevision.node_definition_id == node_id)
           .order_by(models.NodeSourceRevision.revision_no.desc())
           .first())
    return (cur[0] + 1) if cur else 1


# --- writes -----------------------------------------------------------------

def create_revision(
    db: Session,
    node: "models.NodeDefinition",
    *,
    origin: str,
    author_user_id: Optional[int],
    hero: Optional[Dict[str, Any]] = None,
    source_overrides_by_variant: Optional[Dict[str, Any]] = None,
    variants_snapshot: Optional[Any] = None,
    critic_warnings: Optional[Any] = None,
    parent_revision_id: Optional[int] = None,
    commit: bool = True,
) -> models.NodeSourceRevision:
    """Append a new revision to a node's chain. ``revision_no`` auto-increments.
    If ``parent_revision_id`` is not given it defaults to the current latest."""
    if parent_revision_id is None:
        cur = latest(db, node.id)
        parent_revision_id = cur.id if cur else None
    rev = models.NodeSourceRevision(
        node_definition_id=node.id,
        node_name=node.name,
        tenant_id=node.tenant_id,
        revision_no=_next_revision_no(db, node.id),
        parent_revision_id=parent_revision_id,
        origin=origin,
        author_user_id=author_user_id,
        hero=hero or {},
        source_overrides_by_variant=source_overrides_by_variant or {},
        variants_snapshot=variants_snapshot,
        critic_warnings=critic_warnings,
    )
    db.add(rev)
    if commit:
        db.commit()
        db.refresh(rev)
    else:
        db.flush()
    return rev


def revert(db: Session, node: "models.NodeDefinition", target_revision_id: int,
           *, author_user_id: Optional[int]) -> models.NodeSourceRevision:
    """Create a NEW revision whose content equals ``target_revision_id``,
    preserving the intervening history (never rewrites past revisions)."""
    target = get(db, target_revision_id)
    if target is None or target.node_definition_id != node.id:
        raise ValueError("target revision does not belong to this node")
    return create_revision(
        db, node,
        origin=ORIGIN_REVERTED,
        author_user_id=author_user_id,
        hero=dict(target.hero or {}),
        source_overrides_by_variant=dict(target.source_overrides_by_variant or {}),
        variants_snapshot=target.variants_snapshot,
    )


def delete_revision(db: Session, revision_id: int, *, actor_user_id: Optional[int],
                    commit: bool = True) -> Optional[models.NodeSourceRevision]:
    """Explicit, audited delete. Sets ``deleted_at`` + ``deleted_by_user_id`` and
    RETAINS the row — retention of that tombstone from here on is tier-aware,
    see ``prune_expired``. The caller emits the deletion audit event."""
    from datetime import datetime, timezone
    rev = get(db, revision_id)
    if rev is None:
        return None
    rev.deleted_at = datetime.now(timezone.utc)
    if hasattr(rev, "deleted_by_user_id"):
        rev.deleted_by_user_id = actor_user_id
    if commit:
        db.commit()
    return rev


# --- retention ---------------------------------------------------------------

def prune_expired(db: Session, *, days: int = 90,
                   skip_tenant=None,
                   ) -> List[Dict[str, Any]]:
    """Age-prune explicitly-deleted revision tombstones (rows where
    ``delete_revision`` already set ``deleted_at``) once they are older than
    ``days``. An active (non-deleted) revision in a node's chain is NEVER
    touched by age alone — this only reaps rows the caller already chose to
    delete, exactly like the other tables in the §11b sweep.

    Tombstones older than ``days`` (default 90) are hard-deleted.

    Returns one ``{"id": int, "tenant_id": Optional[int]}`` entry per row
    hard-deleted (captured before the delete) so the caller can emit its own
    ``CONTENT_RETENTION_EXPIRED`` audit trail — this module has no request
    context to attribute an actor with, so it does not audit-log itself.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    rows = (
        db.query(models.NodeSourceRevision)
        .filter(models.NodeSourceRevision.deleted_at.isnot(None))
        .filter(models.NodeSourceRevision.deleted_at < cutoff)
        .execution_options(include_deleted=True)
        .order_by(models.NodeSourceRevision.deleted_at.asc())
        .all()
    )
    purged: List[Dict[str, Any]] = []
    for row in rows:
        # GY.C15 — legal hold: skip revisions belonging to a held tenant.
        if skip_tenant is not None and skip_tenant(row.tenant_id):
            continue
        purged.append({"id": int(row.id), "tenant_id": row.tenant_id})
        # ISO-exception: transient retention reaper — these rows were ALREADY
        # soft-deleted by delete_revision() (deleted_at set) and have aged past
        # the retention window; this is the periodic
        # maintenance purge of expired tombstones, hard-delete required (the
        # audited soft-delete happens at delete_revision, and the caller emits
        # CONTENT_RETENTION_EXPIRED for the final purge).
        db.delete(row)
    if purged:
        db.commit()
    return purged


# --- diff -------------------------------------------------------------------

def _files_of(rev: "models.NodeSourceRevision") -> Dict[str, str]:
    """Flatten a revision into {path: text} for diffing across hero + variants."""
    out: Dict[str, str] = {}
    hero = rev.hero or {}
    if hero.get("dockerfile"):
        out["hero/Dockerfile"] = hero["dockerfile"]
    if hero.get("entrypoint_sh"):
        out["hero/entrypoint.sh"] = hero["entrypoint_sh"]
    if hero.get("build_and_push_sh"):
        out["hero/build_and_push.sh"] = hero["build_and_push_sh"]
    for f in (hero.get("source_files") or []):
        if isinstance(f, dict) and f.get("filename"):
            out[f"hero/{f['filename']}"] = f.get("content", "")
    for variant, files in (rev.source_overrides_by_variant or {}).items():
        for f in (files or []):
            if isinstance(f, dict) and f.get("filename"):
                out[f"{variant}/{f['filename']}"] = f.get("content", "")
    return out


def diff(db: Session, node_id: int, from_revision_no: int, to_revision_no: int
         ) -> Dict[str, str]:
    """Per-file unified diff between two revisions of a node. Returns
    {path: unified_diff_text} for every path that differs."""
    revs = {r.revision_no: r for r in list_revisions(db, node_id, include_deleted=True)}
    a, b = revs.get(from_revision_no), revs.get(to_revision_no)
    if a is None or b is None:
        raise ValueError("revision number not found for this node")
    fa, fb = _files_of(a), _files_of(b)
    result: Dict[str, str] = {}
    for path in sorted(set(fa) | set(fb)):
        left = (fa.get(path) or "").splitlines(keepends=True)
        right = (fb.get(path) or "").splitlines(keepends=True)
        if left == right:
            continue
        result[path] = "".join(difflib.unified_diff(
            left, right,
            fromfile=f"rev{from_revision_no}/{path}",
            tofile=f"rev{to_revision_no}/{path}",
        ))
    return result


# --- legacy materialization (task 1.5) --------------------------------------

def _hero_from_spec_json(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "dockerfile": spec.get("hero_dockerfile"),
        "entrypoint_sh": spec.get("entrypoint_sh"),
        "build_and_push_sh": spec.get("build_and_push_sh"),
        "source_files": spec.get("source_files") or [],
    }


def materialize_initial(db: Session, node: "models.NodeDefinition"
                        ) -> models.NodeSourceRevision:
    """Build revision 1 for a legacy node that has no revision yet. Prefers the
    node's retained ``build_specs.spec_json``; falls back to a minimal revision
    recorded from ``executable_path`` / ``gpu_variants`` when the spec is gone."""
    existing = latest(db, node.id)
    if existing is not None:
        return existing

    spec_json: Optional[Dict[str, Any]] = None
    # node -> newest build_spec via variant verification, else via workflow_run.
    vv = (db.query(models.NodeVariantVerification)
          .filter(models.NodeVariantVerification.node_definition_id == node.id)
          .order_by(models.NodeVariantVerification.id.desc())
          .first())
    spec_id = getattr(vv, "build_spec_id", None) if vv else None
    if spec_id:
        rec = db.get(models.BuildSpecRecord, spec_id)
        if rec and rec.spec_json:
            spec_json = rec.spec_json

    if spec_json:
        return create_revision(
            db, node,
            origin=ORIGIN_GENERATED,
            author_user_id=node.user_id,
            hero=_hero_from_spec_json(spec_json),
            source_overrides_by_variant=spec_json.get("source_overrides_by_variant") or {},
            variants_snapshot=spec_json.get("variants"),
        )

    # No retained spec — record what we can so the chain has a base revision.
    return create_revision(
        db, node,
        origin=ORIGIN_GENERATED,
        author_user_id=node.user_id,
        hero={"dockerfile": None, "entrypoint_sh": None,
              "build_and_push_sh": None, "source_files": [],
              "note": "source not retained; materialized from node metadata"},
        source_overrides_by_variant={},
        variants_snapshot=None,
    )

"""node_code_edit business logic (Gate 2b migration of routers/node_code_edit.py).

Owns the pure business logic/orchestration the router used to inline:
revision presentation shaping, copy-name uniqueness, and the custom_nodes
entitlement gate. Data access goes through `crud/node_code_edit.py`.

NOT moved here: `authorize(...)`-based capability checks (`_require_cap`) and
the small HTTPException-builder helpers stay physically in
`routers/node_code_edit.py`. `tests/test_node_code_edit_endpoints.py` does
`monkeypatch.setattr(node_code_edit, "authorize", ...)`, which only takes
effect for code whose `__globals__` is the router module — moving the call
site here would silently stop honoring that patch and desync test behavior
from the real 403 path. That auth-guard boundary mirrors the Depends()-stays-
in-router rule for the same reason.
"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.orm import Session

import models
from crud.node_code_edit import node_name_taken
from services._base import ConflictError, ServiceError
from utils.entitlements import Cap, get_entitlements
import services.node_source_revisions as nsr


class EntitlementRequiredError(ServiceError):
    """The acting tenant lacks the `custom_nodes` capability (router maps
    this to its existing 402 `_entitlement_required()` HTTPException)."""

    def __init__(self) -> None:
        super().__init__(
            "custom_nodes entitlement required",
            code="entitlement_required",
            details={"capability": "custom_nodes"},
        )


def is_system(node: models.NodeDefinition) -> bool:
    return node.tenant_id is None


def materialize_initial_safe(db: Session, node: models.NodeDefinition) -> models.NodeSourceRevision:
    """Materialize revision 1 for a node that has none yet.

    node-code-edit-rebuild §1 makes ``NodeSourceRevision.tenant_id`` NULLABLE, so a
    SYSTEM node (tenant_id NULL, MOD-authored) materializes a tenant-less revision
    directly via the §1 service — no platform-tenant sentinel needed. Kept as a
    thin wrapper so call sites read intently and to preserve a single seam if the
    materialization policy ever needs to differ per node kind.
    """
    return nsr.materialize_initial(db, node)


def revision_summary(rev: models.NodeSourceRevision) -> Dict[str, Any]:
    return {
        "revision_no": rev.revision_no,
        "origin": rev.origin,
        "author_user_id": rev.author_user_id,
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
    }


def variants_view(rev: models.NodeSourceRevision) -> Dict[str, Any]:
    """Present a revision's per-variant source as
    ``{variant: {"dockerfile", "files", "origin"}}``.

    Two channels feed this: ``variants_snapshot`` (the generated/copied
    per-variant Dockerfile + files, shaped like
    ``node_builder.build_dag.types.BuildVariantSpec``) and
    ``source_overrides_by_variant`` (explicit per-variant file edits, keyed
    the same way as ``NodeSourceRevision.source_overrides_by_variant``). An
    override for a variant wins over — and is flagged distinctly from — its
    generated snapshot.
    """
    out: Dict[str, Any] = {}
    for v in (rev.variants_snapshot or []):
        if not isinstance(v, dict):
            continue
        variant = v.get("variant")
        if not variant:
            continue
        out[variant] = {
            "dockerfile": v.get("dockerfile"),
            "files": [f for f in (v.get("source_overrides") or []) if isinstance(f, dict)],
            "origin": rev.origin if rev.origin in (nsr.ORIGIN_GENERATED, nsr.ORIGIN_COPIED) else nsr.ORIGIN_GENERATED,
        }
    for variant, files in (rev.source_overrides_by_variant or {}).items():
        out[variant] = {
            "dockerfile": out.get(variant, {}).get("dockerfile"),
            "files": [f for f in (files or []) if isinstance(f, dict)],
            "origin": nsr.ORIGIN_EDITED,
        }
    return out


def require_custom_nodes(db: Session, current_user) -> None:
    # The entitlement being checked is "can THIS TENANT author/rebuild custom
    # nodes" — resolved off the acting tenant (current_user.tenant), not the
    # source node's tenant. That matters for /copy, where the source can be a
    # tenant-less system node: gating on the source would resolve to
    # the baseline entitlements (which happen to grant custom_nodes) and silently bypass
    # a SaaS plan that doesn't include the capability.
    es = get_entitlements(db, getattr(current_user, "tenant", None))
    if not es.has(Cap.CUSTOM_NODES):
        raise EntitlementRequiredError()


def next_node_copy_name(db: Session, *, tenant_id: int, user_id: int, base_name: str) -> str:
    """Unique node name in the caller's own private-draft namespace
    (``tenant_id`` + ``user_id``). Mirrors ``routers.cogs._next_copy_suffix_name``
    (plain name if free, else " (copy)", " (copy 2)", ...) scoped to node names
    instead of cog paths, so this router doesn't reach into ``cogs.py``."""
    if not node_name_taken(db, tenant_id=tenant_id, user_id=user_id, name=base_name):
        return base_name
    n = 2
    candidate = f"{base_name} (copy)"
    while node_name_taken(db, tenant_id=tenant_id, user_id=user_id, name=candidate):
        candidate = f"{base_name} (copy {n})"
        n += 1
        if n > 1000:
            raise ConflictError(
                "Too many copies of this node; rename existing copies and retry."
            )
    return candidate

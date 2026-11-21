"""Node Definition CRUD operations - Node definitions within tenants."""
import logging
from typing import Optional, List

from sqlalchemy import or_
from sqlalchemy.orm import Session
import models, schemas

logger = logging.getLogger(__name__)


def _index_node(nd: "models.NodeDefinition") -> None:
    """Best-effort RAG upsert. Failure must never block a write."""
    try:
        from utils.nodes_ingester import upsert_node
        upsert_node(nd)
    except Exception as e:  # noqa: BLE001
        logger.debug("node RAG upsert skipped: %s", e)


def _deindex_node(node_id: int) -> None:
    try:
        from utils.nodes_ingester import delete_node
        delete_node(node_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("node RAG delete skipped: %s", e)


def _egress_declare_on_create(
    db: Session,
    tenant_id: int,
    user_id: Optional[int],
    node_id: int,
    allowed_egress,
) -> None:
    """Best-effort egress-declaration audit on node create."""
    if not allowed_egress:
        return  # None or [] → sealed, skip noise
    from services import egress_allowlist
    egress_allowlist.record_egress_declaration(
        db,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        subject_type="node",
        subject_id=node_id,
        entries=allowed_egress,
    )


def _egress_declare_on_update(
    db: Session,
    db_node_def: "models.NodeDefinition",
    old_egress,
    new_egress,
) -> None:
    """Best-effort egress-declaration audit on node update.

    The crud fn does not carry an actor_user_id; fall back to the
    node's own user_id (the creating/owner user).
    """
    if old_egress == new_egress:
        return
    from services import egress_allowlist
    egress_allowlist.record_egress_declaration(
        db,
        tenant_id=db_node_def.tenant_id,
        actor_user_id=db_node_def.user_id,
        subject_type="node",
        subject_id=db_node_def.id,
        entries=new_egress,
        previous_entries=old_egress,
    )


def _user_team_ids(db: Session, *, tenant_id: int, user_id: int) -> set:
    rows = db.query(models.team_members_association.c.team_id).join(
        models.Team,
        models.Team.id == models.team_members_association.c.team_id,
    ).filter(
        models.team_members_association.c.user_id == user_id,
        models.Team.tenant_id == tenant_id,
    ).all()
    return {r[0] for r in rows}


def _team_restricted_node_ids_subq(db: Session, *, tenant_id: int):
    return db.query(models.ResourcePermission.resource_id).filter(
        models.ResourcePermission.resource_type == models.ResourceType.NODE_DEFINITION,
        models.ResourcePermission.team_id.isnot(None),
        models.ResourcePermission.tenant_id == tenant_id,
        models.ResourcePermission.is_active.is_(True),
        models.ResourcePermission.revoked_at.is_(None),
        models.ResourcePermission.resource_id.isnot(None),
    ).subquery()


def _team_visible_node_ids_subq(db: Session, *, tenant_id: int, team_ids):
    if not team_ids:
        return None
    return db.query(models.ResourcePermission.resource_id).filter(
        models.ResourcePermission.resource_type == models.ResourceType.NODE_DEFINITION,
        models.ResourcePermission.team_id.in_(list(team_ids)),
        models.ResourcePermission.tenant_id == tenant_id,
        models.ResourcePermission.is_active.is_(True),
        models.ResourcePermission.revoked_at.is_(None),
        models.ResourcePermission.resource_id.isnot(None),
    ).subquery()


def create_node_definition(
    db: Session,
    node_def: schemas.NodeDefinitionCreate,
    tenant_id: int,
    user_id: Optional[int] = None,
) -> models.NodeDefinition:
    """Create a new node definition.

    Args:
        tenant_id: tenant the node lives under (NULL = system-shared, but
                   that path doesn't go through this function in practice).
        user_id: when set, the node is private to this user inside the
                 tenant. NULL = tenant-shared. Promote a private node to
                 tenant-shared by clearing user_id (separate endpoint).
    """
    # Strip tenant_id from incoming pydantic dict and let the explicit kwargs
    # win, so the caller can't override tenant scoping by smuggling a value
    # through the request body. user_id is wizard-internal — never trust it
    # from the request body either.
    # build_spec_id is request-only metadata (used to bill the build's
    # triggering user) — it is NOT a NodeDefinition column, so exclude it
    # before spreading into the model.
    payload = node_def.model_dump(exclude={"tenant_id", "user_id", "build_spec_id"})
    # node-role §1 — resolve the authoritative classification with a
    # conservative default. Explicit node_role wins; otherwise a tenant's OWN
    # new node is user-facing (a user builds a node to use it), while a
    # system-shared / seeded node (tenant_id is None) defaults to 'system' —
    # hidden from users until a developer deliberately promotes it. `system_only`
    # is derived: drop it here and let the model's before_insert event set it
    # from node_role (never write both).
    _role = str(payload.get("node_role") or "").strip().lower()
    if _role not in ("user", "system"):
        _role = "user" if tenant_id is not None else "system"
    payload["node_role"] = _role
    payload.pop("system_only", None)
    db_node_def = models.NodeDefinition(
        **payload,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    db.add(db_node_def)
    db.commit()
    db.refresh(db_node_def)
    _index_node(db_node_def)
    # GSEC FX.73 p2 (Q56=A) — every new node definition enters the
    # licence manual-review queue; nothing is known about its licence at
    # creation time, so this is deliberately unknown/unknown (never
    # guessed). Best-effort: never blocks node creation.
    # 1788j CE carve (same CE default as 1788e in
    # services/node_authoring_service.py): services.licence_review is paid —
    # with the feature OFF there is no manual-review queue to enqueue, so the
    # hook is a no-op in CE; node creation itself is unchanged.
    from utils.paid_import import optional_paid
    licence_review = optional_paid("services.licence_review")
    if licence_review is not None:
        licence_review.ensure_queued_on_create(
            db, "node_definition", str(db_node_def.id))
    # C5c — audit egress declarations on node create.
    # Empty / None = sealed (no noise). Non-empty → record declaration.
    _egress_declare_on_create(
        db, tenant_id, user_id, db_node_def.id, db_node_def.allowed_egress)
    return db_node_def


def get_node_definitions(
    db: Session,
    tenant_id: int,
    skip: int = 0,
    limit: int = 100,
    *,
    user_id: Optional[int] = None,
    include_private: bool = True,
    is_tenant_admin: bool = False,
    include_unsupported: bool = False,
) -> List[models.NodeDefinition]:
    """Retrieve node definitions visible to a (tenant, user) pair.

    Visibility rules (most-specific wins):
      1. user-private node (tenant_id=T, user_id=U)   — only that user
      2. tenant-shared node (tenant_id=T, user_id NULL) — every user in T
      3. system-shared node (tenant_id NULL, user_id NULL) — every user

    Dedup: if a more-specific row with the same `name` exists for this
    user, hide the less-specific twins. So a user's private "Resize"
    hides the tenant "Resize" which hides the system "Resize".

    `include_private=False` is for admin views (Tenant Settings → Nodes
    tab) that want to see only tenant-scope rows.

    `is_tenant_admin=True` bypasses team-grant restrictions on
    tenant-shared rows: a tenant admin sees all tenant-shared nodes
    regardless of which teams have grants. The router computes this
    from `current_user.parent_user_id is None` and passes it in.

    `include_unsupported=False` (default) hides every row whose
    `node_kind` matches a live system node marked
    `custom_metadata.catalogue_status = 'unsupported'` — keyed on
    node_kind so the tenant palette copies are hidden too. Admin /
    catalogue-management views pass True to see everything.
    """
    # Names this user has private rows for — those suppress tenant + system.
    private_names_subq = None
    if include_private and user_id is not None:
        private_names_subq = db.query(models.NodeDefinition.name).filter(
            models.NodeDefinition.tenant_id == tenant_id,
            models.NodeDefinition.user_id == user_id,
        ).subquery()

    # Names the tenant has shared rows for — those suppress system twins.
    tenant_names_subq = db.query(models.NodeDefinition.name).filter(
        models.NodeDefinition.tenant_id == tenant_id,
        models.NodeDefinition.user_id.is_(None),
    ).subquery()

    # Build the OR'd visibility predicate.
    private_clause = None
    if include_private and user_id is not None:
        private_clause = (
            (models.NodeDefinition.tenant_id == tenant_id)
            & (models.NodeDefinition.user_id == user_id)
        )

    tenant_clause = (
        (models.NodeDefinition.tenant_id == tenant_id)
        & (models.NodeDefinition.user_id.is_(None))
    )
    if private_names_subq is not None:
        tenant_clause = tenant_clause & (
            models.NodeDefinition.name.notin_(private_names_subq.select())
        )

    if not is_tenant_admin:
        restricted_subq = _team_restricted_node_ids_subq(db, tenant_id=tenant_id)
        team_ids = _user_team_ids(db, tenant_id=tenant_id, user_id=user_id) if user_id is not None else set()
        visible_subq = _team_visible_node_ids_subq(db, tenant_id=tenant_id, team_ids=team_ids)
        not_restricted = models.NodeDefinition.id.notin_(restricted_subq.select())
        if visible_subq is not None:
            tenant_clause = tenant_clause & (
                not_restricted | models.NodeDefinition.id.in_(visible_subq.select())
            )
        else:
            tenant_clause = tenant_clause & not_restricted

    # Marketplace — system nodes are visible only when curated AND the
    # tenant has an entitlement row (Download). Non-curated system nodes
    # are invisible to all tenants regardless of entitlement state. The
    # admin curate page uses a separate endpoint that bypasses this
    # filter so platform admins can still see uncurated nodes to flip
    # them on. Tenant-owned nodes (tenant_clause) are unaffected.
    if hasattr(models.NodeDefinition, "is_in_marketplace"):
        entitled_ids_subq = db.query(
            models.TenantNodeEntitlement.node_definition_id,
        ).filter(
            models.TenantNodeEntitlement.tenant_id == tenant_id,
            models.TenantNodeEntitlement.deleted_at.is_(None),
        ).subquery()
        marketplace_clause = (
            models.NodeDefinition.is_in_marketplace.is_(True)
            & models.NodeDefinition.id.in_(entitled_ids_subq.select())
        )
    else:
        marketplace_clause = None

    system_clause = (
        models.NodeDefinition.tenant_id.is_(None)
        & models.NodeDefinition.name.notin_(tenant_names_subq.select())
    )
    if private_names_subq is not None:
        system_clause = system_clause & (
            models.NodeDefinition.name.notin_(private_names_subq.select())
        )
    if marketplace_clause is not None:
        system_clause = system_clause & marketplace_clause

    visibility = system_clause | tenant_clause
    if private_clause is not None:
        visibility = visibility | private_clause

    q = db.query(models.NodeDefinition).filter(visibility)

    if hasattr(models.NodeDefinition, "system_only"):
        q = q.filter(models.NodeDefinition.system_only == False)  # noqa: E712

    if not include_unsupported:
        unsupported_kinds_subq = db.query(models.NodeDefinition.node_kind).filter(
            models.NodeDefinition.tenant_id.is_(None),
            models.NodeDefinition.deleted_at.is_(None),
            models.NodeDefinition.node_kind.isnot(None),
            models.NodeDefinition.custom_metadata['catalogue_status'].as_string() == 'unsupported',
        ).subquery()
        q = q.filter(or_(
            models.NodeDefinition.node_kind.is_(None),
            models.NodeDefinition.node_kind.notin_(unsupported_kinds_subq.select()),
        ))
    # J164b — the palette dedupe runs BEFORE pagination (see below), so a page
    # never shrinks below `limit` because of duplicates.
    rows = _dedupe_palette_rows(db, tenant_id, q.all())[skip:skip + limit]
    return _hide_nodes_with_soft_deleted_images(db, tenant_id, rows)


def _palette_copy_identity(nd) -> Optional[tuple]:
    """The dedupe identity of one palette row, or None when it must NEVER be
    collapsed.

    J164b — identity is (tenant_id, user_id, node_kind, casefolded name):
    user_id is part of the key so a user's private node or fork that happens
    to share the tenant copy's name is a DIFFERENT identity and stays listed
    (the round-1 key (tenant_id, name) silently hid it), and node_kind is
    required so rows with no kind identity (user-authored nodes) are never
    collapsed at all. Rows that differ only by name case/whitespace are the
    same palette entry the two seeders wrote from different sources
    (domain spec name vs metadata-file name), so the name is casefolded.
    """
    kind = (getattr(nd, "node_kind", None) or "").strip()
    if not kind:
        return None
    name = (nd.name or "").strip().casefold()
    if not name:
        return None
    return (getattr(nd, "tenant_id", None), getattr(nd, "user_id", None), kind, name)


def _dedupe_palette_rows(
    db: Session,
    tenant_id: Optional[int],
    nodes: List[models.NodeDefinition],
) -> List[models.NodeDefinition]:
    """Collapse exact duplicate tenant palette rows to ONE (lowest id wins).

    J164b — the CE room DB held three tenant-1 rows for node_kind='image_resize'
    (ids 34/314/338): the per-tenant copy step guarded on the bare name, so a
    warm re-run of the copy (boot sync + the scheduler's periodic
    `sync_starter_package`, plus the refresh path) added a second live row for
    a source node the tenant already had. The WB library keys rows by node.id
    (CORE/WorkflowBuilder/components/node-library.tsx:935), so three rows
    render three buttons and the strict palette-search locator matches 3 items
    — which is what made "Image Resize" stop showing in the WB.

    This is a read-path guard for DBs that ALREADY hold duplicates (the fix at
    the source is `services.starter_package._copy_identity`); it never writes.
    The OLDEST (lowest-id) row wins — the original provisioned copy that
    dispatch/entitlements/ownership key on. Only rows with a non-NULL node_kind
    and a user_id-NULL (tenant-shared) scope are collapsed; user-private rows
    keep their own identity. Scope is unchanged: this only ever REMOVES
    duplicates of rows the visibility filter already admitted, so no
    tenant-scope widening is possible.
    """
    if not nodes:
        return nodes
    winner: dict = {}
    for nd in nodes:
        if getattr(nd, "user_id", None) is not None:
            continue  # user-private rows are never collapsed
        key = _palette_copy_identity(nd)
        if key is None:
            continue  # NULL node_kind: no kind identity, never collapsed
        rid = int(nd.id) if nd.id is not None else 0
        if key not in winner or rid < winner[key]:
            winner[key] = rid
    out: List[models.NodeDefinition] = []
    for nd in nodes:
        key = _palette_copy_identity(nd)
        if key is None or getattr(nd, "user_id", None) is not None:
            out.append(nd)
            continue
        if winner[key] != (int(nd.id) if nd.id is not None else 0):
            continue
        out.append(nd)
    return out


def _hide_nodes_with_soft_deleted_images(
    db: Session,
    tenant_id: Optional[int],
    nodes: List[models.NodeDefinition],
) -> List[models.NodeDefinition]:
    """Drop NodeDefinitions whose image refs point at a Harbor image
    that's currently in the soft-delete retention window.

    Tenant Registry soft-deletes (repository, tag) pairs into
    tenant_image_deletions. We mirror that hide here so the Workflow
    Builder doesn't show a node whose image the user just deleted.

    Match sources, in priority order:
      1. ``gpu_variants`` dict — preferred; explicit variant→ref map.
      2. ``executable_path`` — fallback; the single-image ref the
         register step writes for wizard-built tenant nodes. The
         build-task's register_node.py currently leaves gpu_variants
         NULL on the wizard path (separate bug), so without this
         fallback the cascade-hide can't find anything to match
         against and the deleted node stays visible in the palette.
         Observed on stage with node id=119 PDF→PNG Converter:
         tenant_image_deletions had the row, executable_path pointed
         at it, but gpu_variants was NULL and the filter passed it
         through.

    Matching is on the trailing "repo:tag" tail because the host +
    project prefix varies per deployment (registry.example.com vs
    example.com) but the repo:tag stays stable.
    Same convention the harbor router's tenant-images endpoint uses.

    System nodes (tenant_id IS NULL) are never hidden — their images
    don't live in the per-tenant Harbor project.
    """
    if not tenant_id or not nodes:
        return nodes
    rows = db.query(models.TenantImageDeletion).filter(
        models.TenantImageDeletion.tenant_id == tenant_id,
        models.TenantImageDeletion.reaped_at.is_(None),
    ).all()
    if not rows:
        return nodes
    deleted_tails = {f"{r.repository}:{r.tag}" for r in rows}

    def _tail_in_deleted(ref) -> bool:
        if not isinstance(ref, str) or not ref:
            return False
        return ref.split("/")[-1] in deleted_tails

    def _node_is_soft_deleted(node: models.NodeDefinition) -> bool:
        # Tenant-owned rows only; system rows can't reference per-tenant
        # repos and shouldn't be hidden by a tenant's deletions.
        if node.tenant_id != tenant_id:
            return False

        variants = node.gpu_variants or {}
        if variants:
            # Structured variant map: hide only if EVERY variant is
            # soft-deleted. Multi-variant nodes that lost some
            # variants but kept others stay runnable.
            active = 0
            deleted = 0
            for ref in variants.values():
                if not isinstance(ref, str) or not ref:
                    continue
                if ref.split("/")[-1] in deleted_tails:
                    deleted += 1
                else:
                    active += 1
            return deleted > 0 and active == 0

        # Fallback: single-image node (no gpu_variants populated).
        # executable_path carries the only image ref the build-task
        # register step wrote. If that image is soft-deleted, the
        # node has nowhere to run — hide it.
        exec_path = getattr(node, "executable_path", None)
        return _tail_in_deleted(exec_path)

    return [n for n in nodes if not _node_is_soft_deleted(n)]


def get_node_definition_by_id(
    db: Session,
    node_definition_id: int,
    tenant_id: int,
    *,
    user_id: Optional[int] = None,
) -> Optional[models.NodeDefinition]:
    """Retrieve a single node definition by id.

    Permits: system rows, tenant rows for this tenant, and (when user_id
    is given) the caller's own private rows. A private row owned by a
    *different* user in the same tenant is treated as not-found, not 403,
    so listing endpoints behave the same as direct fetches.
    """
    visibility = (
        (models.NodeDefinition.tenant_id == tenant_id)
        & (models.NodeDefinition.user_id.is_(None))
    ) | models.NodeDefinition.tenant_id.is_(None)
    if user_id is not None:
        visibility = visibility | (
            (models.NodeDefinition.tenant_id == tenant_id)
            & (models.NodeDefinition.user_id == user_id)
        )
    return db.query(models.NodeDefinition).filter(
        models.NodeDefinition.id == node_definition_id,
        visibility,
    ).first()


def update_node_definition(
    db: Session,
    db_node_def: models.NodeDefinition,
    node_def_update: schemas.NodeDefinitionUpdate
) -> models.NodeDefinition:
    """Update a node definition's attributes."""
    old_egress = db_node_def.allowed_egress
    update_data = node_def_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_node_def, key, value)
    db.add(db_node_def)
    db.commit()
    db.refresh(db_node_def)
    _index_node(db_node_def)
    # C5c — audit egress declarations on node update.
    _egress_declare_on_update(db, db_node_def, old_egress, db_node_def.allowed_egress)
    return db_node_def


def delete_node_definition(db: Session, db_node_def: models.NodeDefinition):
    """HARD-delete a node definition row.

    Used by the §11b retention-GC sweep AFTER the tenant's retention
    period has elapsed for a soft-deleted node. User-facing delete
    paths must call ``soft_delete_node_definition`` instead — direct
    hard-deletion breaks the ISO 27001 audit trail.
    """
    nid = int(db_node_def.id)
    db.delete(db_node_def)
    db.commit()
    _deindex_node(nid)


def soft_delete_node_definition(
    db: Session,
    db_node_def: models.NodeDefinition,
    *,
    actor_user_id: int,
) -> "models.NodeDefinition":
    """Wizard-rebuild §11b — stamp the row as soft-deleted.

    Sets ``deleted_at = now()`` + ``deleted_by_user_id = actor_user_id``
    on the live row. The pre-existing SQLAlchemy ``with_loader_criteria``
    hook (database.py) auto-filters every subsequent SELECT so the row
    becomes invisible to listings, palette, lookup-by-name, etc.

    Hard-deletion is deferred to the daily retention sweep
    (``_run_retention_hard_delete`` in services/scheduler.py); when
    ``deleted_at < now() - tenant.retention_days`` that job calls
    ``delete_node_definition`` + reclaims the Harbor manifest.

    Re-deindexes from Qdrant (node RAG) so the soft-deleted node also
    disappears from the wizard's describe-search results.
    """
    from datetime import datetime, timezone
    nid = int(db_node_def.id)
    db_node_def.deleted_at = datetime.now(timezone.utc)
    db_node_def.deleted_by_user_id = actor_user_id
    db.add(db_node_def)
    db.commit()
    _deindex_node(nid)
    return db_node_def


def get_node_definition_by_name(
    db: Session,
    name: str,
    tenant_id: int,
    *,
    user_id: Optional[int] = None,
    include_system: bool = False,
):
    """Lookup a node by name within a tenant.

    Resolution order (most-specific wins): caller's private row →
    tenant-shared row → (when ``include_system`` is True) system row.

    ``include_system`` defaults False so registration-upsert callers
    (`POST /node-definitions`, image-upload flow) keep their current
    "find an existing TENANT twin or nothing" behaviour and never
    accidentally match a system row. Callers that resolve a name for
    the visible-to-this-user lookup (wizard apply, template apply)
    pass ``include_system=True`` so system nodes — which the wizard
    catalog and the WorkflowBuilder palette already render — are
    reachable.
    """
    if user_id is not None:
        priv = db.query(models.NodeDefinition).filter(
            models.NodeDefinition.name == name,
            models.NodeDefinition.tenant_id == tenant_id,
            models.NodeDefinition.user_id == user_id,
        ).first()
        if priv is not None:
            return priv
    tenant_hit = db.query(models.NodeDefinition).filter(
        models.NodeDefinition.name == name,
        models.NodeDefinition.tenant_id == tenant_id,
        models.NodeDefinition.user_id.is_(None),
    ).first()
    if tenant_hit is not None:
        return tenant_hit
    if include_system:
        return db.query(models.NodeDefinition).filter(
            models.NodeDefinition.name == name,
            models.NodeDefinition.tenant_id.is_(None),
            models.NodeDefinition.user_id.is_(None),
        ).first()
    return None


def promote_node_definition_to_tenant(
    db: Session,
    db_node_def: models.NodeDefinition,
) -> models.NodeDefinition:
    """Make a user-private node tenant-shared by clearing user_id."""
    db_node_def.user_id = None
    db.add(db_node_def)
    db.commit()
    db.refresh(db_node_def)
    _index_node(db_node_def)
    return db_node_def


def record_node_version(
    db: Session, node_def: "models.NodeDefinition"
) -> Optional["models.NodeVersion"]:
    """node-version-pinning — capture the node's CURRENT build as an immutable
    NodeVersion. Called after a build sets ``image_digest`` (the imagebuilder
    manifest callback). Idempotent on digest: re-recording the same digest
    returns the existing row. Returns None when the node has no digest yet —
    there is nothing durable to pin to, and the run path resolves it by tag.

    ``version`` is a per-node display counter assigned here (v1, v2, …) because
    ``NodeDefinition.version`` is never bumped by the build path; the digest is
    the real identity (uq_node_version_digest).
    """
    from sqlalchemy import func as _func
    from sqlalchemy.exc import IntegrityError

    digest = (getattr(node_def, "image_digest", None) or "").strip()
    if not digest:
        return None

    existing = (
        db.query(models.NodeVersion)
        .filter(
            models.NodeVersion.node_definition_id == node_def.id,
            models.NodeVersion.image_digest == digest,
        )
        .first()
    )
    if existing:
        return existing

    next_version = (
        db.query(_func.max(models.NodeVersion.version))
        .filter(models.NodeVersion.node_definition_id == node_def.id)
        .scalar()
        or 0
    ) + 1

    nv = models.NodeVersion(
        node_definition_id=node_def.id,
        node_kind=node_def.node_kind,
        version=next_version,
        image_digest=digest,
        gpu_variants=node_def.gpu_variants,
        # Per-variant digests are not yet produced by the build pipeline; leave
        # NULL so the worker pulls variants by tag. Populated in a later phase.
        gpu_variant_digests=None,
        executable_path=node_def.executable_path,
        manifest_ref=node_def.manifest_ref,
    )
    # SAVEPOINT so a concurrent recorder racing on the unique digest rolls back
    # only this insert, then we return the row the winner created.
    try:
        with db.begin_nested():
            db.add(nv)
            db.flush()
    except IntegrityError:
        return (
            db.query(models.NodeVersion)
            .filter(
                models.NodeVersion.node_definition_id == node_def.id,
                models.NodeVersion.image_digest == digest,
            )
            .first()
        )
    return nv


def resolve_node_run_pin(
    db: Session, node_run_id: Optional[int]
) -> Optional["models.NodeVersion"]:
    """node-version-pinning — the pinned NodeVersion for a NodeRun's workflow
    node, or None when the task has no node_run (system tasks) or the node is
    unpinned (legacy → resolve current build). Chain:
    NodeRun -> WorkflowNode.pinned_node_version_id -> NodeVersion.
    """
    if not node_run_id:
        return None
    return (
        db.query(models.NodeVersion)
        .join(
            models.WorkflowNode,
            models.WorkflowNode.pinned_node_version_id == models.NodeVersion.id,
        )
        .join(
            models.NodeRun,
            models.NodeRun.workflow_node_id == models.WorkflowNode.id,
        )
        .filter(models.NodeRun.id == node_run_id)
        .first()
    )


def stamp_node_run_execution(
    db: Session,
    node_run_id: Optional[int],
    node_version: Optional["models.NodeVersion"] = None,
) -> None:
    """node-version-pinning — record the exact build a run executes, so a
    completed NodeRun is reproducible/auditable. Pinned: stamp the pin's id +
    digest. Unpinned: stamp the node's CURRENT digest (what :latest resolves to
    now) so the audit trail is complete either way. No-op without a node_run.
    """
    if not node_run_id:
        return
    nr = (
        db.query(models.NodeRun)
        .filter(models.NodeRun.id == node_run_id)
        .first()
    )
    if not nr:
        return
    if node_version is not None:
        nr.node_version_id = node_version.id
        nr.executed_image_digest = node_version.image_digest
        return
    wn = (
        db.query(models.WorkflowNode)
        .filter(models.WorkflowNode.id == nr.workflow_node_id)
        .first()
    )
    if wn and wn.node_definition and wn.node_definition.image_digest:
        nr.executed_image_digest = wn.node_definition.image_digest
        nv = current_node_version(db, wn.node_definition_id)
        if nv:
            nr.node_version_id = nv.id


def current_node_version(
    db: Session, node_definition_id: Optional[int]
) -> Optional["models.NodeVersion"]:
    """node-version-pinning — the NodeVersion for a node's CURRENT build.

    Get-or-create semantics via record_node_version: returns the immutable row
    for the node's present image_digest (creating it if a build recorded a
    digest but no version row exists yet), or None when the node has no digest
    (legacy tag-only node → callers leave the pin NULL / resolve-current).
    """
    if not node_definition_id:
        return None
    nd = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.id == node_definition_id)
        .first()
    )
    if not nd:
        return None
    return record_node_version(db, nd)


def resolve_pin_for_new_workflow_node(
    db: Session,
    node_definition_id: Optional[int],
    explicit_version_id: Optional[int] = None,
) -> Optional[int]:
    """node-version-pinning — pick the NodeVersion id to pin a freshly created
    WorkflowNode to. Prefers a VALID explicit pin the builder saved into the
    graph (must belong to this node definition), else the node's current build.
    Returns an int id, or None → unpinned (resolve-current at dispatch).
    """
    if explicit_version_id:
        ok = (
            db.query(models.NodeVersion.id)
            .filter(
                models.NodeVersion.id == explicit_version_id,
                models.NodeVersion.node_definition_id == node_definition_id,
            )
            .first()
        )
        if ok:
            return explicit_version_id
    nv = current_node_version(db, node_definition_id)
    return nv.id if nv else None


def latest_versions_for_nodes(db: Session, node_ids) -> dict:
    """node-version-pinning — map {node_definition_id: {"id": version_id,
    "version": version_num}} for the CURRENT (newest) build of each node, in one
    query, so the palette endpoint can attach both the display number (badge)
    and the version id (needed to re-pin) without an N+1. Empty input → {}.

    Digest is identity and version is a monotonic counter, so max(version) picks
    a unique row per node.
    """
    from sqlalchemy import func as _func, and_ as _and

    ids = [i for i in (node_ids or []) if i]
    if not ids:
        return {}
    subq = (
        db.query(
            models.NodeVersion.node_definition_id.label("nid"),
            _func.max(models.NodeVersion.version).label("mv"),
        )
        .filter(models.NodeVersion.node_definition_id.in_(ids))
        .group_by(models.NodeVersion.node_definition_id)
        .subquery()
    )
    rows = (
        db.query(
            models.NodeVersion.node_definition_id,
            models.NodeVersion.id,
            models.NodeVersion.version,
        )
        .join(
            subq,
            _and(
                models.NodeVersion.node_definition_id == subq.c.nid,
                models.NodeVersion.version == subq.c.mv,
            ),
        )
        .all()
    )
    return {nid: {"id": vid, "version": vnum} for nid, vid, vnum in rows}

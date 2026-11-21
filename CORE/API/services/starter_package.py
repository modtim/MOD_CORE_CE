"""MOD Core starter package — the default node palette every plan grants.

Owner decision 2026-09-27 (launch-completion-megaplan 26.3a): a new tenant's
palette is the free "MOD Core" package, granted through the existing plan-grant
mechanism (PlanPackage -> reconcile_plan_grants -> source='plan_grant'
entitlements). Without it a fresh signup sees an empty Workflow Builder palette,
because a system node is only visible with an entitlement and no plan included
any node.

The package holds every curated, free, user-facing system node. It only ever
GROWS: membership rows are never deleted (ISO, no hard deletes). A node that
later gets a price stays a member and is logged, so an admin can decide.

`sync_starter_package` is the periodic self-heal (new nodes join, new plans get
the package, every subscribed tenant is reconciled). `grant_starter_nodes` is
the inline hook for the signup paths, so a new tenant's palette is populated
before they first open the builder.
"""
import logging

from sqlalchemy import or_
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

STARTER_SLUG = "mod-core"
STARTER_NAME = "MOD Core"
STARTER_DESCRIPTION = "Every free MOD system node. Included in every plan."


def _eligible_system_node_filters():
    """Single source of truth for "a node belongs in every tenant's palette":
    a live, curated (marketplace), free, user-facing system node."""
    return [
        models.NodeDefinition.tenant_id.is_(None),
        models.NodeDefinition.is_in_marketplace.is_(True),
        models.NodeDefinition.system_only.is_(False),
        models.NodeDefinition.node_role != "system",
        models.NodeDefinition.deleted_at.is_(None),
        or_(
            models.NodeDefinition.price_cents.is_(None),
            models.NodeDefinition.price_cents <= 0,
        ),
    ]


def eligible_system_nodes(db: Session) -> list:
    """The curated, free, user-facing, live system NodeDefinitions (rows)."""
    rows = db.query(models.NodeDefinition).filter(*_eligible_system_node_filters()).all()
    return [
        nd for nd in rows
        if not (isinstance(nd.custom_metadata, dict)
                and nd.custom_metadata.get("catalogue_status") == "unsupported")
    ]


def eligible_node_ids(db: Session) -> set:
    """Curated, free, user-facing, live system nodes (ids)."""
    return {nd.id for nd in eligible_system_nodes(db)}


def _palette_tenant_ids(db: Session) -> list:
    """The tenants whose palette this module owns.

    Default population = every SUBSCRIBED tenant (the population the plan-grant
    entitlement reconcile already makes sense for). 1665p (CE Gap O remainder)
    adds ONE more, CE-only: the single default tenant. A CE install never runs a
    signup/plan-assignment path, so it holds no Subscription row and was
    therefore skipped by both the entitlement reconcile and the tenant-copy
    backfill — which is why its palette only contained the tenant_id=1 rows that
    other seeds wrote directly (Text LLM, Task Extract/merge, Plane, RAG/LoRA
    export, Speech-to-text) and hid every system-scope row, including the 5
    datetime_* domain primitives. The CE default tenant is single-tenant
    (WS2.1), so this can never pull another tenant's rows into view and never
    touches SaaS/paid, where `_is_ce_edition()` is False and the population is
    byte-for-byte the old `Subscription.tenant_id` query.
    """
    ids = [tid for (tid,) in db.query(models.Subscription.tenant_id).distinct()]
    from services.node_scanner import _is_ce_edition, default_tenant_id
    if _is_ce_edition():
        ce_tid = default_tenant_id()
        if ce_tid not in ids:
            ids.append(ce_tid)
    return ids


def ensure_starter_package(db: Session) -> dict:
    """Create the package if missing, add newly eligible nodes, and bind it to
    every live plan. Idempotent; the caller commits."""
    pkg = (
        db.query(models.NodePackage)
        .filter(models.NodePackage.slug == STARTER_SLUG, models.NodePackage.tenant_id.is_(None))
        .first()
    )
    if pkg is None:
        pkg = models.NodePackage(
            name=STARTER_NAME, slug=STARTER_SLUG, description=STARTER_DESCRIPTION,
            tenant_id=None, price_cents=None, is_in_marketplace=False,
        )
        db.add(pkg)
        db.flush()
        logger.info("starter package: created %r (id=%s)", STARTER_NAME, pkg.id)

    members = {
        nid for (nid,) in db.query(models.NodePackageItem.node_definition_id)
        .filter(models.NodePackageItem.package_id == pkg.id)
    }
    eligible = eligible_node_ids(db)
    for nid in sorted(eligible - members):
        db.add(models.NodePackageItem(package_id=pkg.id, node_definition_id=nid))
    stale = members - eligible
    if stale:
        logger.warning(
            "starter package: %d member node(s) no longer free/curated, still granted "
            "by every plan until an admin removes them: %s", len(stale), sorted(stale),
        )

    bound = {
        pid for (pid,) in db.query(models.PlanPackage.plan_id)
        .filter(models.PlanPackage.package_id == pkg.id)
    }
    plans = [pid for (pid,) in db.query(models.Plan.id).filter(models.Plan.deleted_at.is_(None))]
    new_plans = [pid for pid in plans if pid not in bound]
    for pid in new_plans:
        db.add(models.PlanPackage(plan_id=pid, package_id=pkg.id))
    db.flush()
    return {"package_id": pkg.id, "added_nodes": len(eligible - members), "bound_plans": len(new_plans)}


def grant_starter_nodes(db: Session, tenant_id: int) -> dict:
    """Inline hook for signup/plan-assignment paths: make sure the package is
    current, then backfill this tenant's node copies and reconcile its plan
    grants. Never raises; the caller commits."""
    import crud
    try:
        ensure_starter_package(db)
        backfill_tenant_node_copies(db, [tenant_id], commit_each=False)
        return crud.node_ownership_cud.reconcile_plan_grants(db, tenant_id)
    except Exception:  # noqa: BLE001
        logger.warning("starter package grant failed for tenant %s", tenant_id, exc_info=True)
        return {"added": 0, "removed": 0}


def _copy_fields_from(source):
    """Field subset a per-tenant copy inherits from its system canonical.
    Kept identical to the signup path's copy in crud/tenant_cud.py so both
    produce the same rows."""
    return dict(
        description=source.description,
        type=source.type,
        executable_path=source.executable_path,
        input_schema=source.input_schema,
        output_schema=source.output_schema,
        configuration_schema=source.configuration_schema,
        custom_metadata=source.custom_metadata,
        process_type=source.process_type,
        version=source.version,
        cost_in_credits=source.cost_in_credits,
        node_kind=source.node_kind,
        batch_mode=source.batch_mode,
        accepted_formats=source.accepted_formats,
        gpu_variants=source.gpu_variants,
    )


def _norm_name(name) -> str:
    """The name form the copy guard compares on (case/whitespace-insensitive)."""
    return (name or "").strip().casefold()


def _live_tenant_names(db: Session, tenant_id: int) -> set:
    """The node names the tenant already has a LIVE (non-soft-deleted) copy of.

    Kept for the standalone sweep script (backfill_tenant_node_copies.py:74),
    which still speaks the name-only identity. `backfill_tenant_node_copies`
    itself guards on `_copy_identity` — see the J164 note there.
    """
    return {
        n for (n,) in db.query(models.NodeDefinition.name)
        .filter(models.NodeDefinition.tenant_id == tenant_id)
        .filter(models.NodeDefinition.deleted_at.is_(None))
    }


def _copy_identity(kind, name) -> tuple:
    """The identity a per-tenant palette copy is guarded on: (node_kind, name).

    J164 — the CE room DB held three tenant-1 rows for node_kind='image_resize'
    (ids 34/314/338): the name-only guard compared an EXACT name against rows
    the tenant already had, and the two seeders that create tenant-1 rows write
    the name from different sources — the domain registry uses the spec name
    (node_definitions_domains/image_ops.py:493 "Image Resize") while the
    scanner uses the metadata-file name (services/node_scanner.py:1105) and
    admits tenant-scoped rows even when that name is NOT on the keeplist
    (node_scanner.py:1072, the is_user_authored branch). Any case/whitespace
    drift between the two, or a tombstone revived by the refresh path
    (routers/node_definitions_domains/node_authoring.py:3390 ->
    node_authoring_service.py:1690 _clear_soft_deletes_for_node_refs, while
    this step deliberately creates a fresh copy beside a soft-deleted row),
    leaves 2+ live tenant-shared rows for the SAME source node. Refresh All
    spawns the scan unthrottled (node_authoring.py:3394 threading.Thread), so
    repeats compound.

    Identity is (node_kind, normalized name) — the source system row's own
    identity for a tenant-shared copy — NOT kind + container tail: two
    genuinely different starter variants can share kind+image, and a tail key
    would silently skip the second one. Rows with a NULL node_kind keep the
    old name-only identity (a user-authored node has no kind identity).
    """
    kind = (kind or "").strip()
    return (kind, _norm_name(name)) if kind else ("", _norm_name(name))


def _live_tenant_copy_keys(db: Session, tenant_id: int) -> set:
    """Identities the tenant already has a LIVE tenant-shared copy (user_id NULL)
    of. User-private rows are deliberately NOT consulted: a private node never
    suppresses the tenant's own palette copy."""
    rows = db.query(
        models.NodeDefinition.node_kind,
        models.NodeDefinition.name,
    ).filter(
        models.NodeDefinition.tenant_id == tenant_id,
        models.NodeDefinition.user_id.is_(None),
        models.NodeDefinition.deleted_at.is_(None),
    ).all()
    return {_copy_identity(kind, name) for kind, name in rows}


def backfill_tenant_node_copies(
    db: Session, tenant_ids, *, commit_each: bool = True
) -> dict:
    """Create the missing per-tenant copies of every starter-eligible system
    node (the MOD Core palette), idempotently.

    Root cause this heals (26.3): the per-tenant node copies are only ever made
    at tenant creation (`_provision_tenant_defaults` in crud/tenant_cud.py).
    System nodes that are seeded AFTER a tenant exists (RAG, SDXL-Turbo, Frame
    Boost, LoRA Input, the converter set, ...) never get a copy, so they are
    absent from the tenant's palette — which is why tenants 7/28/29 (provisioned
    later) have them and tenant 1 does not.

    Rules (never touches existing rows, never soft-deletes):
      * A copy is created for (tenant, source node) ONLY when NO live
        (deleted_at IS NULL) tenant-shared row with that source's identity
        exists for the tenant. The identity is `_copy_identity` —
        (node_kind, casefolded name), or name-only when the source has no
        node_kind — NOT the bare name: two seeders write the same source
        node's name from different sources (domain spec vs metadata file),
        so a name-only guard let case/whitespace drift add a second live
        copy of ONE source node (room DB: ids 34/314/338, all tenant_id=1,
        node_kind='image_resize'). An existing live copy is left completely
        untouched.
      * If the only existing row is soft-deleted, a FRESH copy is created
        alongside it (we deliberately do NOT undelete: the tombstone keeps its
        audit identity, and node_definitions has no unique (name, tenant_id)
        constraint so a second live row is legal).
      * `tenant_ids=None` means every subscribed tenant (the same population
        the starter-package entitlement sync reconciles).
    Returns {'created': int, 'tenants': int, 'per_tenant': {tid: int}}.
    The caller commits (or pass commit_each to commit per tenant so one bad
    tenant cannot roll back the rest).
    """
    from utils.node_cost import tier_columns_available

    if tenant_ids is None:
        tenant_ids = _palette_tenant_ids(db)
    tenant_ids = list(dict.fromkeys(tenant_ids))
    copy_tiers = tier_columns_available(db)
    # The copy must inherit the CE stamp: ce_included is the marker the Workflow
    # Builder palette uses to hide the seeded twin of a local entry (Human
    # Approval) and to render the CE badge. An unstamped copy lands on the
    # column's server_default FALSE, so the gate showed twice (tenant copy next
    # to the pinned local entry) between this sync and the next node scan's
    # self-heal — and again every time Refresh All soft-deleted the copy and this
    # sync recreated it. Only when the column exists (a DB predating migration
    # 20261007zu rejects the kwarg).
    from services.node_scanner import _ce_keep_list_has_column
    copy_ce_stamp = _ce_keep_list_has_column(db)
    created_total = 0
    per_tenant = {}
    for tid in tenant_ids:
        sources = eligible_system_nodes(db)
        live_keys = _live_tenant_copy_keys(db, tid)
        created = 0
        for src in sources:
            key = _copy_identity(
                getattr(src, "node_kind", None), getattr(src, "name", None)
            )
            if key in live_keys:
                continue
            kw = _copy_fields_from(src)
            if copy_tiers:
                kw["cost_credits_by_tier"] = dict(src.cost_credits_by_tier or {})
                kw["cost_basis"] = src.cost_basis
            if copy_ce_stamp and src.ce_included is True:
                kw["ce_included"] = True
            db.add(models.NodeDefinition(name=src.name, tenant_id=tid, **kw))
            live_keys.add(key)
            created += 1
        if created:
            if commit_each:
                db.commit()
            else:
                db.flush()
        per_tenant[tid] = created
        created_total += created
    result = {"created": created_total, "tenants": len(tenant_ids), "per_tenant": per_tenant}
    if created_total:
        logger.info("starter package: backfilled %d tenant node copies %s", created_total, per_tenant)
    return result


def curate_ce_keeplist_nodes(db: Session) -> int:
    """2400LR-pal1 — CE only: curate (`is_in_marketplace=True`) every user-facing
    node named in the CE node-seed keep-list so the starter package grants it.

    `seed_marketplace.py` (FREE_CATALOGUE) is paid and never ships in CE, and it
    has no inputs/export anyway, so a fresh CE tenant's palette lacked Image Input
    and the other generic inputs. Resolves each keep-list `name` to a live system
    row (tenant_id IS NULL) AND to the CE default tenant's own keeplist rows —
    node_scanner's WS2.7 CE exception seeds keep-listed system nodes as TENANT-
    scoped rows for `default_tenant_id()`, and `curate_ce_keeplist_nodes` runs
    only under `_is_ce_edition()`, so the tenant arm is scoped to exactly that
    one tenant (`tenant_id == default_tenant_id()`); it can never match another
    tenant's row, and off CE the tenant arm is not built at all. Skips system-
    role converters, system_only / System typed rows, priced rows and soft-
    deleted rows. Idempotent; never un-curates; no deletes. No-op off CE or when
    no keep-list loads. Caller commits.
    Returns the number of rows newly curated."""
    from services.node_scanner import _is_ce_edition, _load_ce_keeplist_names, default_tenant_id
    if not _is_ce_edition():
        return 0
    names = _load_ce_keeplist_names()
    if not names:
        return 0
    scope_filters = [
        models.NodeDefinition.name.in_(sorted(names)),
        models.NodeDefinition.is_in_marketplace.is_(False),
        models.NodeDefinition.deleted_at.is_(None),
        models.NodeDefinition.node_role != "system",
        or_(
            models.NodeDefinition.price_cents.is_(None),
            models.NodeDefinition.price_cents <= 0,
        ),
    ]
    # P17 (RED-LEDGER:115): the CE keeplist rows the scanner seeds are tenant-
    # scoped (WS2.7), so the old `tenant_id IS NULL` filter alone left them
    # uncurated and the starter package never granted them. Admit ONLY the CE
    # default tenant's own rows — never another tenant's same-named row.
    ce_tenant_id = default_tenant_id()
    rows = (
        db.query(models.NodeDefinition)
        .filter(
            or_(
                models.NodeDefinition.tenant_id.is_(None),
                models.NodeDefinition.tenant_id == ce_tenant_id,
            ),
            *scope_filters,
        )
        .all()
    )
    curated = 0
    for nd in rows:
        if getattr(nd, "system_only", False) or (nd.type or "") == "System":
            continue
        nd.is_in_marketplace = True
        curated += 1
    if curated:
        db.flush()
    logger.info("CE keep-list curation: newly curated=%d (keep-list names=%d)", curated, len(names))
    return curated


def sync_starter_package(db: Session) -> dict:
    """Periodic self-heal: package membership + entitlements + node copies.
    Commits."""
    import crud
    curate_ce_keeplist_nodes(db)
    summary = ensure_starter_package(db)
    db.commit()
    tenant_ids = _palette_tenant_ids(db)
    added = removed = 0
    copies_created = 0
    for tid in tenant_ids:
        try:
            rc = crud.node_ownership_cud.reconcile_plan_grants(db, tid)
            copies = backfill_tenant_node_copies(db, [tid], commit_each=False)
            db.commit()
            added += rc.get("added", 0)
            removed += rc.get("removed", 0)
            copies_created += copies.get("created", 0)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.warning("starter package reconcile failed for tenant %s", tid, exc_info=True)
    summary.update(
        tenants=len(tenant_ids), granted=added, revoked=removed, copies_created=copies_created,
    )
    if summary["added_nodes"] or summary["bound_plans"] or added or removed or copies_created:
        logger.info("starter package sync: %s", summary)
    return summary

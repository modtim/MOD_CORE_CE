"""node-ownership-purchases — one-time node ownership: entitlement + purchase +
plan-grant reconcile logic. All ORM lives here so routers stay ORM-free
(layering gate). Ownership is perpetual and tenant-wide; it is orthogonal to
per-run compute metering (CreditUsageLog), which is untouched.
"""
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

import models
from services.entitlement_source import active_filter

logger = logging.getLogger(__name__)

VALID_SOURCES = {"plan_grant", "purchase", "admin_grant"}


# --- reads -----------------------------------------------------------------

def get_entitlement(db: Session, tenant_id: int, node_definition_id: int):
    # iso-soft-delete-sweep: filter tombstones explicitly (not just via the
    # soft-delete listener) so ownership checks are correct even on a session
    # without the listener installed (e.g. tests, ad-hoc scripts).
    return (
        db.query(models.TenantNodeEntitlement)
        .filter(
            models.TenantNodeEntitlement.tenant_id == tenant_id,
            models.TenantNodeEntitlement.node_definition_id == node_definition_id,
            active_filter(models.TenantNodeEntitlement),
        )
        .first()
    )


def is_node_owned(db: Session, tenant_id: int, node_definition_id: int) -> bool:
    return get_entitlement(db, tenant_id, node_definition_id) is not None


def is_priced_marketplace_node(nd: "models.NodeDefinition") -> bool:
    """A maintained (system, tenant_id IS NULL) marketplace node with a positive
    one-time price. Only these require ownership; free + tenant-authored nodes
    never do."""
    return bool(
        nd is not None
        and nd.tenant_id is None
        and getattr(nd, "is_in_marketplace", False)
        and (getattr(nd, "price_cents", None) or 0) > 0
    )


def tenant_may_run_node(db: Session, tenant_id: int, node_definition_id: int, trial_node_definition_id=None) -> bool:
    """Run-time paywall (used at enqueue + credential mint). A priced maintained
    node requires ownership; everything else passes. Unknown node → pass (other
    layers 404), fail-open here so this check never wedges dispatch of a valid
    free node.

    node-trial-runs: `trial_node_definition_id` is the node being trialed in a
    `node_trial` run. It exempts EXACTLY that node from the paywall — any OTHER
    unowned priced node in the same trial run is still blocked."""
    nd = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.id == node_definition_id)
        .first()
    )
    if nd is None or not is_priced_marketplace_node(nd):
        return True
    if trial_node_definition_id is not None and node_definition_id == trial_node_definition_id:
        return True  # scoped trial exemption
    return is_node_owned(db, tenant_id, node_definition_id)


def tenant_may_run_node_kind(db: Session, tenant_id: int, node_kind: str, trial_node_kind=None) -> bool:
    """Same paywall keyed by node_kind (what the task payload carries). Only a
    SYSTEM node of that kind can be priced, so match tenant_id IS NULL.

    node-trial-runs: `trial_node_kind` exempts exactly the trialed node's kind in
    a node_trial run; a different unowned node is still blocked."""
    if not node_kind:
        return True
    nd = (
        db.query(models.NodeDefinition)
        .filter(
            models.NodeDefinition.node_kind == node_kind,
            models.NodeDefinition.tenant_id.is_(None),
        )
        .first()
    )
    if nd is None or not is_priced_marketplace_node(nd):
        return True
    if trial_node_kind and node_kind == trial_node_kind:
        return True  # scoped trial exemption
    return is_node_owned(db, tenant_id, nd.id)


def package_node_ids(db: Session, package_id: int):
    """node_definition_ids belonging to a package."""
    return [
        nid
        for (nid,) in db.query(models.NodePackageItem.node_definition_id)
        .filter(models.NodePackageItem.package_id == package_id)
        .all()
    ]


def _plan_included_node_ids(db: Session, plan_id: int) -> set:
    """Every node a plan grants free — direct (PlanIncludedNode) UNION every node
    inside a plan-included package (PlanPackage -> NodePackageItem)."""
    if not plan_id:
        return set()
    direct = {
        nid
        for (nid,) in db.query(models.PlanIncludedNode.node_definition_id)
        .filter(models.PlanIncludedNode.plan_id == plan_id)
        .all()
    }
    via_pkg = {
        nid
        for (nid,) in db.query(models.NodePackageItem.node_definition_id)
        .join(models.PlanPackage, models.PlanPackage.package_id == models.NodePackageItem.package_id)
        .filter(models.PlanPackage.plan_id == plan_id)
        .all()
    }
    return direct | via_pkg


def _tenant_plan_id(db: Session, tenant_id: int):
    sub = (
        db.query(models.Subscription)
        .filter(models.Subscription.tenant_id == tenant_id)
        .first()
    )
    return getattr(sub, "plan_id", None) if sub else None


def plan_includes_node(db: Session, tenant_id: int, node_definition_id: int) -> bool:
    """True when the tenant's ACTIVE plan grants this node for free (directly or
    via a plan-included package)."""
    plan_id = _tenant_plan_id(db, tenant_id)
    if not plan_id:
        return False
    return node_definition_id in _plan_included_node_ids(db, plan_id)


# --- grants ----------------------------------------------------------------

def _add_entitlement(db: Session, tenant_id, node_definition_id, source, purchase_id=None, granted_by_user_id=None):
    # iso-soft-delete-sweep (2026-09-22): entitlements are soft-deleted (refund /
    # plan-grant reconcile), and the (tenant_id, node_definition_id) uniqueness
    # is a plain constraint, so a tombstoned row still owns the slot. Look it up
    # WITH tombstones (the soft-delete listener hides them from get_entitlement)
    # and REACTIVATE rather than INSERT a duplicate. A still-live row is returned
    # unchanged, matching the original IntegrityError fallback.
    existing = (
        db.query(models.TenantNodeEntitlement)
        .execution_options(include_deleted=True)
        .filter(
            models.TenantNodeEntitlement.tenant_id == tenant_id,
            models.TenantNodeEntitlement.node_definition_id == node_definition_id,
        )
        .first()
    )
    if existing is not None:
        if existing.deleted_at is not None:
            existing.deleted_at = None
            existing.source = source
            existing.purchase_id = purchase_id
            existing.granted_by_user_id = granted_by_user_id
            db.add(existing)
            db.flush()
        return existing
    ent = models.TenantNodeEntitlement(
        tenant_id=tenant_id,
        node_definition_id=node_definition_id,
        source=source,
        purchase_id=purchase_id,
        granted_by_user_id=granted_by_user_id,
    )
    try:
        with db.begin_nested():
            db.add(ent)
            db.flush()
        return ent
    except IntegrityError:
        return get_entitlement(db, tenant_id, node_definition_id)


def grant_free_entitlement(db: Session, tenant_id, node_definition_id, granted_by_user_id=None, source="admin_grant"):
    """Idempotent free grant — download of a free marketplace node / admin gift."""
    ent = get_entitlement(db, tenant_id, node_definition_id)
    if ent:
        return ent
    return _add_entitlement(db, tenant_id, node_definition_id, source, granted_by_user_id=granted_by_user_id)


def record_purchase(db: Session, tenant_id, node_definition_id, amount_cents, stripe_payment_intent_id, purchased_by_user_id=None, currency="usd"):
    """Idempotent on stripe_payment_intent_id."""
    if stripe_payment_intent_id:
        existing = (
            db.query(models.NodePurchase)
            .filter(models.NodePurchase.stripe_payment_intent_id == stripe_payment_intent_id)
            .first()
        )
        if existing:
            return existing
    p = models.NodePurchase(
        tenant_id=tenant_id,
        node_definition_id=node_definition_id,
        amount_cents=amount_cents,
        currency=currency,
        stripe_payment_intent_id=stripe_payment_intent_id,
        purchased_by_user_id=purchased_by_user_id,
    )
    try:
        with db.begin_nested():
            db.add(p)
            db.flush()
        return p
    except IntegrityError:
        return (
            db.query(models.NodePurchase)
            .filter(models.NodePurchase.stripe_payment_intent_id == stripe_payment_intent_id)
            .first()
        )


def grant_purchase_entitlement(db: Session, tenant_id, node_definition_id, purchase):
    """Grant PERMANENT ownership from a purchase. Upgrades an existing plan_grant
    row to 'purchase' in place (no duplicate) so a later downgrade can't revoke a
    paid-for node. Idempotent."""
    pid = purchase.id if purchase else None
    ent = get_entitlement(db, tenant_id, node_definition_id)
    if ent:
        ent.source = "purchase"
        if pid:
            ent.purchase_id = pid
        return ent
    ent = _add_entitlement(db, tenant_id, node_definition_id, "purchase", purchase_id=pid)
    # Lost a race: the winner's row exists but may still be plan_grant — upgrade.
    if ent and ent.source != "purchase":
        ent.source = "purchase"
        if pid:
            ent.purchase_id = pid
    return ent


def fulfill_purchase(db: Session, tenant_id, node_definition_id, amount_cents, stripe_payment_intent_id, purchased_by_user_id=None, currency="usd") -> Tuple:
    """Webhook entry point — record the purchase and grant permanent ownership,
    both idempotent on the payment-intent id. Returns (purchase, entitlement)."""
    p = record_purchase(db, tenant_id, node_definition_id, amount_cents, stripe_payment_intent_id, purchased_by_user_id, currency)
    ent = grant_purchase_entitlement(db, tenant_id, node_definition_id, p)
    return p, ent


def refund_purchase(db: Session, stripe_payment_intent_id):
    """On a refund: mark the purchase refunded and revoke the 'purchase'
    entitlement — UNLESS the node is still plan-granted, in which case downgrade
    it to 'plan_grant' rather than removing it. Idempotent."""
    p = (
        db.query(models.NodePurchase)
        .filter(models.NodePurchase.stripe_payment_intent_id == stripe_payment_intent_id)
        .first()
    )
    if not p:
        return None
    if p.refunded_at is None:
        p.refunded_at = datetime.now(timezone.utc)
    if not p.node_definition_id:
        return p
    ent = get_entitlement(db, p.tenant_id, p.node_definition_id)
    if ent and ent.source == "purchase":
        if plan_includes_node(db, p.tenant_id, p.node_definition_id):
            ent.source = "plan_grant"
            ent.purchase_id = None
        else:
            # iso-soft-delete-sweep: refund revokes ownership — soft-delete the
            # entitlement (ISO 27001 no-hard-delete). _add_entitlement
            # reactivates it if the node is later re-granted/re-purchased.
            ent.deleted_at = datetime.now(timezone.utc)
    return p


# --- reconciler ------------------------------------------------------------

def reconcile_plan_grants(db: Session, tenant_id: int) -> dict:
    """Make the tenant's source='plan_grant' entitlements match its active plan's
    included nodes. ONLY plan_grant rows are added/removed — 'purchase' and
    'admin_grant' rows are never touched (that is the whole point of the
    discriminator). Returns {'added': n, 'removed': n}."""
    plan_id = _tenant_plan_id(db, tenant_id)
    # Direct plan nodes UNION plan-included package nodes (Phase 2).
    included = _plan_included_node_ids(db, plan_id) if plan_id else set()

    current = (
        db.query(models.TenantNodeEntitlement)
        .filter(
            models.TenantNodeEntitlement.tenant_id == tenant_id,
            models.TenantNodeEntitlement.source == "plan_grant",
            models.TenantNodeEntitlement.deleted_at.is_(None),
        )
        .all()
    )
    current_ids = {e.node_definition_id for e in current}

    added = 0
    for nid in included - current_ids:
        # If the tenant already owns it via purchase/admin_grant, leave that be.
        if get_entitlement(db, tenant_id, nid) is None:
            _add_entitlement(db, tenant_id, nid, "plan_grant")
            added += 1

    removed = 0
    for e in current:
        if e.node_definition_id not in included:
            # iso-soft-delete-sweep: plan no longer grants this node — soft-delete
            # the plan_grant entitlement (ISO 27001 no-hard-delete).
            # _add_entitlement reactivates it if the plan re-includes it later.
            e.deleted_at = datetime.now(timezone.utc)
            removed += 1

    return {"added": added, "removed": removed}


def set_node_price(db: Session, node_definition_id: int, price_cents: Optional[int], price_tier: Optional[str] = None):
    """Admin — set/clear a maintained node's one-time price. Pricing is only
    meaningful on a SYSTEM (tenant_id IS NULL) node; refuse otherwise so a
    tenant-authored node can never be sold. price_cents None/0 clears (free).
    Returns the node, or None if not found. Raises ValueError on a non-system node.
    """
    nd = db.query(models.NodeDefinition).filter(models.NodeDefinition.id == node_definition_id).first()
    if nd is None:
        return None
    if nd.tenant_id is not None and price_cents:
        raise ValueError("Only system (maintained) nodes can be priced")
    nd.price_cents = price_cents or None
    nd.price_tier = price_tier
    return nd


def entitlement_sources_for_nodes(db: Session, tenant_id: int, node_ids) -> dict:
    """{node_definition_id: source} for the tenant's entitlements among node_ids,
    in one query — so the list/store endpoint can mark owned + ownership_source
    without an N+1. Empty input → {}."""
    ids = [i for i in (node_ids or []) if i]
    if not tenant_id or not ids:
        return {}
    rows = (
        db.query(models.TenantNodeEntitlement.node_definition_id, models.TenantNodeEntitlement.source)
        .filter(
            models.TenantNodeEntitlement.tenant_id == tenant_id,
            models.TenantNodeEntitlement.node_definition_id.in_(ids),
        )
        .all()
    )
    return {nid: src for nid, src in rows}


# --- packages (Phase 2) ----------------------------------------------------

def fulfill_package_purchase(db: Session, tenant_id, package_id, amount_cents, stripe_payment_intent_id, purchased_by_user_id=None, currency="usd"):
    """Buy a package: ONE NodePurchase (package_id set, node_definition_id NULL)
    plus 'purchase' ownership of every node in the package. Idempotent on the
    payment-intent id. Returns (purchase, [granted_node_ids])."""
    p = None
    if stripe_payment_intent_id:
        p = (
            db.query(models.NodePurchase)
            .filter(models.NodePurchase.stripe_payment_intent_id == stripe_payment_intent_id)
            .first()
        )
    if p is None:
        p = models.NodePurchase(
            tenant_id=tenant_id, node_definition_id=None, package_id=package_id,
            amount_cents=amount_cents, currency=currency,
            stripe_payment_intent_id=stripe_payment_intent_id,
            purchased_by_user_id=purchased_by_user_id,
        )
        try:
            with db.begin_nested():
                db.add(p)
                db.flush()
        except IntegrityError:
            p = (
                db.query(models.NodePurchase)
                .filter(models.NodePurchase.stripe_payment_intent_id == stripe_payment_intent_id)
                .first()
            )
    granted = []
    for nid in package_node_ids(db, package_id):
        grant_purchase_entitlement(db, tenant_id, nid, p)
        granted.append(nid)
    return p, granted


def get_package(db: Session, package_id: int):
    return db.query(models.NodePackage).filter(models.NodePackage.id == package_id).first()


def is_priced_marketplace_package(pkg: "models.NodePackage") -> bool:
    return bool(
        pkg is not None and pkg.tenant_id is None
        and getattr(pkg, "is_in_marketplace", False)
        and (getattr(pkg, "price_cents", None) or 0) > 0
    )


def tenant_owns_all_package_nodes(db: Session, tenant_id: int, package_id: int) -> bool:
    ids = package_node_ids(db, package_id)
    if not ids:
        return False
    owned = set(entitlement_sources_for_nodes(db, tenant_id, ids).keys())
    return set(ids).issubset(owned)


def create_package(db: Session, name, description=None, slug=None, price_cents=None, price_tier=None, is_in_marketplace=True):
    pkg = models.NodePackage(
        name=name, description=description, slug=slug,
        price_cents=price_cents or None, price_tier=price_tier,
        is_in_marketplace=is_in_marketplace, tenant_id=None,
    )
    db.add(pkg)
    db.flush()
    return pkg


def add_package_item(db: Session, package_id, node_definition_id):
    ex = (
        db.query(models.NodePackageItem)
        .filter(
            models.NodePackageItem.package_id == package_id,
            models.NodePackageItem.node_definition_id == node_definition_id,
        )
        .first()
    )
    if ex:
        return ex
    item = models.NodePackageItem(package_id=package_id, node_definition_id=node_definition_id)
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
        return item
    except IntegrityError:
        return (
            db.query(models.NodePackageItem)
            .filter(
                models.NodePackageItem.package_id == package_id,
                models.NodePackageItem.node_definition_id == node_definition_id,
            )
            .first()
        )


def bind_plan_package(db: Session, plan_id, package_id):
    ex = (
        db.query(models.PlanPackage)
        .filter(models.PlanPackage.plan_id == plan_id, models.PlanPackage.package_id == package_id)
        .first()
    )
    if ex:
        return ex
    pp = models.PlanPackage(plan_id=plan_id, package_id=package_id)
    try:
        with db.begin_nested():
            db.add(pp)
            db.flush()
        return pp
    except IntegrityError:
        return (
            db.query(models.PlanPackage)
            .filter(models.PlanPackage.plan_id == plan_id, models.PlanPackage.package_id == package_id)
            .first()
        )


def list_marketplace_packages(db: Session, *, tenant_id=None, is_platform_admin: bool = False):
    """Official (tenant_id NULL) packages flagged for the marketplace.

    GG.6c: private/unlisted bundles are excluded here (browse) unless
    `tenant_id` is in `allowed_tenant_ids` or the caller is a platform
    admin — see services/listing_visibility.apply_list_filter, the ONE
    shared filter every NodePackage browse/search query site must use.
    """
    # FIX-CE-STORE-API-DENY-PART-2 (owner 11-16, "store api routes are Deny!!!", WAVE 5
    # second rebuild): services/listing_visibility.py moved api_allow -> deny (CE has no
    # store scope), so resolve the shared visibility filter optionally. A paid tree carries
    # it and filters byte-identically; a CE build has no private/unlisted NodePackage rows
    # at all (the only route that sets visibility is the denied store router), so the
    # filter is a no-op there and every official marketplace package lists unchanged.
    try:
        import services.listing_visibility as _lv
    except ImportError:  # CE build: the store visibility service is not shipped
        _lv = None

    pkgs = (
        db.query(models.NodePackage)
        .filter(models.NodePackage.tenant_id.is_(None), models.NodePackage.is_in_marketplace.is_(True))
        .order_by(models.NodePackage.name)
        .all()
    )
    if is_platform_admin:
        return pkgs
    # Small, admin-curated table — filter in Python with the one shared
    # `can_list` rule (public / unlisted-if-owner / private-if-allowed)
    # rather than a partial SQL clause that can't express the
    # allowed_tenant_ids JSONB-containment check and a browse-vs-detail
    # distinction at once.
    if _lv is None:  # CE build: no store visibility layer -> nothing to filter out
        return pkgs
    return [p for p in pkgs if _lv.can_list(p, tenant_id, is_platform_admin=is_platform_admin)]


# --- try-before-buy: sample + discovery (node-trial-runs) -------------------

def set_node_sample(db: Session, node_definition_id, sample_input_ref=None, sample_output_ref=None, demo_workflow_ref=None):
    """Admin — set a node's curated demo refs (system nodes only, like pricing)."""
    nd = db.query(models.NodeDefinition).filter(models.NodeDefinition.id == node_definition_id).first()
    if nd is None:
        return None
    if nd.tenant_id is not None:
        raise ValueError("Only system (maintained) nodes can carry a marketplace demo")
    if sample_input_ref is not None:
        nd.sample_input_ref = sample_input_ref or None
    if sample_output_ref is not None:
        nd.sample_output_ref = sample_output_ref or None
    if demo_workflow_ref is not None:
        nd.demo_workflow_ref = demo_workflow_ref or None
    return nd


def mark_marketplace_seen(db: Session, tenant_id):
    from datetime import datetime, timezone
    t = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if t is not None:
        t.marketplace_seen_at = datetime.now(timezone.utc)
    return t


def new_marketplace_node_count(db: Session, tenant_id, since=None) -> int:
    """Count priced maintained marketplace nodes the tenant does NOT own — the
    'N new nodes to buy' badge. When `since` is given, restrict to nodes scanned
    (registered/updated) after it, approximating 'new since last visit'."""
    owned_ids = {
        nid for (nid,) in db.query(models.TenantNodeEntitlement.node_definition_id)
        .filter(models.TenantNodeEntitlement.tenant_id == tenant_id).all()
    }
    q = (
        db.query(models.NodeDefinition.id)
        .filter(
            models.NodeDefinition.tenant_id.is_(None),
            models.NodeDefinition.is_in_marketplace.is_(True),
            models.NodeDefinition.price_cents.isnot(None),
            models.NodeDefinition.price_cents > 0,
            models.NodeDefinition.deleted_at.is_(None),
        )
    )
    if since is not None:
        q = q.filter(models.NodeDefinition.scanned_at.isnot(None), models.NodeDefinition.scanned_at > since)
    return sum(1 for (nid,) in q.all() if nid not in owned_ids)


def public_node_detail(db: Session, node_definition_id) -> Optional[dict]:
    """Safe, display-only fields for the public node page (no secrets, no tenant
    data). Only surfaces a published marketplace system node."""
    nd = (
        db.query(models.NodeDefinition)
        .filter(
            models.NodeDefinition.id == node_definition_id,
            models.NodeDefinition.tenant_id.is_(None),
            models.NodeDefinition.is_in_marketplace.is_(True),
            models.NodeDefinition.deleted_at.is_(None),
        )
        .first()
    )
    if nd is None:
        return None
    return {
        "id": nd.id,
        "name": nd.name,
        "description": nd.description,
        "node_kind": nd.node_kind,
        "type": nd.type,
        "input_schema": nd.input_schema,
        "output_schema": nd.output_schema,
        "price_cents": nd.price_cents,
        "price_tier": nd.price_tier,
        "sample_output_ref": nd.sample_output_ref,
        "has_demo": bool(nd.sample_input_ref or nd.demo_workflow_ref),
    }

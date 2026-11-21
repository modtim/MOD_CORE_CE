"""services/entitlement_guard.py — GG.4 (launch-completion-megaplan, gg-commerce).

Single call-through for run-time entitlement checks: the node paywall
(`crud.node_ownership_cud.tenant_may_run_node_kind`, fail-OPEN on a check
error so a DB blip never wedges dispatch of a valid free node) plus the
marketplace defense-in-depth guard (`routers.worker_tasks._check_marketplace_entitlement`,
fail-CLOSED — SaaS purchase-row check / on-prem signed-licence check, GG.L3),
and the extension points the GG.1 schema (`WorkflowListing`,
`TenantWorkflowEntitlement`) now supports for published workflows, plus a
placeholder for GF's not-yet-built stack listings.

Behaviour preservation: `tenant_may_run_node_kind` and
`_check_marketplace_entitlement` are NOT moved or modified — both stay
importable from their existing modules by name, because
tests/test_node_ownership.py, tests/test_enqueue_entitlement_guard.py,
tests/test_onprem_licence_items.py, tests/test_paywall_failopen_metric.py and
tests/test_node_credentials_paywall_functional.py import them directly.
`check_run` below calls them in the same order, with the same
try/except/metric shape, that `enqueue_task_internal` used to run inline
(see routers/worker_tasks.py, GG.4 comment at the call site).

CE-NOPAID-4.5c (launch-completion-megaplan) — the 5-name shim. The five public
names of this module (`check_run`, `preflight_run`, `check_workflow_run`,
`check_stack_run`, `assert_cross_region_audit_allowed`) are now SHIM functions:
each resolves the entitlement provider (services/entitlement_provider.py,
CE-NOPAID-4.5a) and, when a non-null provider is registered, delegates to it;
while the resolved provider is still the null base the module's own body —
today's real body, kept here as the fallback until CE-NOPAID-4.7 moves it to
the non-CE add-on module — answers. Same posture as the two
routed call sites (routers/worker_tasks.py, routers/workflows_domains/workflows.py,
CE-NOPAID-4.5b), so the non-CE tree is behaviourally identical today (same 402/403,
same fail-open paywall half / fail-closed marketplace half, same exempt rules,
same `active_filter` semantics) and every existing import of these five names
(2 non-test sites + the guard/preflight/audit/listing tests) keeps working
unchanged. This file is SHARED code: it ships to CE and to the non-CE editions; non-CE behaviour
is unchanged.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from services.entitlement_source import active_filter

logger = logging.getLogger(__name__)


def _system_node_definition_id(db: Session, node_kind: str) -> Optional[int]:
    """The SYSTEM (tenant_id IS NULL) NodeDefinition id for a node_kind, or
    None. Only system nodes can be priced, so this is the same lookup the
    paywall predicate (`tenant_may_run_node_kind`) keys on — reused here so
    the dispatch-time 402 and the pre-flight items carry the identical id."""
    if not node_kind:
        return None
    row = (
        db.query(models.NodeDefinition.id)
        .filter(
            models.NodeDefinition.node_kind == node_kind,
            models.NodeDefinition.tenant_id.is_(None),
        )
        .order_by(models.NodeDefinition.id)
        .first()
    )
    return row[0] if row else None


def _legacy_check_run(
    db: Session,
    tenant_id: Optional[int],
    node_kind: Optional[str],
    trial_node_kind: Optional[str] = None,
    *,
    check_marketplace: bool = True,
    metric_site: str = "enqueue",
) -> bool:
    """Node-kind paywall + marketplace guard, identical to the inline block
    `enqueue_task_internal` ran before GG.4 wrapped it. Raises HTTPException
    to refuse a run (402 paywall / 403 marketplace-guard / 402 on-prem
    licence-invalid, all unchanged from the wrapped functions); returns True
    to allow.

    `check_marketplace=False` reproduces the OTHER existing call site
    (`get_task_credentials`, credential-mint defense-in-depth #2), which
    today only ever runs the paywall half with its own metric label
    ("credential_mint") and does NOT call `_check_marketplace_entitlement`.
    That call site is left untouched by this task (the decomp's read window
    only covers the enqueue site) — this parameter exists so a future caller
    can opt into the identical pattern instead of re-duplicating it, it is
    not wired into `get_task_credentials` today.
    """
    if not node_kind or not tenant_id:
        return True

    import crud

    try:
        allowed = crud.node_ownership_cud.tenant_may_run_node_kind(
            db, tenant_id, node_kind, trial_node_kind=trial_node_kind
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — never fail-closed on a check error
        allowed = True
        from routers.worker_tasks import _record_paywall_check_error
        _record_paywall_check_error(metric_site)

    if not allowed:
        # GG.UI.6b — carry the definition id in the refusal string so the
        # failed-run parser (WB lib/blocked-run.ts) can link the Buy CTA
        # straight to /store/node/{id} instead of the bare node listing.
        _nd_id = _system_node_definition_id(db, node_kind)
        _id_suffix = f" (id {_nd_id})" if _nd_id is not None else ""
        raise HTTPException(
            status_code=402,
            detail=f"Node '{node_kind}' must be purchased before it can run{_id_suffix}",
        )

    if check_marketplace:
        from routers.worker_tasks import _check_marketplace_entitlement
        _check_marketplace_entitlement(db, tenant_id, node_kind, trial_node_kind=trial_node_kind)

    return True


def _legacy_assert_cross_region_audit_allowed(db=None) -> bool:
    """GA/mesh cross-region audit aggregation MUST call this; single-region
    export stays ungated."""
    from utils import entitlements

    ent = entitlements.get_entitlements(db)
    if entitlements.Cap.AUDIT_CROSS_REGION not in ent.features:
        raise HTTPException(
            status_code=402,
            detail="Cross-region audit aggregation requires the AUDIT_CROSS_REGION entitlement.",
        )
    return True


def _legacy_preflight_run(
    db: Session,
    tenant_id: Optional[int],
    workflow_version,
    *,
    graph: Optional[dict] = None,
    check_listing: bool = True,
    metric_site: str = "preflight",
) -> list:
    """GG.UI.6b — structured purchase-required pre-flight for a run.

    Walks the version's graph nodes and the workflow's source listing,
    reusing the SAME predicates `check_run` / `check_workflow_run` run at
    dispatch time (fail-open paywall half, fail-closed marketplace/licence
    half, on-prem signed-licence branch included) and returns EVERY missing
    item as a dict instead of raising on the first one:

        {"kind": "node"|"workflow",
         "id": <NodeDefinition.id or WorkflowListing.id>,
         "name": <display name>,
         "node_kind": <str, nodes only>,
         "trial_available": bool}

    An empty list means the run may proceed; the caller raises a structured
    402 when it is not. The dispatch-time guards stay in place as defence in
    depth (a purchase between submit and dispatch, or a graph that diverges
    from the version, is still caught there).
    """
    items: list = []
    if not tenant_id or workflow_version is None:
        return items
    graph = getattr(workflow_version, "graph_json", None) or {}
    nodes = graph.get("nodes") if isinstance(graph, dict) else None

    # Resolve each node's node_kind the same way the orchestrator's
    # dispatch-time enrichment does: stored data.node_kind wins when truthy,
    # otherwise the NodeDefinition behind data.nodeDefinitionId is
    # authoritative. Only distinct kinds are checked once.
    seen_kinds = set()
    for node in (nodes or []):
        data = (node or {}).get("data") or {}
        node_kind = data.get("node_kind")
        if not node_kind:
            nd_id = data.get("nodeDefinitionId")
            if nd_id:
                nd = (
                    db.query(models.NodeDefinition)
                    .filter(models.NodeDefinition.id == nd_id)
                    .first()
                )
                node_kind = getattr(nd, "node_kind", None) if nd else None
        if not node_kind or node_kind in seen_kinds:
            continue
        seen_kinds.add(node_kind)

        import crud
        try:
            allowed = crud.node_ownership_cud.tenant_may_run_node_kind(
                db, tenant_id, node_kind
            )
        except HTTPException:
            allowed = False  # an explicit refusal is a block, not a pass
        except Exception:  # noqa: BLE001 — same fail-open as check_run
            allowed = True
            from routers.worker_tasks import _record_paywall_check_error
            _record_paywall_check_error(metric_site)

        if allowed:
            # Marketplace / on-prem-licence half: the predicate raises when
            # blocked (403 SaaS / 402 licence); collect it as an item.
            from routers.worker_tasks import _check_marketplace_entitlement
            try:
                _check_marketplace_entitlement(db, tenant_id, node_kind)
            except HTTPException:
                allowed = False

        if not allowed:
            nd_id = _system_node_definition_id(db, node_kind)
            nd_name = None
            if nd_id is not None:
                nd = db.get(models.NodeDefinition, nd_id)
                if nd is not None:
                    nd_name = getattr(nd, "name", None)
                    trial_available = bool(
                        getattr(nd, "sample_input_ref", None)
                        or getattr(nd, "demo_workflow_ref", None)
                    )
                else:
                    trial_available = False
            else:
                trial_available = False
            items.append({
                "kind": "node",
                "id": nd_id,
                "name": nd_name or node_kind,
                "node_kind": node_kind,
                "trial_available": trial_available,
            })

    # The workflow listing the version was installed from (GG.6): running a
    # SEALED installed workflow requires an active
    # TenantWorkflowEntitlement, gated by the exact predicate dispatch will
    # use (check_workflow_run). Open-licence installed copies are the
    # post-purchase editable copy — the purchase already happened, so they
    # are exempt (is_sealed() is False for them). `check_listing=False`
    # skips this half entirely (used by unit tests that exercise only the
    # node walk).
    workflow = getattr(workflow_version, "workflow", None)
    listing_id = getattr(workflow, "source_listing_id", None) if workflow else None
    if check_listing and listing_id:
        import services.sealed_workflow as _sealed
        if not _sealed.is_sealed(db, workflow):
            listing_id = None
    if listing_id:
        try:
            check_workflow_run(db, tenant_id, listing_id)
        except HTTPException:
            items.append({
                "kind": "workflow",
                "id": listing_id,
                "name": f"Workflow listing {listing_id}",
                "node_kind": None,
                "trial_available": False,
            })

    return items


def _legacy_check_workflow_run(
    db: Session,
    tenant_id: Optional[int],
    workflow_listing_id: Optional[int],
) -> bool:
    """GG.1/GG.3 extension point: gate running a published `WorkflowListing`
    the same way `_check_marketplace_entitlement` gates a non-CE system node —
    fails CLOSED (403) when the listing is priced and the tenant holds no
    ACTIVE (deleted_at IS NULL) `TenantWorkflowEntitlement`.

    Exempt: no listing id given (nothing to check — callers pass None until
    GG.6 install / GG.8 sealed-run wire a real listing id through), the
    publisher's own tenant, and free (`price_cents` NULL or 0) listings.

    Not yet called from any dispatch path (GG.6 install and GG.8 sealed-run
    haven't landed) — this is the extension point the decomp asks GG.4 to
    add ahead of them; exercised directly by tests/test_entitlement_guard.py.
    """
    if workflow_listing_id is None or not tenant_id:
        return True

    listing = (
        db.query(models.WorkflowListing)
        .filter(
            models.WorkflowListing.id == workflow_listing_id,
            models.WorkflowListing.deleted_at.is_(None),
        )
        .first()
    )
    if listing is None:
        return True  # unknown/soft-deleted listing id — other layers 404, not this guard's job

    # GG.6c: private listings fail CLOSED for a tenant that isn't the
    # publisher and isn't in allowed_tenant_ids — same posture as an unpaid
    # priced listing, not "unknown" (the guard's job is to gate running).
    # FIX-CE-STORE-API-DENY-PART-2 (owner 11-16, "store api routes are Deny!!!", WAVE 5
    # second rebuild): services/listing_visibility.py moved api_allow -> deny (CE has no
    # store scope), so resolve it optionally — a paid tree carries it and behaves
    # byte-identically; a CE build has no private/unlisted listings to hide (no store
    # publish route exists), so the visibility layer is simply absent and we fall through.
    try:
        import services.listing_visibility as _lv
    except ImportError:  # CE build: the store service is not shipped
        _lv = None
    if _lv is not None and not _lv.can_view(listing, tenant_id):
        return False

    if listing.publisher_tenant_id == tenant_id:
        return True  # a publisher always owns their own listing

    if not (listing.price_cents or 0) > 0:
        return True  # free listing — no entitlement required

    ent = (
        db.query(models.TenantWorkflowEntitlement.id)
        .filter(
            models.TenantWorkflowEntitlement.tenant_id == tenant_id,
            models.TenantWorkflowEntitlement.workflow_listing_id == workflow_listing_id,
            active_filter(models.TenantWorkflowEntitlement),
        )
        .first()
    )
    if ent is None:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Workflow listing {workflow_listing_id} is a listing that needs an add-on and "
                "tenant has no active entitlement"
            ),
        )
    return True


def _legacy_check_stack_run(
    db: Session,
    tenant_id: Optional[int],
    stack_listing_ids: Optional[Iterable[int]],
) -> bool:
    """GG.1/GG.3 extension point for GF's stack listings. Group F has not
    landed a `stack_listings` table (or any stack-entitlement table) in this
    codebase yet, so there is nothing to check ownership against.

    Per the owner's fail-closed-for-billed-items rule, a silent no-op TODO here
    would be a security hole the day GG.5/GG.6 start writing real
    `required_stack_listing_ids` onto a `WorkflowListing` and something calls
    this with real ids — so calling with any ids fails CLOSED (403) instead.
    Calling with no ids (empty/None — the only thing anything in this
    codebase does today) is a no-op pass.
    """
    ids = [i for i in (stack_listing_ids or []) if i is not None]
    if not ids or not tenant_id:
        return True
    raise HTTPException(
        status_code=403,
        detail=(
            "Stack entitlement checks are not available in this installation; "
            ""
            f"refusing rather than allowing an unverified stack {ids}"
        ),
    )


# --- CE-NOPAID-4.5c: the 5-name shim over the provider -----------------------
# CE-NOPAID-4.8 P1: on the CE edition (utils.paid_import.is_ce_edition) the null
# provider's answer is final (allow, no DB read, never 402); the legacy bodies
# below only answer on the non-CE tree while the null base is resolved.
# The five public names of this module are the shim; the bodies above are the
# fallback (today's real bodies, moved to `paid_addon/services/paid_entitlements.py`
# (CORE/API/paid_addon/, the one add-on home per CE-NOPAID-5.2 part 1) by CE-NOPAID-4.7).
# Posture identical to the two routed call sites (CE-NOPAID-4.5b):
# while the resolved provider is still the null base the fallback answers, so the
# non-CE tree is behaviourally unchanged today; once a real provider is registered
# the shim delegates to it with the identical argument tuple.
# The provider import is INSIDE each shim (as every call site here does) so a
# test that monkeypatches `services.entitlement_provider.get_entitlement_provider`
# is honoured at call time, not frozen at import time.

def check_run(
    db: Session,
    tenant_id: Optional[int],
    node_kind: Optional[str],
    trial_node_kind: Optional[str] = None,
    *,
    check_marketplace: bool = True,
    metric_site: str = "enqueue",
) -> bool:
    """Shim: delegate to the registered provider, else today's body."""
    from services.entitlement_provider import (
        EntitlementProvider as _NullEntitlementProvider,
        get_entitlement_provider,
    )
    from utils.paid_import import is_ce_edition as _is_ce_edition
    provider = get_entitlement_provider(db)
    if type(provider) is _NullEntitlementProvider and not _is_ce_edition():
        return _legacy_check_run(
            db, tenant_id, node_kind, trial_node_kind,
            check_marketplace=check_marketplace, metric_site=metric_site,
        )
    return provider.check_run(
        db, tenant_id, node_kind, trial_node_kind,
        check_marketplace=check_marketplace, metric_site=metric_site,
    )


def preflight_run(
    db: Session,
    tenant_id: Optional[int],
    workflow_version,
    *,
    graph: Optional[dict] = None,
    check_listing: bool = True,
    metric_site: str = "preflight",
) -> list:
    """Shim: delegate to the registered provider, else today's body."""
    from services.entitlement_provider import (
        EntitlementProvider as _NullEntitlementProvider,
        get_entitlement_provider,
    )
    from utils.paid_import import is_ce_edition as _is_ce_edition
    provider = get_entitlement_provider(db)
    if type(provider) is _NullEntitlementProvider and not _is_ce_edition():
        return _legacy_preflight_run(
            db, tenant_id, workflow_version,
            graph=graph, check_listing=check_listing, metric_site=metric_site,
        )
    return provider.preflight_run(
        db, tenant_id, workflow_version,
        graph=graph, check_listing=check_listing, metric_site=metric_site,
    )


def check_workflow_run(
    db: Session,
    tenant_id: Optional[int],
    workflow_listing_id: Optional[int],
) -> bool:
    """Shim: delegate to the registered provider, else today's body."""
    from services.entitlement_provider import (
        EntitlementProvider as _NullEntitlementProvider,
        get_entitlement_provider,
    )
    from utils.paid_import import is_ce_edition as _is_ce_edition
    provider = get_entitlement_provider(db)
    if type(provider) is _NullEntitlementProvider and not _is_ce_edition():
        return _legacy_check_workflow_run(db, tenant_id, workflow_listing_id)
    return provider.check_workflow_run(db, tenant_id, workflow_listing_id)


def check_stack_run(
    db: Session,
    tenant_id: Optional[int],
    stack_listing_ids: Optional[Iterable[int]],
) -> bool:
    """Shim: delegate to the registered provider, else today's body."""
    from services.entitlement_provider import (
        EntitlementProvider as _NullEntitlementProvider,
        get_entitlement_provider,
    )
    from utils.paid_import import is_ce_edition as _is_ce_edition
    provider = get_entitlement_provider(db)
    if type(provider) is _NullEntitlementProvider and not _is_ce_edition():
        return _legacy_check_stack_run(db, tenant_id, stack_listing_ids)
    return provider.check_stack_run(db, tenant_id, stack_listing_ids)


def assert_cross_region_audit_allowed(db=None) -> bool:
    """Shim: delegate to the registered provider, else today's body."""
    from services.entitlement_provider import (
        EntitlementProvider as _NullEntitlementProvider,
        get_entitlement_provider,
    )
    from utils.paid_import import is_ce_edition as _is_ce_edition
    provider = get_entitlement_provider(db)
    if type(provider) is _NullEntitlementProvider and not _is_ce_edition():
        return _legacy_assert_cross_region_audit_allowed(db)
    return provider.assert_cross_region_audit_allowed(db)
